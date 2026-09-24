"""
Tests for grades.py (STEP 5d, grades + trends, 2026-09-19).

Pure/deterministic, like test_canvas_shadow.py: every function under test
takes its data as explicit arguments, so nothing here touches the real
~/canvas-scraper output or the real briefing.db.

Run (from service/):
    ../venv/bin/python -m pytest test_grades.py -q
"""

from __future__ import annotations

import datetime as _dt

import grades

TODAY = _dt.date(2026, 9, 19)

KNOWN_COURSES = {
    "ENGL001": {"class": "engl"}, "ECON001": {"class": "econ"},
    "MATH001": {"class": "math"}, "PHIL001": {"class": "phil"},
    "CMSC001": {"class": "cmsc"}, "CS-Advising": {"class": "advising"},
}


def _normalize(raw, state):
    return raw if raw in (state.get("courses") or {}) else raw


def _assignment(name, points=10.0, published=True, score=None,
                workflow_state=None, missing=False, excused=False,
                completion_state="not_started"):
    sub = None
    if score is not None or workflow_state or missing or excused:
        sub = {"score": score, "workflow_state": workflow_state,
               "missing": missing, "excused": excused}
    return {"name": name, "points_possible": points, "published": published,
            "submission": sub, "completion_state": completion_state}


def _bundle(course_code, course_id=1, assignments=None, current_score=None,
           current_grade=None, final_score=None, final_grade=None):
    return {
        "course": {"id": course_id, "course_code": course_code,
                  "current_score": current_score, "current_grade": current_grade,
                  "final_score": final_score, "final_grade": final_grade},
        "assignments": assignments or [], "quizzes": [],
    }


# --- attribute_courses / course_facts --------------------------------------


class TestAttributeCourses:
    def test_scopes_to_known_real_courses_only(self):
        scrape = {"courses": [
            _bundle("PHIL001"),
            _bundle("CMNS Pre-Orientation"),  # not in KNOWN_COURSES at all
            _bundle("CS-Advising"),           # known, but not a real class
        ]}
        out = grades.attribute_courses(scrape, KNOWN_COURSES, _normalize)
        assert set(out) == {"PHIL001"}

    def test_graded_missing_ungraded_counted_correctly(self):
        a = [
            _assignment("A", score=9.0, workflow_state="graded", completion_state="graded"),
            _assignment("B", missing=True, completion_state="missing"),
            _assignment("C", completion_state="not_started"),
            _assignment("D", points=0),  # not gradable at all
            _assignment("E", published=False, score=10),  # unpublished, excluded
        ]
        facts = grades.course_facts(_bundle("PHIL001", assignments=a))
        assert facts["gradable_count"] == 3  # A, B, C (D and E excluded)
        assert facts["graded_count"] == 1
        assert facts["missing_count"] == 1

    def test_locked_but_scored_assignment_still_counts_as_graded(self):
        """The real bug found against the live account, 2026-09-19: a
        scored assignment whose window later locked comes back
        completion_state == "locked" from canvas-scraper (its resolver
        checks locked_for_user before graded-ness). This module must read
        submission.score directly, not trust completion_state, or a
        locked-but-graded item silently vanishes from both counts."""
        a = [_assignment("W1 Homework", score=10.0, workflow_state="graded",
                         completion_state="locked")]
        facts = grades.course_facts(_bundle("ENGL001", assignments=a))
        assert facts["graded_count"] == 1
        assert facts["gradable_count"] == 1

    def test_excused_counts_toward_neither_side(self):
        a = [_assignment("Excused", excused=True, completion_state="excused")]
        facts = grades.course_facts(_bundle("PHIL001", assignments=a))
        assert facts["gradable_count"] == 0
        assert facts["graded_count"] == 0
        assert facts["missing_count"] == 0

    def test_current_score_never_recomputed_only_copied(self):
        facts = grades.course_facts(_bundle("PHIL001", current_score=91.67,
                                            final_score=6.33))
        assert facts["current_score"] == 91.67
        assert facts["final_score"] == 6.33


# --- grade_display_text -----------------------------------------------------


