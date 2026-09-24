"""
Tests for orchestrator.py's step5c_discovery -- the 2026-09-19 rewrite that
wires DISCOVERY.md's already-designed-but-unwired machinery in for the
first time: `opportunity.detect_recurrence()` (a ledger-only, no-network
source of leads), §14.5 ranking by `expected_value()`/`crowding_score()`
instead of arbitrary result order, and §13.3 backward-planning via
`compute_act_by()`. The web-search half is mocked (`llm_cli.call_with_tools`)
so these run with no network/LLM call at all.

Run (from service/):
    ../venv/bin/python -m pytest test_step5c_discovery.py -q
"""

import datetime as _dt

import orchestrator  # first: inserts ROOT onto sys.path for the two below
import ledger
import opportunity

TODAY = _dt.date(2026, 9, 19)


def _run(tmp_path, dry_run=False):
    run = orchestrator.Run(db_path=str(tmp_path / "briefing.db"),
                           dry_run=dry_run)
    return run


def _ctx(entries=()):
    return {"today": TODAY, "state": {"requirements": {"entries": list(entries)}}}


def _req(id, statement="x", keywords=("x",), active=True):
    return opportunity.requirement(id, statement, keywords=list(keywords),
                                   active=active)


class TestDryRun:

    def test_dry_run_makes_no_llm_call_and_returns_nothing(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("llm_cli.call_with_tools must not be "
                                 "called in dry-run")
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _boom)
        run = _run(tmp_path, dry_run=True)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert out == []
        assert any("step5c" in s and "dry-run" in s for s in run.skipped)


class TestRecurrenceOnly:
    """No active requirements, no LLM call at all -- recurrence is pure
    computation from the ledger."""

    def test_no_requirements_and_empty_ledger_returns_nothing(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("nothing to search for -- must not call the LLM")
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _boom)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([]))
        assert out == []

    def test_a_recurring_family_from_last_year_surfaces_without_any_requirement(
            self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not call the LLM for a recurrence-only run")
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _boom)

        run = _run(tmp_path)
        fam_id = "lead-computing-catalyst-sprinternship-abcdef0123"
        row = {"id": fam_id, "family": fam_id, "kind": "lead",
               "date": "2025-09-25", "regime": "lead",
               "disposition": "surfaced", "on": "2025-09-25"}
        ledger.append(run.ledger_path, [row])

        out = orchestrator.step5c_discovery(run, _ctx([]))
        assert len(out) == 1
        item = out[0]
        assert item["regime"] == "lead"
        assert "Computing Catalyst Sprinternship" in item["title"]
        assert "may reopen" in item["title"]
        assert item["confidence_tier"] == "speculative"  # one prior sighting

    def test_two_prior_sightings_are_inferred_not_speculative(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("no requirements active")))
        run = _run(tmp_path)
        fam_id = "lead-research-fair-1122334455"
        rows = [
            {"id": fam_id, "family": fam_id, "kind": "lead", "date": "2024-09-20",
             "regime": "lead", "disposition": "surfaced", "on": "2024-09-20"},
            {"id": fam_id, "family": fam_id, "kind": "lead", "date": "2025-09-22",
             "regime": "lead", "disposition": "surfaced", "on": "2025-09-22"},
        ]
        ledger.append(run.ledger_path, rows)
        out = orchestrator.step5c_discovery(run, _ctx([]))
        assert len(out) == 1
        assert out[0]["confidence_tier"] == "inferred"

    def test_a_family_outside_the_recurrence_window_does_not_surface(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("no requirements active")))
        run = _run(tmp_path)
        fam_id = "lead-spring-thing-0011223344"
        row = {"id": fam_id, "family": fam_id, "kind": "lead", "date": "2025-01-15",
               "regime": "lead", "disposition": "surfaced", "on": "2025-01-15"}
        ledger.append(run.ledger_path, [row])
        out = orchestrator.step5c_discovery(run, _ctx([]))
        assert out == []


