"""
Grades + trends (2026-09-19). Deterministic-only: every function here takes
its data as explicit arguments and returns plain dicts/lists/numbers --
nothing in this file calls an LLM, matches PROJECT_INSTRUCTIONS.md's rule
that factual calculations and historical comparisons stay deterministic
Python (`service/orchestrator.py`'s grades step is where an LLM MAY be used,
and only for phrasing a fact this module already computed, never to compute
the fact itself).

What this reads
----------------
`canvas_shadow.load_canvas_scrape()`'s per-course bundle -- specifically
`bundle["assignments"]`, which already carries Canvas's own per-submission
`completion_state` (graded/missing/submitted/not_started/excused/locked/
external_action_required -- see canvas-scraper's status/resolver.py) and
`course["current_score"/"current_grade"/"final_score"/"final_grade"]`.

A graded QUIZ also appears in `bundle["assignments"]` (Canvas represents it
as an Assignment with `is_quiz_assignment: true` -- confirmed empirically
against the real account, 2026-09-19: every quiz with a non-null
`assignment_id` has a matching entry in `assignments`). Iterating
`bundle["quizzes"]` as well would double-count every graded quiz, so this
module only ever reads `bundle["assignments"]` for grade/completion
statistics -- `bundle["quizzes"]` is Canvas's own reflection of the SAME
underlying grade for those objects, not additional information.

Never recomputes a course grade. `current_score`/`current_grade` (and
`final_score`/`final_grade`) are copied verbatim from Canvas's own
`computed_current_score`/`computed_current_grade` fields -- reimplementing
Canvas's weighted/dropped-lowest math here would be exactly the kind of
invented number PROJECT_INSTRUCTIONS.md §1.6 and the pasted task both
forbid ("never present an estimate as an official Canvas grade"). What this
module DOES compute itself -- graded/gradable/missing counts, and every
trend -- is arithmetic over Canvas's own per-item facts, not a re-derivation
of the grade itself.
"""

from __future__ import annotations

import datetime as _dt
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from render_briefing import COURSE_PAIRS  # noqa: E402

# §2.1's five real academic classes -- CS-Advising/UMD/Personal/Campus/
# System/Lead never carry a Canvas grade. Derived, not hardcoded again, for
# the same reason lifecycle.py derives MY_CLASS_CLASSES this way: this file
# and the gate's own course-pairing check can never silently name a
# different five classes.
REAL_COURSE_CLASSES = set(COURSE_PAIRS) - {"advising", "umd", "personal", "none"}

# How far back "recent" reaches for the performance/missing-work trend
# baseline. Chosen to span roughly two weeks of coursework -- long enough
# that a single day's regrade doesn't look like a trend, short enough that
# "recent" still means something at UMD's ~15-week semester scale.
TREND_BASELINE_DAYS = 14
# A course needs at least this many graded items before ANY score number
# from it is trusted for a trend claim -- a 100% built from one assignment
# is real (Canvas said so) but not evidence of anything yet.
MIN_GRADED_FOR_TREND = 3
# A score move smaller than this many percentage points is well within the
# kind of run-to-run noise a single re-graded assignment can cause; §5.5's
# own Heavy Day rationale (weighted load, not a raw count) is the same
# "don't fire on noise" principle applied here to a percentage instead of an
# item count.
MEANINGFUL_SCORE_DELTA = 5.0
DECLINE_ATTENTION_DELTA = 10.0
# Missing-work deltas smaller than this are a single assignment coming due
# and not yet turned in -- not a pattern.
MEANINGFUL_MISSING_DELTA = 2


