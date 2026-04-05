#!/usr/bin/env python3
"""
clean_all.py

Safely clean project-generated files:

- Remove all files and subdirectories inside the `workspace/clips` directory (but keep the directory).
- Remove all files and subdirectories inside the `workspace/segments` directory (but keep the directory).
- Remove all files and subdirectories inside the `workspace/scripts` directory (but keep the directory).

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

# Add project root to sys.path so 'src' module can be found
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
    parser = argparse.ArgumentParser(description="Clean workspace directories")
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
    from src.config import CLIPS_DIR, SCRIPTS_DIR, SEGMENTS_DIR

    parser.add_argument(
        "--clips-dir",
        default=str(CLIPS_DIR),
        help=f"Clips directory name (default: {CLIPS_DIR})",
    )
    parser.add_argument(
        "--segments-dir",
        default=str(SEGMENTS_DIR),
        help=f"Segments directory name (default: {SEGMENTS_DIR})",
    )
    parser.add_argument(
        "--scripts-dir",
        default=str(SCRIPTS_DIR),
        help=f"Scripts directory name (default: {SCRIPTS_DIR})",
    )
    args = parser.parse_args(argv)

    # Determine project root (parent directory of the src folder)
    project_dir = Path(__file__).resolve().parent.parent

    clips_dir = project_dir / args.clips_dir
    segments_dir = project_dir / args.segments_dir
    scripts_dir = project_dir / args.scripts_dir

    print(f"[INFO] Project directory: {project_dir}")
    print(f"[INFO] Target clips dir: {clips_dir}")
    print(f"[INFO] Target segments dir: {segments_dir}")
    print(f"[INFO] Target scripts dir: {scripts_dir}")
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

    # Clear scripts directory contents
    try:
        clear_directory_contents(scripts_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[ERROR] Unexpected error while clearing scripts: {e}", file=sys.stderr)
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
