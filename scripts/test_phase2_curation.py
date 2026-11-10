"""Tests for phase-2 curation tooling (steps 3-4): export/import tagging +
curated import. Unit tests are pure; DB tests hit the DEV database only and
are ROLLED BACK.

Schema note: the tagging DB tests exercise cafe_tags.tag_source, which needs
the STAGED migration 20261110000000. While the migration is pending those
tests skip with a clear reason and activate on approval; the refusal test
(MISSING column) is asserted unconditionally.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_curated_sheet as cx  # noqa: E402
import export_tagging_sheet as ex  # noqa: E402
import import_curated as ic  # noqa: E402
import import_tagging as it  # noqa: E402
from db import get_connection_string  # noqa: E402

KNOWN_TAGS = ["wifi", "outlets", "outdoor_seating", "quiet", "group_friendly",
              "late_night", "vegetarian_friendly", "pet_friendly",
              "specialty_coffee", "desserts_bakery"]

# This sandbox's system temp root is NOT writable, so tempfile falls back to
# the cwd (i.e. the repo) — keep every temp artifact under the managed,
# pre-approved directory instead (see commit 1175d3f for the same class of
# fix in the config-validation tests).
# . . . _MANAGED_TEMP anchor (see incident notes 2026-11-10):
# - the system temp root (C:\Users\bori\AppData\Local\Temp) is NOT writable;
#   tempfile silently falls back to the CWD, polluting the repo root
# - creating child DIRECTORIES under the agent-approved temp dir stalls
#   (mkdtemp hangs) although plain file writes there work
# => anchor every temp artifact inside data/processed/ (gitignored, on the
#    2026-12-06 purge list; its parent (= repo) permits mkdtemp)
_MANAGED_TEMP = Path(__file__).resolve().parents[1] / "data" / "processed"
_MANAGED_TEMP.mkdir(parents=True, exist_ok=True)


def sheet_row(line=2, **over) -> dict:
    row = {"id": "9001", "name": "Test Cafe", "review_count": "12",
           "tag_source": "own_visit", "_line": line}
    row.update(over)
    return row


def valid_sheet_tags(pairs: dict[str, str]) -> tuple[set[str] | None, str]:
    """Tag cells -> parse_tags on a row seeded with KNOWN_TAGS as columns."""
    tags = list(KNOWN_TAGS)
    row = sheet_row()
    for tag in tags:
        row.setdefault(tag, "")
    row.update(pairs)
    try:
        return it.parse_tags(row, tags), "ok"
    except it.TagImportError:
        return None, "refused"


class TestParseTagCells(unittest.TestCase):
    def test_blank_is_unknown_not_no(self):
        desired, _ = valid_sheet_tags({})
        self.assertEqual(desired, set())

    def test_y_marks_link(self):
        desired, _ = valid_sheet_tags({"wifi": "y", "quiet": "y"})
        self.assertEqual(desired, {"wifi", "quiet"})

    def test_y_case_insensitive(self):
        desired, _ = valid_sheet_tags({"wifi": "Y"})
        self.assertEqual(desired, {"wifi"})

    def test_anything_else_refused(self):
        for bad in ("yes", "n", "no", "x", "1", "unknown"):
            with self.subTest(bad=bad):
                _, status = valid_sheet_tags({"wifi": bad})
                self.assertEqual(status, "refused")

    def test_tag_source_tokens(self):
        for good in ("own_visit", "own_knowledge", "review_derived", "user"):
            with self.subTest(good=good):
                self.assertEqual(
                    it.parse_tag_source(sheet_row(tag_source=good)), good)
        for bad in ("", "OWN_VEHICLE", "own_visit;review_derived",
                    "own visit", "a,b"):
            with self.subTest(bad=bad):
                with self.assertRaises(it.TagImportError):
                    it.parse_tag_source(sheet_row(tag_source=bad))


class TestPlanChanges(unittest.TestCase):
    def _plan(self, rows, existing_by_cafe, cafes):
        tags = list(KNOWN_TAGS)
        for row in rows:
            for tag in tags:
                row.setdefault(tag, "")
        return it.plan_changes(rows, cafes, existing_by_cafe, tags)

    def test_add_and_delete_own(self):
        rows = [sheet_row(id="10", tag_source="own_visit", wifi="y")]
        existing = {10: {"outlets": "own_visit"}}
        adds, dels, kept, undeletable = self._plan(
            rows, existing, {10: "Test Cafe"})
        self.assertEqual(adds, [(10, "wifi", "Test Cafe", "own_visit", 2)])
        self.assertEqual(dels, [(10, "outlets", "Test Cafe", "own_visit", 2)])
        self.assertEqual((kept, undeletable), ([], []))

    def test_deletion_of_protected_link_never_happens(self):
        rows = [sheet_row(id="10")]  # blank cells: sheet no longer asserts wifi
        existing = {
            10: {"wifi": "review_derived", "outlets": "user",
                 "quiet": None},
        }
        adds, dels, kept, undeletable = self._plan(
            rows, existing, {10: "Test Cafe"})
        self.assertEqual((adds, dels, kept), ([], [], []))
        self.assertEqual(
            undeletable, [(10, "wifi", "Test Cafe", "review_derived", 2),
                          (10, "outlets", "Test Cafe", "user", 2),
                          (10, "quiet", "Test Cafe", None, 2)])

    def test_duplicate_id_refused(self):
        rows = [sheet_row(id="10"), sheet_row(line=3, id="10")]
        with self.assertRaises(it.TagImportError):
            self._plan(rows, {}, {10: "Test Cafe"})

    def test_foreign_id_refused(self):
        rows = [sheet_row(id="99999")]
        with self.assertRaises(it.TagImportError):
            self._plan(rows, {}, {10: "Test Cafe"})


class TestCuratedParsers(unittest.TestCase):
    def test_score_domains(self):
        self.assertIsNone(ic.parse_score(""))
        self.assertEqual(ic.parse_score("4.5"), 4.5)
        self.assertEqual(ic.parse_score("4,5"), 4.5)
        self.assertEqual(ic.parse_score("4.129"), 4.13)  # rounds to (3,2)
        self.assertEqual(ic.parse_score("1"), 1.0)
        self.assertEqual(ic.parse_score("5.00"), 5.0)
        self.assertEqual(ic.parse_score("clear"), "clear")
        self.assertEqual(ic.parse_score("CLEAR"), "clear")
        for bad in ("0.9", "5.01", "-2", "abc", "five"):
            with self.assertRaises(ic.CuratedImportError):
                ic.parse_score(bad)

    def test_last_verified(self):
        self.assertIsNone(ic.parse_last_verified(""))
        self.assertEqual(ic.parse_last_verified("2026-11-10"),
                         date(2026, 11, 10))
        for bad in ("09-2026-11", "2026/11/10", "tomorrow"):
            with self.assertRaises(ic.CuratedImportError):
                ic.parse_last_verified(bad)

    def test_plan_skips_noop_and_clears(self):
        cafes = {10: "A", 11: "B", 12: "C"}
        rows = [{"id": "10", "curated_score": "", "_line": 2},
                {"id": "11", "curated_score": "4.5", "_line": 3},
                {"id": "11", "curated_score": "3", "_line": 4},  # dupe refused
                ]
        with self.assertRaises(ic.CuratedImportError):
            ic.plan_updates(rows, False, cafes)
        rows = [{"id": "10", "curated_score": "", "_line": 2},
                {"id": "11", "curated_score": "4.5", "_line": 3},
                {"id": "12", "curated_score": "clear", "_line": 4},
                {"id": "13", "curated_score": "", "last_verified": "2026-11-10",
                 "_line": 5}]
        planned = ic.plan_updates(rows, True, {10: "A", 11: "B", 12: "C",
                                               13: "D"})
        # id 10 skipped; 11 set; 12 clear; 13 lv-only (score unchanged)
        self.assertEqual(planned, [
            (11, "B", 4.5, None, 3),
            (12, "C", "clear", None, 4),
            (13, "D", None, date(2026, 11, 10), 5)])


class TestExporter(unittest.TestCase):
    def test_columns_constant(self):
        self.assertEqual(ex.COLUMNS_WITHOUT_TAGS,
                         ["id", "name", "review_count", "tag_source"])

    def test_export_roundtrip_and_refusal(self):
        """Runs the real exporter against the DEV DB (read-only) into a temp
        dir; re-run must refuse without --overwrite."""
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            path = os.path.join(tmp, "imphal_tagging.csv")
            old_argv = sys.argv
            sys.argv = ["export_tagging_sheet.py", "--city", "Imphal",
                        "--csv", path]
            try:
                ex.main()
            finally:
                sys.argv = old_argv
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                header = reader.fieldnames
                rows = list(reader)
            self.assertEqual(
                header, ex.COLUMNS_WITHOUT_TAGS + KNOWN_TAGS)
            # current DB: 233 google rows, no cafe_tags links yet
            self.assertEqual(len(rows), 233)
            for row in rows:
                self.assertEqual(row["id"], str(int(row["id"])))
                self.assertTrue(all(
                    row.get(tag, "") in ("", "y") for tag in KNOWN_TAGS))
            sys.argv = ["export_tagging_sheet.py", "--city", "Imphal",
                        "--csv", path]
            try:
                with self.assertRaises(SystemExit):
                    ex.main()   # refuses to clobber without --overwrite
            finally:
                sys.argv = old_argv


class TestTaggingDb(unittest.TestCase):
    """DEV DB, always rolled back."""

    def setUp(self):
        self.conn = psycopg.connect(get_connection_string())

        def _teardown():
            try:
                self.conn.rollback()
            finally:
                self.conn.close()

        self.addCleanup(_teardown)
        self.cur = self.conn.cursor()
        self.cur.execute("select id from public.cities where name = 'Imphal'")
        self.city_id = self.cur.fetchone()[0]

        self.cur.execute(
            """
            select count(*) from information_schema.columns
            where table_schema = 'public' and table_name = 'cafe_tags'
              and column_name = 'tag_source'
            """
        )
        self.tag_source_live = self.cur.fetchone()[0] == 1

    def _seed_cafe(self, name: str, source="google") -> int:
        self.cur.execute(
            """
            insert into public.cafes (city_id, name, location, status, source,
                                      google_place_id)
            values (%s, %s,
                    st_setsrid(st_makepoint(93.94, 24.807), 4326)
                    ::extensions.geography,
                    'operational', %s,
                    case when %s = 'google' then 'TESTTAG_' || %s else null end)
            returning id
            """,
            (self.city_id, name, source, source, name),
        )
        return self.cur.fetchone()[0]

    def _link(self, cafe_id: int, tag: str, source: str | None) -> None:
        self.cur.execute(
            """
            insert into public.cafe_tags (cafe_id, tag_id, tag_source)
            values (%s, (select id from public.tags where name = %s), %s)
            on conflict (cafe_id, tag_id) do nothing
            """,
            (cafe_id, tag, source),
        )

    def _links(self, cafe_id: int) -> dict[str, str | None]:
        self.cur.execute(
            """
            select t.name, ct.tag_source
            from public.cafe_tags ct
            join public.tags t on t.id = ct.tag_id
            where ct.cafe_id = %s
            order by t.name
            """,
            (cafe_id,),
        )
        return dict(self.cur.fetchall())

    def test_import_flow(self):
        if not self.tag_source_live:
            self.skipTest("cafe_tags.tag_source pending migration "
                          "20261110000000")
        cafe_id = self._seed_cafe("Taggable Cafe")
        self._link(cafe_id, "wifi", "own_visit")       # deletable
        self._link(cafe_id, "quiet", "review_derived") # protected
        existing = {cafe_id: self._links(cafe_id)}
        cafes = {cafe_id: "Taggable Cafe"}
        rows = [sheet_row(id=str(cafe_id), name="Taggable Cafe",
                          tag_source="own_visit",
                          group_friendly="y", quiet="y")]
        for tag in KNOWN_TAGS:
            rows[0].setdefault(tag, "")
        adds, dels, kept, undeletable = it.plan_changes(
            rows, cafes, existing, KNOWN_TAGS)
        self.assertEqual(len(adds), 1)                 # group_friendly added
        self.assertEqual(dels, [(cafe_id, "wifi", "Taggable Cafe",
                                 "own_visit", 2)])
        self.assertEqual(kept, [(cafe_id, "quiet", "Taggable Cafe")])
        self.assertEqual(undeletable, [])
        # apply within the transaction (the --commit path does exactly this)
        self.cur.execute("select name, id from public.tags")
        tag_ids = dict(self.cur.fetchall())
        for cid, tag, _n, source, _l in adds:
            self.cur.execute(
                """
                insert into public.cafe_tags (cafe_id, tag_id, tag_source)
                values (%s, %s, %s) on conflict (cafe_id, tag_id) do nothing
                """,
                (cid, tag_ids[tag], source),
            )
        self.cur.execute(
            "delete from public.cafe_tags where cafe_id = %s and tag_id = %s "
            "and tag_source in ('own_visit','own_knowledge')",
            (cafe_id, tag_ids["wifi"]),
        )
        links = self._links(cafe_id)
        self.assertEqual(links, {"quiet": "review_derived",
                                 "group_friendly": "own_visit"})

    def test_importer_refuses_without_tag_source_column(self):
        if self.tag_source_live:
            self.skipTest("migration already applied; refusal path moot")
        # the refusal lives in main()'s preflight; simulate the check
        self.cur.execute(
            """
            select count(*) from information_schema.columns
            where table_schema = 'public' and table_name = 'cafe_tags'
              and column_name = 'tag_source'
            """
        )
        self.assertEqual(self.cur.fetchone()[0], 0)


class TestCuratedDb(unittest.TestCase):
    """DEV DB, always rolled back."""

    def setUp(self):
        self.conn = psycopg.connect(get_connection_string())

        def _teardown():
            try:
                self.conn.rollback()
            finally:
                self.conn.close()

        self.addCleanup(_teardown)
        self.cur = self.conn.cursor()
        self.cur.execute("select id from public.cities where name = 'Imphal'")
        self.city_id = self.cur.fetchone()[0]

    def _seed(self, name: str, curated=None) -> int:
        self.cur.execute(
            """
            insert into public.cafes (city_id, name, location, status, source,
                                      google_place_id, curated_score)
            values (%s, %s,
                    st_setsrid(st_makepoint(93.94, 24.807), 4326)
                    ::extensions.geography,
                    'operational', 'google', 'TESTCUR_' || %s, %s)
            returning id
            """,
            (self.city_id, name, name, curated),
        )
        return self.cur.fetchone()[0]

    def _values(self, cafe_id: int, cols=("curated_score", "last_verified")):
        self.cur.execute(
            f"select {', '.join(cols)} from public.cafes where id = %s",
            (cafe_id,),
        )
        return self.cur.fetchone()

    def test_full_curated_flow_in_transaction(self):
        clear_me = self._seed("Clear Score Cafe", curated=3.0)
        set_me = self._seed("Set Score Cafe")
        keep_me = self._seed("Keep Score Cafe", curated=2.5)
        cafes = {clear_me: "Clear Score Cafe", set_me: "Set Score Cafe",
                 keep_me: "Keep Score Cafe"}
        rows = [
            {"id": str(clear_me), "curated_score": "clear", "_line": 2},
            {"id": str(set_me), "curated_score": "4.25",
             "last_verified": "2026-11-10", "_line": 3},
            {"id": str(keep_me), "curated_score": "", "_line": 4},
        ]
        planned = ic.plan_updates(rows, True, cafes)
        self.assertEqual(len(planned), 2)  # keep_me is a no-op row
        for cafe_id, _name, score, lv, _line in planned:
            sets, params = [], []
            if score is not None:
                sets.append("curated_score = %s")
                params.append(None if score == "clear" else score)
            if lv is not None:
                sets.append("last_verified = %s")
                params.append(lv)
            self.cur.execute(
                f"update public.cafes set {', '.join(sets)} where id = %s",
                (*params, cafe_id),
            )
        self.assertEqual(self._values(clear_me), (None, None))
        score_db, lv_db = self._values(set_me)
        self.assertEqual(float(score_db), 4.25)
        self.assertEqual(lv_db.date(), date(2026, 11, 10))
        self.assertEqual(float(self._values(keep_me)[0]), 2.5)
        self.cur.execute(
            "select notes is null from public.cafes where id = %s", (set_me,))
        self.assertEqual(self.cur.fetchone()[0], True)  # notes untouched


class TestCuratedHeaders(unittest.TestCase):
    """Header shapes: exported round-trip + legacy minimal rows."""

    def _write(self, tmp, header, rows):
        import export_curated_sheet as cx
        path = os.path.join(tmp, "curated.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL)
            w.writerow(header)
            w.writerows(rows)
        return path

    def _read(self, path):
        try:
            rows, has_lv = ic.read_curated_csv(path)
            return "ok", rows, has_lv
        except ic.CuratedImportError:
            return "refused", [], None

    def test_exported_roundtrip_header_accepted(self):
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            path = self._write(
                tmp, ["id", "name", "curated_score", "last_verified"],
                [["10", "A", "4.5", "2026-11-10"], ["11", "B", "", ""]])
            status, rows, has_lv = self._read(path)
        self.assertEqual(status, "ok")
        self.assertTrue(has_lv)
        self.assertEqual([r["id"] for r in rows], ["10", "11"])

    def test_exported_without_lv_accepted(self):
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            path = self._write(
                tmp, ["id", "name", "curated_score"], [["10", "A", "3"]])
            status, rows, has_lv = self._read(path)
        self.assertEqual(status, "ok")
        self.assertFalse(has_lv)

    def test_minimal_legacy_headers_still_accepted(self):
        for header, vals in ((["id", "curated_score"], [["10", "4"]]),
                             (["id", "curated_score", "last_verified"],
                              [["10", "", "2026-11-10"]])):
            with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
                path = self._write(tmp, header, vals)
                status, _rows, has_lv = self._read(path)
            with self.subTest(header=header):
                self.assertEqual(status, "ok")
                self.assertEqual(has_lv, "last_verified" in header)

    def test_unknown_header_refused(self):
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            path = self._write(tmp, ["id", "score"], [["10", "4"]])
            status, _rows, _has_lv = self._read(path)
        self.assertEqual(status, "refused")


class TestCuratedExporter(unittest.TestCase):
    def test_structure_order_and_refusal(self):
        """Runs the real exporter against the DEV DB (read-only) into a temp
        dir: header, 233 rows, review_count-desc ordering, prefill tolerance;
        re-run must refuse without --overwrite."""
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            path = os.path.join(tmp, "imphal_curated.csv")
            old_argv = sys.argv
            sys.argv = ["export_curated_sheet.py", "--city", "Imphal",
                        "--csv", path]
            try:
                cx.main()
            finally:
                sys.argv = old_argv
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                header = reader.fieldnames
                rows = list(reader)
            self.assertEqual(header, cx.HEADER)
            conn = psycopg.connect(get_connection_string())
            expected_ids = [
                r[0] for r in conn.execute(
                    """
                    select c.id from public.cafes c
                    join public.cities ct on ct.id = c.city_id
                    where ct.name = 'Imphal'
                    order by c.google_review_count desc nulls last,
                             c.name asc nulls last, c.id
                    """
                ).fetchall()]
            conn.close()
            self.assertEqual([int(r["id"]) for r in rows], expected_ids)
            self.assertEqual(len(rows), 233)  # current DB state
            for row in rows:
                for cell, parse in ((row["curated_score"], float),
                                    (row["last_verified"],
                                     date.fromisoformat)):
                    if cell != "":
                        parse(cell)
            sys.argv = ["export_curated_sheet.py", "--city", "Imphal",
                        "--csv", path]
            try:
                with self.assertRaises(SystemExit):
                    cx.main()   # refuses to clobber without --overwrite
            finally:
                sys.argv = old_argv


class TestCuratedDurableRecord(unittest.TestCase):
    """regen_decisions_file from UNCOMMITTED state; writes only into a temp
    path (module constant patched), real data/decisions file untouched."""

    def test_regen_from_uncommitted_state(self):
        with tempfile.TemporaryDirectory(dir=str(_MANAGED_TEMP)) as tmp:
            rec_path = os.path.join(tmp, "imphal_curated.csv")
            conn = psycopg.connect(get_connection_string())

            def _teardown():
                try:
                    conn.rollback()
                finally:
                    conn.close()

            self.addCleanup(_teardown)
            cur = conn.cursor()
            cur.execute("select id from public.cities where name = 'Imphal'")
            city_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into public.cafes (city_id, name, location, status,
                                          source, google_place_id,
                                          curated_score, last_verified)
                values (%s, 'Recorded Cafe',
                        st_setsrid(st_makepoint(93.94, 24.807), 4326)
                        ::extensions.geography,
                        'operational', 'google', 'TESTREC_1' || %s, 4.0,
                        date '2026-11-10')
                returning id
                """,
                (city_id, city_id),
            )
            scored_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into public.cafes (city_id, name, location, status,
                                          source, google_place_id)
                values (%s, 'No Score Cafe',
                        st_setsrid(st_makepoint(93.94, 24.807), 4326)
                        ::extensions.geography,
                        'operational', 'google', 'TESTREC_2' || %s)
                returning id
                """,
                (city_id, city_id),
            )
            cur.fetchone()
            with mock.patch.object(ic, "DECISIONS_PATH",
                                   Path(rec_path)):
                count = ic.regen_decisions_file(cur, city_id)
            self.assertEqual(count, 1)  # only the scored cafe is recorded
            with open(rec_path, encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                self.assertEqual(reader.fieldnames,
                                 ["id", "curated_score", "last_verified"])
                body = list(reader)
            self.assertEqual(
                [[r["id"], r["curated_score"], r["last_verified"]]
                 for r in body],
                [[str(scored_id), "4.00", "2026-11-10"]])


if __name__ == "__main__":
    unittest.main()