def _course_completion_stats(bundle):
    """(gradable, graded, missing) assignment lists for one course bundle.
    `gradable` = published, points_possible > 0 -- the only assignments
    Canvas's own current_score denominator could plausibly include; this is
    used only for OUR OWN counts (§ above never recomputes the grade).

    Reads `submission.score`/`.workflow_state`/`.missing`/`.excused`
    directly rather than trusting canvas-scraper's own `completion_state`
    for this -- verified against the real account, 2026-09-19: two ENGL001
    assignments with a real posted score (submission.score == 10.0, out of
    10) came back `completion_state: "locked"`, not `"graded"`, because
    canvas-scraper's resolver checks `locked_for_user` FIRST (status/
    resolver.py::resolve_assignment) -- correct for canvas-scraper's own
    question ("can the student still act on this"), wrong for ours ("is
    this graded"). A scored-but-now-locked assignment (grading period
    closed, resubmission window shut, whatever the reason) is graded work,
    full stop, and Canvas's own submission object already says so
    unambiguously (docs/FINDINGS.md: "sourced directly from Canvas's own
    submission object, never overridden by our own inference") -- reading
    it directly here sidesteps completion_state's different priority order
    entirely rather than trying to special-case LOCKED.
    """
    gradable, graded, missing = [], [], []
    for a in bundle.get("assignments") or []:
        pts = a.get("points_possible") or 0
        if not a.get("published") or pts <= 0:
            continue
        sub = a.get("submission") or {}
        if sub.get("excused"):
            continue  # excused work counts toward neither side of the ratio
        gradable.append(a)
        if sub.get("score") is not None or sub.get("workflow_state") == "graded":
            graded.append(a)
        elif sub.get("missing"):
            missing.append(a)
    return gradable, graded, missing


def course_facts(bundle):
    """One course bundle -> a flat facts dict. Never raises on a malformed
    bundle -- a missing/odd field degrades to None/0, matching this
    project's "a data problem for the caller to report, not a crash"
    convention (lifecycle.as_date, etc.)."""
    course = bundle.get("course") or {}
    gradable, graded, missing = _course_completion_stats(bundle)
    return {
        "current_score": course.get("current_score"),
        "current_grade": course.get("current_grade"),
        "final_score": course.get("final_score"),
        "final_grade": course.get("final_grade"),
        "graded_count": len(graded),
        "gradable_count": len(gradable),
        "missing_count": len(missing),
    }


def attribute_courses(scrape, known_courses, normalize_course):
    """scrape dict -> {course_label: course_facts(...)}, scoped to courses
    this account actually tracks (§2.1's closed set) -- the same
    known-courses filter `orchestrator._canvas_scraper_candidates` already
    applies to dated candidates, applied here to grade data for the same
    reason: canvas-scraper also discovers onboarding/advising shells that
    were never configured as a tracked course.

    `normalize_course` is `orchestrator._normalize_course` (passed in
    rather than imported, so this module never imports orchestrator --
    orchestrator imports this module, not the other way around).
    """
    out = {}
    for bundle in scrape.get("courses") or []:
        course = bundle.get("course") or {}
        code = course.get("course_code") or course.get("name") or ""
        label = normalize_course(code, {"courses": known_courses})
        info = known_courses.get(label)
        if info is None or info.get("class") not in REAL_COURSE_CLASSES:
            continue
        out[label] = course_facts(bundle)
    return out


def snapshot_row(course_label, facts, captured_at):
    return {
        "captured_at": captured_at,
        "course_label": course_label,
        "current_score": facts.get("current_score"),
        "current_grade": facts.get("current_grade"),
        "final_score": facts.get("final_score"),
        "final_grade": facts.get("final_grade"),
        "graded_count": facts.get("graded_count") or 0,
        "gradable_count": facts.get("gradable_count") or 0,
        "missing_count": facts.get("missing_count") or 0,
        "source": "canvas_scraper",
    }


