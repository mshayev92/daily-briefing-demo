"""2026-10-08: an instructor's moved due date (Canvas announcement) wins over
Canvas's stale due date until Canvas agrees (orchestrator._apply_due_overrides).

Run (from service/):
    ../venv/bin/python -m pytest test_due_overrides.py -q
"""

import datetime as _dt

import pytest

import canvas_shadow
import db as dbmod
import orchestrator as o
import store

TODAY = _dt.date(2026, 10, 8)
STATE = {"courses": {"PHIL001": {"class": "lecture"}}}
ANN = {"id": 6151473,
       "html_url": "https://umd.instructure.com/courses/1000002/"
                   "discussion_topics/6151473"}


class Run(o.Run):
    def __init__(self, db_path):
        super().__init__(db_path=db_path, dry_run=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    canvas = {"items": []}
    monkeypatch.setattr(canvas_shadow, "canvas_authoritative_candidates",
                        lambda scrape=None, tz=None: canvas["items"])
    return {"db": path, "run": Run(path), "canvas": canvas,
            "ctx": {"today": TODAY, "state": STATE}}


def coursework(date="2026-10-08", status="ongoing"):
    return {"id": "phil001-discussion-post-5", "kind": "assignment",
            "course_label": "PHIL001", "title": "Discussion Post 5",
            "date": date, "time": "23:59", "status": status, "notes": None}


def canvas_says(date):
    canvas_row = {"course": "PHIL001", "kind": "assignment",
                  "title": "Discussion Post 5", "due_date": date}
    return [canvas_row]


def extension(due="2026-10-10", time="23:59"):
    return {"title": "Discussion Post 5", "due_date": due, "due_time": time}


def test_announced_date_replaces_stale_canvas_date(env):
    items = [coursework()]
    env["canvas"]["items"] = canvas_says("2026-10-08")
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    overrides = o._apply_due_overrides(env["run"], env["ctx"], items,
                                       None, overrides)
    assert items[0]["date"] == "2026-10-10"
    assert items[0]["time"] == "23:59"
    assert "Canvas still lists Thu, Oct 8" in items[0]["notes"]
    assert any("showing the announced date" in e["message"]
               for e in env["run"].errors)
    assert store.kv_get(env["db"], o.DUE_OVERRIDE_KEY)


def test_override_survives_step3_resetting_date_to_canvas(env):
    items = [coursework()]
    env["canvas"]["items"] = canvas_says("2026-10-08")
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    o._apply_due_overrides(env["run"], env["ctx"], items, None, overrides)
    items[0]["date"] = "2026-10-08"  # step 3 writes Canvas's date back
    o._apply_due_overrides(env["run"], env["ctx"], items, None,
                           store.kv_get(env["db"], o.DUE_OVERRIDE_KEY))
    assert items[0]["date"] == "2026-10-10"


def test_canvas_catching_up_drops_the_override(env):
    items = [coursework()]
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    env["canvas"]["items"] = canvas_says("2026-10-10")
    overrides = o._apply_due_overrides(env["run"], env["ctx"], items,
                                       None, overrides)
    assert overrides == {}
    assert store.kv_get(env["db"], o.DUE_OVERRIDE_KEY) == {}
    assert not any("showing the announced date" in e["message"]
                   for e in env["run"].errors)


def test_no_open_item_keeps_record_without_touching_items(env):
    items = [coursework(status="done")]
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    overrides = o._apply_due_overrides(env["run"], env["ctx"], items,
                                       None, overrides)
    assert items[0]["date"] == "2026-10-08"
    assert len(overrides) == 1


def test_stale_record_is_pruned(env):
    old = _dt.date(2026, 9, 1)
    overrides = {}
    o._record_due_change(overrides, "PHIL001",
                         extension(due=old.isoformat()), ANN, old)
    env["canvas"]["items"] = canvas_says("2026-08-30")
    overrides = o._apply_due_overrides(env["run"], env["ctx"], [],
                                       None, overrides)
    assert overrides == {}


def test_title_match_ignores_case_and_spacing(env):
    items = [coursework()]
    items[0]["title"] = "discussion   post 5"
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    o._apply_due_overrides(env["run"], env["ctx"], items, None, overrides)
    assert items[0]["date"] == "2026-10-10"


def test_still_disagreeing_is_reported_once_a_day(env):
    """2026-10-10: every rebuild logged the same MINOR (23 rows in the errors
    table). The date is still placed each build; the report is once a day."""
    overrides = {}
    o._record_due_change(overrides, "PHIL001", extension(), ANN, TODAY)
    env["canvas"]["items"] = canvas_says("2026-10-08")
    for _ in range(3):                      # three builds today
        o._apply_due_overrides(env["run"], env["ctx"], [coursework()], None,
                               overrides)
    reports = [e for e in env["run"].errors if e["source"] == "step4a"]
    assert len(reports) == 1

    placed = [coursework()]
    env["ctx"]["today"] = TODAY + _dt.timedelta(days=1)
    o._apply_due_overrides(env["run"], env["ctx"], placed, None, overrides)
    reports = [e for e in env["run"].errors if e["source"] == "step4a"]
    assert len(reports) == 2
    assert placed[0]["date"] == "2026-10-10"    # the date is still placed


def test_announcement_wording_matches_canvas_chapter_titles():
    assert o._due_key("ECON001", "Smartbook for Chapter 13") == \
        o._due_key("ECON001", "Chap 13: Perfect Competition")
    assert o._due_key("ECON001", "Chapter 12 homework") == \
        o._due_key("ECON001", "HW Chap 12")
    assert o._due_key("ECON001", "Chapter 12 homework") != \
        o._due_key("ECON001", "Chap 12: The Costs of Production")
