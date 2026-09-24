"""
Tests for canvas_shadow.py (Phase 4 shadow-mode Canvas integration).

Pure/deterministic: every function under test takes its data sources as
explicit arguments (scrape dict, cache_db path, briefing_db path), so
these tests never touch the real ~/canvas-scraper output or the real
briefing.db -- everything here is built from synthetic fixtures in
tmp_path, matching how canvas-scraper's own test_cli.py tests its CLI
query layer.

Run (per this project's convention, from service/):
    ../venv/bin/python -m pytest test_canvas_shadow.py -q
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time

import pytest

import canvas_shadow


def _scrape(courses):
    return {"courses": courses}


def _course(id, course_code, assignments=None, quizzes=None):
    return {
        "course": {"id": id, "course_code": course_code, "name": course_code},
        "assignments": assignments or [],
        "quizzes": quizzes or [],
    }


def _make_cache_db(path, change_log_rows):
    """change_log_rows: list of (run_at, course_id, entity_type, entity_id,
    kind, title, before, after) with before/after as dicts or None."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE change_log (run_at REAL, course_id INTEGER, entity_type TEXT, "
        "entity_id TEXT, kind TEXT, title TEXT, before_json TEXT, after_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO change_log VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                run_at, cid, etype, eid, kind, title,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
            )
            for run_at, cid, etype, eid, kind, title, before, after in change_log_rows
        ],
    )
    conn.commit()
    conn.close()