def grade_display_text(facts, as_of=None, today=None):
    """The one line 'Where you stand' shows per course -- honest about what
    kind of number it is, per the pasted task's rule: never present an
    estimate as an official grade, and never a bare percentage that implies
    more confidence than the sample size backs.

    Always Canvas's own `current_score` (graded work only), never
    `final_score` (ungraded-counts-as-zero) -- `final_score` is Canvas's
    own official field too, but early in a term it is mostly a statement
    about how little has been graded yet, not a forecast, and showing e.g.
    "6%" in September would be exactly the alarmist, misleading framing the
    task asks this feature to avoid.

    `as_of`/`today`: when the figure isn't from today's own read (Canvas
    was unreachable this run and this is the last snapshot on file --
    "preserve previously known data ... rather than replacing it with
    blanks or zeros"), the date it WAS observed is appended so a carried-
    over number is never mistaken for a fresh one (§9.1's "stale data is
    never presented as current").
    """
    gradable = facts.get("gradable_count") or 0
    graded = facts.get("graded_count") or 0
    if gradable == 0:
        return ""
    if graded == 0:
        text = "not yet graded (0 of %d)" % gradable
    else:
        score = facts.get("current_score")
        text = ("%d of %d graded" % (graded, gradable) if score is None else
                "%s%% (%d of %d graded)" % (
                    ("%.1f" % score).rstrip("0").rstrip("."), graded, gradable))
    if as_of and today and as_of != today.isoformat():
        try:
            d = _dt.date.fromisoformat(str(as_of)[:10])
            text += " as of %s %d" % (d.strftime("%b"), d.day)
        except ValueError:
            text += " as of %s" % as_of
    return text


def latest_snapshot(label, fresh_facts, history_rows, captured_at):
    """The freshest-available snapshot for one course: a real facts dict
    from THIS run if Canvas was read successfully, else the newest row
    already in `course_grade_snapshots` -- "preserve previously known data
    when a new retrieval fails rather than replacing it with blanks or
    zeros" (the pasted task, verbatim). Returns (row_dict, stale) where
    `row_dict` has the same shape as `snapshot_row()`'s output (or the raw
    DB row, which carries the same column names) and `stale=True` means the
    row is NOT from this run.
    """
    if label in fresh_facts:
        return snapshot_row(label, fresh_facts[label], captured_at), False
    if history_rows:
        return history_rows[-1], True
    return None, False


# --- trend computation -------------------------------------------------


def _nearest_before(rows, cutoff_date):
    """The most recent snapshot row with captured_at on or before
    `cutoff_date` -- "the baseline from about N days ago", tolerant of a run
    never having happened on the exact day (weekends, a skipped run,
    Canvas being briefly down)."""
    best = None
    for r in rows:
        d = _dt.date.fromisoformat(r["captured_at"][:10])
        if d <= cutoff_date and (best is None or d > best[1]):
            best = (r, d)
    return best[0] if best else None


def performance_trend(course_label, latest, history_rows, today):
    """Meaningful, evidence-backed score change since ~TREND_BASELINE_DAYS
    ago -- "meaningful improvement or decline across multiple graded items",
    never fired from a single re-graded assignment or a course recalculating
    its total. Returns a facts dict or None (no trend, or not enough
    evidence -- never treated as decline per the task's "missing data is
    not deterioration" rule).
    """
    if latest is None or (latest.get("graded_count") or 0) < MIN_GRADED_FOR_TREND:
        return None
    if latest.get("current_score") is None:
        return None
    baseline = _nearest_before(
        history_rows, today - _dt.timedelta(days=TREND_BASELINE_DAYS))
    if baseline is None or baseline.get("current_score") is None:
        return None
    new_evidence = (latest.get("graded_count") or 0) - (baseline.get("graded_count") or 0)
    if new_evidence < 2:
        return None  # the score could only have moved from noise/recalculation
    delta = latest["current_score"] - baseline["current_score"]
    if abs(delta) < MEANINGFUL_SCORE_DELTA:
        return None
    return {
        "course_label": course_label,
        "delta": delta,
        "direction": "improved" if delta > 0 else "declined",
        "from_score": baseline["current_score"],
        "to_score": latest["current_score"],
        "since_date": baseline["captured_at"][:10],
        "new_graded_items": new_evidence,
        "attention_worthy": delta <= -DECLINE_ATTENTION_DELTA,
    }


