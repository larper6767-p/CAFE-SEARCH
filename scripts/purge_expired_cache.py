"""Purge expired local cache batches (dry-run DEFAULT; --commit deletes).

Scope is HARD-LIMITED to <repo>/data/raw/ and <repo>/data/processed/ (the
PROJECT.md section 15 purge list). data/decisions/ is never touched — owner
review decisions live there and are tracked in git.

Expiry rule: lat/lng and cached responses must be gone 30 days after the
fetch-batch UTC date. A run directory (named like google_20261106T155506Z)
expires when its batch date is older than --older-than days. Files (cache
entries, stray files under processed/) expire by modification time.

Behavior:
- default: lists everything that WOULD be deleted, with counts and size;
  deletes nothing.
- --commit: deletes, but REFUSES when no fresh (newer) Google batch exists
  unless --confirm-without-replacement is also passed, so the loader always
  keeps at least one cached universe.
- --commit --confirm-without-replacement: deletes even with no replacement
  batch (owner's explicit choice, e.g. the 2026-12-06 purge).

Never prints credentials; touches nothing outside the two allowed roots
(every candidate is re-checked against the real paths of both roots before
deletion).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

RUN_DIR_RE = re.compile(r"(google|osm)_(\d{8}T\d{6}Z)$")

def batch_date(dirname: str) -> datetime | None:
    m = RUN_DIR_RE.match(dirname)
    if not m:
        return None
    return datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def collect_expired(root: str, cutoff: datetime) -> dict:
    """Collect expired run dirs / files under one scope root (never decisions)."""
    dirs: list[str] = []
    files: list[str] = []
    if not os.path.isdir(root):
        return {"dirs": dirs, "files": files}
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if os.path.isdir(path):
            bdate = batch_date(entry)
            if bdate is not None and bdate < cutoff:
                dirs.append(path)
            elif entry == "cache" and os.path.basename(root) == "raw":
                # flat param-hash cache: individual files expire by mtime
                for sub in sorted(os.listdir(path)):
                    sub_path = os.path.join(path, sub)
                    if os.path.isfile(sub_path) and sub_path_is_expired(sub_path, cutoff):
                        files.append(sub_path)
        elif os.path.isfile(path) and sub_path_is_expired(path, cutoff):
            files.append(path)
    return {"dirs": dirs, "files": files}


def sub_path_is_expired(path: str, cutoff: datetime) -> bool:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return False
    return mtime < cutoff


def fresh_google_batch_exists(raw_root: str, cutoff: datetime) -> bool:
    if not os.path.isdir(raw_root):
        return False
    for entry in os.listdir(raw_root):
        if not entry.startswith("google_"):
            continue
        bdate = batch_date(entry)
        if bdate and bdate >= cutoff:
            return True
    return False


def tree_size_bytes(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def guard_under_root(path: str, roots: list[str]) -> None:
    """Belt & braces: refuse to delete anything outside the allowed roots."""
    real = os.path.realpath(path)
    for root in roots:
        if real == os.path.realpath(root) or real.startswith(os.path.realpath(root) + os.sep):
            return
    raise RuntimeError(f"candidate escapes allowed roots: {path}")


def human_mb(n: int) -> str:
    return f"{n / (1 << 20):.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--base", default=default_base, help="repo root (default: auto)")
    parser.add_argument("--older-than", type=int, default=30,
                        help="expiry age in days (default: 30 per Google terms)")
    parser.add_argument("--commit", action="store_true",
                        help="actually delete (default: dry-run, lists only)")
    parser.add_argument("--confirm-without-replacement", action="store_true",
                        help="allow --commit even when no fresh batch exists")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.older_than)
    raw_root = os.path.join(args.base, "data", "raw")
    processed_root = os.path.join(args.base, "data", "processed")
    roots = [raw_root, processed_root]

    expired_dirs: list[str] = []
    expired_files: list[str] = []
    for root in roots:
        bucket = collect_expired(root, cutoff)
        expired_dirs.extend(bucket["dirs"])
        expired_files.extend(bucket["files"])
    for candidate in expired_dirs + expired_files:
        guard_under_root(candidate, roots)

    size = sum(tree_size_bytes(d) for d in expired_dirs) + \
        sum(tree_size_bytes(f) for f in expired_files)
    print(f"cutoff: {cutoff.isoformat()} (older than {args.older_than} days)")
    print(f"\n{len(expired_dirs)} directory dir(s) expired:")
    for d in expired_dirs:
        print(f"  DIR  {os.path.relpath(d, args.base)}  ({human_mb(tree_size_bytes(d))})")
    print(f"\n{len(expired_files)} file(s) expired:")
    for f in expired_files:
        print(f"  FILE {os.path.relpath(f, args.base)}")
    if not (expired_dirs or expired_files):
        print("\nNothing is expired yet. Nothing deleted in dry-run.")

    if not args.commit:
        print("\nDRY-RUN: nothing deleted. Pass --commit to delete.")
        return 0

    if not (expired_dirs or expired_files):
        return 0

    has_fresh = fresh_google_batch_exists(raw_root, cutoff)
    if not has_fresh and not args.confirm_without_replacement:
        print("\nREFUSING: no fresh Google batch exists to replace what would be "
              "deleted. The loader needs a cached place-id universe.\n"
              "Re-fetch first, or pass --confirm-without-replacement to confirm.")
        return 2
    if not has_fresh:
        print("\nWARNING: deleting with NO replacement batch (explicit flag).")

    n_dirs = 0
    for d in expired_dirs:
        shutil.rmtree(d)
        n_dirs += 1
    n_files = 0
    for f in expired_files:
        os.remove(f)
        n_files += 1
    print(f"\nDELETED: {n_dirs} dir(s), {n_files} file(s) under data/raw and "
          "data/processed. data/decisions was not touched (tracked in git).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
