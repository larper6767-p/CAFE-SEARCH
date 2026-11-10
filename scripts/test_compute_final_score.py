"""Tests for scripts/compute_final_score.py (phase 2, step 5).

Pure unit tests for the math/branch logic + config parsing. DB integration
tests run against the DEV database only, inside transactions that are ROLLED
BACK -- nothing persists.
"""
from __future__ import annotations

import os
import sys
import unittest

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compute_final_score as cfs  # noqa: E402
from db import get_connection_string  # noqa: E402

GUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "config", "scoring.toml")


def row(**over) -> dict:
    base = {"id": 1, "name": "Cafe X", "status": "operational", "source": "google",
            "google_rating": 4.5, "google_review_count": 90,
            "curated_score": None, "has_location": True}
    base.update(over)
    return base


class TestWrAndBranches(unittest.TestCase):
    def test_wr_at_v_equals_m_is_fifty_fifty(self):
        # v = m = 15: (15*4.5 + 15*C) / 30 = (4.5 + C) / 2
        self.assertEqual(cfs.wr(row(google_review_count=15), 4.2, 15.0),
                         (4.5 + 4.2) / 2)

    def test_wr_big_v_keeps_own_signal(self):
        # v = 871, m = 15: own rating carries ~98%
        val = cfs.wr(row(google_review_count=871), 4.2, 15.0)
        expected = (871 * 4.5 + 15 * 4.2) / (871 + 15)
        self.assertAlmostEqual(val, expected)

    def test_wr_none_without_rating_or_reviews(self):
        self.assertIsNone(cfs.wr(row(google_rating=None), 4.2, 15))
        self.assertIsNone(cfs.wr(row(google_review_count=0), 4.2, 15))

    def test_branch_blended(self):
        final, branch = cfs.score_row(row(curated_score=4.0), 4.2, 15.0, 0.6)
        self.assertEqual(branch, "blended")
        self.assertEqual(final, round(0.6 * cfs.wr(row(), 4.2, 15.0) + 0.4 * 4.0, 2))

    def test_branch_google_only(self):
        # renormalized when curated missing: final = WR directly (no 0.6 factor)
        # v = m = 15 -> WR 50/50 = (4.5 + 4.2)/2 = 4.35
        final, branch = cfs.score_row(row(google_review_count=15), 4.2, 15.0, 0.6)
        self.assertEqual((final, branch), (4.35, "google only"))

    def test_branch_curated_only(self):
        # no rating (manual row) -> curated counts alone, not multiplied by 0.4
        final, branch = cfs.score_row(
            row(google_rating=None, curated_score=4.0), 4.2, 15.0, 0.6)
        self.assertEqual((final, branch), (4.0, "curated only"))

    def test_branch_null(self):
        final, branch = cfs.score_row(
            row(google_rating=None, curated_score=None), 4.2, 15.0, 0.6)
        self.assertIsNone(final)
        self.assertEqual(branch, "null")

    def test_branch_curated_only_missing_rating_with_curated(self):
        final, branch = cfs.score_row(row(google_rating=None, curated_score=3.2),
                                      4.2, 15.0, 0.6)
        self.assertEqual((final, branch), (3.2, "curated only"))

    def test_purged_stub_skipped(self):
        final, branch = cfs.score_row(row(), 4.2, 15.0, 0.6, stub=True)
        self.assertEqual((final, branch), (None, "stub"))
        # derived path: missing location OR unknown status -> stub
        _, b1 = cfs.score_row(row(has_location=False), 4.2, 15.0, 0.6)
        _, b2 = cfs.score_row(row(status="unknown"), 4.2, 15.0, 0.6)
        self.assertEqual((b1, b2), ("stub", "stub"))


class TestComputeC(unittest.TestCase):
    def test_excludes_closed_temporarily(self):
        rows = [row(google_rating=4.0), row(google_rating=5.0),
                row(google_rating=4.8, status="closed_temporarily"),
                row(google_rating=None)]
        c, n = cfs.compute_c(rows, exclude_closed=True)
        self.assertEqual((c, n), (4.5, 2))

    def test_includes_closed_when_flag_off(self):
        rows = [row(google_rating=4.0), row(google_rating=5.0),
                row(google_rating=4.8, status="closed_temporarily")]
        c, n = cfs.compute_c(rows, exclude_closed=False)
        self.assertEqual((c, n), (round((4.0 + 5.0 + 4.8) / 3, 3), 3))

    def test_no_rated_rows(self):
        self.assertEqual(cfs.compute_c([row(google_rating=None)], True),
                         (None, 0))


