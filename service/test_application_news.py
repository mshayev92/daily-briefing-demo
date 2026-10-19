"""2026-10-08: decision emails move a tracked application ("Selected").

Run (from service/):
    ../venv/bin/python -m pytest test_application_news.py -q
"""

import orchestrator as o  # first: inserts ROOT onto sys.path for applications
import applications as apps
import db as dbmod
import store

ITEM = {"id": "umd-umd-ai-advisor-project-2026-10-06",
        "title": "UMD AI Advisor Project"}


def submitted(stage_on="2026-10-06"):
    return {"stage": "submitted", "stage_on": stage_on,
            "follow_up_on": "2026-10-27", "draft_url": None,
            "history": [{"stage": "submitted", "on": stage_on}]}


class Run:
    db_path = None

    def __init__(self, db_path):
        self.db_path = db_path
        self.errors = []

    def log_error(self, source, severity, message):
        self.errors.append(message)


def test_selected_stage_is_labelled_selected():
    assert apps.LABELS["accepted"] == "Selected"
    assert "accepted" in apps.ACTIVE


def test_selected_next_step_is_a_reminder_to_reply():
    rec = apps.from_news(submitted(), "selected", "2026-10-05")
    assert apps.next_step(rec, None, None, "2026-10-08") == \
        "Selected since Oct 5 · reply to confirm now"
    later = dict(rec, follow_up_on="2026-10-12")
    assert apps.next_step(later, None, None, "2026-10-08") == \
        "Selected since Oct 5 · reply to confirm by Oct 12"


def test_older_email_does_not_undo_newer_decision():
    selected = apps.from_news(submitted(), "selected", "2026-10-09")
    assert apps.from_news(selected, "rejected", "2026-10-05") is None


def test_newer_decision_wins():
    selected = apps.from_news(submitted(), "selected", "2026-10-05")
    later = apps.from_news(selected, "interview", "2026-10-08")
    assert later["stage"] == "interview"


def test_untracked_or_same_stage_changes_nothing():
    assert apps.from_news(None, "selected", "2026-10-05") is None
    assert apps.from_news(dict(submitted(), stage="accepted"),
                          "selected", "2026-10-05") is None


def test_title_match_allows_extra_words():
    assert o._same_opportunity("UMD AI Advisor Project",
                               "AI Advisor Project")
    assert not o._same_opportunity("UMD AI Advisor Project",
                                   "Dean's Student Advisory Council")


def test_decision_email_moves_tracked_application(tmp_path):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    store.application_put(conn, ITEM["id"], submitted())
    conn.commit()
    conn.close()
    run = Run(path)
    cand = {"title": "AI Advisor Project", "application_status": "selected",
            "sent_date": "2026-10-05"}
    assert o._apply_application_news(run, {}, [ITEM], [cand]) == 1
    assert store.applications_all(path)[ITEM["id"]]["stage"] == "accepted"


def test_unmatched_decision_is_logged_not_applied(tmp_path):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    run = Run(path)
    cand = {"title": "Summer Research Fellowship",
            "application_status": "selected", "sent_date": "2026-10-05"}
    assert o._apply_application_news(run, {}, [ITEM], [cand]) == 0
    assert any("names no tracked application" in m for m in run.errors)


def test_undo_returns_a_closed_application_to_its_open_stage():
    rejected = dict(submitted(), stage="rejected",
                    history=[{"stage": "submitted", "on": "2026-10-06"},
                             {"stage": "rejected", "on": "2026-10-08"}])
    assert apps.undo_stage(rejected) == "submitted"
    withdrawn = {"stage": "withdrawn", "history": [
        {"stage": "applying", "on": "2026-10-01"},
        {"stage": "withdrawn", "on": "2026-10-02"}]}
    assert apps.undo_stage(withdrawn) == "applying"


def test_undo_skips_earlier_closes_and_repeated_stages():
    record = {"stage": "rejected", "history": [
        {"stage": "interview", "on": "2026-10-01"},
        {"stage": "withdrawn", "on": "2026-10-02"},
        {"stage": "rejected", "on": "2026-10-03"},
        {"stage": "rejected", "on": "2026-10-03"}]}
    assert apps.undo_stage(record) == "interview"


def test_undo_without_history_falls_back_to_applying():
    assert apps.undo_stage(None) == "applying"
    assert apps.undo_stage({"stage": "rejected", "history": []}) == "applying"
