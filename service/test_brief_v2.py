"""Brief v2 (2026-09-22): date coercion, multi-candidate extraction, the
row-level gate quarantine, What changed, opportunity research, campus
events, LLM accounting, and the page-visit baseline.

Run (from service/):
    ../venv/bin/python -m pytest test_brief_v2.py -q
"""

import datetime as _dt
import json
import sqlite3

import pytest

import app
import campus
import db as dbmod
import digest
import lifecycle
import llm_cli
import orchestrator as o
import research
import store
import umd_calendar

TODAY = _dt.date(2026, 9, 22)


@pytest.fixture
def dbpath(tmp_path):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    return path


def _run(dbpath=None, dry=False):
    r = o.Run(db_path=dbpath or ":memory:", dry_run=dry)
    return r


# --- dates -----------------------------------------------------------------

class TestCoercion:
    @pytest.mark.parametrize("raw,want", [
        ("2026-09-23", "2026-09-23"), ("September 23, 2026", "2026-09-23"),
        ("Oct 2, 2026", "2026-10-02"), ("October 2nd, 2026", "2026-10-02"),
        ("9/25/2026", "2026-09-25"), ("next Friday", None), ("", None)])
    def test_dates(self, raw, want):
        assert o.coerce_date(raw) == want

    @pytest.mark.parametrize("raw,want", [
        ("23:59", "23:59"), ("9:05", "09:05"), ("11:59 PM", "23:59"),
        ("3pm", "15:00"), ("12:00 AM", "00:00"), ("noon", None)])
    def test_times(self, raw, want):
        assert o.coerce_time(raw) == want

    def test_unreadable_date_goes_to_attention_with_its_words(self):
        run = _run()
        cand = {"title": "Form", "due_date": "sometime soon",
                "date_evidence": "sometime soon"}
        o._coerce_candidate(run, cand)
        assert cand["due_date"] is None
        assert cand["evidence"] == "insufficient"
        assert "sometime soon" in cand["description"]

    def test_stored_non_iso_dates_are_repaired(self):
        run = _run()
        items = [{"id": "a", "date": "September 23, 2026", "time": "5:30 PM"},
                 {"id": "b", "date": "whenever"}, {"id": "c", "date": "2026-09-30"}]
        o._repair_stored_dates(run, items)
        assert items[0]["date"] == "2026-09-23" and items[0]["time"] == "17:30"
        assert items[1]["date"] is None and items[1]["evidence"] == "insufficient"
        assert items[2]["date"] == "2026-09-30"


# --- email text + extraction -------------------------------------------------

class TestThreadText:
    def test_newest_first_and_quotes_stripped(self):
        body = {"headers": {"Subject": "Re: exam"}, "messages": [
            "Exam is Monday.", "Update: exam moved to Friday.\n\nOn Mon, X wrote:\n> Exam is Monday."]}
        text = o.thread_prompt_text(body)
        assert text.index("moved to Friday") < text.index("Exam is Monday")
        assert text.count("Exam is Monday") == 1

    def test_digest_gets_the_bigger_budget(self):
        long = "item " * 3000
        plain = o.thread_prompt_text({"headers": {"Subject": "hi"}, "messages": [long]})
        dig = o.thread_prompt_text({"headers": {"Subject": "CMNS Digest"}, "messages": [long]})
        assert len(plain) == o.THREAD_CHARS and len(dig) > o.THREAD_CHARS