class TestConfig(unittest.TestCase):
    def test_shipped_config_valid(self):
        cfg = cfs.load_config(GUT_PATH)
        self.assertEqual(cfg["m"], 15.0)
        self.assertEqual(cfg["w"], 0.6)
        self.assertEqual(cfg["c_mode"], "computed")
        self.assertTrue(cfg["exclude_closed_from_c"])

    def test_bad_w_rejected(self):
        path = self._write("w = 1.5\nm = 15.0\n")
        with self.assertRaises(ValueError):
            cfs.load_config(path)

    def test_override_mode_needs_c_override(self):
        path = self._write('m = 15.0\nw = 0.6\nc_mode = "override"\n')
        with self.assertRaises(ValueError):
            cfs.load_config(path)
        path = self._write('m = 15.0\nw = 0.6\nc_mode = "override"\n'
                           "c_override = 4.27\n")
        cfg = cfs.load_config(path)
        self.assertEqual(cfg["c_override"], 4.27)

    @staticmethod
    def _write(text: str) -> str:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("[ranking]\n" + text)
        return path


class TestExplain(unittest.TestCase):
    def test_explain_lines_contain_components(self):
        lines = cfs.explain_lines(row(), 4.271, 15.0, 0.6)
        blob = "\n".join(lines)
        self.assertIn("WR = (v*R + m*C)", blob)
        self.assertIn("C = 4.271", blob)
        self.assertIn("m = 15.0", blob)
        self.assertIn("w = 0.6", blob)
        self.assertIn("branch = google only", blob)


class TestScoringDb(unittest.TestCase):
    """DEV DB, always rolled back."""

    def setUp(self) -> None:
        # rollback FIRST in teardown, then close (psycopg cleanup order trap)
        self.conn = psycopg.connect(get_connection_string())

        def _teardown() -> None:
            try:
                self.conn.rollback()
            finally:
                self.conn.close()

        self.addCleanup(_teardown)
        self.cur = self.conn.cursor()
        self.cur.execute("select id from public.cities where name = 'Imphal'")
        self.city_id = self.cur.fetchone()[0]

    def _seed(self, name: str, *, rating=None, reviews=None, curated=None,
              status="operational", location=True, source="google") -> dict:
        self.cur.execute(
            """
            insert into public.cafes (city_id, name, location, status, source,
                                      google_place_id, google_rating,
                                      google_review_count, curated_score)
            values (%s, %s,
                    case when %s then
                        st_setsrid(st_makepoint(93.94, 24.807), 4326)
                        ::extensions.geography
                    else null end,
                    %s, %s,
                    case when %s then 'TESTSCORE_' || %s else null end,
                    %s, %s, %s)
            returning id
            """,
            (self.city_id, name, location, status, source, source == "google",
             name, rating, reviews, curated),
        )
        cafe_id = self.cur.fetchone()[0]
        return {"id": cafe_id, "name": name, "status": status, "source": source,
                "google_rating": rating and float(rating),
                "google_review_count": reviews, "curated_score":
                    curated and float(curated), "has_location": location}

    def test_full_pipeline_in_transaction(self):
        seeded = [
            self._seed("Blend Cafe", rating=4.5, reviews=105, curated=3.5),
            self._seed("Google Only Cafe", rating=4.8, reviews=340),
            self._seed("Curated Only Cafe", rating=None, curated=4.2,
                       source="manual"),
            self._seed("Null Cafe", rating=None, source="manual"),
            self._seed("Closed Cafe", rating=5.0, reviews=30,
                       status="closed_temporarily"),
            self._seed("Stub Cafe", rating=4.0, reviews=10, location=False),
        ]
        c, n = cfs.compute_c(seeded, exclude_closed=True)
        # rated pool excludes Closed Cafe (flag ON); the not-yet-purged stub row
        # still carries a rating, so it counts (owner spec: only the
        # closed_temporarily exclusion) -> (4.5 + 4.8 + 4.0) / 3
        self.assertEqual((c, n), (4.433, 3))
        results = [cfs.score_row(r, c, 15.0, 0.6) for r in seeded]
        fm = dict(zip((r["name"] for r in seeded), results))
        self.assertEqual(fm["Blend Cafe"][1], "blended")
        self.assertEqual(fm["Google Only Cafe"][1], "google only")
        self.assertEqual(fm["Curated Only Cafe"][1], "curated only")
        self.assertEqual(fm["Null Cafe"], (None, "null"))
        self.assertEqual(fm["Stub Cafe"], (None, "stub"))
        # write path (in-transaction): same UPDATE the --commit run performs
        for r, (final, _) in zip(seeded, results):
            self.cur.execute("update public.cafes set final_score = %s "
                             "where id = %s", (final, r["id"]))
        self.cur.execute(
            "select count(*) from public.cafes where final_score is null "
            "and id = any(%s)",
            ([r["id"] for r in seeded],),
        )
        self.assertEqual(self.cur.fetchone()[0], 2)  # Stub + Null


if __name__ == "__main__":
    unittest.main()
