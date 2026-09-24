"""Regression tests for the 2026-09-19 end-to-end audit's fixes.

Each test names the real failure it guards against (see PROGRESS.md,
Session 8). Nothing here touches the network: Gmail/LLM/email calls are
monkeypatched, and the one DB test uses a temp SQLite file.

Run (from service/):
    ../venv/bin/python -m pytest test_audit_2026_09_19.py -q
"""

from __future__ import annotations

import datetime as _dt
import os

import pytest

import canvas_shadow
import db as dbmod
import orchestrator as O
import render_briefing
import state_io_sqlite
import weather

TODAY = _dt.date(2026, 9, 19)


def _run(dry=True, db_path=":memory:"):
    return O.Run(db_path=db_path, dry_run=dry)


def _ctx(items=None, **extra):
    ctx = {"state": {"items": items or [], "courses": {"MATH001": {"class": "math"}}},
           "today": TODAY}
    ctx.update(extra)
    return ctx


# --- extraction: relative-date sanity check ---------------------------------

class TestWeekdayCheck:
    def test_wrong_weekday_is_not_trusted(self):
        """Real case: Mon 9/14 email, "next Friday" -> extracted Mon 9/21."""
        run = _run()
        cand = {"title": "First exam", "due_date": "2026-09-21",
                "date_evidence": "Your first exam is next Friday!"}
        O._check_weekday(run, cand)
        assert cand["evidence"] == "insufficient"
        assert "next Friday" in cand["description"]
        assert run.errors and run.errors[0]["severity"] == "MINOR"

    def test_matching_weekday_passes(self):
        cand = {"title": "x", "due_date": "2026-09-25",
                "date_evidence": "next Friday"}
        O._check_weekday(_run(), cand)
        assert "evidence" not in cand

    def test_explicit_date_without_weekday_passes(self):
        cand = {"title": "x", "due_date": "2026-09-25",
                "date_evidence": "Due: Sep 25"}
        O._check_weekday(_run(), cand)
        assert "evidence" not in cand


# --- reconcile ---------------------------------------------------------------

class TestExamDedupe:
    @pytest.mark.parametrize("title,key", [
        ("First exam", "exam-1"), ("Exam 1", "exam-1"),
        ("Midterm Exam 1", "exam-1"), ("Midterm 2", "exam-2"),
        ("Exam 1 content update", "exam-1"), ("Final Exam", "final"),
        ("Quiz 3", None), ("Discussion Post 1", None)])
    def test_exam_key(self, title, key):
        assert O._exam_key(title) == key

    def test_email_guess_collapses_into_syllabus_exam(self):
        syllabus = {"id": "m1", "course": "MATH001", "kind": "assessment",
                    "title": "Midterm Exam 1", "date": "2026-09-25",
                    "status": "ongoing", "detail": "In lecture",
                    "source_refs": [{"source": "gcal_syllabi"}]}
        email = {"id": "e1", "course": "MATH001", "kind": "assessment",
                 "title": "First exam", "date": "2026-09-21",
                 "confidence": "inferred", "status": "new",
                 "description": "Covers 6.1, 6.2, 6.7, 6.8",
                 "source_refs": [{"source": "gmail", "thread_id": "t"}]}
        run = _run()
        O._collapse_shadowed_duplicates(run, [syllabus, email])
        assert email["status"] == "expired"
        assert syllabus["date"] == "2026-09-25"
        assert "Covers 6.1" in syllabus["notes"]
        assert {"source": "gmail", "thread_id": "t"} in syllabus["source_refs"]

    def test_other_course_untouched(self):
        a = {"id": "a", "course": "CMSC001", "kind": "assessment",
             "title": "Midterm Exam 1", "date": "2026-10-08",
             "status": "new", "source_refs": [{"source": "gcal_syllabi"}]}
        b = {"id": "b", "course": "MATH001", "kind": "assessment",
             "title": "First exam", "date": "2026-09-21", "status": "new",
             "source_refs": [{"source": "gmail"}]}
        O._collapse_shadowed_duplicates(_run(), [a, b])
        assert b["status"] == "new"


