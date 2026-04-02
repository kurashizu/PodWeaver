#!/usr/bin/env python3
"""
clean_all.py

Safely clean project-generated files:

- Remove all files and subdirectories inside the `clips` directory (but keep the directory).
- Remove all files and subdirectories inside the `segments` directory (but keep the directory).
- Delete `script.txt` in the same project directory (if present).

Usage:
    python clean_all.py          # interactive confirmation
    python clean_all.py --yes    # run without confirmation
    python clean_all.py --dry-run  # show what would be removed, don't actually delete

Notes:
- This script assumes it resides in the project directory (i.e. the same folder that contains
  the `clips` and `segments` folders and the `script.txt` file). It determines paths
  relative to its own file location for safety.
- Use --yes to run non-interactively (useful for automation).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable


def confirm(prompt: str) -> bool:
    try:
        resp = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False
    return resp in ("y", "yes")


def iter_dir_children(directory: Path) -> Iterable[Path]:
    try:
        for p in sorted(directory.iterdir()):
            yield p
    except Exception:
        return


def remove_path(p: Path, dry_run: bool = False) -> bool:
    """
    Remove file or directory. Returns True if removed (or would be removed in dry-run),
    False if skipped or failed.
    """
    try:
        if dry_run:
            print(f"[DRY-RUN] Would remove: {p}")
            return True

        if p.is_dir():
            shutil.rmtree(p)
            print(f"[REMOVED DIR] {p}")
            return True
        else:
            p.unlink()
            print(f"[REMOVED FILE] {p}")
            return True
    except FileNotFoundError:
        # already gone
        return True
    except PermissionError as e:
        print(f"[ERROR] Permission denied removing {p}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[ERROR] Failed to remove {p}: {e}", file=sys.stderr)
        return False


def clear_directory_contents(directory: Path, dry_run: bool = False) -> int:
    """
    Remove all children of `directory` but do NOT remove the directory itself.
    Returns count of removed (or would-be-removed in dry-run) items.
    """
    if not directory.exists():
        print(f"[INFO] Directory not found (skipping): {directory}")
        return 0
    if not directory.is_dir():
        print(f"[WARN] Path is not a directory (skipping): {directory}")
        return 0

    removed = 0
    for child in iter_dir_children(directory):
        ok = remove_path(child, dry_run=dry_run)
        if ok:
            removed += 1
    print(f"[INFO] Cleared {removed} items from {directory}")
    return removed


def delete_file_if_exists(path: Path, dry_run: bool = False) -> bool:
    if not path.exists():
        print(f"[INFO] File not found (skipping): {path}")
        return True
    if not path.is_file():
        print(f"[WARN] Path exists but is not a file (skipping): {path}")
        return False
    return remove_path(path, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean clips, segments and script.txt")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Do not prompt for confirmation; proceed with deletions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without performing deletions",
    )
    parser.add_argument(
        "--clips-dir",
        default="clips",
        help="Clips directory name relative to the script (default: clips)",
    )
    parser.add_argument(
        "--segments-dir",
        default="segments",
        help="Segments directory name relative to the script (default: segments)",
    )
    parser.add_argument(
        "--script-file",
        default="script.txt",
        help="Script file name relative to the script (default: script.txt)",
    )
    args = parser.parse_args(argv)

    # Determine project root (directory containing this script)
    project_dir = Path(__file__).resolve().parent

    clips_dir = project_dir / args.clips_dir
    segments_dir = project_dir / args.segments_dir
    script_file = project_dir / args.script_file

    print(f"[INFO] Project directory: {project_dir}")
    print(f"[INFO] Target clips dir: {clips_dir}")
    print(f"[INFO] Target segments dir: {segments_dir}")
    print(f"[INFO] Target script file: {script_file}")
    print()

    if args.dry_run:
        print("[INFO] DRY RUN - no files will be deleted")
        proceed = True
    elif args.yes:
        proceed = True
    else:
        proceed = confirm(
            "Are you sure you want to delete the contents described above?"
        )

    if not proceed:
        print("No changes made.")
        return 0

    overall_ok = True

    # Clear clips directory contents
    try:
        clear_directory_contents(clips_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[ERROR] Unexpected error while clearing clips: {e}", file=sys.stderr)
        overall_ok = False

    # Clear segments directory contents
    try:
        clear_directory_contents(segments_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[ERROR] Unexpected error while clearing segments: {e}", file=sys.stderr)
        overall_ok = False

    # Delete script.txt
    try:
        ok = delete_file_if_exists(script_file, dry_run=args.dry_run)
        if not ok:
            overall_ok = False
    except Exception as e:
        print(
            f"[ERROR] Unexpected error while deleting script file: {e}", file=sys.stderr
        )
        overall_ok = False

    if overall_ok:
        if args.dry_run:
            print("[DRY-RUN] No changes applied.")
        else:
            print("[DONE] Cleanup completed successfully.")
        return 0
    else:
        print(
            "[ERROR] Cleanup completed with errors. See messages above.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
