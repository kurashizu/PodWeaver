#!/usr/bin/env python3
"""
merge_clips.py

Finds all MP3 files in a clips directory (default: ./clips), sorts them by filename
in human-friendly (natural) order, and concatenates them into a single output MP3
using ffmpeg's concat demuxer.

Usage:
    python merge_clips.py                 # reads ./clips, writes ./merged.mp3
    python merge_clips.py -i clips -o final_episode.mp3
    python merge_clips.py --reencode      # fallback: re-encode instead of stream copy

Notes:
- This script requires the ffmpeg executable to be available in PATH.
- It writes a temporary list file and removes it after merging.
- If the direct stream-copy concat fails (because files have differing codecs/params),
  use --reencode to force re-encoding into a consistent MP3 output.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple


def natural_sort_key(s: str):
    """
    Return a key for natural/human sorting, e.g. file2 < file10.
    Splits on groups of digits.
    """
    parts = re.split(r"(\d+)", s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            key.append(p.lower())
    return key


def find_mp3_files(clips_dir: Path) -> List[Path]:
    if not clips_dir.exists() or not clips_dir.is_dir():
        raise FileNotFoundError(f"Clips directory not found: {clips_dir}")
    files = [
        p for p in clips_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"
    ]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return files


def create_ffmpeg_listfile(files: List[Path], tmpdir: Path) -> Path:
    """
    Create a temporary file containing lines:
      file '/absolute/path/to/file1.mp3'
      file '/absolute/path/to/file2.mp3'
    Returns the Path to the created list file.
    """
    list_path = tmpdir / "ffmpeg_concat_list.txt"
    with list_path.open("w", encoding="utf-8") as f:
        for p in files:
            # Use absolute path. Escape single quotes by backslash to be safer.
            abs_path = str(p.resolve())
            safe_path = abs_path.replace("'", "\\'")
            f.write(f"file '{safe_path}'\n")
    return list_path


def run_ffmpeg_concat(
    listfile: Path, output: Path, reencode: bool = False
) -> Tuple[int, str, str]:
    """
    Call ffmpeg to concat the provided listfile into output.
    If reencode is False, try stream copy: '-c copy'. If that fails or reencode True,
    re-encode to a standard mp3 (libmp3lame).
    Returns (returncode, stdout, stderr).
    """
    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        raise EnvironmentError(
            "ffmpeg executable not found in PATH. Please install it and try again."
        )

    # First try direct concat with stream copy (fast, no re-encode)
    if not reencode:
        cmd = [
            ffmpeg_exe,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listfile),
            "-c",
            "copy",
            str(output),
        ]
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return proc.returncode, proc.stdout, proc.stderr

    # Re-encode fallback (create a consistent mp3)
    cmd_re = [
        ffmpeg_exe,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listfile),
        "-acodec",
        "libmp3lame",
        "-ar",
        "44100",
        "-b:a",
        "192k",
        str(output),
    ]
    proc = subprocess.run(
        cmd_re, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return proc.returncode, proc.stdout, proc.stderr


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Concatenate MP3 clips using ffmpeg (concat demuxer)."
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        default="clips",
        help="Directory containing clip mp3 files (default: ./clips)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="merged.mp3",
        help="Output MP3 path (default: ./merged.mp3)",
    )
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="Force re-encoding instead of stream-copy (useful if clips have differing codecs)",
    )
    args = parser.parse_args(argv)

    clips_dir = Path(args.input_dir)
    output_path = Path(args.output)

    try:
        mp3_files = find_mp3_files(clips_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if not mp3_files:
        print(
            f"No MP3 files found in {clips_dir.resolve()}. Nothing to merge.",
            file=sys.stderr,
        )
        return 0

    print(f"Found {len(mp3_files)} mp3 files in {clips_dir.resolve()}.")
    for i, p in enumerate(mp3_files, 1):
        print(f"  {i:>3}: {p.name}")

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        listfile = create_ffmpeg_listfile(mp3_files, tmpdir)
        print(f"Created ffmpeg list file: {listfile}")

        print(f"Running concat -> {output_path} (reencode={args.reencode}) ...")
        try:
            rc, out, err = run_ffmpeg_concat(
                listfile, output_path, reencode=args.reencode
            )
        except EnvironmentError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 3

        if rc == 0:
            print(f"Success: merged file written to {output_path.resolve()}")
            return 0

        # If stream-copy failed and user didn't request reencode, try re-encoding automatically
        if rc != 0 and not args.reencode:
            print(
                "Stream-copy concat failed. Trying re-encode fallback...",
                file=sys.stderr,
            )
            rc2, out2, err2 = run_ffmpeg_concat(listfile, output_path, reencode=True)
            if rc2 == 0:
                print(
                    f"Success (re-encoded): merged file written to {output_path.resolve()}"
                )
                return 0
            else:
                print("Re-encode attempt failed. ffmpeg output:", file=sys.stderr)
                print(err2, file=sys.stderr)
                return 4

        # If we get here, ffmpeg returned non-zero and user asked reencode (or reencode also failed)
        print("ffmpeg failed. Output (stderr):", file=sys.stderr)
        print(err, file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
