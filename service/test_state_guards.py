"""2026-10-10: lost-update guards between a run's save() and the page's writes.

A run loads state, works for a minute or two, then saves it. The page writes
in that window: goals (PUT /prefs/requirements), item dispositions, and error
rows. These tests replay that interleaving against a real temp database.

Run (from service/):
    ../venv/bin/python -m pytest test_state_guards.py -q
"""

import datetime as _dt

import pytest

import app
import db as dbmod
import notify
import orchestrator as O
import state_io_sqlite as sio
import store


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    real = dbmod.connect
    monkeypatch.setattr(app.dbmod, "connect", lambda p=None: real(path))
    monkeypatch.setattr(app, "_ledger_write", lambda row, d: None)
    return path


def _goal(gid, statement="A goal"):
    return {"id": gid, "statement": statement, "horizon_days": 180,
            "keywords": ["x"], "active": True, "quiet_after_days": 30}


def _item(iid, status, kind="assignment"):
    return {"id": iid, "kind": kind, "title": "Item " + iid,
            "course_label": "ECON001", "status": status}


def _state(goals=(), items=()):
    return {"items": list(items), "errors": [], "last_run_stats": [],
            "requirements": {"synced_at": None, "version": 1,
                             "entries": list(goals)}}

    return {"items": list(items), "errors": [], "last_run_stats": [],
            "requirements": {"synced_at": None, "version": 1,
                             "entries": list(goals)}}


def _goal_ids(path):
    return [e["id"] for e in sio.load(path)[0]["requirements"]["entries"]]


class TestGoalsSurviveTheRunSave:
    def test_goal_edit_made_during_a_run_is_not_reverted(self, dbpath):
        sio.save(_state([_goal("old")]), dbpath)
        run_copy = sio.load(dbpath)[0]          # the run starts here
        app.put_requirements(app.RequirementsBody(
            entries=[app.RequirementIn(id="new", statement="New goal")]))
        sio.save(run_copy, dbpath)              # the run finishes here
        assert _goal_ids(dbpath) == ["new"]

    def test_the_stored_stamp_is_not_rolled_back_by_the_run(self, dbpath):
        sio.save(_state([_goal("old")]), dbpath)
        run_copy = sio.load(dbpath)[0]
        app.put_requirements(app.RequirementsBody(
            entries=[app.RequirementIn(id="new", statement="New goal")]))
        stamped = sio.load(dbpath)[0]["requirements"]["synced_at"]
        assert stamped
        sio.save(run_copy, dbpath)
        assert sio.load(dbpath)[0]["requirements"]["synced_at"] == stamped

    def test_a_run_with_no_edit_in_its_window_still_writes_its_goals(self, dbpath):
        sio.save(_state([_goal("old")]), dbpath)
        run_copy = sio.load(dbpath)[0]
        run_copy["requirements"]["entries"].append(_goal("added-by-run"))
        sio.save(run_copy, dbpath)
        assert _goal_ids(dbpath) == ["old", "added-by-run"]


def _error_rows(path):
    conn = dbmod.connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT at, source, severity, message FROM errors ORDER BY seq")]
    finally:
        conn.close()


def _at(minutes_ago):
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestErrorsSurviveTheRunSave:
    def test_error_written_by_notify_survives_a_stale_run_save(self, dbpath):
        sio.save(_state(), dbpath)
        run_copy = sio.load(dbpath)[0]          # the run starts here
        conn = dbmod.connect(dbpath)
        notify._log_error(conn, _dt.datetime.now(notify.TZ), "push failed: boom")
        conn.close()
        sio.save(run_copy, dbpath)              # the run finishes here
        messages = [r["message"] for r in _error_rows(dbpath)]
        assert messages == ["push failed: boom"]

    def test_crash_keeps_the_errors_the_run_logged_before_it(self, dbpath, monkeypatch):
        monkeypatch.setattr(O.briefing_google, "send_daily_briefing_email",
                            lambda *a, **k: None)
        run = O.Run(db_path=dbpath, dry_run=False, due_only=False)
        run.log_error("step4a", "MAJOR", "earlier problem")
        O._record_crash(run, RuntimeError("late failure"))
        rows = _error_rows(dbpath)
        assert [(r["source"], r["severity"]) for r in rows] == [
            ("step4a", "MAJOR"), ("pipeline", "CRITICAL")]
        assert "late failure" in rows[-1]["message"]

    def test_persist_errors_writes_each_error_once(self, dbpath):
        run = O.Run(db_path=dbpath, dry_run=False, due_only=False)
        run.log_error("step6", "INFO", "one")
        run.persist_errors()
        run.persist_errors()
        run.log_error("step6", "INFO", "two")
        run.persist_errors()
        assert [r["message"] for r in _error_rows(dbpath)] == ["one", "two"]

    def test_dry_run_writes_no_errors(self, dbpath):
        run = O.Run(db_path=dbpath, dry_run=True, due_only=False)
        run.log_error("step6", "INFO", "dry")
        run.persist_errors()
        assert _error_rows(dbpath) == []


