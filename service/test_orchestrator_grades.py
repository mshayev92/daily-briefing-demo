"""
Tests for orchestrator.py's step5d_grades -- the integration point between
grades.py's pure functions and the real course_grade_snapshots table /
Run/ctx machinery. Unlike test_grades.py these DO touch a real (temporary)
SQLite db, exactly like test_orchestrator_reconcile.py's own DB test does --
safe to run fully, including with `dry_run=False`, because step5d_grades's
only side effect is a local SQLite write; it never touches Gmail, Calendar,
or email (see step5d_grades's own docstring on why it's gated on
`run.dry_run` at all despite that).

Run (from service/):
    ../venv/bin/python -m pytest test_orchestrator_grades.py -q
"""

from __future__ import annotations

import datetime as _dt

import db as dbmod
import canvas_shadow
import orchestrator

KNOWN_COURSES = {
    "ENGL001": {"class": "engl"}, "ECON001": {"class": "econ"},
    "MATH001": {"class": "math"}, "PHIL001": {"class": "phil"},
    "CMSC001": {"class": "cmsc"}, "CS-Advising": {"class": "advising"},
}
TODAY = _dt.date(2026, 9, 19)


def _run(db_path, dry_run=False):
    return orchestrator.Run(db_path=str(db_path), dry_run=dry_run)


def _ctx(items=None):
    return {"today": TODAY, "state": {"courses": KNOWN_COURSES, "items": items or []}}


def _assignment(name, points=10.0, score=None, workflow_state=None,
               missing=False, completion_state="not_started"):
    sub = None
    if score is not None or workflow_state or missing:
        sub = {"score": score, "workflow_state": workflow_state,
               "missing": missing, "excused": False}
    return {"name": name, "points_possible": points, "published": True,
            "submission": sub, "completion_state": completion_state}


def _scrape(course_code, current_score, assignments, final_score=None,
           course_id=1000002):
    return {"courses": [{
        "course": {"id": course_id, "course_code": course_code,
                  "current_score": current_score, "current_grade": None,
                  "final_score": final_score, "final_grade": None},
        "assignments": assignments, "quizzes": [],
    }]}


def _insert_old_snapshot(db_path, course_label, score, graded_count,
                         days_ago, missing_count=0):
    conn = dbmod.connect(str(db_path))
    captured_at = (TODAY - _dt.timedelta(days=days_ago)).isoformat() + "T00:00:00Z"
    conn.execute(
        "INSERT INTO course_grade_snapshots "
        "(captured_at, course_label, current_score, current_grade, "
        " final_score, final_grade, graded_count, gradable_count, "
        " missing_count, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (captured_at, course_label, score, None, None, None, graded_count,
         36, missing_count, "canvas_scraper"))
    conn.commit()
    conn.close()


class TestDryRun:
    def test_dry_run_reads_nothing_and_writes_nothing(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        called = []
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape",
                            lambda: called.append(1) or _scrape("PHIL001", 90, []))
        run = _run(db_path, dry_run=True)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])
        assert result == {"course_text": {}, "portfolio_notes": [], "insight_items": []}
        assert not called  # canvas-scraper's own output was never even read
        assert any("dry-run" in s for s in run.skipped)
        conn = dbmod.connect(str(db_path))
        n = conn.execute("SELECT COUNT(*) FROM course_grade_snapshots").fetchone()[0]
        conn.close()
        assert n == 0


class TestFirstRunNoHistory:
    def test_writes_a_snapshot_and_shows_grade_with_no_trend_yet(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        scrape = _scrape("PHIL001", 91.67, [
            _assignment("Post 1", score=5.0, workflow_state="graded",
                       completion_state="graded"),
        ])
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])

        assert "91.7%" in result["course_text"]["PHIL001"]
        assert "1 of 1 graded" in result["course_text"]["PHIL001"]
        # first-ever observation -- no baseline exists yet, so no trend claim
        assert result["insight_items"] == []
        assert result["portfolio_notes"] == []

        conn = dbmod.connect(str(db_path))
        rows = conn.execute("SELECT course_label, current_score FROM "
                            "course_grade_snapshots").fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [("PHIL001", 91.67)]

    def test_unknown_and_non_academic_courses_never_get_a_snapshot(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        scrape = {"courses": [
            {"course": {"id": 1, "course_code": "CMNS Pre-Orientation",
                       "current_score": 100, "current_grade": None,
                       "final_score": None, "final_grade": None},
             "assignments": [], "quizzes": []},
            {"course": {"id": 2, "course_code": "CS-Advising",
                       "current_score": None, "current_grade": None,
                       "final_score": None, "final_grade": None},
             "assignments": [], "quizzes": []},
        ]}
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])
        assert result["course_text"] == {}
        conn = dbmod.connect(str(db_path))
        n = conn.execute("SELECT COUNT(*) FROM course_grade_snapshots").fetchone()[0]
        conn.close()
        assert n == 0


