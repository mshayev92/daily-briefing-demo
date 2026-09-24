"""
Tests for app.py's `_ledger_write()` -- the four disposition endpoints
(mark_done/mark_resolved/lead_confirmed/lead_killed) are the ONLY place a
`handled`/`dismissed`/`confirmed`/`killed` disposition is ever decided
(orchestrator.py's batch run only ever writes `surfaced`), so this is the
single most load-bearing piece of the whole ledger-wiring change: if it
silently no-ops, `detect_recurrence()`/`revealed_preference()` never see a
real disposition, ever.

No FastAPI TestClient here (this venv has no httpx installed, and adding a
new dependency for one test file is a bigger call than this task needs) --
`_ledger_write()` is tested directly against a fake sqlite3.Row-shaped dict,
which is all four endpoints actually pass it.

Run (from service/):
    ../venv/bin/python -m pytest test_app_ledger.py -q
"""

import datetime as _dt
import json

import app
import ledger


def _row(iid="lead-x-1234567890", kind="lead", date=None, regime="lead",
        requirement_ids=(), source_refs=()):
    return {"id": iid, "kind": kind, "date": date, "regime": regime,
            "extra_json": json.dumps({
                "requirement_ids": list(requirement_ids),
                "source_refs": list(source_refs)})}


class TestLedgerWrite:

    def test_writes_a_line_with_the_right_disposition(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setattr(app, "_LEDGER_PATH", str(path))
        app._ledger_write(_row(), "confirmed")
        rows, bad = ledger.read(str(path))
        assert bad == 0
        assert len(rows) == 1
        assert rows[0]["disposition"] == "confirmed"
        assert rows[0]["id"] == "lead-x-1234567890"
        assert rows[0]["regime"] == "lead"

    def test_carries_requirement_ids_through_from_extra_json(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setattr(app, "_LEDGER_PATH", str(path))
        app._ledger_write(_row(requirement_ids=["research"]), "killed")
        rows, _bad = ledger.read(str(path))
        assert rows[0]["requirement_ids"] == ["research"]

    def test_each_of_the_four_dispositions_is_accepted(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setattr(app, "_LEDGER_PATH", str(path))
        for i, disp in enumerate(("handled", "dismissed", "confirmed", "killed")):
            app._ledger_write(_row(iid="item-%d" % i), disp)
        rows, _bad = ledger.read(str(path))
        assert [r["disposition"] for r in rows] == \
            ["handled", "dismissed", "confirmed", "killed"]

    def test_a_ledger_write_failure_is_swallowed_not_raised(self, tmp_path, monkeypatch):
        """A click the user is waiting on must never fail because the
        ledger -- a side channel -- couldn't be written."""
        monkeypatch.setattr(app, "_LEDGER_PATH",
                            str(tmp_path / "no" / "such" / "dir" / "ledger.jsonl"))
        app._ledger_write(_row(), "handled")  # must not raise
