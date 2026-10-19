"""
Tests for orchestrator.py's step3_reconcile -- previously untested.

Written after a real --live run crashed in step11_write_state with
`sqlite3.IntegrityError: UNIQUE constraint failed: items.id`, traced to
step3_reconcile generating the same `id` for two genuinely different
candidates ("Quiz #1" and "Quiz 1" for the same course+date both slugify
to "quiz-1"). The canvas-scraper cutover (Phase 5) made this an actual
crash instead of a never-triggered latent edge case, by producing far
more candidates per course than Gmail extraction ever did.

Run (from service/):
    ../venv/bin/python -m pytest test_orchestrator_reconcile.py -q
"""

from __future__ import annotations

import datetime as _dt

import orchestrator


def _run():
    return orchestrator.Run(db_path=":memory:", dry_run=True)


def _ctx(items=None, today="2026-09-18"):
    return {"state": {"items": items or []}, "today": _dt.date.fromisoformat(today)}


def _candidate(**kwargs):
    base = {
        "thread_id": "t1", "course": "PHIL001", "title": "Essay 2", "kind": "assignment",
        "due_date": "2026-09-20", "due_time": "23:59", "end_time": None, "location": None,
        "canvas_url": None, "organizer": None, "description": None, "links": [],
        "confidence": "inferred", "source": "gmail",
    }
    base.update(kwargs)
    return base


class TestDedupeItemsById:
    """step11_write_state's structural guard: no matter which step
    produced a duplicate id, it must never reach state_io_sqlite.save()
    un-deduplicated. Added after the step3_reconcile-specific fix alone
    did NOT prevent a second live crash with the identical symptom,
    proving a duplicate can arise from somewhere other than
    step3_reconcile's own new-item path."""

    def test_no_duplicates_is_unchanged(self):
        run = _run()
        items = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        result = orchestrator._dedupe_items_by_id(items, run)
        assert [i["id"] for i in result] == ["a", "b"]
        assert run.errors == []

    def test_duplicate_id_renames_the_later_item_and_logs_it(self):
        run = _run()
        items = [
            {"id": "x", "title": "First", "course": "PHIL001", "kind": "assignment"},
            {"id": "x", "title": "Second", "course": "PHIL001", "kind": "assignment"},
        ]
        result = orchestrator._dedupe_items_by_id(items, run)
        ids = [i["id"] for i in result]
        assert ids == ["x", "x-2"]
        assert result[0]["title"] == "First"
        assert result[1]["title"] == "Second"
        assert len(run.errors) == 1
        assert run.errors[0]["severity"] == "MAJOR"
        assert "First" in run.errors[0]["message"] or "Second" in run.errors[0]["message"]

    def test_three_way_duplicate_all_get_distinct_ids(self):
        run = _run()
        items = [{"id": "x", "title": "A"}, {"id": "x", "title": "B"}, {"id": "x", "title": "C"}]
        result = orchestrator._dedupe_items_by_id(items, run)
        ids = [i["id"] for i in result]
        assert len(set(ids)) == 3

    def test_never_crashes_state_save_regardless_of_source(self):
        """The actual guarantee that matters: a duplicate produced by
        ANY source (here simulating step3_reconcile output concatenated
        with step5c_discovery's leads, exactly as main() does) must not
        raise when handed to a real sqlite items table."""
        import sqlite3

        run = _run()
        reconciled = [{"id": "dup", "title": "From reconcile"}]
        leads = [{"id": "dup", "title": "From leads"}]  # accidental collision
        items = orchestrator._dedupe_items_by_id(reconciled + leads, run)

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, title TEXT)")
        for it in items:
            conn.execute("INSERT INTO items (id, title) VALUES (?, ?)", (it["id"], it["title"]))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        conn.close()

    def test_items_without_an_id_are_left_alone(self):
        run = _run()
        items = [{"title": "No id at all"}]
        result = orchestrator._dedupe_items_by_id(items, run)
        assert result == items
        assert run.errors == []