def missing_work_trend(course_label, latest, history_rows, today):
    """Missing-work count increasing or clearing since ~TREND_BASELINE_DAYS
    ago. Returns a facts dict or None."""
    if latest is None:
        return None
    baseline = _nearest_before(
        history_rows, today - _dt.timedelta(days=TREND_BASELINE_DAYS))
    if baseline is None:
        return None
    now_n = latest.get("missing_count") or 0
    then_n = baseline.get("missing_count") or 0
    delta = now_n - then_n
    if delta == 0:
        return None
    if delta > 0 and delta < MEANINGFUL_MISSING_DELTA:
        return None
    if delta < 0 and then_n < MEANINGFUL_MISSING_DELTA:
        return None  # e.g. 1 -> 0 is routine, not a "cleared backlog" story
    return {
        "course_label": course_label,
        "delta": delta,
        "now": now_n,
        "then": then_n,
        "since_date": baseline["captured_at"][:10],
        "cleared": delta < 0 and now_n == 0,
        "attention_worthy": delta >= MEANINGFUL_MISSING_DELTA and now_n >= 3,
    }


_GRADABLE_KINDS = ("assignment", "assessment")


def _window_count(items, start, end):
    n = 0
    for it in items:
        if it.get("kind") not in _GRADABLE_KINDS:
            continue
        d = it.get("date")
        if not d:
            continue
        try:
            dd = _dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        if start <= dd < end:
            n += 1
    return n


def _earliest_gradable_date(items):
    best = None
    for it in items:
        if it.get("kind") not in _GRADABLE_KINDS or not it.get("date"):
            continue
        try:
            d = _dt.date.fromisoformat(str(it["date"])[:10])
        except ValueError:
            continue
        best = d if best is None or d < best else best
    return best


# Weeks of history the baseline may use. Kept inside lifecycle's 35-day
# delete horizon: a window older than that is undercounted by pruning, not
# genuinely quiet.
WORKLOAD_BASELINE_WEEKS = 4


def workload_trend(items, today):
    """"8 graded items due in the next 7 days, vs. 4 in the last 7" -- fired
    only when the coming week is heavy against a typical week SO FAR.

    The baseline is the mean of the last WORKLOAD_BASELINE_WEEKS full weeks
    (the previous 7 days included) that the data actually covers -- a week
    that starts before the earliest known gradable item is before the term
    (or before this pipeline's memory), not a week with zero work. The
    2026-09-19 audit found the old baseline averaged in exactly those empty
    pre-semester weeks and excluded last week, so it reported "recent
    average ~2.2" beside weeks of 11 and 16 and fired on a week that was
    actually lighter than the one before. Returns a facts dict or None.
    """
    next7 = _window_count(items, today, today + _dt.timedelta(days=7))
    prior7 = _window_count(items, today - _dt.timedelta(days=7), today)
    first = _earliest_gradable_date(items)
    if first is None:
        return None
    baseline_weeks = []
    for n in range(WORKLOAD_BASELINE_WEEKS):
        start = today - _dt.timedelta(days=7 * (n + 1))
        if start < first:
            break
        baseline_weeks.append(_window_count(
            items, start, today - _dt.timedelta(days=7 * n)))
    # "Do not claim a trend when there is not enough evidence": at least two
    # real weeks of history, and not all of them empty.
    if len(baseline_weeks) < 2 or not any(baseline_weeks):
        return None
    baseline_mean = statistics.mean(baseline_weeks)
    threshold = max(3, round(0.5 * baseline_mean))
    if next7 < 4 or (next7 - baseline_mean) < threshold:
        return None
    return {
        "next_7_days": next7,
        "previous_7_days": prior7,
        "still_climbing": next7 > prior7,
        "baseline_mean": round(baseline_mean, 1),
        "baseline_weeks": len(baseline_weeks),
    }


