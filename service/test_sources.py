"""2026-09-29: the Sources sheet (sources.py, GET/PUT /sources), the
per-source gating of the pipeline, and Handshake items (handshake.py).

Run (from service/):
    ../venv/bin/python -m pytest test_sources.py -q
"""

import datetime as _dt
import subprocess

import pytest
from fastapi import HTTPException

import app
import db as dbmod
import handshake
import orchestrator as o
import sources

TZ = sources.TZ
TODAY = _dt.date(2026, 9, 29)


def at(h, m=0, day=TODAY):
    return _dt.datetime.combine(day, _dt.time(h, m), tzinfo=TZ)


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    real = dbmod.connect
    monkeypatch.setattr(dbmod, "connect", lambda p=None: real(path))
    return path


# --- due rules -----------------------------------------------------------------

class TestDue:
    daily = {"enabled": True, "interval_hours": 24}

    def test_never_run_is_due(self):
        assert sources.is_due(self.daily, {}, at(3))

    def test_daily_is_due_once_at_or_after_the_morning(self):
        yesterday = {"last_attempt": at(6, 31, TODAY - _dt.timedelta(1)).isoformat(),
                     "ok": True}
        assert not sources.is_due(self.daily, yesterday, at(6, 20))
        assert sources.is_due(self.daily, yesterday, at(6, 30))
        ran = {"last_attempt": at(6, 31).isoformat(), "ok": True}
        assert not sources.is_due(self.daily, ran, at(23))
        assert sources.next_due(self.daily, ran, at(9)) == at(6, 30, TODAY + _dt.timedelta(1))

    def test_hourly_uses_elapsed_time_with_grace(self):
        s = {"enabled": True, "interval_hours": 2}
        st = {"last_attempt": at(8).isoformat(), "ok": True}
        assert not sources.is_due(s, st, at(9, 50))
        assert sources.is_due(s, st, at(9, 56))

    def test_failure_retries_after_an_hour_at_most(self):
        st = {"last_attempt": at(6, 30).isoformat(), "ok": False}
        assert not sources.is_due(self.daily, st, at(7, 0))
        assert sources.is_due(self.daily, st, at(7, 26))

    def test_off_is_never_due(self):
        assert sources.next_due({"enabled": False, "interval_hours": 1}, {}, at(9)) is None

    def test_clean_settings_fills_defaults_and_drops_bad_values(self):
        got = sources.clean_settings({"canvas": {"enabled": False, "interval_hours": 3},
                                      "bogus": {"enabled": True}})
        assert got["canvas"] == {"enabled": False, "interval_hours": 24}
        assert "bogus" not in got and set(got) == set(sources.SOURCES)


class TestRuns:
    def test_record_and_due_now(self, dbpath):
        sources.record(dbpath, "canvas", True, now=at(6, 31), detail="ok")
        sources.record(dbpath, "handshake", False, now=at(6, 31), detail="not signed in")
        st = sources.load_status(dbpath)
        assert st["canvas"]["ok"] and st["canvas"]["last_success"]
        assert st["handshake"]["error"] == "not signed in"
        assert "canvas" not in sources.due_now(dbpath, at(8))
        assert "handshake" in sources.due_now(dbpath, at(8), kind="scraper")

    def test_run_scraper_records_the_last_output_line(self, dbpath):
        def fake(argv, **kw):
            assert argv[-1] == "handshake_scraper"
            return subprocess.CompletedProcess(argv, 0, "x\n20 jobs, 3 events\n", "")
        assert sources.run_scraper("handshake", dbpath, now=at(7), runner=fake)
        assert sources.load_status(dbpath)["handshake"]["detail"] == "20 jobs, 3 events"

    def test_handshake_queries_are_active_goal_keywords(self, dbpath):
        conn = dbmod.connect(dbpath)
        conn.execute("INSERT INTO requirements (id, statement, horizon_days, "
                     "keywords_json, active, quiet_after_days) VALUES "
                     "('a', 's', 90, '[\"NREIP\", \"Acme Fellows\"]', 1, 30), "
                     "('b', 's', 90, '[\"Off one\"]', 0, 30)")
        conn.commit()
        conn.close()
        argv = sources.scraper_command("handshake", dbpath)[0]
        assert argv[-4:] == ["--query", "NREIP", "--query", "Acme Fellows"]

    def test_tick_builds_only_after_the_morning_build(self, dbpath):
        sources.record(dbpath, "gmail", True, now=at(6, 31))
        assert not sources.build_wanted(dbpath, at(5), refreshed=["canvas"])
        sources.record_build_attempt(dbpath, at(6, 30))
        conn = dbmod.connect(dbpath)
        conn.execute("INSERT INTO meta (id, schema_version, last_completed_date) "
                     "VALUES (1, 11, ?)", (TODAY.isoformat(),))
        conn.commit()
        conn.close()
        assert sources.build_wanted(dbpath, at(9), refreshed=["canvas"])
        assert not sources.build_wanted(dbpath, at(5), refreshed=["canvas"])