class TestSearchPath:
    """The LLM half, mocked so no network call happens."""

    def _fake_result(self, **result_kwargs):
        base = {
            "requirement_id": "research", "url": "https://cs.umd.edu/opp",
            "title": "Departmental Research Assistantship",
            "text": "A small departmental research assistantship, application "
                    "required, capped at 3 seats, posted only on this page.",
            "read_from_owner": True,
        }
        base.update(result_kwargs)
        return {"results": [base], "failures": []}

    def test_a_thin_high_value_result_becomes_a_lead_with_a_score(self, tmp_path, monkeypatch):
        captured = {}

        def _fake_call(prompt, **kwargs):
            captured["prompt"] = prompt
            return self._fake_result(
                signals=["application_required", "capped_seats",
                        "no_marketing_seen"],
                value_if_won="high", cost_to_check="low"), {}

        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 1
        item = out[0]
        assert item["regime"] == "lead"
        assert item["score"] > 0
        assert "research" in captured["prompt"].lower()

    def test_thin_high_value_outranks_crowded_low_value(self, tmp_path, monkeypatch):
        def _fake_call(prompt, **kwargs):
            return {
                "results": [
                    {"requirement_id": "research",
                     "url": "https://cs.umd.edu/crowded",
                     "title": "Big Career Fair",
                     "text": "Mass email, front page, Xfinity Center, drop-in.",
                     "read_from_owner": True,
                     "signals": ["mass_email", "front_page", "large_venue",
                                "drop_in"],
                     "value_if_won": "low", "cost_to_check": "low"},
                    {"requirement_id": "research",
                     "url": "https://cs.umd.edu/thin",
                     "title": "Narrow Departmental Slot",
                     "text": "Application required, capped seats, "
                             "departmental only, no marketing.",
                     "read_from_owner": True,
                     "signals": ["application_required", "capped_seats",
                                "departmental_only", "no_marketing_seen"],
                     "value_if_won": "high", "cost_to_check": "low"},
                ],
                "failures": [],
            }, {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 2
        assert "Narrow Departmental Slot" in out[0]["title"]
        assert out[0]["score"] > out[1]["score"]

    def test_eligibility_stated_becomes_a_cited_basis_entry(self, tmp_path, monkeypatch):
        def _fake_call(prompt, **kwargs):
            return self._fake_result(
                eligibility_stated="Open to sophomores and juniors",
                eligibility_assessed="unknown"), {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 1
        basis_claims = [b["claim"] for b in out[0]["basis"]]
        assert any("sophomores and juniors" in c for c in basis_claims)

    def test_deadline_and_prerequisite_produce_an_act_by_before_the_deadline(
            self, tmp_path, monkeypatch):
        def _fake_call(prompt, **kwargs):
            return self._fake_result(
                deadline="2026-11-01", prerequisites=["recommendation"]), {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 1
        assert out[0]["act_by"] is not None
        assert out[0]["act_by"] < "2026-11-01"

    def test_no_prerequisite_means_no_act_by(self, tmp_path, monkeypatch):
        def _fake_call(prompt, **kwargs):
            return self._fake_result(deadline="2026-11-01"), {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 1
        assert out[0]["act_by"] is None

    def test_same_url_from_recurrence_and_search_is_not_duplicated(self, tmp_path, monkeypatch):
        url = "https://cs.umd.edu/opp"
        fam_id = orchestrator._search_lead_id(
            {"title": "Departmental Research Assistantship", "url": url})

        def _fake_call(prompt, **kwargs):
            return self._fake_result(url=url), {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)

        run = _run(tmp_path)
        ledger.append(run.ledger_path, [
            {"id": fam_id, "family": fam_id, "kind": "lead",
             "date": "2025-09-20", "regime": "lead",
             "disposition": "surfaced", "on": "2025-09-20"}])

        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        ids = [it["id"] for it in out]
        assert len(ids) == len(set(ids))

    def test_unknown_signals_from_the_model_are_dropped_not_trusted(self, tmp_path, monkeypatch):
        def _fake_call(prompt, **kwargs):
            return self._fake_result(signals=["made_up_signal"]), {}
        monkeypatch.setattr(orchestrator.llm_cli, "call_with_tools", _fake_call)
        run = _run(tmp_path)
        out = orchestrator.step5c_discovery(run, _ctx([_req("research")]))
        assert len(out) == 1  # did not crash, just ignored the bad signal
