"""Repeated opportunities and the decision undo.

Run (from service/):
    ../venv/bin/python -m pytest test_repeated_opportunities.py -q
"""

import sqlite3

import orchestrator as o  # first: inserts ROOT onto sys.path
import app as webapp
import db as dbmod


class Run:
    db_path = None

    def __init__(self):
        self.errors = []

    def log_error(self, source, severity, message):
        self.errors.append(message)


def opp(id, title, org="Initech", date=None, first_seen="2026-10-03",
        status="new", ref="gmail-1"):
    return {"id": id, "kind": "opportunity", "title": title, "date": date,
            "status": status, "first_seen": first_seen,
            "opportunity": {"organization": org, "type": "internship"},
            "source_refs": [{"source": "gmail", "id": ref}]}


def _opener(path):
    real = dbmod.connect
    return lambda: real(path)


def collapse(items):
    run = Run()
    o._collapse_repeated_opportunities(run, items)
    return run


def test_same_title_org_and_date_expires_the_later_row(monkeypatch):
    monkeypatch.setattr(o.store, "applications_all", lambda p: {})
    a = opp("umd-systems-engineer-intern-2026-10-08", "Systems Engineer Intern",
            date="2026-10-08", ref="gmail-1")
    b = opp("umd-systems-engineer-intern-2026-10-08-3", "Systems Engineer Intern",
            date="2026-10-08", first_seen="2026-10-05", ref="gmail-2")
    items = [a, b]
    collapse(items)
    assert a["status"] == "new"
    assert b["status"] == "expired"
    assert b["expired_reason"] == "duplicate of umd-systems-engineer-intern-2026-10-08"
    refs = [r.get("id") for r in a["source_refs"]]
    assert refs == ["gmail-1", "gmail-2"]


def test_tracked_row_is_kept_over_an_earlier_one(monkeypatch):
    tracked = "umd-systems-engineer-intern-2026-10-08-2"
    monkeypatch.setattr(o.store, "applications_all", lambda p: {tracked: {}})
    a = opp("umd-systems-engineer-intern-2026-10-08", "Systems Engineer Intern",
            date="2026-10-08", first_seen="2026-10-01")
    b = opp(tracked, "Systems Engineer Intern", date="2026-10-08",
            first_seen="2026-10-06")
    collapse([a, b])
    assert a["status"] == "expired"
    assert b["status"] == "new"


def test_different_date_org_or_title_stays_separate(monkeypatch):
    monkeypatch.setattr(o.store, "applications_all", lambda p: {})
    items = [
        opp("x-1", "Systems Engineer Intern", date="2026-10-08"),
        opp("x-2", "Systems Engineer Intern", date="2026-10-09"),
        opp("x-3", "Systems Engineer Intern", org="MITRE", date="2026-10-08"),
        opp("x-4", "Software Engineer Intern", date="2026-10-08"),
    ]
    collapse(items)
    assert all(it["status"] == "new" for it in items)


def test_closed_rows_are_not_collapsed_into(monkeypatch):
    monkeypatch.setattr(o.store, "applications_all", lambda p: {})
    done = opp("x-1", "Systems Engineer Intern", date="2026-10-08",
               status="handled")
    live = opp("x-2", "Systems Engineer Intern", date="2026-10-08")
    collapse([done, live])
    assert done["status"] == "handled"
    assert live["status"] == "new"


def test_decided_and_restore_move_status_and_keep_the_ledger_honest(
        tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    conn.execute("INSERT INTO items (id, kind, title, status, regime, course_class, course_label, extra_json)"
                 " VALUES ('umd-x', 'opportunity', 'X', 'new', 'confirmed', 'umd', 'UMD', '{}')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(webapp.dbmod, "connect", _opener(path))
    written = []
    monkeypatch.setattr(webapp, "_ledger_write",
                        lambda row, disposition: written.append(disposition))

    assert webapp.decided_item("umd-x")["status"] == "handled"
    assert written == ["handled"]
    assert webapp.restore_item("umd-x")["status"] == "new"


def test_restore_leaves_an_open_row_alone(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    conn.execute("INSERT INTO items (id, kind, title, status, regime, course_class, course_label, extra_json)"
                 " VALUES ('umd-y', 'opportunity', 'Y', 'ongoing', 'confirmed', 'umd', 'UMD', '{}')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(webapp.dbmod, "connect", _opener(path))
    assert webapp.restore_item("umd-y")["status"] == "ongoing"
