"""Tests for scripts/import_manual.py (step 7).

Unit tests are pure. DB integration tests run against the DEV database only,
inside transactions that are ROLLED BACK — nothing persists (owner rule).
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from datetime import date

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_manual as im  # noqa: E402
from db import connect, get_connection_string  # noqa: E402

CENTER_LAT, CENTER_LNG = 24.807779351178766, 93.93915469958242
KNOWN_TAGS = frozenset({"wifi", "outlets", "outdoor_seating", "quiet",
                        "group_friendly", "late_night", "vegetarian_friendly",
                        "pet_friendly", "specialty_coffee", "desserts_bakery"})


def base_row(**over) -> dict:
    row = {
        "name": "Fresh Brew House",
        "address": "Fake Road, Imphal",
        "lat": str(CENTER_LAT + 0.001),
        "lng": str(CENTER_LNG + 0.001),
        "hours": "mon-fri 09:00-18:00; sat 09:00-15:00; sun closed",
        "price_band": "2",
        "tags": "wifi;quiet",
        "phone": "+91 98560 99999",
        "website": "https://freshbrew.example.in",
        "instagram_url": "",
        "notes": "",
        "last_verified": "2026-11-09",
        "_line": 2,
    }
    row.update(over)
    return row


def validated(**over):
    row = base_row(**over)
    return im.validate_manual_row(row, CENTER_LAT, CENTER_LNG, KNOWN_TAGS, False)


class TestParseHoursLight(unittest.TestCase):
    def test_ranges_and_closed(self):
        hours = im.parse_hours_light("mon-fri 09:00-18:00; sat 09:00-15:00; sun closed")
        self.assertEqual(hours["mon"], [["09:00", "18:00"]])
        self.assertEqual(hours["fri"], [["09:00", "18:00"]])
        self.assertEqual(hours["sat"], [["09:00", "15:00"]])
        self.assertEqual(hours["sun"], [])

    def test_missing_days_are_closed(self):
        hours = im.parse_hours_light("mon 08:00-10:00")
        self.assertEqual(hours["mon"], [["08:00", "10:00"]])
        self.assertEqual(hours["thu"], [])

    def test_multi_intervals(self):
        hours = im.parse_hours_light("sat 08:00-12:00, 13:00-19:00")
        self.assertEqual(hours["sat"], [["08:00", "12:00"], ["13:00", "19:00"]])

    def test_single_digit_hours_padded(self):
        hours = im.parse_hours_light("mon 9:00-10:30")
        self.assertEqual(hours["mon"], [["09:00", "10:30"]])

    def test_empty_is_none(self):
        self.assertIsNone(im.parse_hours_light(""))
        self.assertIsNone(im.parse_hours_light(None))

    def test_unknown_day(self):
        with self.assertRaises(ValueError):
            im.parse_hours_light("mondey 09:00-10:00")

    def test_reversed_interval(self):
        with self.assertRaises(ValueError):
            im.parse_hours_light("mon 10:00-09:00")

    def test_overnight_rejected(self):
        with self.assertRaises(ValueError):
            im.parse_hours_light("sat 18:00-02:00")

    def test_wrapping_day_range_rejected(self):
        with self.assertRaises(ValueError):
            im.parse_hours_light("fri-mon 10:00-11:00")

    def test_missing_interval(self):
        with self.assertRaises(ValueError):
            im.parse_hours_light("mon 08:00")


class TestValidateManualRow(unittest.TestCase):
    def test_full_row(self):
        params, errors, dist = validated()
        self.assertEqual(errors, [])
        self.assertEqual(params["name"], "Fresh Brew House")
        self.assertEqual(params["price_band"], 2)
        self.assertEqual(params["price_band_source"], "manual")  # owner decision
        self.assertTrue(params["opening_hours"].startswith("{"))
        self.assertEqual(params["last_verified"], date(2026, 11, 9))
        self.assertLess(dist, im.MAX_RADIUS_M)

    def test_center_row_distance_zero(self):
        _, _, dist = validated(lat=str(CENTER_LAT), lng=str(CENTER_LNG))
        self.assertEqual(dist, 0.0)

    def test_beyond_15km_refused(self):
        _, errors, dist = validated(lat=str(CENTER_LAT + 0.20), lng=str(CENTER_LNG + 0.20))
        self.assertGreater(dist, im.MAX_RADIUS_M)
        self.assertTrue(any("15 km" in e for e in errors))

    def test_empty_name(self):
        _, errors, _ = validated(name="   ")
        self.assertTrue(any("name" in e for e in errors))

    def test_bad_band(self):
        _, errors, _ = validated(price_band="7")
        self.assertTrue(any("price_band" in e for e in errors))

    def test_unknown_tag(self):
        _, errors, _ = validated(tags="meow")
        self.assertTrue(any("unknown tag" in e for e in errors))
        _, errors, _ = validated(tags="wifi;meow")
        self.assertTrue(any("meow" in e for e in errors))

    def test_bad_last_verified(self):
        _, errors, _ = validated(last_verified="09-2026-11")
        self.assertTrue(any("last_verified" in e for e in errors))

    def test_notes_and_instagram_require_columns(self):
        _, errors, _ = validated(notes="nice place", instagram_url="https://instagram.com/fake")
        self.assertTrue(any("notes" in e for e in errors))
        self.assertTrue(any("instagram_url" in e for e in errors))

    def test_instagram_url_validated_when_columns_exist(self):
        row = base_row(instagram_url="https://instagram.com/real_handle")
        params, errors, _ = im.validate_manual_row(
            row, CENTER_LAT, CENTER_LNG, KNOWN_TAGS, has_notes_cols=True)
        self.assertEqual(errors, [])
        self.assertEqual(params["instagram_url"], "https://instagram.com/real_handle")

    def test_instagram_url_non_profile_rejected(self):
        row = base_row(instagram_url="https://taplink.example.in/shop")
        _, errors, _ = im.validate_manual_row(
            row, CENTER_LAT, CENTER_LNG, KNOWN_TAGS, has_notes_cols=True)
        self.assertTrue(any("instagram.com profile" in e for e in errors))


class TestCsvReading(unittest.TestCase):
    def _write(self, tmp, name, bom=True, fields=None):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields or im.FIELDS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerow({k: "" for k in (fields or im.FIELDS) if k})
        return path

    def test_missing_bom_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "manual.csv", bom=False)
            with self.assertRaises(im.ImporterError):
                im.read_manual_csv(path)

    def test_header_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "manual.csv", fields=["name", "lat", "lng"])
            with self.assertRaises(im.ImporterError):
                im.read_manual_csv(path)

    def test_template_name_refused(self):
        with self.assertRaises(SystemExit):
            im.ensure_not_template(r"data/manual/imphal_manual_template.csv")
        im.ensure_not_template(r"data/manual/imphal_manual.csv")  # passes


class TestImporterDb(unittest.TestCase):
    """DEV DB, always rolled back."""

    def setUp(self) -> None:
        self.conn = psycopg.connect(get_connection_string())

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

    def _existing(self):
        return im._existing_rows(self.cur)

    def test_intra_file_duplicate_refused(self):
        rows = [base_row(line=2), base_row(line=3)]
        with self.assertRaises(im.ImporterError):
            im.find_intra_file_duplicates(rows)

    def test_distinct_far_rows_not_duplicates(self):
        rows = [base_row(line=2),
                base_row(line=3, lat=str(CENTER_LAT + 0.05), lng=str(CENTER_LNG + 0.05))]
        im.find_intra_file_duplicates(rows)  # must not raise

    def test_insert_and_rerun_skip(self):
        params, errors, dist = validated()
        self.assertEqual(errors, [])
        live_cols = im._columns_present(self.cur, ("notes", "instagram_url"))
        cafe_id = im.insert_manual_row(self.cur, params, self.city_id,
                                       has_notes_cols=live_cols)
        self.cur.execute(
            """
            select source, price_band_source, status, google_place_id,
                   st_x(location::extensions.geometry), st_y(location::extensions.geometry)
            from public.cafes where id = %s
            """,
            (cafe_id,),
        )
        src, band_src, status, gid, x, y = self.cur.fetchone()
        self.assertEqual(src, "manual")
        self.assertEqual(band_src, "manual")
        self.assertEqual(status, "operational")
        self.assertIsNone(gid)
        self.assertAlmostEqual(float(x), CENTER_LNG + 0.001, places=5)  # lng first
        self.assertAlmostEqual(float(y), CENTER_LAT + 0.001, places=5)
        # tags written
        self.cur.execute("select count(*) from public.cafe_tags where cafe_id = %s",
                         (cafe_id,))
        self.assertEqual(self.cur.fetchone()[0], 2)
        # re-import of the same manual row is detected as 'manual' skip
        match, nearby = im.find_match(params, self._existing())
        self.assertEqual(match, "manual")
        self.assertEqual(nearby, [])

    def test_google_near_match_reported_not_merged(self):
        self.cur.execute(
            """
            insert into public.cafes (city_id, name, location, status, source,
                                      google_place_id)
            values (%s, 'Fresh Brew House',
                    st_setsrid(st_makepoint(%s, %s), 4326)::extensions.geography,
                    'operational', 'google', 'TESTIMPORT_GOOGLE_1')
            """,
            (self.city_id, CENTER_LNG + 0.001, CENTER_LAT + 0.001),
        )
        params, errors, _ = validated()
        self.assertEqual(errors, [])
        match, nearby = im.find_match(params, self._existing())
        self.assertIsNone(match)                    # inserted anyway
        self.assertEqual(len(nearby), 1)
        gid, name_db, dist = nearby[0]
        self.assertEqual(gid, "TESTIMPORT_GOOGLE_1")
        self.assertEqual(name_db, "Fresh Brew House")
        self.assertLessEqual(dist, im.SAME_CAFE_M)

    def test_last_verified_written(self):
        params, errors, _ = validated()
        self.assertEqual(params["last_verified"], date(2026, 11, 9))
        cafe_id = im.insert_manual_row(
            self.cur, params, self.city_id,
            has_notes_cols=im._columns_present(self.cur, ("notes", "instagram_url")))
        self.cur.execute("select last_verified from public.cafes where id = %s",
                         (cafe_id,))
        self.assertEqual(self.cur.fetchone()[0].date(), date(2026, 11, 9))

    def test_columns_present_after_migration(self):
        """Migration 20261109100000 (applied 2026-11-09) added both columns."""
        self.assertTrue(im._columns_present(self.cur, ("notes", "instagram_url")))

    def test_notes_and_instagram_stored(self):
        live_cols = im._columns_present(self.cur, ("notes", "instagram_url"))
        row = base_row(instagram_url="https://instagram.com/fresh_brew",
                       notes="Quiet corner, good filter coffee.")
        params, errors, _ = im.validate_manual_row(
            row, CENTER_LAT, CENTER_LNG, KNOWN_TAGS, has_notes_cols=live_cols)
        self.assertEqual(errors, [])
        self.assertEqual(params["instagram_url"], "https://instagram.com/fresh_brew")
        self.assertEqual(params["notes"], "Quiet corner, good filter coffee.")
        cafe_id = im.insert_manual_row(self.cur, params, self.city_id,
                                       has_notes_cols=live_cols)
        self.cur.execute("select instagram_url, notes from public.cafes where id = %s",
                         (cafe_id,))
        insta_db, notes_db = self.cur.fetchone()
        self.assertEqual(insta_db, "https://instagram.com/fresh_brew")
        self.assertEqual(notes_db, "Quiet corner, good filter coffee.")

    def test_last_verified_null_when_absent(self):
        """Empty last_verified cell → stored NULL (only stored when provided)."""
        params, errors, _ = validated(last_verified="")
        self.assertEqual(errors, [])
        self.assertIsNone(params["last_verified"])
        cafe_id = im.insert_manual_row(
            self.cur, params, self.city_id,
            has_notes_cols=im._columns_present(self.cur, ("notes", "instagram_url")))
        self.cur.execute("select last_verified from public.cafes where id = %s",
                         (cafe_id,))
        self.assertIsNone(self.cur.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
