"""2026-09-25: the application tracker, "Draft my application", and goal
keywords that search for named programs.

Run (from service/):
    ../venv/bin/python -m pytest test_applications.py -q
"""

import datetime as _dt
import json

import pytest

import app
import applications
import collect
import db as dbmod
import drafting
import lifecycle
import orchestrator as o
import store

TODAY = _dt.date(2026, 9, 25)


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    real = dbmod.connect
    monkeypatch.setattr(app.dbmod, "connect", lambda p=None: real(path))
    monkeypatch.setattr(app, "_et_today", lambda: TODAY)
    monkeypatch.setattr(app, "_ledger_write", lambda row, d: None)
    return path


def _insert(path, item):
    conn = dbmod.connect(path)
    row = o.state_io_sqlite._item_to_row(item)
    conn.execute("INSERT INTO items (%s) VALUES (%s)" % (
        ", ".join(row), ", ".join(":" + c for c in row)), row)
    conn.commit()
    conn.close()


def _opp(**kw):
    it = {"id": "opp-1", "kind": "opportunity", "status": "new",
          "title": "Quant Analyst", "course_label": "UMD", "course_class": "umd",
          "date": "2026-10-07",
          "opportunity": {"type": "internship", "apply_url": "https://x.org/apply",
                          "dossier": {"eligibility": "open to all undergraduates",
                                      "requirements": ["resume", "one essay"]}}}
    it.update(kw)
    return it


# --- the model ---------------------------------------------------------------

class TestStages:
    def test_submitting_sets_a_follow_up_three_weeks_out(self):
        rec = applications.transition(None, "submitted", TODAY)
        assert rec["follow_up_on"] == "2026-10-16"
        assert rec["history"] == [{"stage": "submitted", "on": "2026-09-25"}]

    def test_repeating_a_stage_keeps_its_date(self):
        first = applications.transition(None, "applying", "2026-09-20")
        again = applications.transition(first, "applying", TODAY)
        assert again["stage_on"] == "2026-09-20" and len(again["history"]) == 1

    def test_unknown_stage_refused(self):
        with pytest.raises(ValueError):
            applications.transition(None, "ghosted", TODAY)

    def test_next_step_lines(self):
        applying = {"stage": "applying", "stage_on": "2026-09-24"}
        assert applications.next_step(applying, "2026-10-07", "2026-09-30", TODAY) \
            == "Applying since Sep 24 · start by Sep 30, due Oct 7"
        assert "deadline (Sep 20) has passed" in applications.next_step(
            applying, "2026-09-20", None, TODAY)
        sub = applications.transition(None, "submitted", "2026-09-01")
        assert applications.next_step(sub, None, None, TODAY) == \
            "Submitted since Sep 1 · follow up now: no word since Sep 1"
        assert applications.follow_up_due(sub, TODAY)
        closed = {"stage": "rejected", "stage_on": "2026-09-24"}
        assert applications.next_step(closed, None, None, TODAY) == ""


# --- lifecycle -----------------------------------------------------------------

class TestLifecycle:
    def test_a_submitted_application_outlives_its_deadline(self):
        it = _opp(date="2026-09-20",
                  application={"stage": "submitted", "stage_on": "2026-09-19"})
        assert lifecycle.expire([it], TODAY) == []
        assert lifecycle.section_for(it, TODAY) == "opportunities"
        kept, report = lifecycle.compact_and_prune([_opp(
            date="2026-06-01", application=it["application"])], TODAY)
        assert kept and not report["deleted"]

    def test_already_expired_row_returns_when_tracked(self):
        it = _opp(status="expired", date="2026-09-20",
                  application={"stage": "interview", "stage_on": "2026-09-24"})
        assert lifecycle.section_for(it, TODAY) == "opportunities"

    def test_ended_or_dismissed_applications_leave(self):
        ended = _opp(date="2026-09-20",
                     application={"stage": "rejected", "stage_on": "2026-09-24"})
        assert lifecycle.section_for(ended, TODAY) is None
        gone = _opp(status="dismissed",
                    application={"stage": "applying", "stage_on": "2026-09-24"})
        assert lifecycle.section_for(gone, TODAY) is None

    def test_draft_is_offered_on_opportunities_but_not_info_sessions(self):
        talk = _opp(id="t", opportunity={"type": "info-session"})
        buckets = {"opportunities": [_opp(), talk], "leads": []}
        lifecycle.assign_ai_actions(buckets, TODAY)
        assert buckets["opportunities"][0]["ai_actions"] == ["draft"]
        assert talk["ai_actions"] == []