class TestMultiCandidateSweep:
    def test_a_digest_yields_every_opportunity(self, monkeypatch):
        monkeypatch.setattr(o.google_api, "search_threads", lambda q, root: [{"id": "t1"}])
        monkeypatch.setattr(o.google_api, "thread_bodies", lambda tid, root: {
            "id": tid, "headers": {"Subject": "Digest"}, "messages": ["..."]})
        monkeypatch.setattr(o.google_api, "labels", lambda root: {})
        monkeypatch.setattr(o.google_api, "relabel", lambda *a, **k: None)
        out = {"candidates": [
            {"thread_id": "t1", "title": "Fellowship A", "kind": "opportunity",
             "due_date": "October 1, 2026", "apply_url": "https://a.example/apply"},
            {"thread_id": "t1", "title": "Hackathon B", "kind": "opportunity",
             "due_date": "2026-10-05"}]}
        monkeypatch.setattr(o.llm_cli, "call", lambda *a, **k: (out, {}))
        run = _run()
        ctx = {"state": {"courses": {}}, "today": TODAY}
        cands = o.step1_2_gmail_sweep(run, ctx)
        assert [c["title"] for c in cands] == ["Fellowship A", "Hackathon B"]
        assert cands[0]["due_date"] == "2026-10-01"
        assert run.emails_processed == 1

    def test_reconcile_carries_opportunity_fields_and_first_seen(self):
        run = _run()
        ctx = {"state": {"items": []}, "today": TODAY}
        items = o.step3_reconcile(run, ctx, [{
            "thread_id": "t1", "source": "gmail", "course": None, "kind": "opportunity",
            "title": "Quant fund", "due_date": "2026-09-25", "organization": "Apex",
            "eligibility": "open to all majors", "apply_url": "https://x.example/a"}])
        it = items[0]
        assert it["kind"] == "opportunity" and it["first_seen"] == "2026-09-22"
        assert it["opportunity"]["organization"] == "Apex"
        assert it["opportunity"]["apply_url"] == "https://x.example/a"

    def test_old_event_is_promoted_to_opportunity(self):
        run = _run()
        items = [{"id": "e1", "kind": "event", "status": "new",
                  "title": "Apex Fund: Quantitative Analyst applications",
                  "source_refs": [{"source": "gmail", "thread_id": "t"}]},
                 {"id": "e2", "kind": "event", "status": "new",
                  "title": "Fall Career & Internship Fair",
                  "source_refs": [{"source": "gmail", "thread_id": "t"}]}]
        o._promote_opportunities(run, items)
        assert items[0]["kind"] == "opportunity"
        assert items[1]["kind"] == "event"


# --- lifecycle ------------------------------------------------------------------

class TestOpportunityLifecycle:
    def test_routing(self):
        it = {"kind": "opportunity", "status": "new", "date": None}
        assert lifecycle.section_for(it, TODAY) == "opportunities"
        it["date"] = "2026-09-21"
        assert lifecycle.section_for(it, TODAY) is None

    def test_expiry(self):
        items = [{"id": "a", "kind": "opportunity", "status": "new", "date": "2026-09-21"},
                 {"id": "b", "kind": "opportunity", "status": "new", "date": None,
                  "first_seen": "2026-08-01"},
                 {"id": "c", "kind": "opportunity", "status": "new", "date": None,
                  "first_seen": "2026-09-20"}]
        assert set(lifecycle.expire(items, TODAY)) == {"a", "b"}


# --- gate quarantine -------------------------------------------------------------

class TestQuarantine:
    def test_bad_label_row_is_left_off_not_the_whole_page(self, monkeypatch):
        spec = {"sections": {"coming_up": [
            {"id": "ok-1", "course_label": "UMD"},
            {"id": "bad-1", "course_label": "econ 001"}]}}
        calls = []

        def fake(spec_dict):
            calls.append(spec_dict)
            ids = [r["id"] for r in spec_dict["sections"]["coming_up"]]
            if "bad-1" in ids:
                return None, ["13: bad course label 'econ 001'"]
            return "<html>", []
        monkeypatch.setattr(o, "_render_via_cli", fake)
        run = _run()
        html, problems = o.step8_render(run, {}, spec)
        assert html == "<html>" and problems == []
        assert any("row left off" in e["message"] for e in run.errors)

    def test_structural_problem_still_blocks(self, monkeypatch):
        monkeypatch.setattr(o, "_render_via_cli",
                            lambda s: (None, ["10: expected exactly 4 script blocks"]))
        html, problems = o.step8_render(_run(), {}, {"sections": {}})
        assert html is None and problems


# --- What changed ----------------------------------------------------------------