class TestGradeDisplayText:
    def test_nothing_gradable_yet_is_blank(self):
        assert grades.grade_display_text({"gradable_count": 0, "graded_count": 0}) == ""

    def test_gradable_but_nothing_graded_never_shows_a_bare_score(self):
        text = grades.grade_display_text(
            {"gradable_count": 36, "graded_count": 0, "current_score": None})
        assert "0 of 36" in text
        assert "%" not in text

    def test_normal_case_shows_score_and_sample_size(self):
        text = grades.grade_display_text(
            {"gradable_count": 36, "graded_count": 2, "current_score": 91.666})
        assert "91.7%" in text
        assert "2 of 36 graded" in text

    def test_stale_snapshot_is_dated(self):
        text = grades.grade_display_text(
            {"gradable_count": 5, "graded_count": 3, "current_score": 80.0},
            as_of="2026-09-10", today=TODAY)
        assert "as of Sep 10" in text

    def test_fresh_snapshot_carries_no_date_suffix(self):
        text = grades.grade_display_text(
            {"gradable_count": 5, "graded_count": 3, "current_score": 80.0},
            as_of=TODAY.isoformat(), today=TODAY)
        assert "2026-09-19" not in text


class TestLatestSnapshot:
    def test_fresh_facts_win_when_available(self):
        fresh = {"PHIL001": {"current_score": 90, "graded_count": 3,
                             "gradable_count": 10, "missing_count": 0}}
        row, stale = grades.latest_snapshot("PHIL001", fresh, [], "2026-09-19T00:00:00Z")
        assert stale is False
        assert row["current_score"] == 90

    def test_falls_back_to_last_history_row_when_fresh_unavailable(self):
        history = [grades.snapshot_row("PHIL001", {"current_score": 70}, "2026-09-01T00:00:00Z")]
        row, stale = grades.latest_snapshot("PHIL001", {}, history, "2026-09-19T00:00:00Z")
        assert stale is True
        assert row["current_score"] == 70

    def test_nothing_available_at_all(self):
        row, stale = grades.latest_snapshot("PHIL001", {}, [], "2026-09-19T00:00:00Z")
        assert row is None and stale is False


# --- performance_trend -------------------------------------------------------


def _snap(label, score, graded, days_ago):
    return grades.snapshot_row(
        label, {"current_score": score, "graded_count": graded},
        (TODAY - _dt.timedelta(days=days_ago)).isoformat() + "T00:00:00Z")


class TestPerformanceTrend:
    def test_first_run_no_history_is_no_trend_not_a_decline(self):
        latest = _snap("PHIL001", 90, 5, 0)
        assert grades.performance_trend("PHIL001", latest, [], TODAY) is None

    def test_too_few_graded_items_is_insufficient_evidence(self):
        latest = _snap("MATH001", 100, 1, 0)
        baseline = _snap("MATH001", 100, 1, 20)
        assert grades.performance_trend(
            "MATH001", latest, [baseline], TODAY) is None

    def test_unchanged_grade_produces_no_trend(self):
        latest = _snap("ECON001", 92, 8, 0)
        baseline = _snap("ECON001", 92, 5, 20)
        assert grades.performance_trend(
            "ECON001", latest, [baseline], TODAY) is None

    def test_score_moved_but_no_new_graded_evidence_is_not_trusted(self):
        """A course recalculating its total (drop-lowest kicking in, a
        weight change) can move current_score with ZERO new grading
        events -- must not read as a real trend."""
        latest = _snap("ECON001", 85, 5, 0)
        baseline = _snap("ECON001", 92, 5, 20)  # same graded_count
        assert grades.performance_trend(
            "ECON001", latest, [baseline], TODAY) is None

    def test_meaningful_decline_detected(self):
        latest = _snap("PHIL001", 78, 8, 0)
        baseline = _snap("PHIL001", 92, 3, 16)
        facts = grades.performance_trend("PHIL001", latest, [baseline], TODAY)
        assert facts is not None
        assert facts["direction"] == "declined"
        assert facts["new_graded_items"] == 5
        assert facts["attention_worthy"] is True  # 14pt drop >= DECLINE_ATTENTION_DELTA

    def test_meaningful_but_moderate_decline_is_not_attention_worthy(self):
        latest = _snap("ECON001", 87, 8, 0)
        baseline = _snap("ECON001", 93, 3, 16)  # -6, meaningful but < 10
        facts = grades.performance_trend("ECON001", latest, [baseline], TODAY)
        assert facts is not None and facts["attention_worthy"] is False

    def test_improvement_detected_and_never_flagged_attention_worthy(self):
        latest = _snap("CMSC001", 95, 8, 0)
        baseline = _snap("CMSC001", 80, 3, 16)
        facts = grades.performance_trend("CMSC001", latest, [baseline], TODAY)
        assert facts["direction"] == "improved"
        assert facts["attention_worthy"] is False

    def test_baseline_too_recent_is_insufficient_evidence(self):
        """Only 5 days of history -- not enough to claim a 'recent trend'
        yet, even if the raw numbers moved."""
        latest = _snap("PHIL001", 70, 6, 0)
        baseline = _snap("PHIL001", 90, 3, 5)
        assert grades.performance_trend(
            "PHIL001", latest, [baseline], TODAY) is None


