#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_segments.py
-----------------
Split a long podcast script (UTF-8 text) into sentence-bounded segments,
each no longer than a configured maximum number of characters (default 4500).
Segments are written as text files into an output directory.

Default behavior:
    - Input:  Projects/podcast_script.txt  (relative to this script)
    - Output dir: Projects/workspace/segments/
    - Max chars per segment: 4500
    - Sentence splitting is done with a Chinese/English-aware heuristic:
        Chinese punctuation: 。！？；…… (and variants)
        English punctuation: .!? (including ellipses)
      Newlines are also treated as sentence boundaries.
    - If a single sentence is longer than the max, it will be split:
        - For sentences containing whitespace, split at the last whitespace
          before the limit (to avoid breaking words when possible).
        - Otherwise, perform a hard character cut.

Usage:
    python3 Projects/split_segments.py
or specify arguments:
    python3 Projects/split_segments.py --input path/to/script.txt --outdir out/segments --max-chars 4200

Output:
    - files: outdir/segment_01.txt, segment_02.txt, ...
    - manifest: outdir/segments_list.txt (filename + char count)
    - prints a small summary to stdout
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Tuple

DEFAULT_INPUT = os.path.join(
    os.path.dirname(__file__) or ".", "..", "workspace", "script.txt"
)
DEFAULT_OUTDIR = os.path.join(
    os.path.dirname(__file__) or ".", "..", "workspace", "segments"
)
DEFAULT_MAX_CHARS = 1000


def split_into_sentences(text: str) -> List[str]:
    """
    Heuristic sentence splitter supporting Chinese and English punctuation.
    Keeps delimiters as part of the sentence.

    The regex captures the minimal run of characters up to and including the first
    sentence terminator (Chinese punctuation, ellipsis, English ., ?, !, or newline).
    """
    # Normalize CRLF -> LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # The regex:
    #   - .+?       : non-greedy match of anything (including newlines thanks to re.S)
    #   - (?: ... ): one of the terminators below
    #     - [。！？；]             : common Chinese sentence terminators
    #     - ……                    : Chinese ellipsis (two char)
    #     - \.{1,3}               : English ., .., ...
    #     - [!?]+                 : sequences of ! or ?
    #     - \n                    : newline as a boundary
    #
    # We keep the delimiter as part of the matched sentence.
    pattern = re.compile(r".+?(?:[。！？；]|……|\.{1,3}|[!?]+|\n)", re.S)
    parts = pattern.findall(text)

    # If regex fails to find matches (unlikely), return whole text as one piece
    if not parts:
        return [text.strip()]

    # Trim whitespace at ends of each sentence, but keep punctuation
    sentences = [p.strip() for p in parts if p.strip()]
    return sentences


def chunk_sentences(sentences: List[str], max_chars: int) -> List[str]:
    """
    Accumulate sentences into segments with total length <= max_chars.

    If a single sentence is longer than max_chars, split it:
      - Prefer splitting at a whitespace boundary (for Latin text),
      - Otherwise hard cut into chunks of size max_chars.
    """
    segments: List[str] = []
    cur = ""

    def flush_current():
        nonlocal cur
        if cur:
            segments.append(cur.strip())
            cur = ""

    for s in sentences:
        s_len = len(s)
        cur_len = len(cur)
        if cur_len + s_len <= max_chars:
            # append to current segment
            if cur == "":
                cur = s
            else:
                # separate sentences with a single space for readability
                cur = cur + " " + s
            continue

        # If current buffer non-empty, flush it first
        if cur:
            flush_current()

        # If sentence itself fits alone, start a new current with it
        if s_len <= max_chars:
            cur = s
            continue

        # Sentence too long by itself -> must be split
        # Strategy: try to split by whitespace boundaries first for latin text
        remaining = s
        while remaining:
            if len(remaining) <= max_chars:
                # fits into one chunk
                segments.append(remaining.strip())
                break

            # look for last whitespace within max_chars window
            window = remaining[:max_chars]
            # find last whitespace char in window
            idx = max(window.rfind(" "), window.rfind("\t"), window.rfind("\n"))
            if idx > 0:
                chunk = window[:idx].rstrip()
                segments.append(chunk)
                # skip the whitespace we split on
                remaining = remaining[idx:].lstrip()
            else:
                # no whitespace in window, hard cut
                chunk = window
                segments.append(chunk)
                remaining = remaining[len(chunk) :]

        # continue with next sentence

    # flush any remaining buffer
    if cur:
        segments.append(cur.strip())

    return segments


def write_segments(segments: List[str], outdir: str) -> List[Tuple[str, int]]:
    """
    Write each segment to a numbered file under outdir.
    Returns a list of (filepath, char_count).
    """
    os.makedirs(outdir, exist_ok=True)
    # compute padding width based on count
    pad = max(2, len(str(len(segments))))
    manifest: List[Tuple[str, int]] = []
    for i, seg in enumerate(segments, start=1):
        fname = f"segment_{i:0{pad}d}.txt"
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as fout:
            fout.write(seg)
        manifest.append((path, len(seg)))
    # write a simple manifest file
    manifest_path = os.path.join(outdir, "segments_list.txt")
    with open(manifest_path, "w", encoding="utf-8") as m:
        for p, l in manifest:
            m.write(f"{os.path.basename(p)}\t{l}\n")
    return manifest


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Split a podcast script into sentence-bounded segments."
    )
    p.add_argument(
        "--input", "-i", default=DEFAULT_INPUT, help="Path to input text file (UTF-8)."
    )
    p.add_argument(
        "--outdir",
        "-o",
        default=DEFAULT_OUTDIR,
        help="Directory to write segments into.",
    )
    p.add_argument(
        "--max-chars",
        "-m",
        default=DEFAULT_MAX_CHARS,
        type=int,
        help="Maximum characters per segment (approximate, uses len()).",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Do not write files; just print summary."
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isfile(args.input):
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        sys.exit(2)

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        print("Input file is empty; nothing to do.", file=sys.stderr)
        sys.exit(0)

    sentences = split_into_sentences(text)
    segments = chunk_sentences(sentences, args.max_chars)

    if args.dry_run:
        print("Dry run summary:")
        print(f"  Sentences extracted: {len(sentences)}")
        print(f"  Segments (would write): {len(segments)}")
        for i, seg in enumerate(segments, 1):
            print(f"    {i:02d}: {len(seg)} chars")
        return

    manifest = write_segments(segments, args.outdir)

    total_chars = sum(length for _, length in manifest)
    print("Split complete.")
    print(f"  Input: {args.input}")
    print(f"  Output dir: {args.outdir}")
    print(f"  Max chars per segment: {args.max_chars}")
    print(f"  Sentences: {len(sentences)}")
    print(f"  Segments written: {len(manifest)}")
    print(f"  Total characters written: {total_chars}")
    print("")
    print("First few segments:")
    for p, l in manifest[:10]:
        print(f"  {os.path.basename(p)}  {l} chars")
    print("")
    print(f"Manifest saved to: {os.path.join(args.outdir, 'segments_list.txt')}")


if __name__ == "__main__":
    main()
