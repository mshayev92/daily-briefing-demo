"""Tests for run_stats.py and state_io.py.

Kept separate from test_briefing.py because these cover the two modules added
on 2026-09-09, and several assertions are pinned to real values from the
2026-09-09T11:09:00Z run rather than to synthetic fixtures. Run with:

    python3 test_state_and_stats.py
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

import run_stats
import state_io

# Real values from the 2026-09-09 run, for assertions that must not drift.
REAL_START = "2026-09-09T11:09:00Z"
REAL_WRITE = datetime(2026, 9, 9, 11, 42, tzinfo=timezone.utc)
REAL_DURATION = 1980.0


class TestRunStats(unittest.TestCase):

    def _entry(self, **kw):
        base = dict(
            start_time=REAL_START, emails_processed=17, items_found=70,
            items_changed=12,
            delivery={"email": True, "drive": True, "artifact": True},
            error_count=0, state_path="/nonexistent", page_path="/nonexistent",
            now=REAL_WRITE)
        base.update(kw)
        return run_stats.entry(**base)

    def test_duration_matches_the_real_run(self):
        self.assertEqual(self._entry()["duration_seconds"], REAL_DURATION)

    def test_duration_is_never_none(self):
        """The whole reason this module exists: null on 3 of the first 5 runs."""
        for now in (REAL_WRITE, datetime.now(timezone.utc)):
            self.assertIsNotNone(self._entry(now=now)["duration_seconds"])

    def test_clock_skew_gives_zero_not_none(self):
        e = self._entry(start_time="2026-09-09T12:00:00Z",
                        now=datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(e["duration_seconds"], 0.0)

    def test_accepts_timestamp_without_z(self):
        self.assertEqual(
            self._entry(start_time="2026-09-09T11:09:00")["duration_seconds"],
            REAL_DURATION)

    def test_missing_file_sizes_as_zero_not_crash(self):
        e = self._entry()
        self.assertEqual(e["bytes_state"], 0)
        self.assertEqual(e["bytes_page"], 0)

    def test_delivery_triple_always_present(self):
        e = self._entry(delivery={})
        self.assertEqual(set(e["delivery"]), {"email", "drive", "artifact"})
        self.assertFalse(any(e["delivery"].values()))

    def test_prepend_keeps_seven_newest_first(self):
        prior = [{"i": i} for i in range(9)]
        out = run_stats.prepend(prior, self._entry())
        self.assertEqual(len(out), 7)
        self.assertEqual(out[0]["start_time"], REAL_START)

    def test_prepend_tolerates_absent_history(self):
        self.assertEqual(len(run_stats.prepend(None, self._entry())), 1)


class TestStateFilenames(unittest.TestCase):

    GOOD = "college_assistant_state_2026-09-09T114200Z.json"

    def test_the_superseded_sort_trap(self):
        """3.1's load-bearing filter: `s` and `b` sort above any 2026- stamp."""
        names = [
            "college_assistant_state_superseded_2026-09-01T000000Z.json",
            "college_assistant_state_backup_9999.json",
            "college_assistant_state_temp.json",
            "college_assistant_state_2026-09-08T110700Z.json",
            self.GOOD,
        ]
        self.assertEqual(state_io.order_candidates(names)[0], self.GOOD)
        self.assertEqual(len(state_io.order_candidates(names)), 2)

    def test_rejects_near_misses(self):
        for bad in ("college_assistant_state_2026-09-09T1142Z.json",
                    "college_assistant_state_2026-09-09T114200Z.json.bak",
                    "xcollege_assistant_state_2026-09-09T114200Z.json",
                    "college_assistant_state.json"):
            self.assertIsNone(state_io.parse_stamp(bad), bad)

    def test_sorts_by_parsed_stamp_not_string(self):
        names = ["college_assistant_state_2026-09-09T090000Z.json",
                 "college_assistant_state_2026-09-09T100000Z.json"]
        self.assertEqual(state_io.order_candidates(names)[0], names[1])

    def test_new_name_matches_the_read_filter(self):
        n = state_io.new_name(REAL_WRITE)
        self.assertIsNotNone(state_io.parse_stamp(n))

    def test_serialize_is_compact(self):
        s = state_io.serialize({"a": 1, "b": [1, 2]})
        self.assertNotIn(" ", s)