class TestErrorsRetention:
    def test_keeps_the_newest_fifty(self, dbpath):
        conn = dbmod.connect(dbpath)
        store.append_errors(conn, [
            {"timestamp": _at(i), "source": "s", "severity": "MINOR",
             "message": "m%d" % i} for i in range(60)])
        conn.commit()
        conn.close()
        msgs = [r["message"] for r in _error_rows(dbpath)]
        assert len(msgs) == 50
        assert "m0" in msgs and "m49" in msgs and "m50" not in msgs

    def test_drops_rows_older_than_thirty_days(self, dbpath):
        conn = dbmod.connect(dbpath)
        store.append_errors(conn, [
            {"timestamp": _at(40 * 24 * 60), "source": "s",
             "severity": "MINOR", "message": "ancient"},
            {"timestamp": _at(5), "source": "s", "severity": "MINOR",
             "message": "recent"}])
        conn.commit()
        conn.close()
        assert [r["message"] for r in _error_rows(dbpath)] == ["recent"]

    def test_load_lists_newest_first_by_time(self, dbpath):
        sio.save(_state(), dbpath)              # a meta row, so load() answers
        conn = dbmod.connect(dbpath)
        store.append_errors(conn, [
            {"timestamp": _at(1), "source": "s", "severity": "MINOR",
             "message": "newer"},
            {"timestamp": _at(30), "source": "s", "severity": "MINOR",
             "message": "older"}])      # appended second, but older
        conn.commit()
        conn.close()
        msgs = [e["message"] for e in sio.load(dbpath)[0]["errors"]]
        assert msgs == ["newer", "older"]



def _stored_status(path, iid):
    conn = dbmod.connect(path)
    try:
        return conn.execute("SELECT status FROM items WHERE id = ?",
                            (iid,)).fetchone()[0]
    finally:
        conn.close()


class TestItemStatusSurvivesTheRunSave:
    def test_restore_made_during_a_run_is_not_reverted(self, dbpath):
        sio.save(_state(items=[_item("i1", "handled")]), dbpath)
        run_copy = sio.load(dbpath)[0]          # the run sees it as handled
        app.restore_item("i1")                  # the page restores it
        sio.save(run_copy, dbpath)
        assert _stored_status(dbpath, "i1") == "new"

    def test_done_click_made_during_a_run_still_wins(self, dbpath):
        sio.save(_state(items=[_item("i2", "new")]), dbpath)
        run_copy = sio.load(dbpath)[0]
        app.mark_done("i2")
        sio.save(run_copy, dbpath)
        assert _stored_status(dbpath, "i2") == "handled"

    def test_status_the_run_changes_itself_is_kept(self, dbpath):
        sio.save(_state(items=[_item("i3", "new")]), dbpath)
        run_copy = sio.load(dbpath)[0]
        run_copy["items"][0]["status"] = "expired"   # lifecycle, no page edit
        sio.save(run_copy, dbpath)
        assert _stored_status(dbpath, "i3") == "expired"

    def test_closed_row_written_before_stamping_still_wins(self, dbpath):
        sio.save(_state(items=[_item("i4", "new")]), dbpath)
        run_copy = sio.load(dbpath)[0]
        conn = dbmod.connect(dbpath)
        conn.execute("UPDATE items SET status = 'dismissed', "
                     "status_changed_at = NULL WHERE id = 'i4'")
        conn.commit()
        conn.close()
        sio.save(run_copy, dbpath)
        assert _stored_status(dbpath, "i4") == "dismissed"

    def test_older_database_gains_the_stamp_column(self, tmp_path):
        path = str(tmp_path / "old.db")
        dbmod.init_db(path)
        conn = dbmod.connect(path)
        conn.execute("ALTER TABLE items DROP COLUMN status_changed_at")
        conn.commit()
        conn.close()
        dbmod.init_db(path)
        conn = dbmod.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        conn.close()
        assert "status_changed_at" in cols
