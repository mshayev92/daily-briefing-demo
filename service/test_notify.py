"""2026-09-29: app notifications (notify.py, /api/notifications, /notify/settings).

Run (from service/):
    ../venv/bin/python -m pytest test_notify.py -q
"""

import json
import datetime as _dt

import pytest

import app
import db as dbmod
import notify
import store

TZ = notify.TZ


def at(h, m=0, day=_dt.date(2026, 9, 29)):
    return _dt.datetime.combine(day, _dt.time(h, m), tzinfo=TZ)


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    monkeypatch.setattr(notify, "TOPIC_FILE", str(tmp_path / "topic"))
    (tmp_path / "topic").write_text("secret-topic\n")
    return path


def add_item(path, id, kind="assignment", date="2026-09-30", time="12:00",
             status="ongoing", title="HW 1", course="ECON001", act_by=None):
    conn = dbmod.connect(path)
    conn.execute(
        "INSERT INTO items (id, course_class, course_label, kind, title, date, "
        "time, status, act_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (id, "econ", course, kind, title, date, time, status, act_by))
    conn.commit()
    conn.close()


class Sink:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, topic, title, body, priority):
        if self.fail:
            raise OSError("down")
        self.calls.append((topic, title, body, priority))


def ledger(path):
    conn = dbmod.connect(path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY seq")]
    conn.close()
    return rows


def first_run(path, now):
    notify.run(path, now=now, poster=Sink())  # baseline


def published(path, day):
    """The page built and delivered on `day` (what step11 writes)."""
    conn = dbmod.connect(path)
    conn.execute("INSERT OR REPLACE INTO meta (id, last_completed_date) "
                 "VALUES (1, ?)", (day,))
    conn.commit()
    conn.close()


class TestDeadlines:
    def test_24h_once_then_dedup(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-30", time="08:00")
        sink = Sink()
        assert notify.run(dbpath, now=at(9), poster=sink) == 1
        assert notify.run(dbpath, now=at(10), poster=sink) == 0
        assert len(ledger(dbpath)) == 1

    def test_not_yet_within_24h(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-10-01", time="12:00")
        notify.run(dbpath, now=at(9), poster=Sink())
        assert ledger(dbpath) == []

    def test_3h_only_for_exams(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "hw", kind="assignment", date="2026-09-29", time="11:00")
        add_item(dbpath, "ex", kind="assessment", date="2026-09-29", time="11:00")
        notify.run(dbpath, now=at(9), poster=Sink())
        keys = {r["key"] for r in ledger(dbpath)}
        assert "deadline3:ex:2026-09-29" in keys
        assert "deadline3:hw:2026-09-29" not in keys

    def test_done_dismissed_and_past_skipped(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "d", status="handled", date="2026-09-29", time="12:00")
        add_item(dbpath, "x", status="dismissed", date="2026-09-29", time="12:00")
        add_item(dbpath, "p", date="2026-09-29", time="08:00")
        notify.run(dbpath, now=at(9), poster=Sink())
        assert ledger(dbpath) == []

    def test_rows_the_page_hides_are_not_pushed(self, dbpath):
        """2026-09-29: a syllabus placeholder the page hid as a duplicate
        pushed "Deadline soon" beside its Canvas twin."""
        first_run(dbpath, at(9))
        add_item(dbpath, "ph", date="2026-09-29", time="20:00")
        add_item(dbpath, "hw", date="2026-09-29", time="20:00")
        notify.store.kv_set(dbpath, "page_hidden_ids", ["ph"])
        notify.run(dbpath, now=at(9), poster=Sink())
        assert {r["item_id"] for r in ledger(dbpath)} == {"hw"}


class TestPush:
    def test_push_carries_the_text(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", title="Secret Essay", course="PHIL001",
                 date="2026-09-29", time="20:00")
        sink = Sink()
        notify.run(dbpath, now=at(9), poster=sink)
        (topic, title, body, _), = sink.calls
        assert topic == "secret-topic"
        assert title == "Deadline soon"
        assert "PHIL001: Secret Essay" in body

    def test_quiet_hours_hold_then_release_coalesced(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-30", time="12:00")
        add_item(dbpath, "b", date="2026-09-30", time="13:00")
        sink = Sink()
        assert notify.run(dbpath, now=at(23), poster=sink) == 0
        assert sink.calls == []
        assert notify.run(dbpath, now=at(7, 1, _dt.date(2026, 9, 30)), poster=sink) == 1
        assert sink.calls[0][1] == "Deadline soon · 2"
        assert len(sink.calls[0][2].splitlines()) == 2

    def test_held_stale_event_is_dropped(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-30", time="05:00")
        sink = Sink()
        notify.run(dbpath, now=at(23), poster=sink)
        notify.run(dbpath, now=at(7, 1, _dt.date(2026, 9, 30)), poster=sink)
        assert sink.calls == []

    def test_daily_cap(self, dbpath):
        store.kv_set(dbpath, notify.SETTINGS_KEY, {"cap": 2})
        first_run(dbpath, at(9))
        sink = Sink()
        for i in range(4):
            add_item(dbpath, "e%d" % i, kind="assessment", date="2026-09-29",
                     time="20:00")
            notify.run(dbpath, now=at(9, i), poster=sink)
            # a new kind each round would coalesce; same kind -> one push/run
        assert len(sink.calls) == 2

    def test_push_failure_never_raises_and_retries(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-29", time="20:00")
        assert notify.run(dbpath, now=at(9), poster=Sink(fail=True)) == 0
        sink = Sink()
        assert notify.run(dbpath, now=at(9, 15), poster=sink) == 1

    def test_many_kinds_become_one_summary(self, dbpath):
        first_run(dbpath, at(9))
        conn = dbmod.connect(dbpath)
        for i, k in enumerate(("deadline", "grade", "opportunity", "followup")):
            conn.execute("INSERT INTO notifications (key,kind,title,body,created_at)"
                         " VALUES (?,?,?,?,?)", ("k%d" % i, k, "t", "b", "x"))
        conn.commit()
        conn.close()
        sink = Sink()
        notify.run(dbpath, now=at(9), poster=sink)
        assert [c[1] for c in sink.calls] == ["Briefing · 4 updates"]
        assert sink.calls[0][2] == "b\nb\nb\nb"

    def test_kind_switched_off(self, dbpath):
        store.kv_set(dbpath, notify.SETTINGS_KEY, {"kinds": {"deadline": False}})
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-29", time="20:00")
        assert notify.run(dbpath, now=at(9), poster=Sink()) == 0
        assert ledger(dbpath) == []

    def test_no_topic_file_no_push(self, dbpath, tmp_path):
        (tmp_path / "topic").unlink()
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-29", time="20:00")
        sink = Sink()
        assert notify.run(dbpath, now=at(9), poster=sink) == 0
        assert len(ledger(dbpath)) == 1 and sink.calls == []


class TestOtherKinds:
    def test_opportunities_baseline_then_new(self, dbpath):
        add_item(dbpath, "old", kind="opportunity", status="new", date=None, time=None)
        sink = Sink()
        notify.run(dbpath, now=at(9), poster=sink)
        assert sink.calls == []                   # existing = baseline
        add_item(dbpath, "new1", kind="lead", status="open", date=None, time=None)
        notify.run(dbpath, now=at(9, 15), poster=sink)
        assert [(c[1], c[2]) for c in sink.calls] == [("New opportunity", "HW 1")]

    def test_grade_change(self, dbpath):
        conn = dbmod.connect(dbpath)
        for stamp, score, n in (("2026-09-28T10:00:00Z", 95.8, 4),
                                ("2026-09-29T13:00:00Z", 97.2, 6)):
            conn.execute("INSERT INTO course_grade_snapshots (captured_at,"
                         "course_label,current_score,graded_count) VALUES (?,?,?,?)",
                         (stamp, "PHIL001", score, n))
        conn.commit()
        conn.close()
        ev = notify.grade_events(dbmod.connect(dbpath), at(15))
        assert len(ev) == 1 and "97.2%" in ev[0]["body"] and "95.8%" in ev[0]["body"]

    def test_grade_same_score_says_what_changed(self, dbpath):
        """2026-10-11 push read "ENGL001: 100% (was 100%)"."""
        conn = dbmod.connect(dbpath)
        for stamp, n in (("2026-09-28T10:00:00Z", 4), ("2026-09-29T13:00:00Z", 5)):
            conn.execute("INSERT INTO course_grade_snapshots (captured_at,"
                         "course_label,current_score,graded_count) VALUES (?,?,?,?)",
                         (stamp, "ENGL001", 100.0, n))
        conn.commit()
        conn.close()
        ev = notify.grade_events(dbmod.connect(dbpath), at(15))
        assert [e["body"] for e in ev] == ["ENGL001: 100%, 1 more item graded"]

    def test_grade_push_names_what_was_graded(self, dbpath):
        conn = dbmod.connect(dbpath)
        for stamp, score, items in (
                ("2026-09-28T10:00:00Z", 97.9, [["Quiz 4", 9, 10]]),
                ("2026-09-29T13:00:00Z", 100.0, [["Digital Forum", 10, 10],
                                                 ["Quiz 4", 9, 10], ["Quiz 5", 10.0, 10]])):
            conn.execute("INSERT INTO course_grade_snapshots (captured_at,"
                         "course_label,current_score,graded_count,graded_items) "
                         "VALUES (?,?,?,?,?)",
                         (stamp, "PHIL001", score, len(items), json.dumps(items)))
        conn.commit()
        conn.close()
        ev = notify.grade_events(dbmod.connect(dbpath), at(15))
        assert [e["body"] for e in ev] == [
            "PHIL001 · Digital Forum 10/10, Quiz 5 10/10 · course grade 100% (was 97.9%)"]

    def test_grade_push_without_item_names_keeps_the_count(self, dbpath):
        """A snapshot from before graded_items existed (NULL) falls back."""
        conn = dbmod.connect(dbpath)
        conn.execute("INSERT INTO course_grade_snapshots (captured_at,course_label,"
                     "current_score,graded_count) VALUES ('2026-09-28T10:00:00Z','ENGL001',100,4)")
        conn.execute("INSERT INTO course_grade_snapshots (captured_at,course_label,"
                     "current_score,graded_count,graded_items) VALUES "
                     "('2026-09-29T13:00:00Z','ENGL001',100,5,'[[\"W5\",10,10]]')")
        conn.commit()
        conn.close()
        ev = notify.grade_events(dbmod.connect(dbpath), at(15))
        assert [e["body"] for e in ev] == ["ENGL001: 100%, 1 more item graded"]

    def test_followup_due_today(self, dbpath):
        add_item(dbpath, "app1", kind="opportunity", status="new", date=None, time=None,
                 title="Acme Fellows")
        conn = dbmod.connect(dbpath)
        conn.execute("INSERT INTO applications (item_id,stage,stage_on,follow_up_on,"
                     "updated_at) VALUES ('app1','submitted','2026-09-08','2026-09-29','x')")
        conn.commit()
        ev = notify.followup_events(conn, at(9))
        assert [e["body"] for e in ev] == ["Acme Fellows"]

    def test_source_alert_after_two_failures_once_then_recovery(self, dbpath):
        state = {}
        conn = dbmod.connect(dbpath)

        def status(ok, attempt):
            store.kv_set(dbpath, "source_status", {"canvas": {
                "ok": ok, "last_attempt": attempt, "last_success": attempt,
                "error": "login expired"}})
        status(False, "t1")
        assert notify.source_events(conn, dbpath, at(9), state) == []
        status(False, "t1")                      # same tick seen twice
        assert notify.source_events(conn, dbpath, at(9), state) == []
        status(False, "t2")
        (alert,) = notify.source_events(conn, dbpath, at(9), state)
        assert alert["priority"] == "high"
        status(False, "t3")
        assert notify.source_events(conn, dbpath, at(9), state) == []
        status(True, "t4")
        (ok,) = notify.source_events(conn, dbpath, at(9), state)
        assert ok["title"] == "Source recovered"

    def test_brief_event_counts_next_two_days(self, dbpath):
        add_item(dbpath, "a", date="2026-09-30", time="12:00")
        add_item(dbpath, "b", date="2026-10-09", time="12:00")
        sink = Sink()
        first_run(dbpath, at(6))
        published(dbpath, "2026-09-29")
        notify.run(dbpath, now=at(9), poster=sink)
        brief = [r for r in ledger(dbpath) if r["kind"] == "brief"][0]
        assert brief["body"] == "1 due in the next 2 days"

    def test_brief_waits_for_the_day_to_publish_and_goes_out_once(self, dbpath):
        """2026-10-10: the brief used to follow the morning run whether or not
        its build published, so a failed build announced yesterday's page."""
        first_run(dbpath, at(6))
        sink = Sink()
        notify.run(dbpath, now=at(9), poster=sink)           # not published yet
        assert [r for r in ledger(dbpath) if r["kind"] == "brief"] == []
        published(dbpath, "2026-09-29")                      # a retry publishes
        notify.run(dbpath, now=at(10), poster=sink)
        notify.run(dbpath, now=at(11), poster=sink)          # same day again
        assert [r["title"] for r in ledger(dbpath) if r["kind"] == "brief"] == [
            "Brief ready"]
        assert [c[1] for c in sink.calls] == ["Brief ready"]

    def test_brief_push_carries_weather(self, dbpath):
        import sources
        sources.cache_put(dbpath, "weather", at(9).date(), {
            "precip_probability": 60, "umbrella_needed": True, "temp_now": 58.2,
            "feels_like": 51.0, "temp_max": 71.6, "temp_min": 55.2,
            "conditions": "Rain"})
        sink = Sink()
        first_run(dbpath, at(6))
        published(dbpath, "2026-09-29")
        notify.run(dbpath, now=at(9), poster=sink)
        (_, title, body, _), = sink.calls
        assert title == "Brief ready"
        assert body.splitlines()[0] == (
            "Bring an umbrella (60% rain) \u00b7 58\u00b0 now (feels 51\u00b0) "
            "\u00b7 high 72\u00b0 / low 55\u00b0 \u00b7 Rain")


class TestApi:
    @pytest.fixture(autouse=True)
    def patch_db(self, dbpath, monkeypatch):
        real = dbmod.connect
        monkeypatch.setattr(dbmod, "connect", lambda p=None: real(dbpath))
        monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", dbpath)
        self.path = dbpath

    def test_events_since_and_read(self):
        first_run(self.path, at(9))
        add_item(self.path, "a", date="2026-09-29", time="23:00")
        notify.run(self.path, now=at(10), poster=Sink())
        out = app.api_notifications(since=0)
        assert len(out["events"]) == 1 and out["events"][0]["body"].startswith("ECON001: HW 1")
        assert app.api_notifications(since=out["events"][0]["seq"])["events"] == []
        app.api_notification_read(out["events"][0]["key"])
        assert ledger(self.path)[0]["read_at"]

    def test_settings_roundtrip_and_validation(self):
        body = app.NotifySettingsBody(kinds={"grade": False}, quiet=["23:00", "06:00"], cap=3)
        got = app.put_notify_settings(body)
        assert got["kinds"]["grade"] is False and got["quiet"] == ["23:00", "06:00"]
        with pytest.raises(app.HTTPException):
            app.put_notify_settings(app.NotifySettingsBody(kinds={"nope": True}))
        with pytest.raises(app.HTTPException):
            app.put_notify_settings(app.NotifySettingsBody(kinds={}, quiet=["25:00", "06:00"]))

    def test_glance(self):
        add_item(self.path, "a", date="2099-01-01", time="12:00")
        g = app.api_glance()
        assert g["next"][0]["id"] == "a" and g["attention_count"] == 0


def test_quiet_window_wraps_midnight():
    q = ["22:00", "07:00"]
    assert notify.in_quiet(at(23), q) and notify.in_quiet(at(3), q)
    assert not notify.in_quiet(at(7), q) and not notify.in_quiet(at(12), q)


def test_tick_that_pushes_nothing_prints_nothing(dbpath, capsys):
    """run_daily.sh runs `notify.py run` on every 15-minute tick; a tick with
    nothing to push must not add a line to the daily log."""
    assert notify.main(["run", "--db", dbpath]) == 0
    assert capsys.readouterr().out == ""


class TestHeldEventsAtRelease:
    """2026-10-10: a push held in quiet hours was sent at release even when
    Michael had handled or dismissed its item in the meantime."""

    RELEASE = _dt.date(2026, 9, 30)

    def test_item_closed_before_release_is_not_pushed(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-30", time="12:00")
        notify.run(dbpath, now=at(23), poster=Sink())      # held overnight
        conn = dbmod.connect(dbpath)
        conn.execute("UPDATE items SET status = 'handled' WHERE id = 'a'")
        conn.commit()
        conn.close()
        sink = Sink()
        assert notify.run(dbpath, now=at(7, 1, self.RELEASE), poster=sink) == 0
        assert sink.calls == []
        assert ledger(dbpath)[0]["pushed_at"]               # closed, not held

    def test_item_still_open_at_release_is_pushed(self, dbpath):
        first_run(dbpath, at(9))
        add_item(dbpath, "a", date="2026-09-30", time="12:00")
        notify.run(dbpath, now=at(23), poster=Sink())
        sink = Sink()
        assert notify.run(dbpath, now=at(7, 1, self.RELEASE), poster=sink) == 1
        assert sink.calls[0][1] == "Deadline soon"

    def _hold_source_alert(self, path):
        conn = dbmod.connect(path)
        alert = notify._ev("source:canvas:t1", "source", None, "Source needs you",
                           "canvas is failing: login", "high")
        notify.record(conn, [alert], at(23), notify.default_settings())
        conn.close()

    def test_source_alert_dropped_once_the_source_recovers(self, dbpath):
        first_run(dbpath, at(9))
        self._hold_source_alert(dbpath)
        store.kv_set(dbpath, "source_status", {"canvas": {
            "ok": True, "last_attempt": "t2", "last_success": "t2"}})
        sink = Sink()
        notify.run(dbpath, now=at(7, 1, self.RELEASE), poster=sink)
        assert sink.calls == []

    def test_source_alert_still_pushed_while_the_source_fails(self, dbpath):
        first_run(dbpath, at(9))
        self._hold_source_alert(dbpath)
        store.kv_set(dbpath, "source_status", {"canvas": {
            "ok": False, "last_attempt": "t2", "error": "login"}})
        sink = Sink()
        notify.run(dbpath, now=at(7, 1, self.RELEASE), poster=sink)
        assert [c[1] for c in sink.calls] == ["Source needs you"]
