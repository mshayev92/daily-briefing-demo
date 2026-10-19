"""Tracked syllabus items follow the Syllabi Dates calendar: a renamed or moved
event is followed, and an item whose event is gone expires
(orchestrator._follow_syllabus_calendar)."""

import datetime as _dt

import orchestrator as o

FIRST = _dt.date(2026, 10, 9)
LAST = _dt.date(2027, 2, 6)
ECON = "ECON001: SmartBook Assignments Due (weekly)"


def _item(iid, date, event_id, status="new", extra_source=None, title="Weekly SmartBook Assignments"):
    refs = [{"source": "gcal_syllabi", "event_id": event_id}]
    if extra_source:
        refs.append({"source": extra_source, "thread_id": "x"})
    return {"id": iid, "kind": "assignment", "course": "ECON001",
            "title": title, "date": date,
            "status": status, "source_refs": refs}


def _state(*items):
    return {"items": list(items), "courses": {"ECON001": {}}}


def _ev(title, start, all_day=True):
    return {"title": title, "start": start, "all_day": all_day}


def test_missing_event_expires_item():
    st = _state(_item("a", "2026-10-11", ECON))
    o._follow_syllabus_calendar(st, [], FIRST, LAST)
    assert st["items"][0]["status"] == "expired"
    assert "Syllabi Dates" in st["items"][0]["expired_reason"]


def test_present_event_keeps_item():
    st = _state(_item("a", "2026-10-11", ECON))
    o._follow_syllabus_calendar(st, [_ev(ECON, "2026-10-11")], FIRST, LAST)
    assert st["items"][0]["status"] == "new"
    assert st["items"][0]["date"] == "2026-10-11"


def test_item_outside_window_is_not_judged():
    st = _state(_item("a", "2026-10-01", "ECON001: Old"),
                _item("b", "2027-03-01", "ECON001: Later"))
    o._follow_syllabus_calendar(st, [], FIRST, LAST)
    assert [i["status"] for i in st["items"]] == ["new", "new"]


def test_item_with_another_source_is_kept():
    st = _state(_item("a", "2026-10-11", ECON, extra_source="gmail"))
    o._follow_syllabus_calendar(st, [], FIRST, LAST)
    assert st["items"][0]["status"] == "new"


def test_finished_item_is_not_touched():
    st = _state(_item("a", "2026-10-11", ECON, status="handled"))
    o._follow_syllabus_calendar(st, [], FIRST, LAST)
    assert st["items"][0]["status"] == "handled"


def test_renamed_event_same_day_is_followed():
    st = _state(_item("a", "2026-10-11", ECON))
    new = "ECON001: SmartBook Chapter 7 Due (weekly)"
    o._follow_syllabus_calendar(st, [_ev(new, "2026-10-11")], FIRST, LAST)
    it = st["items"][0]
    assert it["status"] == "new"
    assert it["title"] == "SmartBook Chapter 7 Due (weekly)"
    assert it["source_refs"][0]["event_id"] == new


def test_moved_event_with_similar_title_is_followed():
    st = _state(_item("a", "2026-10-11", ECON))
    moved = "ECON001: SmartBook Assignment Due (weekly)"
    o._follow_syllabus_calendar(st, [_ev(moved, "2026-10-14")], FIRST, LAST)
    it = st["items"][0]
    assert it["status"] == "new"
    assert it["date"] == "2026-10-14"
    assert it["title"] == "SmartBook Assignment Due (weekly)"


def test_unrelated_event_does_not_rename_item():
    st = _state(_item("a", "2026-10-11", ECON))
    o._follow_syllabus_calendar(
        st, [_ev("ECON001: Exam 1", "2026-10-11")], FIRST, LAST)
    assert st["items"][0]["status"] == "expired"


def test_other_course_event_does_not_rename_item():
    st = _state(_item("a", "2026-10-11", ECON))
    o._follow_syllabus_calendar(
        st, [_ev("MATH001: SmartBook Assignments Due (weekly)", "2026-10-11")], FIRST, LAST)
    assert st["items"][0]["status"] == "expired"


def test_event_tracked_under_its_own_title_is_not_taken():
    other = _item("b", "2026-10-11", "ECON001: Something", title="Something")
    st = _state(_item("a", "2026-10-11", ECON), other)
    o._follow_syllabus_calendar(
        st, [_ev("ECON001: Something", "2026-10-11")], FIRST, LAST)
    by_id = {i["id"]: i for i in st["items"]}
    assert by_id["a"]["status"] == "expired"
    assert by_id["b"]["status"] == "new"


def test_one_event_follows_only_one_item():
    a = _item("a", "2026-10-11", ECON)
    b = _item("b", "2026-10-11", "ECON001: Gone", title="Gone Item")
    st = _state(a, b)
    o._follow_syllabus_calendar(
        st, [_ev("ECON001: SmartBook Assignments Due (weekly) Replacement", "2026-10-11")],
        FIRST, LAST)
    statuses = sorted(i["status"] for i in st["items"])
    assert statuses == ["expired", "new"]


class _Run:
    def __init__(self):
        self.errors = []

    def log_error(self, step, level, msg):
        self.errors.append((step, level, msg))


def test_failed_read_changes_nothing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(o.google_api, "calendar_events", boom)
    st = _state(_item("a", "2026-10-11", ECON))
    st["calendars"] = {"syllabi": "cal-id"}
    run = _Run()
    ctx = {"state": st, "today": _dt.date(2026, 10, 10)}
    assert o._syllabus_calendar_candidates(run, ctx) == []
    assert st["items"][0]["status"] == "new"
    assert run.errors and run.errors[0][1] == "MINOR"


def test_renamed_event_yields_candidate_on_next_read(monkeypatch):
    """After a rename the item is tracked under the new title, so the normal
    reader offers the renamed event as a candidate for that item."""
    new = "ECON001: SmartBook Chapter 7 Due (weekly)"
    monkeypatch.setattr(o.google_api, "calendar_events",
                        lambda *a, **k: [_ev(new, "2026-10-11")])
    st = _state(_item("a", "2026-10-11", ECON))
    st["calendars"] = {"syllabi": "cal-id"}
    ctx = {"state": st, "today": _dt.date(2026, 10, 10)}
    cands = o._syllabus_calendar_candidates(_Run(), ctx)
    assert [c["title"] for c in cands] == ["SmartBook Chapter 7 Due (weekly)"]
    assert st["items"][0]["status"] == "new"