def _scrape():
    return {"courses": [{"course": {"id": 1, "course_code": "MATH001", "name": "Math"},
                         "announcements": [{"id": 10, "message_html": "<p>Short note.</p>",
                                            "html_url": "https://umd.instructure.com/courses/1/discussion_topics/10"}]},
                        {"course": {"id": 2, "course_code": "STTS26", "name": "Onboarding"},
                         "announcements": []}]}


def _row(etype, kind, eid, title, after=None, before=None, cid=1):
    return {"run_at": 1.0, "course_id": cid, "type": etype, "id": str(eid),
            "kind": kind, "title": title, "after": after or {}, "before": before or {}}


class TestDigest:
    def test_build_groups_and_labels(self):
        rows = [
            _row("announcement", "new", 10, "Quiz 1 solution"),
            _row("file", "new", 20, "notes.pdf", {"display_name": "notes.pdf"}),
            _row("file", "new", 21, "flyer.png", {"display_name": "flyer.png"}),
            _row("assignment", "changed", 30, "HW 3",
                 {"name": "HW 3", "due_at": "2026-09-26T03:59:00Z"},
                 {"name": "HW 3", "due_at": "2026-09-24T03:59:00Z"}),
            _row("quiz", "changed", 31, "Quiz 2", {"title": "Quiz 2", "completion_state": "graded"},
                 {"title": "Quiz 2", "completion_state": "not_started"}),
            _row("page", "changed", "p", "Syllabus", {"body_hash": "a"}, {"body_hash": "a"}),
            _row("file", "new", 40, "x.pdf", {"display_name": "x.pdf"}, cid=2),
        ]
        groups = digest.build(rows, _scrape(), {"MATH001": "MATH001"}, o.TZ, TODAY)
        assert [g["course"] for g in groups] == ["MATH001"]
        labels = [e["label"] for e in groups[0]["entries"]]
        assert labels == ["Announcement", "Moved", "Graded", "Posted"]
        moved = groups[0]["entries"][1]["text"]
        assert moved == "HW 3 · now due Fri 9/25 (was tomorrow)"
        assert "flyer" not in groups[0]["entries"][3]["text"]

    def test_baseline_same_day_keeps_its_start(self, dbpath):
        store.kv_set(dbpath, digest.BASELINE_KEY,
                     {"date": "2026-09-21", "since": 100.0, "until": 500.0})
        since, base = digest.baseline(dbpath, TODAY, 1000.0)
        assert since == 500.0
        store.kv_set(dbpath, digest.BASELINE_KEY, dict(base, until=900.0))
        again, _ = digest.baseline(dbpath, TODAY, 1200.0)
        assert again == 500.0

    def test_long_announcements_summarized_once(self, dbpath):
        long = "Word " * 100
        groups = [{"course": "MATH001", "entries": [
            {"label": "Announcement", "text": "T", "body": long}]}]
        calls = []

        def llm(prompt, sp, schema):
            calls.append(prompt)
            return {"summaries": [{"id": "0", "summary": "One line."}]}
        digest.summarize_announcements(groups, dbpath, llm, print)
        assert groups[0]["entries"][0]["summary"] == "One line."
        groups2 = [{"course": "MATH001", "entries": [
            {"label": "Announcement", "text": "T", "body": long}]}]
        digest.summarize_announcements(groups2, dbpath, llm, print)
        assert len(calls) == 1 and groups2[0]["entries"][0]["summary"] == "One line."

    def test_change_strip_text(self):
        changes = [{"course": "X", "entries": [{"label": "Announcement"},
                                               {"label": "Posted"}]}]
        text = o.change_strip_text({"new": 2, "resolved": 1}, changes, True)
        assert text.startswith("Since yesterday: 1 announcement")
        assert "2 new items" in text and "1 resolved" in text
        assert o.change_strip_text({}, [], False) == ""


# --- research -------------------------------------------------------------------

