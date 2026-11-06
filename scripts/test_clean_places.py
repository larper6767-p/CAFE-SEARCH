"""Unit tests for step-5 cleaning rules and the loader gate (stdlib unittest).

Run: .\\.venv\\Scripts\\python.exe scripts\\test_clean_places.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clean_places as cp  # noqa: E402
import csv_gate  # noqa: E402


def place(name="Kahva", primary="cafe", types=None, status="OPERATIONAL",
          price=None, hours=None, lat=24.8078, lng=93.9392):
    return {
        "id": "pid_" + name.lower().replace(" ", "_"),
        "displayName": {"text": name},
        "primaryType": primary,
        "types": types if types is not None else ([primary] if primary else []),
        "businessStatus": status,
        "priceLevel": price,
        "regularOpeningHours": hours,
        "location": {"latitude": lat, "longitude": lng}
        if lat is not None else None,
    }


class TextNormalizationTests(unittest.TestCase):
    def test_collapse_and_trim(self):
        self.assertEqual(cp.normalize_text("  Kahva   Espresso  "), "Kahva Espresso")

    def test_nbsp(self):
        self.assertEqual(cp.normalize_text("Café\u00a0Kahva"), "Café Kahva")

    def test_none(self):
        self.assertEqual(cp.normalize_text(None), "")

    def test_nfc_composition(self):
        self.assertEqual(cp.normalize_text("Kaf\u0065\u0301"), "Kafé")


class KeywordTests(unittest.TestCase):
    def test_matches_from_approved_list(self):
        for name in ("Kahva Coffee", "STEAM tea room", "1985 RETRO fountain BISTRO",
                     "Café 175°", "Bakehouse 9"):
            self.assertTrue(cp.name_has_keyword(name), name)

    def test_non_matches(self):
        for name in ("NANDALAL STORE", "Kukubi Studio", "Family Dhaba", "Hotel Blue"):
            self.assertFalse(cp.name_has_keyword(name), name)


class ClassificationTests(unittest.TestCase):
    CITY_R = 12.0  # km, mirrors the cities row used in tests

    def test_auto_types_include_within_radius(self):
        for primary in ("cafe", "coffee_shop", "tea_house"):
            d, r = cp.classify_place(place(primary=primary), 1.0, self.CITY_R)
            self.assertEqual((d, r), ("include", "type_include"), primary)

    def test_auto_types_outside_radius_review(self):
        d, r = cp.classify_place(place(primary="cafe"), 12.47, self.CITY_R)
        self.assertEqual((d, r), ("review", "outside_city_radius_km(12.47)"))

    def test_beyond_15km_excluded(self):
        d, r = cp.classify_place(place(primary="cafe"), 15.01, self.CITY_R)
        self.assertEqual((d, r), ("exclude", "outside_city_radius_km(15.01)"))

    def test_bakery_default_exclude(self):
        p = place(name="Likla Bakery", primary="bakery")
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("exclude", "bakery_default_exclude"))

    def test_cake_shop_default_exclude(self):
        p = place(name="Nandalal Store", primary="cake_shop")
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("exclude", "bakery_default_exclude"))

    def test_bakery_review_via_cafe_family_in_types(self):
        p = place(name="Likla Bakery", primary="bakery", types=["bakery", "cafe"], price=None)
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("review", "bakery_cafe_signal"))

    def test_bakery_review_via_keyword_name(self):
        p = place(name="Bake Me @ Coffee Corner", primary="bakery")
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("review", "bakery_cafe_signal"))

    def test_restaurant_with_cafe_name_reviews(self):
        p = place(name="Café Kohli Restaurant", primary="restaurant", types=["restaurant"])
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("review", "cafe_like_name"))

    def test_plain_restaurant_excluded(self):
        p = place(name="Family Dhaba", primary="restaurant")
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("exclude", "type_exclude"))

    def test_noise_types_excluded_unless_named(self):
        for name, primary in (("NANDALAL STORE", "store"), ("Kukubi Studio", "educational_institution"),
                              ("Hotel Blue", "hotel"), ("Gift Corner", "gift_shop")):
            self.assertEqual(cp.classify_place(place(name=name, primary=primary), 1.0, self.CITY_R),
                             ("exclude", "type_exclude"), name)
        self.assertEqual(cp.classify_place(place(name="Roastery Studio", primary="store"), 1.0, self.CITY_R),
                         ("review", "cafe_like_name"))

    def test_no_primarytype_falls_back_to_types(self):
        p = place(name="Kahva", primary=None, types=["coffee_shop"])
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("include", "type_include"))
        p2 = place(name="NANDALAL", primary=None, types=["store"])
        self.assertEqual(cp.classify_place(p2, 1.0, self.CITY_R), ("exclude", "type_exclude"))

    def test_closed_permanently_wins(self):
        p = place(name="Kahva", status="CLOSED_PERMANENTLY")
        self.assertEqual(cp.classify_place(p, 1.0, self.CITY_R), ("exclude", "closed_permanently"))


class PriceBandTests(unittest.TestCase):
    def test_full_mapping(self):
        self.assertEqual(cp.price_band("PRICE_LEVEL_INEXPENSIVE"), 1)
        self.assertEqual(cp.price_band("price_level_inexpensive"), 1)
        self.assertEqual(cp.price_band("PRICE_LEVEL_MODERATE"), 2)
        self.assertEqual(cp.price_band("PRICE_LEVEL_EXPENSIVE"), 3)
        self.assertEqual(cp.price_band("PRICE_LEVEL_VERY_EXPENSIVE"), 3)
        self.assertEqual(cp.price_band("PRICE_LEVEL_FREE"), 1)

    def test_missing_is_none(self):
        self.assertIsNone(cp.price_band(None))
        self.assertIsNone(cp.price_band(""))
        self.assertIsNone(cp.price_band("PRICE_LEVEL_UNSPECIFIED"))


class HoursTests(unittest.TestCase):
    def test_missing(self):
        self.assertIsNone(cp.normalize_hours(None))
        self.assertIsNone(cp.normalize_hours({"periods": []}))

    def test_simple_day(self):
        hours = {"periods": [{"open": {"day": 1, "time": "0700"},
                              "close": {"day": 1, "time": "2100"}}]}
        self.assertEqual(cp.normalize_hours(hours),
                         {"mon": [["07:00", "21:00"]], "tue": [], "wed": [], "thu": [],
                          "fri": [], "sat": [], "sun": []})

    def test_two_intervals_one_day_sorted(self):
        hours = {"periods": [
            {"open": {"day": 3, "time": "1600"}, "close": {"day": 3, "time": "2100"}},
            {"open": {"day": 3, "time": "0700"}, "close": {"day": 3, "time": "1200"}},
        ]}
        out = cp.normalize_hours(hours)
        self.assertEqual(out["wed"], [["07:00", "12:00"], ["16:00", "21:00"]])

    def test_24_7_open_without_close(self):
        hours = {"periods": [{"open": {"day": 0, "time": "0000"}}]}
        out = cp.normalize_hours(hours)
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            self.assertEqual(out[d], [["00:00", "24:00"]], d)

    def test_sat_to_sunday_wrap(self):
        hours = {"periods": [{"open": {"day": 6, "time": "2200"},
                              "close": {"day": 0, "time": "0200"}}]}
        out = cp.normalize_hours(hours)
        self.assertEqual(out["sat"], [["22:00", "24:00"]])
        self.assertEqual(out["sun"], [["00:00", "02:00"]])

    def test_partial_days_are_closed(self):
        hours = {"periods": [{"open": {"day": 2, "time": "0900"},
                              "close": {"day": 2, "time": "1300"}}]}
        out = cp.normalize_hours(hours)
        self.assertEqual(out["tue"], [["09:00", "13:00"]])
        for d in ("mon", "wed", "thu", "fri", "sat", "sun"):
            self.assertEqual(out[d], [], d)

    def test_24_hour_single_day(self):
        hours = {"periods": [{"open": {"day": 5, "time": "0000"},
                              "close": {"day": 5, "time": "0000"}}]}
        self.assertEqual(cp.normalize_hours(hours)["fri"], [["00:00", "24:00"]])

    def test_2400_close_is_boundary(self):
        hours = {"periods": [{"open": {"day": 1, "time": "0900"},
                              "close": {"day": 1, "time": "2400"}}]}
        self.assertEqual(cp.normalize_hours(hours)["mon"], [["09:00", "24:00"]])


class WhitespaceInRowsTests(unittest.TestCase):
    def test_name_and_address_collapsed(self):
        p = place(name="Kahva   Espresso\nCafe\u00a0Two")
        p["formattedAddress"] = "  Uripok\t Road   , Imphal "
        rows = cp.build_rows({"lat": 24.8078, "lng": 93.9392, "radius_m": 12000.0},
                             {p["id"]: p}, "")
        self.assertEqual(rows[0]["name"], "Kahva Espresso Cafe Two")
        self.assertEqual(rows[0]["address"], "Uripok Road , Imphal")


class DedupeFlagTests(unittest.TestCase):
    def test_same_name_close_flagged(self):
        a = place(name="Kahva", lat=24.80780, lng=93.93920)
        b = place(name="kAHVA", lat=24.80785, lng=93.93920)  # ~5 m apart
        a["id"], b["id"] = "A", "B"
        flags = cp.scan_same_name_flags([a, b])
        self.assertEqual(flags, {"A", "B"})

    def test_same_name_far_not_flagged(self):
        a = place(name="Kahva", lat=24.8078, lng=93.9392)
        b = place(name="Kahva", lat=24.1555, lng=93.9392)  # far
        a["id"], b["id"] = "A", "B"
        self.assertEqual(cp.scan_same_name_flags([a, b]), set())

    def test_different_names_not_flagged(self):
        a = place(name="Kahva", lat=24.80780, lng=93.93920)
        b = place(name="Amber", lat=24.80781, lng=93.93920)
        a["id"], b["id"] = "A", "B"
        self.assertEqual(cp.scan_same_name_flags([a, b]), set())


class CsvWriterTests(unittest.TestCase):
    def _rows(self):
        p1 = place(name="Kahva")
        p2 = place(name="Amber cafe", primary="coffee_shop", price="PRICE_LEVEL_MODERATE")
        p2["id"] = "B"
        rows = cp.build_rows({"lat": 24.8078, "lng": 93.9392, "radius_m": 12000.0},
                             {p1["id"]: p1, p2["id"]: p2}, "")
        return rows

    def test_write_csv_idempotent_bytes(self):
        rows = self._rows()
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = os.path.join(tmp, "a.csv"), os.path.join(tmp, "b.csv")
            cp.write_csv(rows, p1)
            cp.write_csv(rows, p2)
            with open(p1, "rb") as f1, open(p2, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())

    def test_csv_has_bom_and_sorted_by_review_count(self):
        import csv as csvmod
        rows = self._rows()
        rows[0]["review_count"] = "50"
        rows[1]["review_count"] = "3"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.csv")
            cp.write_csv(rows, path)
            with open(path, "rb") as fh:
                self.assertTrue(fh.read(3) == b"\xef\xbb\xbf")
            back = cp.sort_key(rows[0]), cp.sort_key(rows[1])
            self.assertLess(back[0], back[1])  # 50 first


class CsvGateTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"google_place_id": "A", "decision": "include"},
            {"google_place_id": "B", "decision": "exclude"},
            {"google_place_id": "C", "decision": "review"},
        ]

    def test_gate_refuses_non_include_rows(self):
        kept = csv_gate.filter_include_rows(self.rows)
        self.assertEqual([r["google_place_id"] for r in kept], ["A"])

    def test_structure_detects_drift(self):
        # duplicate id
        errs = csv_gate.validate_structure(self.rows + [{"google_place_id": "A", "decision": "include"}],
                                           ["A", "B", "C"])
        self.assertTrue(any("duplicate" in e for e in errs))
        self.assertTrue(any("row count" in e for e in errs))
        # missing id
        errs = csv_gate.validate_structure(self.rows[:2], ["A", "B", "C"])
        self.assertTrue(any("missing" in e for e in errs))
        # extra id
        errs = csv_gate.validate_structure(self.rows, ["A", "B"])
        self.assertTrue(any("not in the fetched set" in e for e in errs))
        # empty id
        errs = csv_gate.validate_structure([{"google_place_id": "", "decision": "include"}], ["A"])
        self.assertTrue(any("empty place-id" in e for e in errs))

    def test_structure_detects_invalid_decision(self):
        errs = csv_gate.validate_structure([{"google_place_id": "A", "decision": "INCLUDE"}], ["A"])
        self.assertTrue(any("invalid decision" in e for e in errs))

    def test_valid_structure_passes(self):
        errs = csv_gate.validate_structure(self.rows, ["A", "B", "C"])
        self.assertEqual(errs, [])

    def test_read_approved_csv_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.csv")
            cp.write_csv([
                {"google_place_id": "A", "decision": "include", "reason": "type_include",
                 "name": "Kahva", "address": "", "primary_type": "cafe", "types": "cafe",
                 "business_status": "operational", "price_level": "", "suggested_price_band": "",
                 "rating": "", "review_count": "", "hours_present": "false", "hours_json": "",
                 "lat": "", "lng": "", "city_distance_km": "0.00"},
            ], path)
            rows = csv_gate.read_approved_csv(path)
            self.assertEqual(rows[0]["google_place_id"], "A")


if __name__ == "__main__":
    unittest.main()