def assessment_cluster_trend(items, today):
    """A week, within the next 14 days, whose assessment (quiz/exam) count
    is both a real cluster (>=3) and clearly above the semester's own
    typical pace -- avoids flagging "2 quizzes this week" in a course that
    always has 2 quizzes a week. Returns a facts dict or None."""
    assessments = [it for it in items if it.get("kind") == "assessment"
                   and it.get("date")]
    dated = []
    for it in assessments:
        try:
            dated.append(_dt.date.fromisoformat(str(it["date"])[:10]))
        except ValueError:
            continue
    if not dated:
        return None
    earliest = min(dated)
    weeks_elapsed = max(1, (today - earliest).days / 7.0)
    semester_weekly_avg = len(dated) / weeks_elapsed

    best = None
    for offset in range(0, 14):
        start = today + _dt.timedelta(days=offset)
        end = start + _dt.timedelta(days=7)
        n = sum(1 for d in dated if start <= d < end)
        if n >= 3 and n >= 2 * semester_weekly_avg:
            if best is None or n > best[1]:
                best = (start, n)
    if best is None:
        return None
    start, n = best
    return {
        "week_of": start.isoformat(), "count": n,
        "semester_weekly_avg": round(semester_weekly_avg, 1),
    }


def course_upcoming_load(items, course_label, today, days=7):
    """How many of THIS course's own assignment/assessment items are due
    in the next `days` -- the per-course half of "a concerning combination
    of upcoming assessments and recent performance" (workload_trend/
    assessment_cluster_trend above are cross-course)."""
    end = today + _dt.timedelta(days=days)
    n = 0
    for it in items:
        if it.get("kind") not in _GRADABLE_KINDS:
            continue
        if it.get("course_label") != course_label:
            continue
        d = it.get("date")
        if not d:
            continue
        try:
            dd = _dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        if today <= dd < end:
            n += 1
    return n


# --- note/insight text (deterministic; the LLM touch in orchestrator.py
# only ever rephrases one of these, never invents beyond them) -----------


def _fmt_pct(score):
    return ("%.1f" % score).rstrip("0").rstrip(".")


def performance_note_text(facts):
    d = facts
    verb = "improved" if d["direction"] == "improved" else "declined"
    return ("%s: grade %s %s points (%s%% → %s%%) over %d newly graded "
            "item%s since %s." % (
                d["course_label"], verb, _fmt_pct(abs(d["delta"])),
                _fmt_pct(d["from_score"]), _fmt_pct(d["to_score"]),
                d["new_graded_items"], "" if d["new_graded_items"] == 1 else "s",
                d["since_date"]))


def missing_note_text(facts):
    d = facts
    if d["cleared"]:
        return ("%s: missing work cleared — %d item%s outstanding on "
                "%s, none now." % (d["course_label"], d["then"],
                                    "" if d["then"] == 1 else "s", d["since_date"]))
    verb = "up" if d["delta"] > 0 else "down"
    return ("%s: missing work %s to %d item%s (was %d) since %s." % (
        d["course_label"], verb, d["now"], "" if d["now"] == 1 else "s",
        d["then"], d["since_date"]))


def workload_note_text(facts):
    d = facts
    n = d["next_7_days"]
    plural = "" if n == 1 else "s"
    typical = "a typical week so far had ~%d" % round(d["baseline_mean"])
    if d["still_climbing"]:
        return ("Workload up: %d graded item%s due in the next 7 days, vs. "
                "%d in the last 7 (%s)." % (n, plural, d["previous_7_days"],
                                            typical))
    return ("Workload stays heavy: %d graded item%s due in the next 7 days "
            "after %d in the last 7 (%s)." % (n, plural, d["previous_7_days"],
                                              typical))


def cluster_note_text(facts):
    d = facts
    when = _dt.date.fromisoformat(d["week_of"])
    return ("Assessment cluster: %d exams/quizzes in the week of %s "
            "(typical week has about %s)." % (
                d["count"], when.strftime("%b %-d"), _fmt_pct(d["semester_weekly_avg"])))


