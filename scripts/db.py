"""Shared helpers for connecting to the Supabase Postgres database.

Connection details come from DATABASE_URL in .env (never committed).
Scripts never print the URL or the password — use masked_host() instead.
"""
from __future__ import annotations

import os

from dotenv import find_dotenv, load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict


def get_connection_string() -> str:
    """Read DATABASE_URL from .env / environment; fail loudly if missing."""
    load_dotenv(find_dotenv())
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in "
            "the Supabase session pooler connection string."
        )
    return url


def masked_host(url: str | None = None) -> str:
    """Return 'host:port/database' with user and password removed. Safe to print."""
    info = conninfo_to_dict(url or get_connection_string())
    host = info.get("host", "<no host>")
    port = info.get("port", 5432)
    dbname = info.get("dbname", "<no db>")
    return f"{host}:{port}/{dbname}"


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a psycopg connection. autocommit filenames call transactions explicitly."""
    return psycopg.connect(
        url or get_connection_string(),
        autocommit=True,  # scripts manage transactions via conn.transaction()
    )