class TestStateLoad(unittest.TestCase):

    def test_falls_through_corrupt_newest(self):
        names = ["college_assistant_state_2026-09-09T114200Z.json",
                 "college_assistant_state_2026-09-08T110700Z.json"]

        def rd(n):
            return "{ not json" if "0909" in n.replace("-", "") else '{"v":10}'

        state, src, problems = state_io.load(names, rd)
        self.assertEqual(state, {"v": 10})
        self.assertIn("110700Z", src)
        self.assertTrue(problems and problems[0].startswith("MAJOR"))

    def test_legacy_fallback(self):
        state, src, _ = state_io.load(
            [state_io.LEGACY_NAME], lambda n: '{"legacy":true}')
        self.assertEqual(src, state_io.LEGACY_NAME)

    def test_total_failure_returns_none_and_says_so(self):
        state, src, problems = state_io.load(["nope.txt"], lambda n: "")
        self.assertIsNone(state)
        self.assertIsNone(src)
        self.assertTrue(any("empty state" in p for p in problems))

    def test_load_local_reads_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, TestStateFilenames.GOOD)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"schema_version":10}')
            state, src, problems = state_io.load_local(d)
            self.assertEqual(state["schema_version"], 10)
            self.assertEqual(problems, [])


class TestProjection(unittest.TestCase):

    TODAY = date(2026, 9, 9)

    def setUp(self):
        self.state = {
            "schema_version": 10,
            "items": [{"id": "a", "due_date": "2026-09-10"},
                      {"id": "b", "due_date": "2025-01-01"}],
            "learned_patterns": {"canvas_nicknames": {"Math": "MATH140"}},
            "errors": [{"m": "x" * 200} for _ in range(50)],
            "last_run_stats": [{"i": i} for i in range(7)],
            "umd_dates": [{"date": "2026-09-14", "label": "in window"},
                          {"date": "2026-12-01", "label": "out"}],
            "undocumented_key": {"keep": "me"},
        }

    def test_drops_errors_and_old_stats(self):
        p = state_io.for_context(self.state, self.TODAY)
        self.assertNotIn("errors", p)
        self.assertEqual(len(p["last_run_stats"]), 1)

    def test_omits_umd_dates_entirely_while_parked(self):
        """Was `test_windows_umd_dates_only`, asserting 21 entries windowed to
        the 1 inside the horizon. §7c is parked (2026-09-10), so the key is
        omitted outright — nothing reads it, and 1 entry of unusable context is
        still context. Restore the windowing assertion alongside
        lifecycle.UMD_DEADLINES_ENABLED."""
        p = state_io.for_context(self.state, self.TODAY)
        self.assertNotIn("umd_dates", p)

    def test_items_are_never_windowed(self):
        """Dedupe needs out-of-window items; slicing them is a correctness bug."""
        p = state_io.for_context(self.state, self.TODAY)
        self.assertEqual(len(p["items"]), 2)

    def test_preserves_undocumented_keys(self):
        """3.1: a rewrite keeping only documented fields would destroy data."""
        p = state_io.for_context(self.state, self.TODAY)
        self.assertEqual(p["undocumented_key"], {"keep": "me"})
        self.assertEqual(p["learned_patterns"], self.state["learned_patterns"])

    def test_projection_is_marked_so_it_cannot_be_written_back(self):
        self.assertTrue(state_io.for_context(self.state, self.TODAY)["_projected"])

    def test_empty_state_projects_to_empty(self):
        self.assertEqual(state_io.for_context(None, self.TODAY), {})

    def test_savings_are_reported_in_bytes(self):
        s = state_io.projection_savings(self.state, self.TODAY)
        self.assertGreater(s["saved_bytes"], 0)
        self.assertEqual(s["full_bytes"] - s["context_bytes"], s["saved_bytes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