class TestBuildRetries:
    """2026-10-10: a failed 06:30 build used to leave yesterday's page up all
    day, because a tick rebuilds only after a published build."""

    @staticmethod
    def publish(path):
        conn = dbmod.connect(path)
        conn.execute("INSERT INTO meta (id, schema_version, last_completed_date) "
                     "VALUES (1, 11, ?)", (TODAY.isoformat(),))
        conn.commit()
        conn.close()

    def test_failed_morning_build_is_retried_hourly(self, dbpath):
        sources.record_build_attempt(dbpath, at(6, 30))
        assert not sources.build_wanted(dbpath, at(7, 0))
        assert sources.build_wanted(dbpath, at(7, 31))
        sources.record_build_attempt(dbpath, at(7, 31))
        assert not sources.build_wanted(dbpath, at(8, 0))
        assert sources.build_wanted(dbpath, at(8, 31))

    def test_retries_stop_after_the_daily_limit(self, dbpath):
        for h in range(6, 6 + sources.BUILD_ATTEMPTS_PER_DAY):
            sources.record_build_attempt(dbpath, at(h, 30))
        assert not sources.retry_left(dbpath, at(18))
        assert not sources.build_wanted(dbpath, at(18))

    def test_a_missed_morning_build_is_caught_up_by_the_first_tick(self, dbpath):
        assert sources.build_wanted(dbpath, at(9))

    def test_a_tick_at_0630_does_not_build_ahead_of_the_morning_run(self, dbpath):
        """Both timers fire at 06:30. The morning run builds, not the tick
        that happens to win the lock."""
        assert not sources.build_wanted(dbpath, at(6, 30))
        assert not sources.build_wanted(dbpath, at(6, 45))
        assert sources.build_wanted(dbpath, at(7, 31))

    def test_a_published_day_rebuilds_only_for_new_data(self, dbpath):
        sources.record_build_attempt(dbpath, at(6, 30))
        for name, src in sources.SOURCES.items():
            if src["kind"] == "pipeline":
                sources.record(dbpath, name, True, now=at(6, 31))
        self.publish(dbpath)
        assert not sources.build_wanted(dbpath, at(9))
        assert not sources.retry_left(dbpath, at(9))
        assert sources.build_wanted(dbpath, at(9), refreshed=["canvas"])

    def test_the_attempt_count_resets_each_day(self, dbpath):
        sources.record_build_attempt(dbpath, at(6, 30))
        tomorrow = TODAY + _dt.timedelta(days=1)
        assert sources.retry_left(dbpath, at(7, 31, day=tomorrow))
        assert sources.build_wanted(dbpath, at(7, 31, day=tomorrow))


# --- page API ------------------------------------------------------------------

