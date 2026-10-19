"""Undo of recent page actions (app.py: actions table, POST/GET /undo).

Each action that changes an item's status records what it replaced; POST
/undo puts the newest unreversed status back. Called directly, the way
test_app_ledger.py does, since this venv has no httpx for TestClient.

Run (from the repo root):
    venv/bin/python -m pytest service/test_undo.py -q
"""

import datetime as _dt

import pytest
from fastapi import HTTPException

import app
import db as dbmod


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    real_connect = dbmod.connect
    conn = real_connect(path)
    for iid, kind, regime, status in (
            ("hw-1", "assignment", "confirmed", "new"),
            ("att-1", "attention", "confirmed", "new"),
            ("opp-1", "opportunity", "confirmed", "surfaced")):
        conn.execute("INSERT INTO items (id, course_class, course_label, kind, "
                     "title, regime, status) VALUES (?, 'umd', 'X', ?, ?, ?, ?)",
                     (iid, kind, iid, regime, status))
    conn.commit()
    conn.close()
    # The endpoints open the default db; point them at this one.
    monkeypatch.setattr(dbmod, "connect", lambda *a, **k: real_connect(path))
    monkeypatch.setattr(app, "_ledger_write", lambda *a, **k: None)
    return path


def _status(path, iid):
    conn = dbmod.connect(path)
    try:
        return conn.execute("SELECT status FROM items WHERE id = ?", (iid,)).fetchone()[0]
    finally:
        conn.close()


def test_done_then_undo_restores_status(db):
    app.mark_done("hw-1")
    assert _status(db, "hw-1") == "handled"
    assert app.latest_undo()["item_id"] == "hw-1"
    out = app.undo_last()
    assert out == {"id": "hw-1", "action": "done", "status": "new"}
    assert _status(db, "hw-1") == "new"
    assert app.latest_undo() == {}


def test_undo_removes_done_record(db):
    app.mark_done("hw-1")
    app.undo_last()
    conn = dbmod.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM done_resolved WHERE item_id = 'hw-1'"
                            ).fetchone()[0] == 0
    finally:
        conn.close()


def test_undo_walks_back_one_action_at_a_time(db):
    app.dismiss_item("opp-1")
    app.mark_resolved("att-1")
    assert app.undo_last()["id"] == "att-1"
    assert _status(db, "att-1") == "new"
    assert app.undo_last()["id"] == "opp-1"
    assert _status(db, "opp-1") == "surfaced"
    with pytest.raises(HTTPException) as err:
        app.undo_last()
    assert err.value.status_code == 404


def test_undone_action_is_not_undone_twice(db):
    app.mark_done("hw-1")
    app.undo_last()
    app.mark_done("hw-1")
    assert app.undo_last()["action"] == "done"
    assert _status(db, "hw-1") == "new"


def test_restore_and_decided_are_not_recorded(db):
    app.mark_done("hw-1")
    app.restore_item("hw-1")
    # The restore is not an undoable action: the done is still the newest.
    assert app.latest_undo()["action"] == "done"


def _application_row(path, iid):
    conn = dbmod.connect(path)
    try:
        return conn.execute("SELECT stage, follow_up_on FROM applications "
                            "WHERE item_id = ?", (iid,)).fetchone()
    finally:
        conn.close()


def test_stage_click_is_undone_back_to_untracked(db):
    app.set_application_stage("opp-1", app.StageIn(stage="applying"))
    assert app.latest_undo()["label"] == "Status: Applying"
    out = app.undo_last()
    assert out == {"id": "opp-1", "action": "application", "stage": "", "follow_up_on": ""}
    assert _application_row(db, "opp-1") is None


def test_stage_click_is_undone_back_to_the_stage_before(db):
    app.set_application_stage("opp-1", app.StageIn(stage="applying"))
    app.set_application_stage("opp-1", app.StageIn(stage="submitted"))
    out = app.undo_last()
    assert out["stage"] == "applying"
    assert _application_row(db, "opp-1")["stage"] == "applying"


def test_follow_up_date_change_is_undone(db):
    app.set_application_stage("opp-1", app.StageIn(stage="submitted"))
    before = _application_row(db, "opp-1")["follow_up_on"]
    app.set_application_stage("opp-1", app.StageIn(follow_up_on="2026-12-01"))
    out = app.undo_last()
    assert out["follow_up_on"] == before
    assert _application_row(db, "opp-1")["follow_up_on"] == before


def test_decision_is_one_undo_for_the_stage_and_the_item(db):
    app.set_application_stage("opp-1", app.StageIn(stage="accepted"))
    app.set_application_stage("opp-1", app.StageIn(stage="confirmed", decided=True))
    assert _status(db, "opp-1") == "handled"
    out = app.undo_last()
    assert out["status"] == "surfaced" and out["stage"] == "accepted"
    assert _status(db, "opp-1") == "surfaced"
    assert _application_row(db, "opp-1")["stage"] == "accepted"


def test_tracked_application_is_undone_by_removing_it(db):
    rec = app.add_application(app.ManualIn(title="Terrapin Fellow", stage="applying"))
    assert app.latest_undo()["item_id"] == rec["id"]
    app.undo_last()
    assert _application_row(db, rec["id"]) is None


# --- request bodies (2026-10-13 audit) ---------------------------------------

def test_replacing_prefs_requires_the_value_it_replaces():
    """A body without `entries` used to save an empty list: all watches gone."""
    from pydantic import ValidationError
    for model, body in ((app.WatchesBody, {"version": 1}),
                        (app.CustomBody, {"version": 1}),
                        (app.QuizBody, {"version": 1}),
                        (app.NotifySettingsBody, {"quiet": ["22:00", "07:00"], "cap": 6})):
        with pytest.raises(ValidationError):
            model(**body)
    assert app.WatchesBody(entries=[]).entries == []


def test_manual_application_fields_have_limits():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        app.ManualIn(title="x" * 301)
    assert app.ManualIn(title="x" * 300).title