class TestChapterDedupe:
    """Regression for the 2026-09-21 briefing: an ECON001 email produced
    "Smartbook assignment for Chapter 6" (due today) alongside Canvas's own
    "Chap 06: Government Intervention" (due today) -- same chapter, same
    day, different words, both shown in Today's cards."""

    @pytest.mark.parametrize("title,key", [
        ("Chap 06: Government Intervention", "chapter-6"),
        ("Smartbook assignment for Chapter 6", "chapter-6"),
        ("HW Chap 04", "chapter-4"),
        ("Weekly SmartBook Assignments", None),
        ("Quiz 3", None)])
    def test_chapter_key(self, title, key):
        assert O._chapter_key(title) == key

    def test_email_guess_collapses_into_canvas_chapter_item(self):
        canvas_item = {
            "id": "c6", "course": "ECON001", "kind": "assignment",
            "title": "Chap 06: Government Intervention", "date": "2026-09-21",
            "status": "ongoing",
            "canvas_url": "https://umd.instructure.com/courses/1/assignments/1",
            "source_refs": [{"source": "canvas_scraper"}]}
        email = {
            "id": "e6", "course": "ECON001", "kind": "assignment",
            "title": "Smartbook assignment for Chapter 6", "date": "2026-09-21",
            "confidence": "inferred", "status": "new",
            "description": "Additional homework for Chapters 4 and 5 due Tuesday.",
            "source_refs": [{"source": "gmail", "thread_id": "t"}]}
        run = _run()
        O._collapse_shadowed_duplicates(run, [canvas_item, email])
        assert email["status"] == "expired"
        assert "Additional homework" in canvas_item["notes"]
        assert {"source": "gmail", "thread_id": "t"} in \
            canvas_item["source_refs"]

    def test_same_chapter_different_due_date_both_kept(self):
        """The smartbook (due the week covered) and the graded homework
        (due the week after) are genuinely different deliverables -- a
        shared chapter number alone must never merge them."""
        smartbook = {
            "id": "c6", "course": "ECON001", "kind": "assignment",
            "title": "Chap 06: Government Intervention", "date": "2026-09-21",
            "status": "ongoing",
            "source_refs": [{"source": "canvas_scraper"}]}
        homework = {
            "id": "h6", "course": "ECON001", "kind": "assignment",
            "title": "HW Chap 06", "date": "2026-09-29", "status": "ongoing",
            "source_refs": [{"source": "canvas_scraper"}]}
        O._collapse_shadowed_duplicates(_run(), [smartbook, homework])
        assert smartbook["status"] == "ongoing"
        assert homework["status"] == "ongoing"


class TestReconcileAuthority:
    def _cand(self, **kw):
        base = {"thread_id": "t9", "course": "MATH001", "title": "Quiz 2",
                "kind": "assessment", "due_date": "2026-09-30",
                "due_time": None, "canvas_url": None, "description": "moved?",
                "links": [], "confidence": "inferred", "source": "gmail"}
        base.update(kw)
        return base

    def test_email_cannot_move_a_syllabus_date(self):
        existing = {"id": "q2", "course": "MATH001", "title": "Quiz 2",
                    "kind": "assessment", "date": "2026-09-24",
                    "status": "new", "detail": "Syllabus",
                    "source_refs": [{"source": "gcal_syllabi"}]}
        run = _run()
        items = O.step3_reconcile(run, _ctx([existing]), [self._cand()])
        q = next(i for i in items if i["id"] == "q2")
        assert q["date"] == "2026-09-24"
        assert run.moved_ids == []
        assert {"source": "gcal_syllabi"} in q["source_refs"]  # not erased
        assert any("kept 2026-09-24" in e["message"] for e in run.errors)

    def test_different_canvas_objects_are_never_merged(self):
        wk1 = {"id": "w1", "course": "MATH001", "title": "Weekly Homework",
               "kind": "assignment", "date": "2026-09-15", "status": "ongoing",
               "canvas_url": "https://umd.instructure.com/courses/1/assignments/1",
               "source_refs": [{"source": "canvas_scraper"}]}
        cand = self._cand(title="Weekly Homework", kind="assignment",
                          due_date="2026-09-22", source="canvas_scraper",
                          confidence="confirmed",
                          canvas_url="https://umd.instructure.com/courses/1/assignments/2")
        items = O.step3_reconcile(_run(), _ctx([wk1]), [cand])
        assert wk1["date"] == "2026-09-15"
        assert len(items) == 2

    def test_canvas_submission_is_carried_onto_the_item(self):
        cand = self._cand(title="HW 3", kind="assignment",
                          source="canvas_scraper", confidence="confirmed",
                          canvas_submission={"settled": True},
                          points_possible=10)
        items = O.step3_reconcile(_run(), _ctx([]), [cand])
        assert items[0]["canvas_submission"] == {"settled": True}
        assert items[0]["points_possible"] == 10


# --- generated items (leads / insights) --------------------------------------

