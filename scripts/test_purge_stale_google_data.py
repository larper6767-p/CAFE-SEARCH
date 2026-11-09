"""Rolled-back DB tests for the purge stub shape (owner decisions, 2026-11-08).

Covers scripts/purge_stale_google_data.py UPDATE_SQL semantics and the schema
rules applied by migration 20261108090000. DEV DATABASE ONLY; every test runs
inside a transaction that is ROLLED BACK — cats stay 233 and nothing persists.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import purge_stale_google_data as psg  # noqa: E402
from db import get_connection_string  # noqa: E402

CENTER_LAT, CENTER_LNG = 24.807779351178766, 93.93915469958242
OLD_VERIFIED = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
HOURS = json.dumps({
    "mon": [["08:00", "17:00"]], "tue": [["08:00", "17:00"]],
    "wed": [["08:00", "17:00"]], "thu": [["08:00", "17:00"]],
    "fri": [["08:00", "17:00"]], "sat": [["08:00", "18:00"]],
    "sun": [],
}, separators=(",", ":"))


def stale_ts() -> datetime:
    """One day past the 30-day cutoff, so the purge UPDATE matches it."""
    return datetime.now(timezone.utc) - timedelta(days=31)


class PurgeStubShape(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = psycopg.connect(get_connection_string())  # non-autocommit

        def _teardown() -> None:
            # rollback FIRST (discard everything), then close
            try:
                self.conn.rollback()
            finally:
                self.conn.close()

        self.addCleanup(_teardown)
        self.cur = self.conn.cursor()
        self.cur.execute("select id from public.cities where name = 'Imphal'")
        self.city_id = self.cur.fetchone()[0]

    def _seed_google(self, pid: str, *, band, band_source, fetched, curated=3.5) -> int:
        self.cur.execute(
            """
            insert into public.cafes
                (city_id, name, address, location, phone, website, opening_hours,
                 status, source, google_place_id, google_rating, google_review_count,
                 price_band, price_band_source, curated_score, final_score,
                 last_verified, google_data_fetched_at)
            values (%s, 'Old Stub Cafe', 'Old Far Address',
                    st_setsrid(st_makepoint(%s, %s), 4326)::extensions.geography,
                    '+91 90000 00000', 'old.example.in',
                    %s::jsonb, 'operational', 'google', %s, 4.10, 12,
                    %s, %s, %s, 3.90, %s, %s)
            returning id
            """,
            (self.city_id, CENTER_LNG + 0.01, CENTER_LAT + 0.01, HOURS,
             pid, band, band_source, curated, OLD_VERIFIED, fetched),
        )
        return self.cur.fetchone()[0]

    def _tag(self, cafe_id: int, tag: str = "wifi") -> None:
        self.cur.execute(
            """
            insert into public.cafe_tags (cafe_id, tag_id)
            values (%s, (select id from public.tags where name = %s))
            on conflict (cafe_id, tag_id) do nothing
            """,
            (cafe_id, tag),
        )

    def _run_purge(self) -> int:
        self.cur.execute(psg.UPDATE_SQL, ("30",))
        return self.cur.rowcount

    def _row(self, cafe_id: int) -> dict:
        self.cur.execute(
            """
            select name, status, final_score, google_data_fetched_at,
                   location, address, phone, website, opening_hours,
                   google_rating, google_review_count,
                   price_band, price_band_source, curated_score,
                   google_place_id, last_verified
            from public.cafes where id = %s
            """,
            (cafe_id,),
        )
        cols = ["name", "status", "final_score", "fetched_at", "location",
                "address", "phone", "website", "hours", "rating", "reviews",
                "price_band", "price_band_source", "curated_score",
                "place_id", "last_verified"]
        return dict(zip(cols, self.cur.fetchone()))

    def test_01_stale_google_row_becomes_stub(self):
        cafe_id = self._seed_google("TESTPURGE_STALE_A", band=2, band_source="google",
                                    fetched=stale_ts())
        self._tag(cafe_id)
        touched = self._run_purge()
        self.assertEqual(touched, 1)  # exactly this seeded stale row
        row = self._row(cafe_id)
        self.assertIsNone(row["name"])               # owner decision: google stubs nameless
        self.assertEqual(row["status"], "unknown")   # owner decision
        for col in ("final_score", "fetched_at", "location", "address", "phone",
                    "website", "hours", "rating", "reviews",
                    "price_band", "price_band_source"):
            self.assertIsNone(row[col], f"{col} must be NULL on a purged stub")
        self.assertEqual(float(row["curated_score"]), 3.5)  # curated data survives
        self.assertEqual(row["place_id"], "TESTPURGE_STALE_A")  # identity survives
        self.assertEqual(row["last_verified"], OLD_VERIFIED)    # manual semantics untouched
        self.cur.execute("select count(*) from public.cafe_tags where cafe_id = %s",
                         (cafe_id,))
        self.assertEqual(self.cur.fetchone()[0], 1)             # tags stay attached

    def test_02_manual_price_band_survives_purge(self):
        cafe_id = self._seed_google("TESTPURGE_STALE_B", band=2, band_source="manual",
                                    fetched=stale_ts())
        self._run_purge()
        row = self._row(cafe_id)
        self.assertEqual(row["price_band"], 2)           # manual band NEVER dropped
        self.assertEqual(row["price_band_source"], "manual")
        self.assertIsNone(row["name"])                   # name is still Google-derived

    def test_03_fresh_row_not_touched(self):
        cafe_id = self._seed_google("TESTPURGE_FRESH_C", band=2, band_source="google",
                                    fetched=datetime.now(timezone.utc))
        touched = self._run_purge()
        self.assertEqual(touched, 0)  # only stale rows match
        row = self._row(cafe_id)
        self.assertEqual(row["name"], "Old Stub Cafe")
        self.assertEqual(row["price_band"], 2)
        self.assertEqual(row["status"], "operational")

    def test_04_manual_row_name_null_rejected(self):
        import psycopg.errors as pes
        with self.assertRaises(pes.CheckViolation) as ctx:
            self.cur.execute(
                """
                insert into public.cafes (city_id, name, location, status, source,
                                          price_band, price_band_source)
                values (%s, null,
                        st_setsrid(st_makepoint(%s, %s), 4326)::extensions.geography,
                        'operational', 'manual', 2, 'manual')
                """,
                (self.city_id, CENTER_LNG + 0.02, CENTER_LAT + 0.02),
            )
        self.assertIn("cafes_name_live_guard_check", str(ctx.exception))

    def test_05_old_status_values_still_accepted(self):
        for status in ("operational", "closed_temporarily", "closed_permanently"):
            self.cur.execute(
                """
                insert into public.cafes (city_id, name, location, status, source)
                values (%s, %s,
                        st_setsrid(st_makepoint(%s, %s), 4326)::extensions.geography,
                        %s, 'manual')
                """,
                (self.city_id, f"Status Probe {status}", CENTER_LNG + 0.03,
                 CENTER_LAT + 0.03, status),
            )
        # no exception = all three legacy statuses still work


if __name__ == "__main__":
    unittest.main()