# --- compose ----------------------------------------------------------------------

class TestCompose:
    def test_tracked_rows_lead_and_do_not_count_against_the_cap(self):
        rows = [_opp(id="u%d" % i, title=t, date="2026-10-0%d" % (i + 1))
                for i, t in enumerate(("Quant", "Fellowship", "Research lab",
                                       "Sprinternship", "Hackathon"))]
        rows.append(_opp(id="t1", date="2026-09-20", status="expired",
                         application=applications.transition(
                             None, "submitted", "2026-09-19")))
        run = o.Run(db_path=":memory:", dry_run=True)
        ctx = {"today": TODAY, "state": {}}
        composed = o.step7_compose(run, ctx, rows)
        ids = [r["id"] for r in composed["prepped"]["opportunities"]]
        assert ids[0] == "t1" and len(ids) == 1 + o.OPPORTUNITIES_SHOWN_MAX
        row = composed["prepped"]["opportunities"][0]
        assert row["app_stage_label"] == "Submitted"
        assert row["act_by_line"].startswith("Submitted since Sep 19")
        assert "closed Sun, Sep 20" in row["opp_meta"]
        assert composed["opps_stamp"] == "1 in progress · Top 4 of 5 open"

    def test_confirmed_lead_becomes_an_opportunity_once(self):
        lead = {"id": "lead-acme-fellows-abc1234567", "regime": "lead",
                "kind": "lead", "status": "confirmed", "title": "Acme Fellows",
                "act_by": "2026-10-01", "source_url": "https://careers.example.com/step",
                "opportunity": {"apply_url": "https://careers.example.com/step",
                                "deadline": "2026-10-20"}}
        items = [lead]
        run = o.Run(db_path=":memory:", dry_run=True)
        o._promote_confirmed_leads(run, items, TODAY)
        o._promote_confirmed_leads(run, items, TODAY)
        assert len(items) == 2
        new = items[1]
        assert new["kind"] == "opportunity" and new["regime"] == "confirmed"
        assert new["date"] == "2026-10-20" and new["act_by"] == "2026-10-01"
        assert new["opportunity"]["apply_url"] == "https://careers.example.com/step"
        assert lifecycle.section_for(new, TODAY) == "opportunities"

    def test_application_is_attached_but_never_saved(self, dbpath):
        conn = dbmod.connect(dbpath)
        store.application_put(conn, "opp-1", applications.transition(
            None, "applying", TODAY))
        conn.commit()
        conn.close()
        items = [_opp()]
        o._attach_applications(o.Run(db_path=dbpath, dry_run=True), items)
        assert items[0]["application"]["stage"] == "applying"
        row = o.state_io_sqlite._item_to_row(items[0])
        assert "application" not in json.loads(row["extra_json"])


# --- discovery queries --------------------------------------------------------------

class TestQueries:
    def test_one_query_per_named_program_for_next_summer(self):
        req = {"id": "big-tech", "statement": "internships",
               "keywords": ["Acme Fellows internship", "Globex Explore"]}
        assert collect.plan_queries(req, "University of Maryland", TODAY) == [
            "Acme Fellows internship 2027 application deadline",
            "Globex Explore 2027 application deadline"]

    def test_recently_searched_queries_rotate_out_before_the_cap(self):
        req = {"id": "r", "statement": "s", "active": True,
               "keywords": ["k%d" % i for i in range(9)]}
        first = collect.plan([req], TODAY)["queries"]
        assert len(first) == collect.MAX_SEARCHES_PER_RUN
        second = collect.plan([req], TODAY, skip={q["query"] for q in first})
        assert {q["query"] for q in second["queries"]}.isdisjoint(
            q["query"] for q in first)
        assert len(second["queries"]) == 3


