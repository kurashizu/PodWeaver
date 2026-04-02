#!/usr/bin/env python3
"""
Batch synthesize text files in ./segment -> downloads mp3 to OUT_DIR using FreeTTS API.

Changes in this version:
- Per-file retry logic: each file will be attempted up to N times (synthesize + download).
- Expose `--file-attempts` CLI argument to control per-file attempts.
- Reduce default MAX_CHARS to 1000.
- Exit with non-zero code if any file ultimately fails (so callers can decide whether to merge).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_TTS = "https://freetts.org/api/tts"
API_AUDIO = "https://freetts.org/api/audio/{}"

# FreeTTS constraints / recommendations
MAX_CHARS = 4500  # reduced per user's request
RATE_LIMIT_SLEEP = 60  # seconds to wait on 429 by default


def build_session(
    connect_retries: int = 3, backoff_factor: float = 0.5
) -> requests.Session:
    """
    Build a requests Session with a urllib3 Retry strategy for connection-level reliability.
    Note: application-level 429 handling is implemented in synthesize_text.
    """
    session = requests.Session()
    retry = Retry(
        total=connect_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def synthesize_text(
    session: requests.Session,
    text: str,
    voice: str,
    rate: str = "+0%",
    pitch: str = "+0Hz",
    timeout: int = 60,
    max_attempts: int = 4,
) -> Optional[str]:
    """
    POST /api/tts with the given text. Returns file_id on success, otherwise None.

    - session: requests.Session to use (with connection retry policy).
    - timeout: request timeout in seconds.
    - max_attempts: how many times to retry at application level (handles 429 and 5xx).
    """
    payload = {"text": text, "voice": voice, "rate": rate, "pitch": pitch}
    headers = {"Content-Type": "application/json"}

    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.post(API_TTS, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            wait = min(2**attempt, 60)
            print(
                f"[ERROR] Network error on synth attempt {attempt}: {e}. Sleeping {wait}s.",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(
                    f"[ERROR] Failed to parse JSON response: {resp.text!r}",
                    file=sys.stderr,
                )
                return None
            file_id = data.get("file_id")
            if not file_id:
                print(f"[ERROR] No file_id returned: {data}", file=sys.stderr)
            return file_id

        if resp.status_code == 429:
            # Rate limit: recommended to wait a full minute before retrying
            print(
                f"[WARN] 429 Too Many Requests. Waiting {RATE_LIMIT_SLEEP}s before retrying... ({attempt}/{max_attempts})",
                file=sys.stderr,
            )
            time.sleep(RATE_LIMIT_SLEEP)
            continue

        if 500 <= resp.status_code < 600:
            wait = min(2**attempt, 60)
            print(
                f"[WARN] Server error {resp.status_code}. Sleeping {wait}s and retrying ({attempt}/{max_attempts})",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        # Client error - won't succeed by retrying
        print(
            f"[ERROR] TTS request failed {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        return None

    print("[ERROR] Exceeded attempts for synthesize_text", file=sys.stderr)
    return None


def download_audio(
    session: requests.Session, file_id: str, dest_path: Path, timeout: int = 60
) -> bool:
    """
    GET /api/audio/{file_id} and write it to dest_path.
    Streams into file to avoid loading the whole MP3 in memory.
    Returns True on success.
    """
    url = API_AUDIO.format(file_id)
    try:
        with session.get(url, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                text_preview = r.text[:200] if r.text else ""
                print(
                    f"[ERROR] Failed to download audio {file_id}: status {r.status_code} - {text_preview}",
                    file=sys.stderr,
                )
                return False

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except requests.RequestException as e:
        print(
            f"[ERROR] Network error when downloading audio {file_id}: {e}",
            file=sys.stderr,
        )
        return False


def is_text_file(p: Path) -> bool:
    return p.is_file() and not p.name.startswith(".")


def create_sample_segment(seg_dir: Path) -> None:
    seg_dir.mkdir(parents=True, exist_ok=True)
    sample = seg_dir / "sample1.txt"
    if sample.exists():
        print(f"[INFO] Sample file already exists: {sample}")
        return
    sample.write_text("Hello from FreeTTS API — this is a sample.", encoding="utf-8")
    print(f"[INFO] Wrote sample file: {sample}")


def process_file_with_retries(
    session: requests.Session,
    file_path: Path,
    out_dir: Path,
    voice: str,
    rate: str,
    pitch: str,
    request_timeout: int,
    per_file_attempts: int,
    per_call_attempts: int,
    pause_between_attempts: float = 1.0,
) -> bool:
    """
    Attempt to synthesize and download a given file up to per_file_attempts times.
    - per_call_attempts controls max attempts inside synthesize_text (application-level).
    Returns True if succeeded, False otherwise.
    """
    stem = file_path.stem
    out_file = out_dir / f"{stem}.mp3"

    for attempt in range(1, per_file_attempts + 1):
        print(f"[INFO] ({file_path.name}) File attempt {attempt}/{per_file_attempts}")

        # Read text
        try:
            text = file_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[ERROR] Failed to read '{file_path}': {e}.", file=sys.stderr)
            return False

        if not text:
            print(f"[SKIP] '{file_path.name}' is empty. Skipping.")
            return True  # treat empty file as success (nothing to synthesize)

        if len(text) > MAX_CHARS:
            print(
                f"[WARN] '{file_path.name}' length {len(text)} > {MAX_CHARS}. Skipping. Consider pre-splitting files."
            )
            return False

        # Synthesize (this function itself does retries for 429/5xx)
        file_id = synthesize_text(
            session,
            text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            timeout=request_timeout,
            max_attempts=per_call_attempts,
        )
        if not file_id:
            print(
                f"[WARN] ({file_path.name}) synth failed on attempt {attempt}/{per_file_attempts}."
            )
            time.sleep(pause_between_attempts)
            continue

        # Download
        ok = download_audio(session, file_id, out_file, timeout=request_timeout)
        if ok:
            print(f"[DONE] Saved '{out_file}'")
            return True
        else:
            # If download failed, attempt again (the generated file on server might have expired; retry synthesize)
            print(
                f"[WARN] ({file_path.name}) download failed on attempt {attempt}/{per_file_attempts}. Retrying..."
            )
            # Remove partially downloaded file if present
            try:
                if out_file.exists():
                    out_file.unlink()
            except Exception:
                pass
            time.sleep(pause_between_attempts)

    print(
        f"[ERROR] ({file_path.name}) All {per_file_attempts} attempts failed.",
        file=sys.stderr,
    )
    return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch synthesize text files using FreeTTS"
    )
    parser.add_argument(
        "--segment-dir",
        "-s",
        default="segment",
        help="Directory containing segment text files (default: ./segment)",
    )
    parser.add_argument(
        "--out-dir",
        "-o",
        default=".",
        help="Directory to save mp3 files (default: ./ )",
    )
    parser.add_argument(
        "--voice",
        "-v",
        default="en-US-JennyNeural",
        help="Voice short name (default en-US-JennyNeural)",
    )
    parser.add_argument("--rate", default="+0%", help="Speaking rate (e.g. +0%, -20%)")
    parser.add_argument("--pitch", default="+0Hz", help="Pitch (e.g. +0Hz, -5Hz)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout seconds (per HTTP call)",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing mp3 files"
    )
    parser.add_argument(
        "--create-sample",
        action="store_true",
        help="Create a small sample file in the segment dir and exit",
    )
    parser.add_argument(
        "--file-attempts",
        type=int,
        default=4,
        help="How many times to attempt synthesize+download per file (default: 4)",
    )
    parser.add_argument(
        "--call-attempts",
        type=int,
        default=4,
        help="How many internal attempts synthesize_text should try for transient errors (default: 4)",
    )
    args = parser.parse_args(argv)

    seg_dir = Path(args.segment_dir)
    out_dir = Path(args.out_dir)

    if args.create_sample:
        create_sample_segment(seg_dir)
        return 0

    if not seg_dir.exists() or not seg_dir.is_dir():
        print(
            f"[ERROR] Segment directory '{seg_dir}' does not exist or is not a directory. Use --create-sample to create one.",
            file=sys.stderr,
        )
        return 2

    files = sorted(
        [
            p
            for p in seg_dir.iterdir()
            if is_text_file(p) and p.name != "segments_list.txt"
        ],
        key=lambda p: p.name,
    )
    if not files:
        print(
            f"[INFO] No files found in '{seg_dir}'. Nothing to synthesize.",
            file=sys.stderr,
        )
        return 0

    print(
        f"[INFO] Found {len(files)} files in '{seg_dir}'. Output -> '{out_dir.resolve()}' Voice: {args.voice}"
    )

    # Prepare session with connection-level retry policy
    session = build_session(connect_retries=3, backoff_factor=0.5)

    # Ensure output dir exists
    out_dir.mkdir(parents=True, exist_ok=True)

    failed_files: List[Path] = []

    for idx, file_path in enumerate(files, 1):
        print(f"[FILE] ({idx}/{len(files)}) Processing '{file_path.name}'")
        stem = file_path.stem
        out_file = out_dir / f"{stem}.mp3"

        if out_file.exists() and not args.overwrite:
            print(
                f"[SKIP] ({idx}/{len(files)}) '{file_path.name}' -> '{out_file.name}' exists. Use --overwrite to replace."
            )
            continue

        success = process_file_with_retries(
            session=session,
            file_path=file_path,
            out_dir=out_dir,
            voice=args.voice,
            rate=args.rate,
            pitch=args.pitch,
            request_timeout=args.timeout,
            per_file_attempts=args.file_attempts,
            per_call_attempts=args.call_attempts,
            pause_between_attempts=1.0,
        )

        if not success:
            failed_files.append(file_path)

        # small pause to reduce burstiness
        time.sleep(0.5)

    if failed_files:
        print("[ERROR] Some files failed to synthesize/download:", file=sys.stderr)
        for p in failed_files:
            print("  -", p, file=sys.stderr)
        print(
            "[ERROR] Aborting with non-zero exit code so caller can decide whether to merge.",
            file=sys.stderr,
        )
        return 3

    print("[INFO] All files processed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