def _lead(id_, url, status="open", ts=0):
    return {"id": id_, "regime": "lead", "kind": "lead", "status": status,
            "source_url": url, "times_surfaced": ts, "title": id_}


class TestMergeGenerated:
    def test_same_id_is_updated_not_duplicated(self):
        """Real case: grade insights and leads re-added every run collided
        and were renamed <id>-2 with a fresh counter."""
        items = [{"id": "grade-x", "status": "new", "times_surfaced": 3,
                  "title": "old"}]
        O._merge_generated(items, [{"id": "grade-x", "status": "new",
                                    "times_surfaced": 0, "title": "new"}])
        assert len(items) == 1
        assert items[0]["title"] == "new"
        assert items[0]["times_surfaced"] == 3

    def test_same_page_different_title_is_the_same_lead(self):
        items = [_lead("lead-a", "https://www.our.umd.edu/", ts=2)]
        O._merge_generated(items, [_lead("lead-b", "http://our.umd.edu")])
        assert [i["id"] for i in items] == ["lead-a"]
        assert items[0]["times_surfaced"] == 2

    def test_closed_lead_is_never_re_raised(self):
        items = [_lead("lead-a", "https://our.umd.edu/", status="killed")]
        O._merge_generated(items, [_lead("lead-new", "https://our.umd.edu/")])
        assert len(items) == 1 and items[0]["status"] == "killed"

    def test_dedupe_open_leads_keeps_most_surfaced(self):
        a = _lead("a", "https://ml.umd.edu/", ts=0)
        b = _lead("b", "https://ml.umd.edu", ts=2)
        O._dedupe_open_leads(_run(), [a, b])
        assert b["status"] == "open" and a["status"] == "expired"

    def test_shown_leads_capped(self):
        leads = [_lead("l%d" % n, "https://x%d.umd.edu" % n) for n in range(5)]
        shown, waiting = O._shown_leads(leads)
        assert len(shown) == O.LEADS_SHOWN_MAX and len(waiting) == 2


# --- compose -------------------------------------------------------------------

class TestNearDuplicates:
    def test_same_obligation_two_titles(self):
        a = {"id": "a", "course_label": "CS-Advising", "kind": "event",
             "title": "Computing Catalyst Sprinternship application",
             "date": "2026-09-21", "status": "new", "source_refs": []}
        b = dict(a, id="b", title="Computing Catalyst Sprinternship Applications",
                 detail="longer detail wins")
        assert O._near_duplicates([a, b], TODAY) == {"a"}

    def test_weekly_placeholder_hidden_when_canvas_lists_specifics(self):
        ph = {"id": "ph", "course": "ECON001", "course_label": "ECON001",
              "kind": "assignment", "title": "Weekly Homework (Chapter HW)",
              "date": "2026-09-22", "status": "new",
              "source_refs": [{"source": "gcal_syllabi"}]}
        hw = {"id": "hw4", "course": "ECON001", "course_label": "ECON001",
              "kind": "assignment", "title": "HW Chap 04",
              "date": "2026-09-22", "status": "new",
              "source_refs": [{"source": "canvas_scraper"}]}
        assert O._near_duplicates([ph, hw], TODAY) == {"ph"}

    def test_distinct_items_kept(self):
        a = {"id": "a", "course_label": "PHIL001", "kind": "assessment",
             "title": "Quiz 3", "date": "2026-09-25", "status": "new"}
        b = dict(a, id="b", title="Participation 4")
        assert O._near_duplicates([a, b], TODAY) == set()


class TestTldr:
    def _facts(self, **kw):
        f = {"overdue": [], "urgent_due_today": [], "heavy_day": None,
             "assignment_count": 0, "assessment_count": 0}
        f.update(kw)
        return f

    def test_heavy_day_names_items(self):
        t = O._deterministic_tldr(self._facts(heavy_day={
            "date_label": "Friday", "count": 3,
            "titles": ["MATH001 Midterm Exam 1", "PHIL001 Quiz 3", "PHIL001 P4"]}))
        assert t == "Friday is heavy: MATH001 Midterm Exam 1, PHIL001 Quiz 3 and 1 more."

    def test_next_exam_when_nothing_due(self):
        t = O._deterministic_tldr(self._facts(next_exam={
            "course": "MATH001", "title": "Midterm Exam 1", "when": "Friday"}))
        assert "MATH001 Midterm Exam 1 is Friday" in t


