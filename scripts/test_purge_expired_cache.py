"""Tests for scripts/purge_expired_cache.py — pure + tmp dirs; no DB, no real data."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import purge_expired_cache as pc  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
CUTOFF_30 = NOW - pc.timedelta(days=30)  # 2026-10-08 — like the real Dec-6 rule


def stamp(path: str, day: int) -> None:
    ts = datetime(2026, 9, day, tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))


class TestBatchDate(unittest.TestCase):
    def test_parses_google_run_dir(self):
        self.assertEqual(
            pc.batch_date("google_20261106T155506Z"),
            datetime(2026, 11, 6, 15, 55, 6, tzinfo=timezone.utc))

    def test_rejects_non_run_dir(self):
        self.assertIsNone(pc.batch_date("cache"))
        self.assertIsNone(pc.batch_date("step5_review_20261106T170726Z.csv"))


class TestCollectExpired(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.base_path = self.base.name
        raw = os.path.join(self.base_path, "data", "raw")
        processed = os.path.join(self.base_path, "data", "processed")
        decisions = os.path.join(self.base_path, "data", "decisions")
        for root in (raw, processed, decisions):
            os.makedirs(root, exist_ok=True)
        # old google batch (expired), fresh google batch (kept), old osm (expired)
        self.old_google = os.path.join(raw, "google_20260901T000000Z")
        self.fresh_google = os.path.join(raw, "google_20261106T000000Z")
        self.old_osm = os.path.join(raw, "osm_20260901T000000Z")
        for d in (self.old_google, self.fresh_google, self.old_osm):
            os.makedirs(d, exist_ok=True)
            stamp(d, 2)  # mtime does not drive dir expiry; batch date does
        # flat cache with one old and one fresh entry
        cache = os.path.join(raw, "cache")
        os.makedirs(cache, exist_ok=True)
        self.old_cache = os.path.join(cache, "old.json")
        self.fresh_cache = os.path.join(cache, "fresh.json")
        open(self.old_cache, "wb").close()
        open(self.fresh_cache, "wb").close()
        stamp(self.old_cache, 1)   # pre-cutoff mtime
        # old file under processed (expired), fresh one (the marked review CSV)
        self.old_processed = os.path.join(processed, "old_review.csv")
        self.fresh_processed = os.path.join(processed, "step5_review_fresh.csv")
        open(self.old_processed, "wb").close()
        open(self.fresh_processed, "wb").close()
        stamp(self.old_processed, 1)
        # decisions: old mtime but NEVER scanned
        self.decision = os.path.join(decisions, "imphal_decisions.csv")
        open(self.decision, "wb").close()
        stamp(self.decision, 1)

    def test_collects_expected_candidates_only(self):
        from os.path import join
        expired_dirs, expired_files = [], []
        for rel in (("data", "raw"), ("data", "processed")):
            bucket = pc.collect_expired(join(self.base_path, *rel), CUTOFF_30)
            expired_dirs.extend(bucket["dirs"])
            expired_files.extend(bucket["files"])
        self.assertEqual(len(expired_dirs), 2)  # old google + old osm
        self.assertTrue(self.old_google in expired_dirs)
        self.assertTrue(self.old_osm in expired_dirs)
        self.assertNotIn(self.fresh_google, expired_dirs)
        self.assertEqual(sorted(expired_files),
                         sorted([self.old_cache, self.old_processed]))
        # fresh cache entry and the current review CSV stay
        self.assertNotIn(self.fresh_cache, expired_files)
        self.assertNotIn(self.fresh_processed, expired_files)

    def test_decisions_root_protected_via_main_roots_and_guard(self):
        """The collector is generic on purpose; the roots+guard in main() enforce scope."""
        import inspect
        src = inspect.getsource(pc.main)
        self.assertIn('os.path.join(args.base, "data", "raw")', src)
        self.assertIn('os.path.join(args.base, "data", "processed")', src)
        self.assertNotIn('"data", "decisions"', src)
        with self.assertRaises(RuntimeError):
            pc.guard_under_root(os.path.join(self.base_path, "data", "decisions",
                                             "imphal_decisions.csv"),
                                [os.path.join(self.base_path, "data", "raw"),
                                 os.path.join(self.base_path, "data", "processed")])

    def test_fresh_batch_gate(self):
        self.assertTrue(pc.fresh_google_batch_exists(
            os.path.join(self.base_path, "data", "raw"), CUTOFF_30))
        # after the fresh batch ages out, the gate must refuse
        import datetime as dtmod
        late = datetime(2026, 12, 10, tzinfo=dtmod.timezone.utc) - pc.timedelta(days=30)
        self.assertFalse(pc.fresh_google_batch_exists(
            os.path.join(self.base_path, "data", "raw"), late))

    def test_guard_blocks_outside_root(self):
        with self.assertRaises(RuntimeError):
            pc.guard_under_root(os.path.join(self.base_path, "data", "decisions",
                                             "imphal_decisions.csv"),
                                [os.path.join(self.base_path, "data", "raw"),
                                 os.path.join(self.base_path, "data", "processed")])
        # inside-root passthrough works
        pc.guard_under_root(os.path.join(self.base_path, "data", "raw", "cache"),
                            [os.path.join(self.base_path, "data", "raw")])


if __name__ == "__main__":
    unittest.main()
