"""Loader gate for the owner-approved review CSV (pure functions, no DB).

The step-6 loader must, before writing anything:
1. read the CSV (UTF-8 BOM, all fields quoted),
2. call validate_structure(): row count AND place IDs must match the fetched
   universe exactly (the owner may have edited decisions, never ids/rows),
3. take rows only through filter_include_rows(): any row whose decision is
   not 'include' is refused at row level (not ingested). Invalid decision
   tokens are a hard error via validate_structure().

Raised errors are fatal for the load run: no partial ingestion.
"""
from __future__ import annotations

import csv

DECISIONS = frozenset({"include", "review", "exclude"})


def read_approved_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def validate_structure(rows: list[dict], expected_ids: list[str],
                       source_name: str = "review CSV") -> list[str]:
    """Integrity checks. Returns a list of errors; empty list = valid."""
    errors: list[str] = []
    ids = [r.get("google_place_id") or "" for r in rows]
    if "" in ids:
        errors.append(f"{ids.count('')} empty place-id row(s) in {source_name}")
    dupes = {pid for pid in ids if pid and ids.count(pid) > 1}
    if dupes:
        errors.append(f"{len(dupes)} duplicate place id(s) in {source_name}: {sorted(dupes)[:3]}")
    id_set = set(ids)
    expected_set = set(expected_ids)
    missing = expected_set - id_set
    if missing:
        errors.append(f"{len(missing)} fetched place id(s) missing from {source_name} "
                      f"(e.g. {sorted(missing)[:3]})")
    extra = id_set - expected_set
    if extra:
        errors.append(f"{len(extra)} place id(s) not in the fetched set {source_name} "
                      f"(e.g. {sorted(extra)[:3]})")
    if len(rows) != len(expected_ids):
        errors.append(f"row count {len(rows)} != fetched unique count {len(expected_ids)}")
    for line_no, row in enumerate(rows, start=2):  # header is line 1
        decision = row.get("decision") or ""
        if decision not in DECISIONS:
            errors.append(f"line {line_no}: invalid decision {decision!r}")
    return errors


def filter_include_rows(rows: list[dict]) -> list[dict]:
    """Refuse (skip) any row whose decision is not 'include'; return the rest."""
    kept = [r for r in rows if (r.get("decision") or "").strip() == "include"]
    refused = len(rows) - len(kept)
    if refused:
        print(f"[gate] refused {refused} row(s) without decision 'include' — not ingesting them")
    return kept