class TestMissingWorkTrend:
    def _snap_missing(self, label, n, days_ago):
        return grades.snapshot_row(
            label, {"missing_count": n},
            (TODAY - _dt.timedelta(days=days_ago)).isoformat() + "T00:00:00Z")

    def test_no_baseline_is_no_trend(self):
        latest = self._snap_missing("CMSC001", 3, 0)
        assert grades.missing_work_trend("CMSC001", latest, [], TODAY) is None

    def test_small_increase_is_routine_not_a_pattern(self):
        latest = self._snap_missing("CMSC001", 1, 0)
        baseline = self._snap_missing("CMSC001", 0, 18)
        assert grades.missing_work_trend(
            "CMSC001", latest, [baseline], TODAY) is None

    def test_meaningful_increase_flagged_attention_worthy(self):
        latest = self._snap_missing("CMSC001", 3, 0)
        baseline = self._snap_missing("CMSC001", 0, 18)
        facts = grades.missing_work_trend("CMSC001", latest, [baseline], TODAY)
        assert facts["delta"] == 3 and facts["attention_worthy"] is True

    def test_cleared_backlog_is_good_news_not_attention_worthy(self):
        latest = self._snap_missing("CMSC001", 0, 0)
        baseline = self._snap_missing("CMSC001", 4, 18)
        facts = grades.missing_work_trend("CMSC001", latest, [baseline], TODAY)
        assert facts["cleared"] is True and facts["attention_worthy"] is False

    def test_one_to_zero_is_not_a_cleared_backlog_story(self):
        latest = self._snap_missing("CMSC001", 0, 0)
        baseline = self._snap_missing("CMSC001", 1, 18)
        assert grades.missing_work_trend(
            "CMSC001", latest, [baseline], TODAY) is None


# --- workload / assessment cluster ------------------------------------------


def _items_in_window(kind, start_offset, count, span=6):
    return [{"kind": kind, "date": (TODAY + _dt.timedelta(
        days=start_offset + (i % (span + 1)))).isoformat()} for i in range(count)]


class TestWorkloadTrend:
    def test_quiet_semester_no_upcoming_spike_is_no_trend(self):
        items = _items_in_window("assignment", 0, 2)
        assert grades.workload_trend(items, TODAY) is None

    def test_heavy_upcoming_week_vs_quiet_recent_baseline(self):
        items = _items_in_window("assignment", 0, 8)  # next 7 days
        items += _items_in_window("assignment", -14, 2)  # ~2 weeks ago
        items += _items_in_window("assignment", -28, 1)
        facts = grades.workload_trend(items, TODAY)
        assert facts is not None
        assert facts["next_7_days"] == 8

    def test_first_weeks_of_semester_no_baseline_is_insufficient_evidence(self):
        items = _items_in_window("assignment", 0, 8)  # nothing before today
        assert grades.workload_trend(items, TODAY) is None

    def test_still_climbing_true_when_above_last_week_too(self):
        items = _items_in_window("assignment", 0, 8)      # next 7: 8
        items += _items_in_window("assignment", -7, 4)    # prior 7: 4
        items += _items_in_window("assignment", -28, 1)
        facts = grades.workload_trend(items, TODAY)
        assert facts["still_climbing"] is True
        assert "up" in grades.workload_note_text(facts).lower()

    def test_real_state_case_lighter_than_last_week_is_no_trend(self):
        """The real account, 2026-09-19: next 7 days = 11, previous 7 = 16.
        The old baseline (pre-semester zeros, last week excluded) called
        that "stays heavy (recent average ~2.2)". Against the weeks the data
        actually covers, a week lighter than the last is not a trend."""
        items = _items_in_window("assignment", 0, 11)     # next 7: 11
        items += _items_in_window("assignment", -7, 16)   # prior 7: 16
        items += _items_in_window("assignment", -14, 6)
        items += _items_in_window("assignment", -21, 5)
        assert grades.workload_trend(items, TODAY) is None

    def test_elevated_but_not_climbing_is_phrased_honestly(self):
        items = _items_in_window("assignment", 0, 11)     # next 7: 11
        items += _items_in_window("assignment", -7, 12)   # prior 7: 12
        items += _items_in_window("assignment", -28, 1)   # quiet older weeks
        facts = grades.workload_trend(items, TODAY)
        assert facts is not None
        assert facts["still_climbing"] is False
        text = grades.workload_note_text(facts)
        assert "up" not in text.lower()
        assert "12" in text and "11" in text

    def test_weeks_before_the_first_known_item_are_not_quiet_weeks(self):
        items = _items_in_window("assignment", 0, 8)
        items += _items_in_window("assignment", -7, 7)
        # data starts 7 days ago: only one covered baseline week -> too thin
        assert grades.workload_trend(items, TODAY) is None