# --- page endpoints ------------------------------------------------------------------

class TestEndpoints:
    def test_stage_click_is_stored_and_listed(self, dbpath):
        _insert(dbpath, _opp())
        out = app.set_application_stage("opp-1", app.StageIn(stage="submitted"))
        assert out["follow_up_on"] == "2026-10-16"
        assert app.application_stages() == {"opp-1": "submitted"}

    def test_stage_refused_on_coursework_and_unknown_stage(self, dbpath):
        _insert(dbpath, {"id": "hw", "kind": "assignment", "title": "HW",
                         "course_label": "MATH001", "course_class": "math"})
        with pytest.raises(app.HTTPException) as e:
            app.set_application_stage("hw", app.StageIn(stage="applying"))
        assert e.value.status_code == 409
        with pytest.raises(app.HTTPException):
            app.set_application_stage("hw", app.StageIn(stage="maybe"))

    def test_draft_writes_a_doc_marks_applying_and_is_saved(self, dbpath, monkeypatch, tmp_path):
        _insert(dbpath, _opp())
        mem = tmp_path / "mem"
        mem.mkdir()
        (mem / "user_application_story_bank.md").write_text(
            "---\nname: x\n---\n1. Built the NJWG encampment app.\n")
        monkeypatch.setattr(drafting, "MEMORY_DIR", str(mem))
        monkeypatch.setattr(drafting, "page_text", lambda item: "")
        monkeypatch.setattr(app, "_email_text", lambda item: "")
        prompts, docs = [], []

        def fake_llm(prompt, **kw):
            prompts.append(prompt)
            return ("## Why I'm interested\nI built [name the app's users].", {})
        monkeypatch.setattr(app.llm_cli, "call", fake_llm)
        import google_api
        monkeypatch.setattr(google_api, "ensure_folder", lambda name, root="": "F")
        monkeypatch.setattr(google_api, "create_doc", lambda f, name, html, root="": (
            docs.append(html) or ("id1", "https://docs.google.com/document/d/id1")))

        out = app.ai_action("opp-1", "draft", app.AiActionIn())
        assert "https://docs.google.com/document/d/id1" in out["answer"]
        assert "[name the app's users]" in out["answer"]
        assert "NJWG encampment app" in prompts[0]
        assert "open to all undergraduates" in docs[0]      # listing, verbatim
        assert "<h2>Why I&#x27;m interested</h2>" in docs[0]
        assert app.application_stages() == {"opp-1": "applying"}
        again = app.ai_action("opp-1", "draft", app.AiActionIn())
        assert again["saved"] and len(docs) == 1

    def test_redraft_reads_the_questions_pasted_into_the_doc(self, dbpath, monkeypatch):
        _insert(dbpath, _opp())
        conn = dbmod.connect(dbpath)
        rec = applications.transition(None, "applying", TODAY)
        rec["draft_url"] = "https://docs.google.com/document/d/abc123/edit"
        store.application_put(conn, "opp-1", rec)
        conn.commit()
        conn.close()
        import google_api
        monkeypatch.setattr(google_api, "doc_text", lambda fid, root="": (
            "Q1. Describe a time you led a team." if fid == "abc123" else ""))
        monkeypatch.setattr(drafting, "page_text", lambda item: "")
        monkeypatch.setattr(app, "_email_text", lambda item: "")
        seen = {}
        monkeypatch.setattr(drafting, "draft", lambda item, ctx, *a: (
            seen.setdefault("ctx", ctx), ("ok", "https://docs.google.com/d/new"))[1])
        app.ai_action("opp-1", "draft", app.AiActionIn())
        assert "Describe a time you led a team." in seen["ctx"]

    def test_draft_refused_for_non_opportunities(self, dbpath):
        _insert(dbpath, {"id": "hw", "kind": "assignment", "title": "HW",
                         "course_label": "MATH001", "course_class": "math"})
        with pytest.raises(app.HTTPException) as e:
            app.ai_action("hw", "draft", app.AiActionIn())
        assert e.value.status_code == 409


