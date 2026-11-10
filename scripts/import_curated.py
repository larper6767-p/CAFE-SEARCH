"""Phase 2, step 4: import the owner's curated scores + last_verified.

Dry-run DEFAULT; --commit gated on the owner. One transaction.

CSV format (data/processed/ working file, UTF-8 BOM, all quoted):
    id, curated_score[, last_verified]
Semantics (owner, 2026-11-10):
  curated_score:
    blank          -> leave the stored value UNCHANGED
    'clear'        -> set NULL
    1.00 - 5.00    -> set (numeric(3,2) domain; more than 2 decimals rounds)
    anything else  -> hard error, no write at all
  last_verified:
    blank          -> leave UNCHANGED
    YYYY-MM-DD     -> set the year-month-day
    anything else  -> hard error
  cafes.notes is NEVER touched by this importer.
The ids come from the tagging export's universe (cafes.id), so no name fuzz.

Note: 'clear' semantics apply to curated_score only (as approved); last_verified
has no clear token — say the word if you want removal support later.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection_string, masked_host  # noqa: E402

HEADER_PLAIN = ["id", "curated_score"]
HEADER_LV = ["id", "curated_score", "last_verified"]


class CuratedImportError(Exception):
    """Hard error: nothing is written."""


def read_curated_csv(path: str) -> tuple[list[dict], bool]:
    """Returns (rows, has_last_verified_column)."""
    with open(path, "rb") as fh:
        if fh.read(3) != b"\xef\xbb\xbf":
            raise CuratedImportError(
                f"{Path(path).name} is missing the UTF-8 BOM — re-save as "
                "'CSV UTF-8' (Excel default)")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        if header not in (HEADER_PLAIN, HEADER_LV):
            raise CuratedImportError(
                f"header mismatch.\nexpected: {HEADER_LV}\nfound:    {header}")
        rows = list(reader)
    if not rows:
        raise CuratedImportError("no data rows")
    for i, row in enumerate(rows, start=2):
        row["_line"] = i
    return rows, header == HEADER_LV


def parse_score(raw) -> str | float | None:
    """Returns the write-intent for curated_score: None=unchanged,
    'clear'=NULL, else the rounded float."""
    cell = (raw or "").strip()
    if not cell:
        return None
    if cell.lower() == "clear":
        return "clear"
    try:
        value = round(float(cell.replace(",", ".")), 2)
    except ValueError:
        raise CuratedImportError(f"curated_score {raw!r} is not a number, "
                                 "blank, or 'clear'") from None
    if not 1.0 <= value <= 5.0:
        raise CuratedImportError(
            f"curated_score {value} is outside the 1.00-5.00 domain")
    return value


def parse_last_verified(raw) -> date | None:
    """None = unchanged (blank); otherwise the parsed date."""
    cell = (raw or "").strip()
    if not cell:
        return None
    try:
        return date.fromisoformat(cell)
    except ValueError:
        raise CuratedImportError(
            f"last_verified {raw!r} is not YYYY-MM-DD") from None


def plan_updates(rows: list[dict], has_lv: bool,
                 city_cafes: dict[int, str]) -> list[tuple]:
    """Returns [(cafe_id, name, new_score, new_lv, line)] for rows that will
    change something. Blank cells mean unchanged; rows where BOTH cells are
    blank are skipped entirely (no-op)."""
    planned: list = []
    seen: set[int] = set()
    for row in rows:
        line = row["_line"]
        raw_id = str(row.get("id") or "").strip()
        if not raw_id.isdigit():
            raise CuratedImportError(f"line {line}: id {raw_id!r} not an integer")
        cafe_id = int(raw_id)
        if cafe_id not in city_cafes:
            raise CuratedImportError(
                f"line {line}: id {cafe_id} is not a cafe of this city")
        if cafe_id in seen:
            raise CuratedImportError(
                f"line {line}: duplicate cafe id {cafe_id} in the file")
        seen.add(cafe_id)
        score = parse_score(row.get("curated_score"))
        lv = parse_last_verified(row.get("last_verified")) if has_lv else None
        if score is None and lv is None:
            continue  # both cells blank: nothing to do
        planned.append((cafe_id, city_cafes[cafe_id], score, lv, line))
    return planned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    ap.add_argument("--csv", required=True,
                    help="curated CSV path (data/processed/...)")
    ap.add_argument("--commit", action="store_true",
                    help="apply for real (default: dry-run)")
    args = ap.parse_args()

    print(f"DB host: {masked_host()}")
    conn = psycopg.connect(get_connection_string())
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]
    cur.execute("select id, coalesce(name, '') from public.cafes "
                "where city_id = %s", (city_id,))
    city_cafes = dict(cur.fetchall())

    try:
        rows, has_lv = read_curated_csv(args.csv)
        planned = plan_updates(rows, has_lv, city_cafes)
    except CuratedImportError as exc:
        conn.rollback()
        cur.close()
        conn.close()
        raise SystemExit(f"IMPORT REFUSED: {exc}")

    print(f"\ncsv: {args.csv} | rows {len(rows)} "
          f"(last_verified column: {'yes' if has_lv else 'no'})")
    if planned:
        print("\nplanned updates:")
        for cafe_id, name, score, lv, line in planned:
            score_txt = ("unchanged" if score is None else
                         "clear -> NULL" if score == "clear" else f"{score:.2f}")
            lv_txt = "unchanged" if lv is None else lv.isoformat()
            print(f"  line {line}  {name!r} (id {cafe_id})  "
                  f"curated: {score_txt}  last_verified: {lv_txt}")
    else:
        print("\nno-op: every row is blank-blank (nothing to change)")
    print(f"\nverdict: {len(planned)} row(s) would be updated")

    if not args.commit:
        conn.rollback()
        print("DRY-RUN: nothing written. Pass --commit to apply.")
        cur.close()
        conn.close()
        return 0

    updated = 0
    for cafe_id, _name, score, lv, _line in planned:
        sets, params = [], []
        if score is not None:
            sets.append("curated_score = %s")
            params.append(None if score == "clear" else score)
        if lv is not None:
            sets.append("last_verified = %s")
            params.append(lv)
        if not sets:
            continue
        try:
            cur.execute(
                f"update public.cafes set {', '.join(sets)} where id = %s",
                (*params, cafe_id),
            )
        except psycopg.DataError as exc:  # numeric(3,2) overflow / bad date
            conn.rollback()
            cur.close()
            conn.close()
            raise SystemExit(f"IMPORT REFUSED (db-level): id {cafe_id} "
                             f"— {exc}") from exc
        updated += cur.rowcount
    conn.commit()
    print(f"\nCOMMITTED: {updated} row(s) updated "
          "(curated_score and/or last_verified; notes untouched).")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