class TestResearch:
    def test_verified_ignores_case_space_and_curly_quotes(self):
        assert research.verified("Open to ALL  majors", "…it’s open to all majors.")
        assert not research.verified("open to sophomores", "open to all majors")

    def test_unverified_quotes_are_dropped(self, dbpath):
        page = "Apply by October 1. Open to all undergraduates. Submit a resume. " * 10
        item = {"id": "o1", "kind": "opportunity", "status": "new",
                "title": "Fellowship", "date": "2026-10-01",
                "opportunity": {"apply_url": "https://f.example/apply"}}

        def llm(prompt, sp, schema):
            return {"relevant": True, "summary": "A fellowship.",
                    "deadline": "Apply by October 1",
                    "eligibility": "Open to first-years only",
                    "requirements": ["Submit a resume", "Two letters"],
                    "why_it_fits": "Matches your AI interest."}
        done = research.research([item], "profile", dbpath, llm, TODAY, print,
                                 fetch=lambda url: page)
        d = item["opportunity"]["dossier"]
        assert done == ["o1"]
        assert d["deadline"] == "Apply by October 1"
        assert "eligibility" not in d and "eligibility" in d["dropped"]
        assert d["requirements"] == ["Submit a resume"]

    def test_email_is_used_when_the_page_is_a_login_wall(self, dbpath):
        item = {"id": "o2", "kind": "opportunity", "status": "new", "title": "Quant",
                "opportunity": {"apply_url": "https://forms.example/x"}}
        email = "Apex Fund quant team applications DUE 9/25. All majors welcome to apply. " * 3

        def boom(url):
            raise OSError("401")
        research.research([item], "p", dbpath,
                          lambda *a: {"relevant": True, "summary": "S",
                                      "deadline": "DUE 9/25", "requirements": []},
                          TODAY, print, fetch=boom, email_text=lambda it: email)
        d = item["opportunity"]["dossier"]
        assert d["from_email"] is True and d["deadline"] == "DUE 9/25"


# --- campus ----------------------------------------------------------------------

LISTING = """<umd-element-event data-display="list"><p slot="headline">
<a href="https://calendar.umd.edu/ai-talk-2?start=2026-09-24"><span>AI &amp; You</span></a></p>
<div slot="start-date-iso"> 2026-09-24T12:00:00.000 </div>
<div slot="end-date-iso"> 2026-09-24T13:00:00.000 </div>
<div slot="text"><p>A talk.</p></div></umd-element-event>"""


class TestCampus:
    def test_listing_html_parses(self):
        (ev,) = umd_calendar.parse_category_html(LISTING, "research")
        assert ev["title"] == "AI & You" and ev["slug"] == "ai-talk-2"
        assert ev["start"] == _dt.datetime(2026, 9, 24, 12, 0)
        assert ev["topics"] == ["research"]

    def test_default_boosts_come_from_courses(self):
        p = campus.with_default_boosts({"answers": {}}, ["CMSC001", "ECON001"])
        assert "machine learning" in p["answers"]["keywords_boost"]
        assert "finance" in p["answers"]["keywords_boost"]
        mine = campus.with_default_boosts({"answers": {"keywords_boost": ["x"]}}, ["CMSC001"])
        assert mine["answers"]["keywords_boost"] == ["x"]

    def test_busy_blocks_from_calendar_iso(self):
        blocks = campus.busy_from_calendar([
            {"start": "2026-09-22T09:30:00-04:00", "end": "2026-09-22T10:20:00-04:00"},
            {"start": "2026-09-22", "end": "2026-09-23", "all_day": True}], o.TZ)
        assert blocks == [(_dt.datetime(2026, 9, 22, 9, 30), _dt.datetime(2026, 9, 22, 10, 20))]


# --- llm accounting ---------------------------------------------------------------