class TestApi:
    def put(self, settings):
        return app.put_sources(app.SourcesBody(settings=settings))

    def test_get_and_put(self, dbpath):
        got = app.get_sources()
        assert [r["name"] for r in got["sources"]] == list(sources.SOURCES)
        assert got["intervals"] == list(sources.INTERVALS)
        put = self.put({"gmail": {"enabled": True, "interval_hours": 2},
                        "weather": {"enabled": False, "interval_hours": 24}})
        rows = {r["name"]: r for r in put["sources"]}
        assert rows["gmail"]["interval_hours"] == 2
        assert rows["weather"]["enabled"] is False and rows["weather"]["next_due"] is None
        assert rows["canvas"]["enabled"] is True
        assert sources.load_settings(dbpath)["gmail"]["interval_hours"] == 2

    def test_put_rejects_unknown_source_and_interval(self, dbpath):
        with pytest.raises(HTTPException):
            self.put({"myspace": {"enabled": True, "interval_hours": 24}})
        with pytest.raises(HTTPException):
            self.put({"gmail": {"enabled": True, "interval_hours": 3}})


# --- pipeline gating -----------------------------------------------------------

class TestSourceStep:
    def ctx(self, enabled, fetch):
        return {"today": TODAY, "sources": {"enabled": set(enabled), "fetch": set(fetch)}}

    def test_off_returns_default_without_calling(self, dbpath):
        run = o.Run(db_path=dbpath, dry_run=False)
        got = o._source_step(run, self.ctx([], []), "gmail", "step1-2",
                             lambda: pytest.fail("called"), [])
        assert got == [] and "turned off" in run.skipped[0]

    def test_not_due_uses_todays_cache_else_fetches(self, dbpath):
        run = o.Run(db_path=dbpath, dry_run=False)
        ctx = self.ctx(["weather"], ["weather"])
        got = o._source_step(run, ctx, "weather", "step5", lambda: {"t": 1}, None,
                             cache=True)
        assert got == {"t": 1}
        assert sources.load_status(dbpath)["weather"]["ok"] is True
        ctx = self.ctx(["weather"], [])
        assert o._source_step(run, ctx, "weather", "step5",
                              lambda: pytest.fail("called"), None, cache=True) == {"t": 1}
        run2 = o.Run(db_path=dbpath, dry_run=False)
        tomorrow = dict(ctx, today=TODAY + _dt.timedelta(1))
        assert o._source_step(run2, tomorrow, "weather", "step5", lambda: {"t": 2},
                              None, cache=True) == {"t": 2}

    def test_not_due_without_cache_skips(self, dbpath):
        run = o.Run(db_path=dbpath, dry_run=False)
        got = o._source_step(run, self.ctx(["campus"], []), "campus", "step5b",
                             lambda: pytest.fail("called"), [])
        assert got == [] and "not due" in run.skipped[0]

    def test_what_i_cant_see_names_sources_and_skips_schedules(self, dbpath):
        """2026-10-02: every hourly rebuild listed "step1-2 was not read
        this morning -- Email not due this build" as a gap."""
        run = o.Run(db_path=dbpath, dry_run=False)
        ctx = self.ctx(["campus"], [])
        o._source_step(run, ctx, "campus", "step5b", lambda: [], [])
        o._source_step(run, ctx, "gmail", "step1-2", lambda: [], [])

        def boom():
            raise ValueError("time data 'Early Febr'")
        o._source_step(run, self.ctx(["discovery"], ["discovery"]),
                       "discovery", "step5c", boom, [])
        assert o._run_events(run) == [
            {"source": "Web search", "outcome": "failed",
             "detail": "ValueError: time data 'Early Febr'"},
            {"source": "Email", "outcome": "skipped",
             "detail": "Email is turned off in Sources"}]

    def test_footer_says_when_mail_was_last_read_on_a_build_without_it(self, dbpath):
        """2026-10-02 1:45 PM: "No new mail to sort · filing on · repeat
        run" on a build that never read email."""
        ctx = {"state": {}, "today": TODAY, "is_repeat": True,
               "sources": {"enabled": {"gmail"}, "fetch": set()}}
        sources.record(dbpath, "gmail", True, now=_dt.datetime(
            2026, 9, 29, 6, 35, tzinfo=sources.TZ))
        assert o._status_label(ctx, 0, dbpath) == "Mail last read 6:35 AM · filing on"
        ctx["sources"]["fetch"] = {"gmail"}
        assert o._status_label(ctx, 0, dbpath) == "No new mail to sort · filing on"
        assert o._status_label(ctx, 3, dbpath) == "3 emails sorted · filing on"
        ctx["sources"]["enabled"] = set()
        assert o._status_label(ctx, 0, dbpath) == "Email is off in Sources"

    def test_a_major_error_records_a_failure(self, dbpath):
        run = o.Run(db_path=dbpath, dry_run=False)

        def step():
            run.log_error("step4", "MAJOR", "Calendar read failed: 401")
            return {"timeline": []}
        o._source_step(run, self.ctx(["calendar"], ["calendar"]), "calendar",
                       "step4", step, None, cache=True)
        st = sources.load_status(dbpath)["calendar"]
        assert st["ok"] is False and "401" in st["error"]
        assert sources.cache_get(dbpath, "calendar", TODAY) is None