class TestHandAddedAndFollowUps:
    def test_added_application_is_tracked_and_becomes_an_item(self, dbpath):
        rec = app.add_application(app.ManualIn(
            title="Acme  Fellows internship", organization="Acme",
            url="https://buildyourfuture.withgoogle.com/programs/step",
            deadline="2026-10-20", stage="applying"))
        iid = rec["id"]
        assert iid.startswith("manual-acme-fellows-internship-")
        assert app.application_stages() == {iid: "applying"}
        # Trackable and draftable before any run has made it an item.
        out = app.set_application_stage(iid, app.StageIn(stage="submitted"))
        assert out["stage"] == "submitted"
        items = []
        o._attach_applications(o.Run(db_path=dbpath, dry_run=True), items, TODAY)
        it = items[0]
        assert it["title"] == "Acme Fellows internship" and it["date"] == "2026-10-20"
        assert it["opportunity"]["apply_url"].startswith("https://buildyour")
        assert it["application"]["stage"] == "submitted"
        assert it["application"]["manual"]["organization"] == "Acme"
        assert lifecycle.section_for(it, TODAY) == "opportunities"
        again = [it]
        o._attach_applications(o.Run(db_path=dbpath, dry_run=True), again, TODAY)
        assert len(again) == 1

    def test_bad_added_application_refused(self, dbpath):
        for kw in ({"title": " "}, {"title": "x", "url": "http://a.b"},
                   {"title": "x", "deadline": "soon"},
                   {"title": "x", "stage": "rejected"}):
            with pytest.raises(app.HTTPException) as e:
                app.add_application(app.ManualIn(**kw))
            assert e.value.status_code == 422

    def test_follow_up_date_can_be_moved_or_cleared(self, dbpath):
        _insert(dbpath, _opp())
        app.set_application_stage("opp-1", app.StageIn(stage="submitted"))
        out = app.set_application_stage("opp-1", app.StageIn(follow_up_on="2026-10-02"))
        assert out["stage"] == "submitted" and out["follow_up_on"] == "2026-10-02"
        out = app.set_application_stage("opp-1", app.StageIn(follow_up_on=""))
        assert out["follow_up_on"] is None

    def test_follow_up_date_refused_where_it_means_nothing(self, dbpath):
        _insert(dbpath, _opp())
        with pytest.raises(app.HTTPException) as e:
            app.set_application_stage("opp-1", app.StageIn(follow_up_on="2026-10-02"))
        assert e.value.status_code == 409          # not tracked yet
        app.set_application_stage("opp-1", app.StageIn(stage="applying"))
        with pytest.raises(app.HTTPException) as e:
            app.set_application_stage("opp-1", app.StageIn(follow_up_on="2026-10-02"))
        assert e.value.status_code == 422

    def test_an_offer_date_reads_as_reply_by(self):
        rec = applications.set_follow_up(
            applications.transition(None, "offer", TODAY), "2026-10-01")
        assert applications.next_step(rec, None, None, TODAY) == \
            "Offer since Sep 25 · reply by Oct 1"


class TestDraftDocument:
    def test_markdown_is_escaped_and_structured(self):
        html = drafting.markdown_html("## A <b>\n- one **two**\n- three\n\npara")
        assert html == ("<h2>A &lt;b&gt;</h2>\n<ul>\n<li>one <b>two</b></li>\n"
                        "<li>three</li>\n</ul>\n<p>para</p>")

    def test_account_lines_are_not_sent(self, tmp_path):
        (tmp_path / "user_profile.md").write_text(
            "---\nname: p\n---\nIntro.\n\n**Accounts:** a@b.c\nmore\n\nRoles.")
        text = drafting.story_bank(str(tmp_path))
        assert "a@b.c" not in text and "Intro." in text and "Roles." in text