# --- synthetic attention items (skip_row-shaped; see lifecycle.skip_row) -


def _insight_item(item_id, course_class, course_label, today, title, detail, notes):
    """Same shape lifecycle.skip_row() uses for a System housekeeping row:
    `kind: "event"` (§3.3's enum is closed; routing never reads kind for an
    attention row, `needs_attention` sends it there directly), `status:
    "new"`/`times_surfaced: 0` as the DEFAULT -- the caller (orchestrator's
    grades step) overwrites both from the matching prior-run item by id
    when one exists, so the normal 5-surface auto-dismiss/carry-over
    machinery applies to these exactly as it does to any other attention
    row. Deliberately a STABLE id (no date suffix): unlike skip_row's
    per-run truncation notice, an insight is a standing claim about the
    account that should fade via the ordinary surface counter, not reset to
    "new" every single day it happens to still be true.
    """
    return {
        "id": item_id, "kind": "event", "course_class": course_class,
        "course_label": course_label, "regime": "confirmed", "status": "new",
        "date": today.isoformat(), "times_surfaced": 0, "needs_attention": True,
        "title": title, "detail": detail, "notes": notes,
    }


def performance_attention_item(facts, course_class, today):
    d = facts
    return _insight_item(
        "grade-decline-%s" % d["course_label"].lower(), course_class,
        d["course_label"], today,
        title="%s grade has declined" % d["course_label"],
        detail="%s%% → %s%% since %s" % (
            _fmt_pct(d["from_score"]), _fmt_pct(d["to_score"]), d["since_date"]),
        notes=performance_note_text(d) + " Not a single low score -- this "
              "is the trend across everything graded in that span.")


def missing_attention_item(facts, course_class, today):
    d = facts
    return _insight_item(
        "missing-work-%s" % d["course_label"].lower(), course_class,
        d["course_label"], today,
        title="Missing work is accumulating in %s" % d["course_label"],
        detail="%d item%s missing, up from %d since %s" % (
            d["now"], "" if d["now"] == 1 else "s", d["then"], d["since_date"]),
        notes=missing_note_text(d))


def cluster_attention_item(facts, today):
    """Cross-course, so `course_label`/`course_class` follow lifecycle's
    System-housekeeping-row convention (see lifecycle.skip_row) rather than
    naming one course -- a cluster is about the calendar, not any single
    class."""
    d = facts
    return _insight_item(
        "assessment-cluster-%s" % d["week_of"], "none", "System", today,
        title="Assessment cluster ahead: week of %s" % (
            _dt.date.fromisoformat(d["week_of"]).strftime("%b %-d")),
        detail="%d exams/quizzes that week" % d["count"],
        notes=cluster_note_text(d))


def combined_risk_item(perf_facts, upcoming_count, course_class, today):
    """"A concerning combination of upcoming assessments and recent
    performance" -- fires on a real (not necessarily attention_worthy-
    severity on its own) decline PLUS a genuinely busy week ahead in that
    SAME course. One combined item, not two separate notes -- the caller
    is responsible for not also emitting `performance_attention_item`/a
    plain performance portfolio note for this course when this fires."""
    if perf_facts is None or perf_facts["direction"] != "declined":
        return None
    if upcoming_count < 2:
        return None
    d = perf_facts
    return _insight_item(
        "grade-risk-%s" % d["course_label"].lower(), course_class,
        d["course_label"], today,
        title="%s: busy week, and recent grades are down" % d["course_label"],
        detail="%d graded item%s due in the next 7 days; grade %s%% → "
               "%s%% since %s" % (
                   upcoming_count, "" if upcoming_count == 1 else "s",
                   _fmt_pct(d["from_score"]), _fmt_pct(d["to_score"]), d["since_date"]),
        notes="Recent performance and upcoming workload both point the "
              "same direction here -- worth a closer look before the next "
              "few items are due.")
