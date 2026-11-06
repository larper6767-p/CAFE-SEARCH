"""Connectivity check for the Supabase Postgres database.

Prints a masked host (never DATABASE_URL or the password), server version,
and whether PostGIS is installed/available.
"""
from __future__ import annotations

import sys

from db import connect, get_connection_string, masked_host


def main() -> int:
    url = get_connection_string()
    print(f"Connecting to {masked_host(url)} ...")
    try:
        with connect(url) as conn:
            server = conn.execute("select version()").fetchone()[0]
            user, dbname = conn.execute(
                "select current_user, current_database()"
            ).fetchone()
            print(f"Server: {server}")
            print(f"Connected as user {user!r} to database {dbname!r}")

            postgis = conn.execute(
                "select extname, extversion from pg_extension where extname = 'postgis'"
            ).fetchall()
            if postgis:
                print(f"PostGIS: installed (version {postgis[0][1]})")
            else:
                available = conn.execute(
                    "select name, default_version from pg_available_extensions "
                    "where name = 'postgis'"
                ).fetchall()
                if available:
                    print(f"PostGIS: not installed yet; available version {available[0][1]}")
                else:
                    print("PostGIS: NOT AVAILABLE on this instance")

            tables = conn.execute(
                "select count(*) from pg_tables where schemaname = 'public'"
            ).fetchone()[0]
            print(f"Tables in public schema: {tables}")
    except Exception as exc:  # noqa: BLE001 — we want the failure reason printed
        print(f"CONNECTION FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("Connection OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