class TestIdCollisionRegression:
    """The exact bug that crashed the first live Phase 5 run."""

    def test_slug_colliding_titles_get_distinct_ids(self):
        run = _run()
        candidates = [
            _candidate(thread_id="t1", course="CMSC001", title="Quiz #1", due_date="2026-09-16"),
            _candidate(thread_id="t2", course="CMSC001", title="Quiz 1", due_date="2026-09-16"),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)

        assert len(items) == 2, "two genuinely different candidates must not be merged into one item"
        ids = [i["id"] for i in items]
        assert len(set(ids)) == 2, "colliding slugs must be disambiguated, not produce duplicate ids"
        titles = {i["id"]: i["title"] for i in items}
        assert set(titles.values()) == {"Quiz #1", "Quiz 1"}

    def test_three_way_slug_collision_all_distinct(self):
        run = _run()
        candidates = [
            _candidate(thread_id="t1", course="CMSC001", title="Quiz #1", due_date="2026-09-16"),
            _candidate(thread_id="t2", course="CMSC001", title="Quiz 1", due_date="2026-09-16"),
            _candidate(thread_id="t3", course="CMSC001", title="Quiz, 1!", due_date="2026-09-16"),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        ids = [i["id"] for i in items]
        assert len(set(ids)) == len(ids) == 3

    def test_new_id_never_collides_with_a_pre_existing_item(self):
        run = _run()
        existing = [{"id": "cmsc001-quiz-1-2026-09-16", "course": "CMSC001", "title": "Quiz #1", "date": "2026-09-16"}]
        candidates = [_candidate(thread_id="t1", course="CMSC001", title="Quiz 1", due_date="2026-09-16")]
        items = orchestrator.step3_reconcile(run, _ctx(items=existing), candidates)

        ids = [i["id"] for i in items]
        assert len(set(ids)) == len(ids) == 2

    def test_no_collision_case_is_unaffected(self):
        """The common case (no colliding slugs) must still produce the
        original, non-suffixed id -- the fix should be invisible when
        there's nothing to disambiguate."""
        run = _run()
        candidates = [_candidate(thread_id="t1", course="PHIL001", title="Essay 2", due_date="2026-09-20")]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        assert items[0]["id"] == "phil001-essay-2-2026-09-20"


class TestBasicReconcile:
    def test_new_candidate_becomes_new_item(self):
        run = _run()
        items = orchestrator.step3_reconcile(run, _ctx(), [_candidate()])
        assert len(items) == 1
        assert items[0]["title"] == "Essay 2"
        assert items[0]["status"] == "new"
        assert items[0]["confidence"] == "inferred"

    def test_matching_candidate_updates_existing_item_in_place(self):
        run = _run()
        existing = [{
            "id": "phil001-essay-2-2026-09-20", "course": "PHIL001", "title": "Essay 2",
            "date": "2026-09-20", "time": None, "status": "new", "detail": None, "source_refs": [],
        }]
        candidates = [_candidate(due_date="2026-09-22")]  # due date moved
        items = orchestrator.step3_reconcile(run, _ctx(items=existing), candidates)

        assert len(items) == 1, "a matched candidate must update in place, not create a second item"
        assert items[0]["date"] == "2026-09-22"
        assert items[0]["date_changed_on"] == "2026-09-18"
        assert items[0]["id"] == "phil001-essay-2-2026-09-20", "id must stay stable even when the date moves"
        assert "phil001-essay-2-2026-09-20" in run.moved_ids

    def test_empty_candidates_is_pure_passthrough(self):
        run = _run()
        existing = [{"id": "x", "course": "PHIL001", "title": "Essay 2"}]
        items = orchestrator.step3_reconcile(run, _ctx(items=existing), [])
        assert items == existing

    def test_matching_candidate_backfills_canvas_url_and_confidence(self):
        """Real gap found on the first live Phase 5 run: an item first
        created from a Gmail candidate (no canvas_url, per that
        extraction's own rules) must pick up canvas_url/confidence once a
        higher-authority canvas_scraper candidate matches it -- not just
        on brand-new items."""
        run = _run()
        existing = [{
            "id": "phil001-essay-2-2026-09-20", "course": "PHIL001", "title": "Essay 2",
            "date": "2026-09-20", "time": None, "status": "ongoing", "detail": None,
            "source_refs": [], "canvas_url": None, "confidence": "inferred",
            "organizer": None, "description": None,
        }]
        candidates = [_candidate(
            source="canvas_scraper", due_date="2026-09-20", canvas_url="https://x/1",
            confidence="confirmed",
        )]
        items = orchestrator.step3_reconcile(run, _ctx(items=existing), candidates)
        assert items[0]["canvas_url"] == "https://x/1"
        assert items[0]["confidence"] == "confirmed"

    def test_matching_candidate_never_clobbers_a_good_existing_field_with_nothing(self):
        """If `primary` this run happens to be a lower-authority source
        that doesn't supply canvas_url (e.g. the scraper temporarily has
        no data for this exact item), an existing good value must survive."""
        run = _run()
        existing = [{
            "id": "phil001-essay-2-2026-09-20", "course": "PHIL001", "title": "Essay 2",
            "date": "2026-09-20", "canvas_url": "https://x/already-good", "confidence": "confirmed",
            "source_refs": [], "detail": None,
        }]
        candidates = [_candidate(source="gmail", due_date="2026-09-20", canvas_url=None, confidence="inferred")]
        items = orchestrator.step3_reconcile(run, _ctx(items=existing), candidates)
        assert items[0]["canvas_url"] == "https://x/already-good"
        assert items[0]["confidence"] == "confirmed"


class TestAuthorityOrdering:
    """Phase 5 cutover: canvas_scraper must win over gmail for the same
    (course, title, date-window) key."""

    def test_canvas_scraper_wins_date_over_gmail(self):
        """Within step3_reconcile's own +/-1-day clustering window (an
        LLM's off-by-a-day parse of prose, not a genuinely different
        occurrence) -- the scraper's exact date must win the merge."""
        run = _run()
        candidates = [
            _candidate(thread_id="t1", source="gmail", due_date="2026-09-20", canvas_url=None, confidence="inferred"),
            _candidate(thread_id="t2", source="canvas_scraper", due_date="2026-09-21",
                       canvas_url="https://x/1", confidence="confirmed"),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        assert len(items) == 1
        assert items[0]["date"] == "2026-09-21"
        assert items[0]["canvas_url"] == "https://x/1"

    def test_gmail_description_still_contributes_to_merged_detail(self):
        """The email candidate isn't discarded once outranked -- its
        description still folds into merged_detail alongside the
        scraper's (empty) one."""
        run = _run()
        candidates = [
            _candidate(thread_id="t1", source="gmail", due_date="2026-09-20",
                       description="Prof said bring a calculator"),
            _candidate(thread_id="t2", source="canvas_scraper", due_date="2026-09-20", description=None),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        assert "Prof said bring a calculator" in items[0]["detail"]

    def test_source_refs_include_both_sources(self):
        run = _run()
        candidates = [
            _candidate(thread_id="t1", source="gmail", due_date="2026-09-20"),
            _candidate(thread_id="t2", source="canvas_scraper", due_date="2026-09-20"),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        sources = {r["source"] for r in items[0]["source_refs"]}
        assert sources == {"gmail", "canvas_scraper"}

    def test_dates_more_than_a_day_apart_are_not_merged(self):
        run = _run()
        candidates = [
            _candidate(thread_id="t1", source="gmail", due_date="2026-09-20"),
            _candidate(thread_id="t2", source="canvas_scraper", due_date="2026-09-25"),
        ]
        items = orchestrator.step3_reconcile(run, _ctx(), candidates)
        assert len(items) == 2


class TestCampusEventsAreNotMovedOrDoubled:
    """2026-09-25: a campus soccer game showed "Moved · now Fri, Sep 25"
    beside its unchanged row, and the UCLA football game appeared twice."""

    def test_email_sighting_does_not_move_a_campus_event(self):
        existing = {"id": "campus-maryland-mens-soccer-2026-09-25", "course": None,
                    "title": "Maryland Men's Soccer vs UCLA", "kind": "event",
                    "date": "2026-09-25", "status": "ongoing"}
        run = _run()
        orchestrator.step3_reconcile(run, _ctx([existing], "2026-09-25"), [_candidate(
            course=None, title="Maryland Men's Soccer vs UCLA", kind="event",
            due_date="2026-09-26")])
        assert run.moved_ids == []
        assert existing["date"] == "2026-09-25"

    def test_campus_copy_of_emailed_event_is_retired(self):
        email = {"id": "umd-football-2026-09-26", "kind": "event", "status": "new",
                 "date": "2026-09-26", "location": "SECU Stadium", "links": []}
        campus = {"id": "campus-maryland-football-2026-09-26", "kind": "event",
                  "status": "new", "date": "2026-09-26", "location": "SECU Stadium",
                  "links": [{"label": "More info", "url": "https://x"}]}
        soccer = {"id": "campus-soccer-2026-09-26", "kind": "event", "status": "new",
                  "date": "2026-09-26", "location": "Ludwig Field"}
        dropped = orchestrator._retire_campus_duplicates(_run(), [email, campus, soccer])
        assert dropped == [campus["id"]]
        assert soccer["status"] == "new"
        assert email["links"][0]["url"] == "https://x"


def test_own_move_is_counted_once_in_change_strip():
    changes = [{"course": "PHIL001", "entries": [
        {"label": "Moved", "text": "Essay 2 · now Sun, Sep 20", "own_move": True}]}]
    text = orchestrator.change_strip_text({"moved": 1}, changes, True)
    assert "1 date moved" in text


class TestSameThingIsOneRow:
    """2026-09-28: "Coding Project 1 due" (syllabus) and "Project #1:
    Drawing App" (Canvas) both showed under Today - Due; one career fair
    and one talk each showed up as several event rows."""

    def _it(self, id_, title, kind="event", date="2026-09-28", **kw):
        return dict({"id": id_, "title": title, "kind": kind, "date": date,
                     "status": "new", "course": None, "source_refs": []}, **kw)

    def test_syllabus_and_canvas_project_collapse_to_canvas(self):
        syl = self._it("p1-syl", "Coding Project 1 due", "assignment",
                       course="CMSC001", source_refs=[{"source": "gcal_syllabi"}])
        cv = self._it("p1-cv", "Project #1: Drawing App", "assignment",
                      course="CMSC001", time="23:59", status="ongoing",
                      canvas_url="https://c/1",
                      source_refs=[{"source": "canvas_scraper"}])
        orchestrator._retire_same_thing_duplicates(_run(), [syl, cv])
        assert syl["status"] == "expired" and cv["status"] == "ongoing"
        assert {"source": "gcal_syllabi"} in cv["source_refs"]

    def test_weekly_homework_placeholder_yields_to_numbered_canvas_hw(self):
        """2026-09-29: "Weekly Homework (Chapter HW)" came back after the
        user handled Canvas's "HW Chap 06" due the same day."""
        ph = self._it("ph", "Weekly Homework (Chapter HW)", "assignment",
                      course="ECON001", source_refs=[{"source": "gcal_syllabi"}])
        cv = self._it("cv", "HW Chap 06", "assignment", course="ECON001",
                      status="handled", canvas_url="https://c/6",
                      source_refs=[{"source": "canvas_scraper"}])
        other_day = self._it("od", "Weekly Homework (Chapter HW)", "assignment",
                             course="ECON001", date="2026-10-06")
        orchestrator._retire_same_thing_duplicates(_run(), [ph, cv, other_day])
        assert ph["status"] == "expired" and cv["status"] == "handled"
        assert other_day["status"] == "new"

    def test_reworded_announcements_of_one_listing_collapse(self):
        """2026-10-13: three housing emails, three titles, one 10/21 noon
        deadline, all linking the Housing Portal."""
        portal = [{"label": "Housing Portal", "url": "http://starrez.umd.edu"}]
        a = self._it("a", "Fall Reassignment Request Deadline",
                     time="12:00 PM", links=portal)
        b = self._it("b", "Fall Room Reassignment Process", time="12:00",
                     status="ongoing", links=portal)
        c = self._it("c", "Fall Reassignment Request (room change)",
                     time="12:00", links=portal,
                     location="Housing Portal (starrez.umd.edu)")
        orchestrator._retire_same_thing_duplicates(_run(), [a, b, c])
        assert sorted(x["status"] for x in (a, b, c)).count("expired") == 2

    def test_same_hour_events_without_shared_link_stay_apart(self):
        a = self._it("a", "Homecoming Parade", time="12:00",
                     links=[{"url": "https://a"}])
        b = self._it("b", "Homecoming Concert", time="12:00",
                     links=[{"url": "https://b"}])
        c = self._it("c", "Engineering Career Fair", time="12:00",
                     links=[{"url": "https://a"}])
        orchestrator._retire_same_thing_duplicates(_run(), [a, b, c])
        assert {x["status"] for x in (a, b, c)} == {"new"}

    def test_event_titles_ignore_years_and_filler(self):
        """2026-10-13 audit: a later `_title_tokens` shadowed step 6's, so
        "Homecoming 2026" and "Homecoming" stayed two rows."""
        a = self._it("hc-1", "Homecoming 2026")
        b = self._it("hc-2", "Homecoming", time="12:00")
        orchestrator._retire_same_thing_duplicates(_run(), [a, b])
        assert sorted(x["status"] for x in (a, b)) == ["expired", "new"]

    def test_exam_and_midterm_of_one_day_collapse_to_canvas(self):
        """2026-09-30: the masthead named "ECON001 Exam 1" (syllabus,
        assessment) and "ECON001 Midterm 1" (Canvas, assignment)."""
        syl = self._it("ex", "Exam 1", "assessment", course="ECON001",
                       date="2026-10-06", source_refs=[{"source": "gcal_syllabi"}])
        cv = self._it("mt", "Midterm 1", "assignment", course="ECON001",
                      date="2026-10-06", status="ongoing",
                      canvas_url="https://c/m1",
                      source_refs=[{"source": "canvas_scraper"}])
        m2 = self._it("m2", "Midterm 2", "assignment", course="ECON001",
                      date="2026-10-06", canvas_url="https://c/m2")
        orchestrator._retire_same_thing_duplicates(_run(), [syl, cv, m2])
        assert syl["status"] == "expired" and cv["status"] == "ongoing"
        assert m2["status"] == "new"

    def test_canvas_in_class_exam_is_an_assessment_without_its_11_59(self):
        """2026-10-02: the Canvas keeper stayed an "assignment" due 11:59
        PM, so ECON001's Tue 10/6 midterm was missing from the masthead."""
        cv = self._it("mt", "Midterm 1", "assignment", course="ECON001",
                      date="2026-10-06", time="23:59", status="ongoing",
                      submission_types=["none"])
        hw = self._it("hw", "Exam 1 Corrections", "assignment",
                      course="ECON001", time="23:59",
                      submission_types=["online_upload"])
        quiz = self._it("q", "Quiz 4", "assignment", course="ECON001",
                        time="23:59", submission_types=["none"])
        syl = self._it("ex", "Exam 1", "assessment", course="CMSC001")
        assert orchestrator._canvas_exams_are_assessments(
            _run(), [cv, hw, quiz, syl]) == ["mt"]
        assert (cv["kind"], cv["time"]) == ("assessment", None)
        assert (hw["kind"], hw["time"]) == ("assignment", "23:59")
        assert quiz["kind"] == "assignment"
        # Reconcile writes Canvas's 23:59 back each run; it is dropped again.
        cv["time"] = "23:59"
        assert orchestrator._canvas_exams_are_assessments(_run(), [cv]) == []
        assert (cv["kind"], cv["time"]) == ("assessment", None)

    def test_dismissed_weekly_placeholder_does_not_close_canvas_hw(self):
        ph = self._it("ph", "Weekly Homework (Chapter HW)", "assignment",
                      course="ECON001", status="dismissed")
        cv = self._it("cv", "HW Chap 06", "assignment", course="ECON001",
                      status="ongoing", canvas_url="https://c/6")
        orchestrator._retire_same_thing_duplicates(_run(), [ph, cv])
        assert cv["status"] == "ongoing"

    def test_different_numbers_or_days_stay_apart(self):
        a = self._it("a", "Project 1", "assignment", course="CMSC001")
        b = self._it("b", "Project 2", "assignment", course="CMSC001")
        c = self._it("c", "Project 1", "assignment", course="CMSC001",
                     date="2026-10-05")
        orchestrator._retire_same_thing_duplicates(_run(), [a, b, c])
        assert [x["status"] for x in (a, b, c)] == ["new"] * 3

    def test_restated_events_collapse_and_keeper_gains_location(self):
        a = self._it("a", "Planting Healthy Roots BINGO Night", time="19:00")
        b = self._it("b", "Planting Healthy Roots Bingo", location="Stamp")
        c = self._it("c", "Fall 2026 Computer Science Career & Internship Fair")
        d = self._it("d", "Fall Career & Internship Fair", date="2026-10-01")
        orchestrator._retire_same_thing_duplicates(_run(), [a, b, c, d])
        assert (a["status"], b["status"]) == ("new", "expired") or \
            (a["status"], b["status"]) == ("expired", "new")
        assert c["status"] == "new" and d["status"] == "new"

    def test_dismissed_twin_closes_the_open_one(self):
        a = self._it("a", "Good Friends? Good Meeting?: Unpacking the U.S.-China Summit",
                     status="dismissed")
        b = self._it("b", "Good Friends? Good Meeting?: Unpacking the U.S.-China Summit",
                     time="12:00")
        orchestrator._retire_same_thing_duplicates(_run(), [a, b])
        assert b["status"] == "dismissed" and a["status"] == "dismissed"


class TestSyllabusCalendarRefresh:
    """The `Syllabi Dates` calendar must move a tracked item's date.
    2026-10-07: CMSC001 Midterm Exam 1 stayed on Thu 10/8 (a stale seed)
    while the calendar and the Wed discussion meeting said Wed 10/7."""

    STATE = {"calendars": {"syllabi": "cal"},
             "courses": {"CMSC001": {"class": "cmsc"}},
             "items": [{"id": "cmsc001-midterm1-20261008",
                        "course": "CMSC001", "title": "Midterm Exam 1",
                        "kind": "assessment", "date": "2026-10-08",
                        "status": "new", "confidence": "confirmed",
                        "source_refs": [{"source": "gcal_syllabi",
                                         "event_id": "CMSC001: Midterm Exam 1"}]}]}

    def _events(self, monkeypatch, events):
        monkeypatch.setattr(orchestrator.google_api, "calendar_events",
                            lambda *a, **k: events)

    def _ev(self, title, day):
        return {"title": title, "start": day, "end": day, "all_day": True,
                "location": "", "description": ""}

    def test_calendar_date_moves_tracked_item(self, monkeypatch):
        self._events(monkeypatch, [self._ev("CMSC001: Midterm Exam 1",
                                            "2026-10-07")])
        run, ctx = _run(), _ctx(dict(self.STATE)["items"], "2026-10-07")
        ctx["state"] = dict(self.STATE)
        cands = orchestrator._syllabus_calendar_candidates(run, ctx)
        items = orchestrator.step3_reconcile(run, ctx, cands)
        assert items[0]["date"] == "2026-10-07"
        assert run.moved_ids == ["cmsc001-midterm1-20261008"]

    def test_untracked_event_makes_no_new_item(self, monkeypatch):
        self._events(monkeypatch, [self._ev("CMSC001: P2 Due", "2026-10-12")])
        run, ctx = _run(), _ctx(None, "2026-10-07")
        ctx["state"] = dict(self.STATE)
        assert orchestrator._syllabus_calendar_candidates(run, ctx) == []

    def test_read_failure_is_minor_and_keeps_dates(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")
        monkeypatch.setattr(orchestrator.google_api, "calendar_events", boom)
        run, ctx = _run(), _ctx(None, "2026-10-07")
        ctx["state"] = dict(self.STATE)
        assert orchestrator._syllabus_calendar_candidates(run, ctx) == []
        assert run.errors