class TestPerformanceDecline:
    def test_meaningful_decline_since_an_old_snapshot_surfaces_as_attention(
            self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=92, graded_count=3, days_ago=16)

        graded = [_assignment("Item %d" % i, score=6.0, workflow_state="graded",
                              completion_state="graded") for i in range(8)]
        scrape = _scrape("PHIL001", 78.0, graded)
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])

        ids = [it["id"] for it in result["insight_items"]]
        assert "grade-decline-phil001" in ids
        item = next(it for it in result["insight_items"] if it["id"] == "grade-decline-phil001")
        assert item["needs_attention"] is True
        assert item["course_label"] == "PHIL001"

    def test_decline_plus_busy_week_becomes_one_combined_item_not_two(
            self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=90, graded_count=3, days_ago=16)
        graded = [_assignment("Item %d" % i, score=6.0, workflow_state="graded",
                              completion_state="graded") for i in range(6)]
        scrape = _scrape("PHIL001", 82.0, graded)  # -8, meaningful but not attention_worthy alone
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)
        upcoming_items = [
            {"kind": "assignment", "course_label": "PHIL001",
             "date": (TODAY + _dt.timedelta(days=i)).isoformat()} for i in (1, 2, 3)]
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=upcoming_items)

        ids = [it["id"] for it in result["insight_items"]]
        assert "grade-risk-phil001" in ids
        assert "grade-decline-phil001" not in ids  # combined item replaces the plain one


class TestCanvasRetrievalFailure:
    def test_missing_scrape_file_falls_back_to_last_snapshot(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=85.5, graded_count=4, days_ago=2)

        def _raise():
            raise canvas_shadow.NoCanvasScrapeError("no scrape file")
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", _raise)

        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])

        assert "85.5%" in result["course_text"]["PHIL001"]
        assert any(e["severity"] == "MINOR" for e in run.errors)
        assert any(e["severity"] == "CRITICAL" for e in run.errors) is False
        # nothing new was written -- the one pre-seeded row is still the only one
        conn = dbmod.connect(str(db_path))
        n = conn.execute("SELECT COUNT(*) FROM course_grade_snapshots").fetchone()[0]
        conn.close()
        assert n == 1

    def test_unreadable_scrape_is_major_but_never_crashes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))

        def _raise():
            raise ValueError("corrupt json")
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", _raise)

        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])
        assert result["course_text"] == {}
        assert any(e["severity"] == "MAJOR" for e in run.errors)


class TestDismissedInsightsDoNotResurface:
    def test_a_dismissed_grade_decline_item_is_not_re_added(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=92, graded_count=3, days_ago=16)
        graded = [_assignment("Item %d" % i, score=6.0, workflow_state="graded",
                              completion_state="graded") for i in range(8)]
        scrape = _scrape("PHIL001", 78.0, graded)
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)

        prior_items = [{"id": "grade-decline-phil001", "status": "dismissed",
                       "times_surfaced": 5}]
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(items=prior_items), items=[])
        ids = [it["id"] for it in result["insight_items"]]
        assert "grade-decline-phil001" not in ids

    def test_an_open_insight_carries_over_its_surface_count(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=92, graded_count=3, days_ago=16)
        graded = [_assignment("Item %d" % i, score=6.0, workflow_state="graded",
                              completion_state="graded") for i in range(8)]
        scrape = _scrape("PHIL001", 78.0, graded)
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)

        prior_items = [{"id": "grade-decline-phil001", "status": "ongoing",
                       "times_surfaced": 2}]
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(items=prior_items), items=[])
        item = next(it for it in result["insight_items"]
                   if it["id"] == "grade-decline-phil001")
        assert item["times_surfaced"] == 2
        assert item["status"] == "ongoing"


class TestInsufficientHistory:
    def test_baseline_less_than_14_days_old_is_not_trusted(self, tmp_path, monkeypatch):
        db_path = tmp_path / "briefing.db"
        dbmod.init_db(str(db_path))
        _insert_old_snapshot(db_path, "PHIL001", score=92, graded_count=3, days_ago=5)
        graded = [_assignment("Item %d" % i, score=6.0, workflow_state="graded",
                              completion_state="graded") for i in range(8)]
        scrape = _scrape("PHIL001", 78.0, graded)
        monkeypatch.setattr(canvas_shadow, "load_canvas_scrape", lambda: scrape)
        run = _run(db_path, dry_run=False)
        result = orchestrator.step5d_grades(run, _ctx(), items=[])
        assert result["insight_items"] == []
        assert result["portfolio_notes"] == []
