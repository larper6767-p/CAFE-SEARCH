"""Apply SQL migrations from supabase/migrations/ in filename order.

Rules (per AGENTS.md — all schema changes go through migration files):
- Each migration runs in a single transaction (all-or-nothing).
- Applied files are tracked with a sha256 checksum in migrations.schema_migrations
  — a dedicated schema, NOT public, so the Data API never exposes it.
- Editing a file that was already applied fails loudly before anything runs.
- NEVER auto-applies: the default action only LISTS pending + staged files.
  Application needs an explicit flag:
      --apply              apply ALL pending files in supabase/migrations/
      --only <filename>    apply exactly one pending file
  Files in supabase/migrations_staged/ are NEVER applied (or listed as pending);
  they only move out of staging when the owner approves (move + --only).
- Prints a masked DB host (host:port/db, no user/password); never prints DATABASE_URL.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\apply_migrations.py            # list only
    .venv\\Scripts\\python.exe scripts\\apply_migrations.py --apply
    .venv\\Scripts\\python.exe scripts\\apply_migrations.py --only 20261108090000.sql
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg

from db import connect, get_connection_string, masked_host

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
STAGED_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations_staged"

# Idempotent bootstrap: keeps migration state in its own schema (not public),
# so it is never exposed through the Supabase Data API.
BOOTSTRAP = """
create schema if not exists migrations;
create table if not exists migrations.schema_migrations (
    filename text primary key,
    checksum text not null,
    applied_at timestamptz not null default now()
);
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_file(conn, f: Path, applied_now: list[str]) -> None:
    checksum = sha256(f)
    print(f"--- applying {f.name} ...")
    with conn.transaction():
        conn.execute(BOOTSTRAP + "\n" + f.read_text(encoding="utf-8"))
        conn.execute(
            """
            insert into migrations.schema_migrations (filename, checksum)
            values (%s, %s)
            on conflict (filename) do update set checksum = excluded.checksum
            """,
            (f.name, checksum),
        )
    applied_now.append(f.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="apply ALL pending files in supabase/migrations/ "
                             "(default: LIST ONLY — never auto-applies)")
    parser.add_argument("--only", metavar="FILE",
                        help="apply exactly this one pending file (exact filename)")
    args = parser.parse_args()
    if args.apply and args.only:
        print("ERROR: --apply and --only are mutually exclusive.")
        return 1

    url = get_connection_string()
    print(f"DB host: {masked_host(url)}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    staged = sorted(STAGED_DIR.glob("*.sql")) if STAGED_DIR.is_dir() else []
    if not files:
        print(f"No .sql migration files found in {MIGRATIONS_DIR}")
        return 1

    with connect(url) as conn:
        with conn.transaction():
            conn.execute(BOOTSTRAP)
        tracked: dict[str, str] = {}
        for filename, checksum in conn.execute(
            "select filename, checksum from migrations.schema_migrations"
        ).fetchall():
            tracked[filename] = checksum

        # Fail loudly BEFORE applying anything if an applied file was edited.
        edited = [
            f.name
            for f in files
            if f.name in tracked and sha256(f) != tracked[f.name]
        ]
        if edited:
            for name in edited:
                print(f"ERROR: {name} changed after it was applied!")
            print(
                "Never edit an applied migration. Revert the file to its "
                "applied form; new schema changes must go in a NEW migration file."
            )
            return 1

        pending = [f for f in files if f.name not in tracked]
        print(f"tracked: {len(tracked)} | pending in migrations/: {len(pending)} "
              f"| staged (never auto-applied): {len(staged)}")
        for f in pending:
            print(f"  pending : {f.name}")
        for f in staged:
            print(f"  staged  : {f.name}")

        if not args.apply and not args.only:
            print("\nMODE: list only. To apply pending files, use --apply "
                  "(all) or --only <filename>. Staged files stay staged.")
            return 0

        if args.only:
            targets = [f for f in files if f.name == args.only]
            if not targets:
                print(f"ERROR: no pending file named {args.only!r} in "
                      f"{MIGRATIONS_DIR} (check staging or spelling).")
                return 1
            if targets[0].name in tracked:
                print(f"ERROR: {targets[0].name} is already applied "
                      "(checksum-locked). New changes need a NEW migration file.")
                return 1
        else:
            targets = pending
            if not targets:
                print("\nNothing pending — nothing to apply.")
                return 0

        applied_now: list[str] = []
        for f in targets:
            apply_file(conn, f, applied_now)

        total_tracked = conn.execute(
            "select count(*) from migrations.schema_migrations"
        ).fetchone()[0]
    total_line = f"Total tracked: {total_tracked}."
    if applied_now:
        print(f"Done. Applied now: {len(applied_now)} ({', '.join(applied_now)}). "
              + total_line)
    else:
        print(f"Done. Applied now: 0. " + total_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