# --- Handshake items -------------------------------------------------------------

def job(jid, title, employer="Acme", queries=("software engineering intern",),
        expires="2026-10-20T23:59:59-04:00"):
    return {"id": jid, "title": title, "employer": employer,
            "url": "https://umd.joinhandshake.com/jobs/%s" % jid,
            "job_type": "Internship", "employment_type": "Full-Time",
            "locations": ["Boston, MA"], "remote": False, "hybrid": True,
            "on_site": False, "pay": {"min": 36.0, "max": 45.0, "per": "per hour"},
            "start_date": "2027-06-07", "expires_at": expires,
            "matched_queries": list(queries)}


def event(eid, name, start, end, kind="event", **kw):
    return dict({"id": "%s-%s" % (kind, eid), "kind": kind, "name": name,
                 "url": "https://umd.joinhandshake.com/stu/events/%s" % eid,
                 "start": start, "end": end, "medium": "IN_PERSON", "host": "UMD",
                 "categories": ["Employers"], "registered": False,
                 "favorited": False, "registration_ends": None,
                 "employers_count": 0}, **kw)


class TestHandshakeJobs:
    def test_generic_search_hit_is_not_a_lead(self):
        """2026-10-09: generic internship hits are unvetted for his
        eligibility and fit, so they never reach the Leads section."""
        assert handshake.job_leads({"jobs": [job("1", "SWE Intern")]}, TODAY, []) == []

    def test_goal_search_hit_becomes_a_lead_with_the_listing(self):
        goals = [("intern", ["Acme Fellows internship"])]
        jobs = [job("1", "Fellows Intern", "Acme, Inc.", ["Acme Fellows internship"])]
        [lead] = handshake.job_leads({"jobs": jobs}, TODAY, goals)
        assert lead["id"] == "lead-handshake-1" and lead["regime"] == "lead"
        assert lead["source_url"].endswith("/jobs/1")
        assert lead["date"] == "2026-10-20"
        assert "$36-45 per hour" in lead["basis"][0]["claim"]
        assert lead["opportunity"]["apply_url"].endswith("/jobs/1")

    def test_goal_search_hit_must_name_the_program(self):
        goals = [("intern", ["Acme Fellows internship"])]
        jobs = [job("1", "Fellows Intern", "Acme, Inc.", ["Acme Fellows internship"]),
                job("2", "Data Intern", "Acme", ["Acme Fellows internship"])]
        leads = handshake.job_leads({"jobs": jobs}, TODAY, goals)
        assert [l["id"] for l in leads] == ["lead-handshake-1"]
        assert leads[0]["requirement_ids"] == ["intern"]

    def test_goal_program_listing_is_a_lead_whatever_its_type(self):
        """2026-10-02: a full-time "Application Developer Analyst [2027 EDGE
        Program]" led the leads. A named goal program still counts."""
        named = dict(job("2", "Fellows Analyst", "Acme",
                         ["Acme Fellows internship"]), job_type="Job")
        leads = handshake.job_leads(
            {"jobs": [named, job("3", "SWE Intern")]}, TODAY,
            [("intern", ["Acme Fellows internship"])])
        assert [l["id"] for l in leads] == ["lead-handshake-2"]

    def test_listing_closing_within_two_days_is_skipped(self):
        soon = job("1", "SWE Intern", expires="2026-09-30T23:59:59-04:00")
        assert handshake.job_leads({"jobs": [soon]}, TODAY, []) == []