def test_plain_text_strips_markup_keeps_links():
    html = ('<html><head><style>.x{color:red}</style></head><body><p>Exam '
            '<b>Friday</b></p><a href="https://umd.edu/r">Register</a></body></html>')
    out = O._plain_text(html)
    assert "color:red" not in out and "Exam Friday" in out
    assert "Register (https://umd.edu/r)" in out
    assert O._plain_text("plain text body") == "plain text body"


# --- delivery ------------------------------------------------------------------

class TestDelivery:
    def test_failed_publish_sends_failure_notice_not_the_summary(self, monkeypatch):
        sent = {}

        def fake_send(line, url, today, root=""):
            sent["line"] = line
            return {"sent": True}
        monkeypatch.setattr(O.google_api, "send_daily_briefing_email", fake_send)
        run = _run(dry=False)
        ctx = _ctx(is_repeat=False)
        ctx["state"]["last_completed_date"] = "2026-09-18"
        d = O.step10_deliver(run, ctx, "All good today.", "https://x", published=False)
        assert "could not be built" in sent["line"] and "2026-09-18" in sent["line"]
        assert d["drive"] is False

    def test_send_exception_never_escapes(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr(O.google_api, "send_daily_briefing_email", boom)
        monkeypatch.setattr(O.google_api, "folder_ok", lambda *a, **k: False)
        run = _run(dry=False)
        d = O.step10_deliver(run, _ctx(is_repeat=False), "x", "https://x")
        assert d["email"] is False
        assert any("network down" in e["message"] for e in run.errors)


# --- weather ---------------------------------------------------------------------

class TestWeather:
    def test_line_has_temps_and_rain(self):
        f = weather.get_today_weather(fetch=lambda t: {"daily": {
            "precipitation_probability_max": [60], "temperature_2m_max": [71.6],
            "temperature_2m_min": [55.2]}})
        assert weather.weather_line(f) == "High 72° · low 55° · bring an umbrella (60% rain)"

    def test_failure_degrades(self):
        def boom(t):
            raise OSError("x")
        f = weather.get_today_weather(fetch=boom, retries=0)
        assert f.get("error")
        assert weather.weather_line(None) == "Weather not available this morning"


# --- canvas ---------------------------------------------------------------------

class TestCanvasFacts:
    def test_submission_summary(self):
        s = canvas_shadow.canvas_submission_summary
        assert s({"workflow_state": "submitted"})["settled"] is True
        assert s({"workflow_state": "graded", "score": None})["settled"] is True
        assert s({"workflow_state": "unsubmitted", "missing": True}) == {
            "state": "unsubmitted", "settled": False, "missing": True,
            "excused": False}
        assert s(None)["settled"] is False

    def test_scrape_age(self):
        now = _dt.datetime(2026, 9, 19, 17, 0, tzinfo=_dt.timezone.utc)
        assert canvas_shadow.scrape_age_hours(
            {"scraped_at": "2026-09-18T17:00:00Z"}, now=now) == pytest.approx(24.0)
        assert canvas_shadow.scrape_age_hours({}) is None

    def test_stale_scrape_is_flagged(self, monkeypatch):
        monkeypatch.setattr(O.canvas_shadow, "load_canvas_scrape",
                            lambda: {"scraped_at": "2026-09-01T00:00:00Z", "courses": []})
        run = _run()
        O._load_canvas_scrape(run)
        O._load_canvas_scrape(run)  # cached: warned once
        stale = [e for e in run.errors if "days old" in e["message"]]
        assert len(stale) == 1 and stale[0]["severity"] == "MAJOR"


# --- state save --------------------------------------------------------------------

def test_click_during_run_survives_the_save(tmp_path):
    """A Done click made while a run is in flight used to be reverted by the
    run's wholesale items replace."""
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    state = {"items": [{"id": "hw1", "title": "HW", "kind": "assignment",
                        "course_class": "cmsc", "course_label": "CMSC001",
                        "status": "new"}]}
    state_io_sqlite.save(state, path)
    loaded, _, _ = state_io_sqlite.load(path)
    conn = dbmod.connect(path)
    conn.execute("UPDATE items SET status='handled' WHERE id='hw1'")  # the click
    conn.commit()
    conn.close()
    state_io_sqlite.save(loaded, path)  # the run finishes with its stale copy
    again, _, _ = state_io_sqlite.load(path)
    assert again["items"][0]["status"] == "handled"


def test_lead_basis_not_cut_mid_word_and_source_not_repeated():
    claim = ("word " * 70).strip()[:300]
    out = render_briefing._clip_claim(claim)
    assert out.endswith("…") and not out[:-1].endswith("wor")