class TestAssessmentClusterTrend:
    def test_typical_pace_is_not_a_cluster(self):
        # 2/week every week for 8 weeks, including next week -- normal.
        items = []
        for wk in range(-8, 2):
            items += [{"kind": "assessment", "date": (TODAY + _dt.timedelta(
                days=wk * 7 + d)).isoformat()} for d in (1, 3)]
        assert grades.assessment_cluster_trend(items, TODAY) is None

    def test_real_cluster_detected(self):
        items = [{"kind": "assessment", "date": (TODAY - _dt.timedelta(
            days=d)).isoformat()} for d in (60, 50, 40)]  # sparse semester so far
        items += [{"kind": "assessment", "date": (TODAY + _dt.timedelta(
            days=d)).isoformat()} for d in (2, 3, 5)]  # 3 in one upcoming week
        facts = grades.assessment_cluster_trend(items, TODAY)
        assert facts is not None and facts["count"] == 3

    def test_no_assessments_at_all_is_not_a_cluster(self):
        assert grades.assessment_cluster_trend([], TODAY) is None


class TestCourseUpcomingLoad:
    def test_counts_only_the_named_course(self):
        items = [
            {"kind": "assignment", "course_label": "PHIL001",
             "date": (TODAY + _dt.timedelta(days=1)).isoformat()},
            {"kind": "assignment", "course_label": "MATH001",
             "date": (TODAY + _dt.timedelta(days=1)).isoformat()},
            {"kind": "event", "course_label": "PHIL001",
             "date": (TODAY + _dt.timedelta(days=1)).isoformat()},  # not gradable
        ]
        assert grades.course_upcoming_load(items, "PHIL001", TODAY) == 1


# --- synthetic attention items ----------------------------------------------


class TestInsightItems:
    def test_performance_attention_item_shape(self):
        facts = grades.performance_trend(
            "PHIL001", _snap("PHIL001", 78, 8, 0), [_snap("PHIL001", 92, 3, 16)], TODAY)
        item = grades.performance_attention_item(facts, "phil", TODAY)
        assert item["needs_attention"] is True
        assert item["course_label"] == "PHIL001"
        assert item["id"] == "grade-decline-phil001"
        assert item["status"] == "new" and item["times_surfaced"] == 0

    def test_insight_ids_are_stable_across_runs_same_course(self):
        """Not date-suffixed -- unlike lifecycle.skip_row's per-run notice,
        an insight should carry the SAME id run to run so status/
        times_surfaced carry over via the ordinary attention machinery."""
        facts = grades.performance_trend(
            "PHIL001", _snap("PHIL001", 78, 8, 0), [_snap("PHIL001", 92, 3, 16)], TODAY)
        a = grades.performance_attention_item(facts, "phil", TODAY)
        b = grades.performance_attention_item(facts, "phil",
                                              TODAY + _dt.timedelta(days=1))
        assert a["id"] == b["id"]

    def test_combined_risk_item_only_fires_on_decline_plus_busy_week(self):
        decline = grades.performance_trend(
            "PHIL001", _snap("PHIL001", 80, 5, 0), [_snap("PHIL001", 90, 3, 16)], TODAY)
        improved = grades.performance_trend(
            "PHIL001", _snap("PHIL001", 95, 5, 0), [_snap("PHIL001", 80, 3, 16)], TODAY)
        assert grades.combined_risk_item(decline, 3, "phil", TODAY) is not None
        assert grades.combined_risk_item(decline, 1, "phil", TODAY) is None  # not busy enough
        assert grades.combined_risk_item(improved, 3, "phil", TODAY) is None  # not a decline
        assert grades.combined_risk_item(None, 3, "phil", TODAY) is None

    def test_cluster_attention_item_is_cross_course_system_row(self):
        facts = {"week_of": "2026-09-22", "count": 3, "semester_weekly_avg": 0.7}
        item = grades.cluster_attention_item(facts, TODAY)
        assert item["course_label"] == "System" and item["course_class"] == "none"
        assert item["needs_attention"] is True
