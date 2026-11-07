"""DB-side purge of stale Google-sourced field values (dry-run DEFAULT).

Per PROJECT.md section 15 (Google terms): lat/lng and other Google-derived
values must be re-verified or dropped 30 days after the fetch batch. This
script NULLS the Google-derived fields of stale rows and keeps the row as a
STUB identified by google_place_id (indefinitely storable) — all curated data
is preserved. Nothing is deleted; no google_place_id is lost.

Written only for rows:
    source = 'google' AND google_data_fetched_at < now() - interval '30 days'

Field treatment (owner decisions 2026-11-08):
- name -> NULL (Google rows may be nameless; osm/manual rows keep their name)
- status -> 'unknown' (added to the status CHECK by the revision migration)
- final_score -> NULL (recalculated after the refresh re-fill)
- location, address, phone, website, opening_hours,
  google_rating, google_review_count, google_data_fetched_at -> NULL
- price_band / price_band_source -> NULL, but ONLY when
  price_band_source is not 'manual' (manual bands are NEVER dropped)
- last_verified, curated_score, tags, google_place_id, osm_id, city_id:
  untouched

Dry-run lists every affected row first. --commit performs one transaction.
Requires migrations 20261108000010 (+ 20261108090000) to be applied so the
CHECK constraints accept the purged stub shape. Never prints credentials.
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect, get_connection_string, masked_host  # noqa: E402

SELECT_SQL = """
select google_place_id, name,
       to_char(google_data_fetched_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as fetched
from public.cafes
where source = 'google'
  and google_data_fetched_at < now() - (%s || ' days')::interval
order by google_data_fetched_at
"""

UPDATE_SQL = """
update public.cafes set
    name = null,                    -- google stubs may be nameless (guard: manual/osm keep name)
    status = 'unknown',             -- revision migration item (b)
    final_score = null,             -- revision item (c); recalculated after refresh
    location = null,
    address = null,
    phone = null,
    website = null,
    opening_hours = null,
    google_rating = null,
    google_review_count = null,
    google_data_fetched_at = null,
    price_band = case when price_band_source = 'manual' then price_band else null end,
    price_band_source = case when price_band_source = 'manual' then price_band_source else null end
where source = 'google'
  and google_data_fetched_at < now() - (%s || ' days')::interval
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name as in the cities table")
    parser.add_argument("--older-than-days", type=int, default=30,
                        help="staleness threshold in days (default: 30 per Google terms)")
    parser.add_argument("--commit", action="store_true",
                        help="actually purge (default: dry-run, lists only)")
    args = parser.parse_args()

    url = get_connection_string()
    print(f"DB host: {masked_host(url)}")
    conn = psycopg.connect(url)  # non-autocommit: one explicit txn on --commit
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    if not cur.fetchone():
        raise SystemExit(f"City {args.city!r} not found in cities table.")

    cur.execute(SELECT_SQL, (args.older_than_days,))
    rows = cur.fetchall()
    print(f"\ncutoff: now() - {args.older_than_days} days; stale Google rows: {len(rows)}")
    for pid, name, fetched in rows[:20]:
        print(f"  {fetched}  {pid}  {name}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")

    if not rows:
        print("\nNothing to purge yet.")
        cur.close()
        conn.close()
        return 0

    if not args.commit:
        print("\nDRY-RUN: nothing purged. Pass --commit to null Google-derived fields.")
        cur.close()
        conn.close()
        return 0

    cur.execute(UPDATE_SQL, (args.older_than_days,))
    conn.commit()
    print(f"\nPURGED: {cur.rowcount} row(s) reduced to stubs "
          "(name/status='unknown'/final_score nulled per owner decisions; "
          "google_place_id + curated data + cafe_tags kept).")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
