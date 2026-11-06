"""Apply SQL migrations from supabase/migrations/ in filename order.

Rules (per AGENTS.md — all schema changes go through migration files):
- Each migration runs in a single transaction (all-or-nothing).
- Applied files are tracked with a sha256 checksum in migrations.schema_migrations
  — a dedicated schema, NOT public, so the Data API never exposes it.
- Editing a file that was already applied fails loudly before anything runs.
- Prints a masked DB host (host:port/db, no user/password); never prints DATABASE_URL.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\apply_migrations.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg

from db import connect, get_connection_string, masked_host

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"

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


def main() -> int:
    url = get_connection_string()
    print(f"DB host: {masked_host(url)}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
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

        applied_now = 0
        for f in files:
            checksum = sha256(f)
            if f.name in tracked:
                print(f"=== {f.name}: already applied, checksum matches — skipping")
                continue
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
            applied_now += 1

        total_tracked = conn.execute(
            "select count(*) from migrations.schema_migrations"
        ).fetchone()[0]
    print(f"Done. Applied now: {applied_now}. Total tracked: {total_tracked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