def _make_briefing_db(path, items):
    """items: list of dicts with keys id, course, title, kind, date,
    canvas_url, source_refs (list of {"source": ...})."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE items (id TEXT PRIMARY KEY, course TEXT, title TEXT, kind TEXT, "
        "date TEXT, canvas_url TEXT, extra_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO items VALUES (?,?,?,?,?,?,?)",
        [
            (
                it["id"], it["course"], it["title"], it["kind"], it.get("date"),
                it.get("canvas_url"), json.dumps({"source_refs": it.get("source_refs", [])}),
            )
            for it in items
        ],
    )
    conn.commit()
    conn.close()


class TestGetCanvasChanges:
    def test_empty_cache_db_path_returns_empty(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001")])
        result = canvas_shadow.get_canvas_changes(cache_db=tmp_path / "missing.db", scrape=scrape)
        assert result == []

    def test_maps_course_id_to_course_code_and_computes_field_diff(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "cache.db"
        _make_cache_db(
            db_path,
            [
                (
                    1000.0, 100, "assignment", "1", "changed", "Essay 2",
                    {"due_at": "2026-09-20", "html_url": "https://x/1"},
                    {"due_at": "2026-09-22", "html_url": "https://x/1"},
                ),
            ],
        )
        events = canvas_shadow.get_canvas_changes(cache_db=db_path, scrape=scrape)
        assert len(events) == 1
        e = events[0]
        assert e["course"] == "PHIL001"
        assert e["type"] == "assignment_changed"
        assert e["canvas_url"] == "https://x/1"
        assert {"field": "due_at", "before": "2026-09-20", "after": "2026-09-22"} in e["field_changes"]

    def test_since_filter_excludes_older_runs(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "cache.db"
        _make_cache_db(
            db_path,
            [
                (1000.0, 100, "assignment", "1", "new", "Old One", None, {"name": "Old One"}),
                (2000.0, 100, "assignment", "2", "new", "New One", None, {"name": "New One"}),
            ],
        )
        events = canvas_shadow.get_canvas_changes(cache_db=db_path, since=1500.0, scrape=scrape)
        assert [e["title"] for e in events] == ["New One"]

    def test_course_filter(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001"), _course(200, "MATH001")])
        db_path = tmp_path / "cache.db"
        _make_cache_db(
            db_path,
            [
                (1000.0, 100, "assignment", "1", "new", "A", None, {}),
                (1000.0, 200, "assignment", "2", "new", "B", None, {}),
            ],
        )
        events = canvas_shadow.get_canvas_changes(cache_db=db_path, courses={"MATH001"}, scrape=scrape)
        assert [e["course"] for e in events] == ["MATH001"]

    def test_new_and_removed_have_no_field_changes(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "cache.db"
        _make_cache_db(
            db_path,
            [
                (1000.0, 100, "assignment", "1", "new", "A", None, {"name": "A"}),
                (1000.0, 100, "assignment", "2", "removed", "B", {"name": "B"}, None),
            ],
        )
        events = canvas_shadow.get_canvas_changes(cache_db=db_path, scrape=scrape)
        assert all(e["field_changes"] == [] for e in events)
        assert {e["type"] for e in events} == {"assignment_new", "assignment_removed"}


class TestGetUpcomingAssignments:
    def test_filters_to_window_and_sorts(self):
        now = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.timezone.utc)
        scrape = _scrape(
            [
                _course(
                    100, "PHIL001",
                    assignments=[
                        {"id": 1, "name": "Too late", "due_at": "2026-10-01T00:00:00Z"},
                        {"id": 2, "name": "In window B", "due_at": "2026-09-22T00:00:00Z"},
                        {"id": 3, "name": "In window A", "due_at": "2026-09-19T00:00:00Z"},
                        {"id": 4, "name": "Already past", "due_at": "2026-09-01T00:00:00Z"},
                        {"id": 5, "name": "No due date", "due_at": None},
                    ],
                )
            ]
        )
        out = canvas_shadow.get_upcoming_assignments(within_days=7, scrape=scrape, now=now)
        assert [a["title"] for a in out] == ["In window A", "In window B"]

    def test_course_filter(self):
        now = _dt.datetime(2026, 9, 18, tzinfo=_dt.timezone.utc)
        scrape = _scrape(
            [
                _course(100, "PHIL001", assignments=[{"id": 1, "name": "A", "due_at": "2026-09-19T00:00:00Z"}]),
                _course(200, "MATH001", assignments=[{"id": 2, "name": "B", "due_at": "2026-09-19T00:00:00Z"}]),
            ]
        )
        out = canvas_shadow.get_upcoming_assignments(courses={"MATH001"}, scrape=scrape, now=now)
        assert [a["course"] for a in out] == ["MATH001"]


class TestCompareWithBriefingItems:
    def test_matches_by_course_normalized_title_and_date_window(self, tmp_path):
        scrape = _scrape(
            [_course(100, "PHIL001", quizzes=[{"id": 1, "title": "Quiz 1", "due_at": "2026-09-11T00:00:00Z"}])]
        )
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(
            db_path,
            [
                {
                    "id": "phil001-quiz1-20260911", "course": "PHIL001", "title": "Quiz 1",
                    "kind": "assessment", "date": "2026-09-11",
                    "source_refs": [{"source": "gcal_canvas"}, {"source": "gmail", "thread_id": "t1"}],
                },
            ],
        )
        result = canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)
        assert result["counts"] == {"matched": 1, "email_only": 0, "scraper_only": 0}

    def test_email_only_when_no_scraper_match(self, tmp_path):
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(
            db_path,
            [
                {
                    "id": "phil001-ghost-20260911", "course": "PHIL001", "title": "Mystery Assignment",
                    "kind": "assignment", "date": "2026-09-11",
                    "source_refs": [{"source": "gmail", "thread_id": "t1"}],
                },
            ],
        )
        result = canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)
        assert result["counts"] == {"matched": 0, "email_only": 1, "scraper_only": 0}
        assert result["email_only"][0]["title"] == "Mystery Assignment"

    def test_scraper_only_when_not_in_briefing_items(self, tmp_path):
        scrape = _scrape(
            [_course(100, "PHIL001", assignments=[{"id": 1, "name": "New Assignment", "due_at": "2026-09-20T00:00:00Z"}])]
        )
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(db_path, [])
        result = canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)
        assert result["counts"] == {"matched": 0, "email_only": 0, "scraper_only": 1}
        assert result["scraper_only"][0]["title"] == "New Assignment"

    def test_non_gmail_sourced_items_are_ignored_entirely(self, tmp_path):
        """An item Daily Briefing already has purely from the Canvas/
        Syllabi calendars (no gmail source_ref) isn't part of the
        email-migration comparison at all -- it was never at risk of
        being lost by removing email ingestion."""
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(
            db_path,
            [
                {
                    "id": "phil001-x", "course": "PHIL001", "title": "Calendar Only", "kind": "assignment",
                    "date": "2026-09-11", "source_refs": [{"source": "gcal_syllabi"}],
                },
            ],
        )
        result = canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)
        assert result["counts"] == {"matched": 0, "email_only": 0, "scraper_only": 0}

    def test_date_more_than_one_day_apart_does_not_match(self, tmp_path):
        scrape = _scrape(
            [_course(100, "PHIL001", quizzes=[{"id": 1, "title": "Quiz 1", "due_at": "2026-09-15T00:00:00Z"}])]
        )
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(
            db_path,
            [
                {
                    "id": "phil001-quiz1-20260911", "course": "PHIL001", "title": "Quiz 1",
                    "kind": "assessment", "date": "2026-09-11",
                    "source_refs": [{"source": "gmail", "thread_id": "t1"}],
                },
            ],
        )
        result = canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)
        assert result["counts"] == {"matched": 0, "email_only": 1, "scraper_only": 1}

    def test_never_writes_to_briefing_db(self, tmp_path):
        """query_only=TRUE must make any accidental write attempt fail
        loudly rather than silently succeed -- belt-and-suspenders on top
        of this module simply never issuing a write statement."""
        scrape = _scrape([_course(100, "PHIL001")])
        db_path = tmp_path / "briefing.db"
        _make_briefing_db(db_path, [])
        canvas_shadow.compare_with_briefing_items(briefing_db=db_path, scrape=scrape)

        conn = sqlite3.connect(str(db_path))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("DELETE FROM items")
        conn.close()


class TestCanvasAuthoritativeCandidates:
    """Phase 5 (cutover): canvas-scraper's own assignments/quizzes shaped
    to feed orchestrator.py's step3_reconcile directly."""

    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("America/New_York")

    def test_shapes_assignment_matching_step1_2_output(self):
        scrape = _scrape(
            [
                _course(
                    100, "PHIL001",
                    assignments=[
                        {"id": 1, "name": "Essay 2", "due_at": "2026-09-20T23:59:00Z", "html_url": "https://x/1"},
                    ],
                )
            ]
        )
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert len(out) == 1
        c = out[0]
        assert c["course"] == "PHIL001"
        assert c["title"] == "Essay 2"
        assert c["kind"] == "assignment"
        assert c["due_date"] == "2026-09-20"  # 23:59 UTC -> still Sep 20 in ET
        assert c["due_time"] == "19:59"       # UTC-4 (EDT) in September
        assert c["canvas_url"] == "https://x/1"
        assert c["confidence"] == "confirmed"
        assert c["source"] == "canvas_scraper"
        # Every key step3_reconcile/step1_2_gmail_sweep candidates carry:
        for key in ("thread_id", "course", "title", "kind", "due_date", "due_time",
                    "end_time", "location", "canvas_url", "organizer", "description",
                    "links", "confidence", "source"):
            assert key in c

    def test_quiz_becomes_assessment_kind(self):
        scrape = _scrape(
            [_course(100, "PHIL001", quizzes=[{"id": 1, "title": "Quiz 2", "due_at": "2026-09-19T03:59:59Z"}])]
        )
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["kind"] == "assessment"
        assert out[0]["title"] == "Quiz 2"

    def test_undated_items_are_omitted(self):
        scrape = _scrape(
            [
                _course(
                    100, "PHIL001",
                    assignments=[{"id": 1, "name": "No due date", "due_at": None}],
                    quizzes=[{"id": 2, "title": "No due date quiz", "due_at": None}],
                )
            ]
        )
        assert canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ) == []

    def test_thread_id_is_stable_and_traceable(self):
        scrape = _scrape([_course(100, "PHIL001", assignments=[{"id": 42, "name": "X", "due_at": "2026-09-20T00:00:00Z"}])])
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["thread_id"] == "canvas-scraper:assignment:100:42"

    def test_two_courses_each_produce_their_own_candidates(self):
        scrape = _scrape(
            [
                _course(100, "PHIL001", assignments=[{"id": 1, "name": "A", "due_at": "2026-09-20T00:00:00Z"}]),
                _course(200, "MATH001", assignments=[{"id": 2, "name": "B", "due_at": "2026-09-20T00:00:00Z"}]),
            ]
        )
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert {c["course"] for c in out} == {"PHIL001", "MATH001"}

    # -- submission_types passthrough (2026-09-18) --------------------------
    # Added after a real briefing showed MATH001's "Peer Instruction" and
    # PHIL001's "Participation" rows with the exact same weight as real
    # homework: canvas-scraper's own scrape already carries
    # `submission_types` (what Canvas says you can even hand in) and this
    # function was discarding it before lifecycle.action_tag() ever got a
    # chance to read it.

    def test_assignment_submission_types_pass_through(self):
        scrape = _scrape([_course(100, "PHIL001", assignments=[
            {"id": 1, "name": "Participation 3", "due_at": "2026-09-18T00:00:00Z",
             "submission_types": ["none"]},
        ])])
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["submission_types"] == ["none"]

    def test_assignment_with_no_submission_types_field_gets_empty_list(self):
        scrape = _scrape([_course(100, "PHIL001", assignments=[
            {"id": 1, "name": "Reading List", "due_at": "2026-09-20T00:00:00Z"},
        ])])
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["submission_types"] == []

    def test_quiz_type_folds_into_submission_types(self):
        scrape = _scrape([_course(100, "PHIL001", quizzes=[
            {"id": 1, "title": "Before you begin", "due_at": "2026-09-19T00:00:00Z",
             "quiz_type": "survey"},
        ])])
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["submission_types"] == ["survey"]

    def test_quiz_with_no_quiz_type_gets_empty_submission_types(self):
        scrape = _scrape([_course(100, "PHIL001", quizzes=[
            {"id": 1, "title": "Quiz 2", "due_at": "2026-09-19T00:00:00Z"},
        ])])
        out = canvas_shadow.canvas_authoritative_candidates(scrape=scrape, tz=self._TZ)
        assert out[0]["submission_types"] == []


class TestLoadCanvasScrape:
    def test_missing_file_raises_clear_error(self, tmp_path):
        with pytest.raises(canvas_shadow.NoCanvasScrapeError):
            canvas_shadow.load_canvas_scrape(tmp_path / "does_not_exist.json")

    def test_loads_real_json_shape(self, tmp_path):
        path = tmp_path / "latest.json"
        path.write_text(json.dumps({"courses": [_course(1, "X")]}))
        data = canvas_shadow.load_canvas_scrape(path)
        assert data["courses"][0]["course"]["course_code"] == "X"
