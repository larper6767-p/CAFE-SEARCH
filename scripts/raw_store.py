"""Shared helpers for fetch scripts: params hashing, response cache, run manifest.

Raw responses are stored under data/raw/ (gitignored) so re-runs reuse them
without re-billing. Per-call metadata goes to each run's manifest.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

RAW_ROOT = Path(__file__).resolve().parents[1] / "data" / "raw"
CACHE_DIR = RAW_ROOT / "cache"


def new_run_dir(prefix: str) -> Path:
    """Create a fresh data/raw/<prefix><utc-timestamp>/ directory for this run."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RAW_ROOT / f"{prefix}{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def params_hash(endpoint: str, body: bytes, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update(endpoint.encode("utf-8"))
    h.update(b"|")
    h.update(body)
    h.update(b"|")
    h.update(extra.encode("utf-8"))
    return h.hexdigest()[:24]


def cache_file_for(endpoint: str, body: bytes, extra: str = "") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{params_hash(endpoint, body, extra)}.json"


def load_cached(cache_file: Path) -> bytes | None:
    if cache_file.is_file():
        return cache_file.read_bytes()
    return None


def write_cache(cache_file: Path, data: bytes) -> None:
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(cache_file)  # atomic rename


def save_response(run_dir: Path, seq: int, label: str, data: bytes) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
    path = run_dir / f"{seq:03d}_{slug}.json"
    path.write_bytes(data)
    return path


def log_call(run_dir: Path, record: dict) -> None:
    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **record,
    }
    with (run_dir / "manifest.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def summarize_json(data: bytes) -> int:
    """Count places/elements in a raw response without parsing the whole run."""
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return -1
    if isinstance(payload, dict):
        return len(payload.get("places") or payload.get("elements") or [])
    return -1
