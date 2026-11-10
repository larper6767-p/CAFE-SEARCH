"""Phase 2, step 4 (owner ruling 2026-11-10): export the curated-score
WORKING sheet (owner fills it in for personally visited cafes).

The sheet goes to data/processed/ (gitignored, on the 2026-12-06 purge list) —
it is a workspace, not durable data. On --commit the curated importer records
the durable, stripped copy data/decisions/imphal_curated.csv
(id, curated_score, last_verified — tracked in git).

Sheet format (UTF-8 BOM, all fields quoted) — owner ruling:
    id, name, curated_score, last_verified
sorted by review_count desc (same ordering as the tagging sheet).

Cell semantics (rubric §7.1 + import_curated.py):
  curated_score: BLANK = leave unchanged; 'clear' = NULL; else 1.00-5.00
                 (half points allowed; visit-based only — §7.1)
  last_verified: BLANK = unchanged; else YYYY-MM-DD

Existing values are PREFILLED so edits round-trip. Refuses to overwrite an
existing sheet unless --overwrite; NOTE: --overwrite DOES discard un-imported
edits — import first or back the sheet up (warned at runtime).
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect, masked_host  # noqa: E402

HEADER = ["id", "name", "curated_score", "last_verified"]


def render_date(ts) -> str:
    """Calendar-date render of a stored timestamptz value, taken in UTC —
    identical to import_curated.render_date (each script stays standalone);
    a date entered as YYYY-MM-DD renders back as the same calendar date
    whatever the client session TIME ZONE is."""
    if hasattr(ts, "date"):  # timestamptz arrives as an aware datetime
        return ts.astimezone(timezone.utc).date().isoformat()
    return ts.isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    ap.add_argument("--csv", default="data/processed/imphal_curated.csv",
                    help="output path (default: data/processed/imphal_curated.csv)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing sheet — WARNING: discards "
                         "un-imported edits; import the sheet first (--commit) "
                         "or back it up before re-exporting")
    args = ap.parse_args()

    out_path = Path(args.csv)
    if out_path.exists():
        if not args.overwrite:
            raise SystemExit(
                f"refused: {out_path} exists — the working sheet is "
                "owner-edited. Pass --overwrite to replace it deliberately.")
        print("WARNING: overwriting this sheet discards UN-IMPORTED edits — "
              "make sure the previous sheet was imported (--commit) or back "
              "the file up before continuing.")

    print(f"DB host: {masked_host()}")
    conn = connect()  # read-only session
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]

    cur.execute(
        """
        select id, name, curated_score, last_verified
        from public.cafes
        where city_id = %s
        order by google_review_count desc nulls last, name asc nulls last, id
        """,
        (city_id,),
    )
    cafes = cur.fetchall()

    rows = []
    prefilled = 0
    for cafe_id, name, score, lv in cafes:
        rec = {h: "" for h in HEADER}
        rec["id"] = cafe_id
        rec["name"] = name if name is not None else ""
        if score is not None:
            rec["curated_score"] = f"{score:.2f}"
            prefilled += 1
        if lv is not None:
            rec["last_verified"] = render_date(lv)
        rows.append(rec)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"sheet written: {out_path}")
    print(f"rows: {len(cafes)} cafes x 4 columns ({', '.join(HEADER)})")
    print(f"existing curated scores prefilled: {prefilled}")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
