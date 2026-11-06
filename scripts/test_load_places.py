"""Tests for scripts/load_places.py (step 6 loader).

Unit tests are pure and touch nothing. DB integration tests run against the
DEV database ONLY and are fully wrapped in transactions that are ROLLED BACK
at the end of each test — nothing persists (per owner instruction).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import load_places as lp  # noqa: E402
from db import connect, get_connection_string  # noqa: E402

BATCH_TS = datetime(2026, 11, 6, 16, 14, 57, tzinfo=timezone.utc)
CENTER_LAT, CENTER_LNG = 24.807779351178766, 93.93915469958242  # Imphal city row


def good_row(**over) -> dict:
    base = {
        "google_place_id": "TESTPID_LOAD_1",
        "decision": "include",
        "name": "Test Cafe & Bakehouse",
        "address": "Thangal Bazar, Imphal",
        "business_status": "operational",
        "suggested_price_band": "2",
        "rating": "4.3",
        "review_count": "210",
        "hours_json": json.dumps({
            "mon": [["08:00", "17:00"]], "tue": [["08:00", "17:00"]],
            "wed": [["08:00", "17:00"]], "thu": [["08:00", "17:00"]],
            "fri": [["08:00", "17:00"]], "sat": [["08:00", "18:00"]],
            "sun": [],
        }, ensure_ascii=False, separators=(",", ":")),
        "lat": str(CENTER_LAT + 0.01),
        "lng": str(CENTER_LNG + 0.01),
        "phone": " +91 98560 00000 ",
        "website": " example.in/menu ",
    }
    base.update(over)
    return base


def params_for(row: dict) -> dict:
    params, errors = lp.validate_row(row, CENTER_LAT, CENTER_LNG)
    if errors:
        raise AssertionError(f"expected clean row, got errors: {errors}")
    return params


class TestHoursShape(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(lp._check_hours_shape(""))

    def test_full_247_valid(self):
        hours = {d: [["00:00", "24:00"]] for d in lp.WEEKDAYS}
        self.assertEqual(lp._check_hours_shape(json.dumps(hours)), hours)

    def test_sat_sun_wrap_split_ok(self):
        hours = {d: [] for d in lp.WEEKDAYS}
        hours["sat"] = [["08:00", "18:00"], ["18:00", "24:00"]]
        hours["sun"] = [["00:00", "03:00"]]
        self.assertEqual(lp._check_hours_shape(json.dumps(hours)), hours)

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            lp._check_hours_shape("{not json")

    def test_wrong_keys_raise(self):
        with self.assertRaises(ValueError):
            lp._check_hours_shape(json.dumps({d: [] for d in ("mon", "sun")}))

    def test_bad_interval_raise(self):
        hours = {d: [] for d in lp.WEEKDAYS}
        hours["mon"] = [["25:00", "26:00"]]
        with self.assertRaises(ValueError):
            lp._check_hours_shape(json.dumps(hours))

    def test_zero_length_interval_raises(self):
        hours = {d: [] for d in lp.WEEKDAYS}
        hours["mon"] = [["08:00", "08:00"]]
        with self.assertRaises(ValueError):
            lp._check_hours_shape(json.dumps(hours))

    def test_reverse_interval_raises(self):
        hours = {d: [] for d in lp.WEEKDAYS}
        hours["sat"] = [["18:00", "08:00"]]
        with self.assertRaises(ValueError):
            lp._check_hours_shape(json.dumps(hours))


class TestValidateRow(unittest.TestCase):
    def test_clean_row_params(self):
        params, errors = lp.validate_row(good_row(), CENTER_LAT, CENTER_LNG)
        self.assertEqual(errors, [])
        self.assertEqual(params["price_band"], 2)
        self.assertEqual(params["price_band_source"], "google")
        self.assertEqual(params["status"], "operational")
        self.assertEqual(params["rating"], 4.3)
        self.assertEqual(params["review_count"], 210)
        self.assertTrue(params["opening_hours"].startswith("{"))
        self.assertIsNone(params["city_id"])  # filled at write time

    def test_hours_missing_means_null_column(self):
        params, errors = lp.validate_row(good_row(hours_json=""), CENTER_LAT, CENTER_LNG)
        self.assertEqual(errors, [])
        self.assertIsNone(params["opening_hours"])

    def test_invalid_status(self):
        _, errors = lp.validate_row(
            good_row(business_status="permanently_closed"), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("invalid status" in e for e in errors))

    def test_rating_out_of_range(self):
        _, errors = lp.validate_row(good_row(rating="9"), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("rating" in e for e in errors))

    def test_lat_out_of_range(self):
        _, errors = lp.validate_row(good_row(lat="350", lng="93.94"), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("out of range" in e for e in errors))

    def test_nonnumeric_coords(self):
        _, errors = lp.validate_row(good_row(lat="oon", lng=""), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("lat/lng" in e for e in errors))

    def test_beyond_15km_window(self):
        far = good_row(lat=str(CENTER_LAT + 0.20), lng=str(CENTER_LNG + 0.20))
        _, errors = lp.validate_row(far, CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("15 km" in e for e in errors))

    def test_empty_name(self):
        _, errors = lp.validate_row(good_row(name="   "), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("name" in e for e in errors))

    def test_bad_price_band(self):
        _, errors = lp.validate_row(good_row(suggested_price_band="5"), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("price_band" in e for e in errors))

    def test_negative_review_count(self):
        _, errors = lp.validate_row(good_row(review_count="-3"), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("review_count" in e for e in errors))

    def test_empty_pid(self):
        _, errors = lp.validate_row(good_row(google_place_id=""), CENTER_LAT, CENTER_LNG)
        self.assertTrue(any("google_place_id" in e for e in errors))


class TestExportDecisions(unittest.TestCase):
    def test_writes_id_decision_notes_only(self):
        rows = [
            {"google_place_id": "PID_A", "decision": "include", "name": "Cafe A",
             "rating": "4.5", "lat": "24.8", "notes": "owner pick"},
            {"google_place_id": "PID_B", "decision": "review", "name": "Cafe B"},
            {"google_place_id": "PID_C", "decision": "exclude", "name": "Bakery C"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decisions.csv")
            lp.export_decisions(rows, path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(3), b"\xef\xbb\xbf")  # UTF-8 BOM
                import csv
                with open(path, encoding="utf-8-sig", newline="") as fh2:
                    reader = csv.DictReader(fh2)
                    self.assertEqual(reader.fieldnames, ["id", "decision", "notes"])
                    data = list(reader)
        self.assertEqual([r["id"] for r in data], ["PID_A", "PID_B", "PID_C"])
        self.assertEqual([r["decision"] for r in data], ["include", "review", "exclude"])
        self.assertEqual(data[0]["notes"], "owner pick")
        # no Google field content leaked beyond the decision columns
        for record in data:
            for value in record.values():
                self.assertNotIn("Cafe A", str(value) or "")


class TestLoaderDb(unittest.TestCase):
    """Integration — DEV DB, everything inside transactions that are ROLLED BACK."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect(get_connection_string())  # autocommit, read-only use

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def txn_context(self) -> tuple:
        """Non-autocommit connection whose writes are rolled back no matter what."""
        conn = psycopg.connect(get_connection_string())
        self.addCleanup(conn.close)
        self.addCleanup(conn.rollback)
        return conn

    def _city_id(self, cur) -> int:
        cur.execute("select id from public.cities where name = 'Imphal'")
        return cur.fetchone()[0]

    def _seed(self, cur, city_id: int, pid: str, *, band, band_source,
              curated=None) -> None:
        cur.execute(
            """
            insert into public.cafes
                (city_id, name, location, status, source, google_place_id,
                 price_band, price_band_source, curated_score)
            values (%s, %s,
                    st_setsrid(st_makepoint(%s, %s), 4326)::extensions.geography,
                    'operational', 'google', %s, %s, %s, %s)
            """,
            (city_id, f"Old Cafe {pid[-2:]}", CENTER_LNG + 0.02,
             CENTER_LAT + 0.02, pid, band, band_source, curated),
        )

    def _fetch_row(self, cur, pid: str) -> dict:
        cur.execute(
            """
            select name, price_band, price_band_source, curated_score,
                   st_x(location::extensions.geometry) as lng,
                   st_y(location::extensions.geometry) as lat,
                   google_data_fetched_at, last_verified, status
            from public.cafes where google_place_id = %s
            """,
            (pid,),
        )
        cols = ["name", "price_band", "price_band_source", "curated_score",
                "lng", "lat", "fetched_at", "last_verified", "status"]
        return dict(zip(cols, cur.fetchone()))

    def test_01_migration_columns_present(self):
        """Precondition: the 20261107000000 migration has been applied."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                select column_name from information_schema.columns
                where table_schema = 'public' and table_name = 'cafes'
                  and column_name in ('google_data_fetched_at', 'price_band_source')
                order by column_name
                """
            )
            cols = [c[0] for c in cur.fetchall()]
        finally:
            cur.close()
        self.assertEqual(cols, ["google_data_fetched_at", "price_band_source"])

    def test_02_insert_update_counts_and_curation_protection(self):
        conn = self.txn_context()
        cur = conn.cursor()
        city_id = self._city_id(cur)
        # Pre-existing rows (this txn only): manual band + curated score.
        self._seed(cur, city_id, "TESTPID_LOAD_A", band=2, band_source="manual", curated=4.2)
        self._seed(cur, city_id, "TESTPID_LOAD_B", band=2, band_source="google")
        rows = [
            params_for(good_row(google_place_id="TESTPID_LOAD_A",
                                suggested_price_band="3")),
            params_for(good_row(google_place_id="TESTPID_LOAD_B",
                                suggested_price_band="3")),
            params_for(good_row(google_place_id="TESTPID_LOAD_C")),
        ]
        result = lp.load_rows(cur, rows, city_id, BATCH_TS)
        # 2 pre-existing -> updates; 1 new -> inserts
        self.assertEqual(result["counts"], {"inserted": 1, "updated": 2})
        a = self._fetch_row(cur, "TESTPID_LOAD_A")
        self.assertEqual(a["price_band"], 2)              # manual band survives
        self.assertEqual(a["price_band_source"], "manual")
        self.assertEqual(float(a["curated_score"]), 4.2)  # curated untouched
        self.assertEqual(a["fetched_at"], BATCH_TS)       # batch ts, not now()
        b = self._fetch_row(cur, "TESTPID_LOAD_B")
        self.assertEqual(b["price_band"], 3)              # google band refreshed
        self.assertEqual(b["price_band_source"], "google")
        c = self._fetch_row(cur, "TESTPID_LOAD_C")
        # lng FIRST in st_makepoint: st_x == lng, st_y == lat
        self.assertAlmostEqual(float(c["lng"]), CENTER_LNG + 0.01, places=5)
        self.assertAlmostEqual(float(c["lat"]), CENTER_LAT + 0.01, places=5)
        # last_verified was NOT set from the batch (insert default = now())
        self.assertGreater(c["last_verified"], BATCH_TS)
        cur.close()

    def test_03_second_run_is_all_updates(self):
        conn = self.txn_context()
        cur = conn.cursor()
        city_id = self._city_id(cur)
        rows = [params_for(good_row(google_place_id="TESTPID_LOAD_D"))]
        first = lp.load_rows(cur, rows, city_id, BATCH_TS)
        self.assertEqual(first["counts"]["inserted"], 1)
        second = lp.load_rows(cur, rows, city_id, BATCH_TS)
        self.assertEqual(second["counts"], {"inserted": 0, "updated": 1})
        cur.close()

    def test_04_google_refresh_can_drop_band_manual_survives_null(self):
        conn = self.txn_context()
        cur = conn.cursor()
        city_id = self._city_id(cur)
        self._seed(cur, city_id, "TESTPID_LOAD_E", band=2, band_source="manual")
        self._seed(cur, city_id, "TESTPID_LOAD_F", band=2, band_source="google")
        # refresh brings NO band for both rows
        rows_e = [params_for(good_row(google_place_id="TESTPID_LOAD_E",
                                      suggested_price_band=""))]
        rows_f = [params_for(good_row(google_place_id="TESTPID_LOAD_F",
                                      suggested_price_band=""))]
        lp.load_rows(cur, rows_e, city_id, BATCH_TS)
        lp.load_rows(cur, rows_f, city_id, BATCH_TS)
        e = self._fetch_row(cur, "TESTPID_LOAD_E")
        f = self._fetch_row(cur, "TESTPID_LOAD_F")
        self.assertEqual((e["price_band"], e["price_band_source"]),
                         (2, "manual"))   # never overwritten
        # google-sourced band replaced by google's (missing) truth
        self.assertIsNone(f["price_band"])
        self.assertIsNone(f["price_band_source"])
        cur.close()

    def test_05_null_existing_band_gets_filled(self):
        conn = self.txn_context()
        cur = conn.cursor()
        city_id = self._city_id(cur)
        self._seed(cur, city_id, "TESTPID_LOAD_G", band=None, band_source=None)
        rows = [params_for(good_row(google_place_id="TESTPID_LOAD_G",
                                    suggested_price_band="1"))]
        lp.load_rows(cur, rows, city_id, BATCH_TS)
        g = self._fetch_row(cur, "TESTPID_LOAD_G")
        self.assertEqual((g["price_band"], g["price_band_source"]), (1, "google"))
        cur.close()


if __name__ == "__main__":
    unittest.main()
