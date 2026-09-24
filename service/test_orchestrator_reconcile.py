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