class TestHandshakeEvents:
    def test_fairs_and_registered_always_others_by_interest(self):
        evs = [
            event("1", "Fall Career Fair", "2026-10-01T11:00:00-04:00",
                  "2026-10-01T15:00:00-04:00", kind="career_fair", employers_count=286),
            event("2", "Teaching office hours", "2026-09-30T10:00:00-04:00",
                  "2026-09-30T11:00:00-04:00"),
            event("3", "Intro to Machine Learning careers", "2026-10-02T12:00:00-04:00",
                  "2026-10-02T13:00:00-04:00"),
            event("4", "Month-long software program", "2026-09-30T00:00:00-04:00",
                  "2026-11-30T00:00:00-05:00"),
            event("5", "Far-off panel", "2026-11-20T12:00:00-05:00",
                  "2026-11-20T13:00:00-05:00", registered=True),
        ]
        got = handshake.event_items({"events": evs}, TODAY,
                                    ["machine learning", "software"])
        ids = [it["id"] for it in got]
        assert ids == ["campus-hs-career-fair-1", "campus-hs-event-5", "campus-hs-event-3"]
        fair = got[0]
        assert fair["date"] == "2026-10-01" and fair["time"] == "11:00"
        assert fair["course_label"] == "Campus" and "286 employers" in fair["notes"]
        assert fair["links"][0]["url"].endswith("/stu/events/1")

    def test_blocked_keyword_drops_even_a_fair(self):
        evs = [event("1", "Law School Fair", "2026-10-01T11:00:00-04:00",
                     "2026-10-01T15:00:00-04:00", kind="career_fair")]
        assert handshake.event_items({"events": evs}, TODAY, [], blocked=["law"]) == []


def test_step5g_reads_the_scrape(dbpath, tmp_path, monkeypatch):
    path = tmp_path / "latest.json"
    path.write_text('{"scraped_at": "2026-09-29T10:00:00+00:00", "jobs": [], '
                    '"events": [], "errors": ["applications: boom"]}')
    monkeypatch.setattr(handshake, "LATEST", str(path))
    run = o.Run(db_path=dbpath, dry_run=False)
    ctx = {"today": TODAY, "state": {"courses": {"CMSC001": {}}}}
    assert o._handshake_items(run, ctx) == []
    assert any("applications: boom" in e["message"] for e in run.errors)


def test_handshake_event_routes_to_the_campus_section():
    import lifecycle
    ev = event("1", "Fall Career Fair", "2026-10-01T11:00:00-04:00",
               "2026-10-01T15:00:00-04:00", kind="career_fair")
    [item] = handshake.event_items({"events": [ev]}, TODAY, [])
    assert lifecycle.section_for(item, TODAY) == "campus"
    assert item["relevance"]["score"] == handshake.SCORE_FAIR


def test_sheet_shows_pipeline_sources_waiting_for_the_morning_build():
    s = {"enabled": True, "interval_hours": 24}
    assert sources.next_fetch("gmail", s, {}, at(0, 30), built=False) == at(6, 30)
    assert sources.next_fetch("canvas", s, {}, at(0, 30), built=False) == at(0, 30)
    assert sources.next_fetch("gmail", {"enabled": True, "interval_hours": 2}, {},
                              at(9), built=True) == at(9)