class TestLlmAccounting:
    def test_calls_are_recorded_and_budget_enforced(self, monkeypatch):
        env = {"total_cost_usd": 0.6, "usage": {"input_tokens": 10, "output_tokens": 2}}
        monkeypatch.setattr(llm_cli, "_run_once", lambda *a, **k: ("ok", env))
        llm_cli.reset_accounting(budget_usd=1.0)
        try:
            llm_cli.call("p", system_prompt="s", label="t1")
            llm_cli.call("p", system_prompt="s", label="t2")
            with pytest.raises(llm_cli.BudgetExceeded):
                llm_cli.call("p", system_prompt="s", label="t3")
            assert [c["label"] for c in llm_cli.CALLS] == ["t1", "t2"]
            assert llm_cli.spent_usd() == pytest.approx(1.2)
        finally:
            llm_cli.reset_accounting(None)

    def test_thinking_is_off_by_default(self, monkeypatch):
        seen = {}

        def fake_run(cmd, **kw):
            seen["env"] = kw.get("env") or {}

            class P:
                returncode, stderr = 0, ""
                stdout = json.dumps({"result": "x", "total_cost_usd": 0})
            return P()
        monkeypatch.setattr(llm_cli.subprocess, "run", fake_run)
        llm_cli.call("p", system_prompt="s")
        assert seen["env"]["MAX_THINKING_TOKENS"] == "0"

    def test_calls_are_stored(self, dbpath):
        store.record_llm_calls(dbpath, "2026-09-22T10:00:00Z", [
            {"label": "tldr", "model": "haiku", "ok": True, "cost_usd": 0.001,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "seconds": 1.0}])
        conn = sqlite3.connect(dbpath)
        assert conn.execute("SELECT label FROM llm_calls").fetchone()[0] == "tldr"


# --- page visits ------------------------------------------------------------------

class TestVisit:
    def test_reload_in_a_session_keeps_the_previous_baseline(self, dbpath, monkeypatch):
        monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", dbpath)
        store.kv_set(dbpath, "page_visits",
                     {"previous": None, "current": "2026-09-21T01:00:00+00:00"})
        first = app.visit()
        assert first["previous"] == "2026-09-21T01:00:00+00:00"
        again = app.visit()
        assert again["previous"] == "2026-09-21T01:00:00+00:00"


class TestDigestEvents:
    def test_many_events_from_one_email_become_ranked_campus_rows(self):
        run = _run()
        ctx = {"state": {"courses": {"CMSC001": {"class": "cmsc"}}}, "today": TODAY}
        ref = [{"source": "gmail", "thread_id": "dig"}]
        items = [{"id": "e%d" % i, "kind": "event", "status": "new",
                  "title": t, "source_refs": ref, "course_label": "UMD"}
                 for i, t in enumerate(("Club GBM", "Python workshop", "Yoga"))]
        items.append({"id": "solo", "kind": "event", "status": "new", "title": "Talk",
                      "source_refs": [{"source": "gmail", "thread_id": "one"}]})
        o._flag_digest_events(run, ctx, items)
        assert all(it.get("digest") for it in items[:3]) and not items[3].get("digest")
        assert items[1]["relevance"]["score"] > items[0]["relevance"]["score"]
        assert lifecycle.section_for(dict(items[1], date="2026-09-24"), TODAY) == "campus"


class TestOpportunityMeta:
    def test_organization_not_repeated_when_the_title_names_it(self):
        it = {"title": "Social Media & Growth Intern - Capy's Journey", "date": None,
              "opportunity": {"type": "internship", "organization": "Capy's Journey"}}
        assert o._opportunity_row_fields(it, TODAY)["opp_meta"] == \
            "Internship · no deadline stated"
        it["opportunity"]["organization"] = "Apex Fund"
        it["title"] = "Quantitative Analyst applications"
        assert "Apex Fund" in o._opportunity_row_fields(it, TODAY)["opp_meta"]


class TestDossierSummary:
    def test_a_summary_about_the_sources_is_dropped(self, dbpath):
        item = {"id": "o3", "kind": "opportunity", "status": "new", "title": "Info"}
        d = research.dossier_for(
            item, "p", "", dbpath,
            lambda *a: {"relevant": True, "requirements": [],
                        "summary": "Email digest listing jobs, but no details are provided."},
            TODAY, None, email_text="Info session Wednesday. " * 5)
        assert d["summary"] == "" and "summary" in d["dropped"]
