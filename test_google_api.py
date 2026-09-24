"""Tests for google_api.py and the §1.9 connector-precedence rules.

`health()` is the only part reachable without network or google-auth
installed, and it is also the part a failing 7:00 AM run depends on, so it is
where the coverage goes. The API wrappers are thin passthroughs; testing them
would be testing googleapiclient.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import google_api


def _write(root, name, obj):
    with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _token(days_ago=0, scopes=None, refresh=True):
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    tok = {
        "token": "REDACTED-not-a-real-token",
        "scopes": list(google_api.SCOPES if scopes is None else scopes),
        "consent_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_refresh": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if refresh:
        tok["refresh_token"] = "REDACTED"
    return tok


class HealthTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_missing_client_is_critical(self):
        v = google_api.health(self.root)
        self.assertFalse(v["ok"])
        self.assertEqual(v["severity"], "CRITICAL")
        self.assertIn("credentials.json", v["message"])

    def test_missing_token_is_critical_and_names_the_fix(self):
        _write(self.root, "credentials.json", {"installed": {}})
        v = google_api.health(self.root)
        self.assertFalse(v["ok"])
        self.assertIn("--authorize", v["message"])

    def test_fresh_token_is_ok_and_quiet(self):
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(days_ago=0))
        v = google_api.health(self.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["severity"], "INFO")
        self.assertAlmostEqual(v["idle_days"], 0.0, delta=0.1)

    def test_a_week_old_token_is_fine_because_this_is_an_internal_app(self):
        """Regression. This module once warned MAJOR at day 5 on a 7-day rule
        that does not apply to Internal apps. A recurring false MAJOR trains
        the reader to ignore MAJOR rows, which is worse than silence."""
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(days_ago=9))
        v = google_api.health(self.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["severity"], "INFO")

    def test_idle_past_six_months_is_not_ok(self):
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(days_ago=200))
        v = google_api.health(self.root)
        self.assertFalse(v["ok"])
        self.assertIn("idle", v["message"])

    def test_long_idle_warns_and_names_the_bigger_problem(self):
        """160 days idle means the pipeline has not run, not just that a
        token is aging. The message has to say so."""
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(days_ago=160))
        v = google_api.health(self.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["severity"], "MAJOR")
        self.assertIn("has not run", v["message"])

    def test_access_token_without_refresh_token_is_critical(self):
        """Works for an hour, then an unattended run is stuck with no way to
        complete a consent flow."""
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(refresh=False))
        v = google_api.health(self.root)
        self.assertFalse(v["ok"])
        self.assertIn("refresh_token", v["message"])

    def test_narrow_scope_is_reported_as_unfixable_by_refresh(self):
        """A refresh cannot widen a granted scope; only fresh consent can.
        This is the drive.file trap: a token that works but lists nothing."""
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token(
            scopes=["https://www.googleapis.com/auth/gmail.modify"]))
        v = google_api.health(self.root)
        self.assertFalse(v["ok"])
        self.assertFalse(v["scopes_ok"])
        self.assertIn("auth/drive", " ".join(v["missing_scopes"]))
        self.assertIn("re-authorize", v["message"])

    def test_missing_stamps_degrade_rather_than_guessing(self):
        _write(self.root, "credentials.json", {"installed": {}})
        tok = _token()
        del tok["consent_at"]; del tok["last_refresh"]
        _write(self.root, "token.json", tok)
        v = google_api.health(self.root)
        self.assertTrue(v["ok"])
        self.assertEqual(v["severity"], "MAJOR")
        self.assertIsNone(v["idle_days"])

    def test_last_refresh_wins_over_consent_at(self):
        """A token consented 100 days ago but refreshed today is not idle."""
        _write(self.root, "credentials.json", {"installed": {}})
        tok = _token(days_ago=100)
        tok["last_refresh"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        _write(self.root, "token.json", tok)
        v = google_api.health(self.root)
        self.assertEqual(v["severity"], "INFO")
        self.assertLess(v["idle_days"], 1)


    def test_health_never_returns_a_credential(self):
        """§1.9: the verdict is the only thing a run may see."""
        _write(self.root, "credentials.json", {"installed": {}})
        _write(self.root, "token.json", _token())
        blob = json.dumps(google_api.health(self.root))
        self.assertNotIn("REDACTED", blob)
        self.assertNotIn("refresh_token", blob)


class RefreshErrorTest(unittest.TestCase):

    def test_invalid_grant_names_password_change_not_the_seven_day_rule(self):
        msg = google_api.classify_refresh_error(
            Exception("invalid_grant: Token has been expired or revoked."))
        self.assertIn("password", msg)
        self.assertIn("Internal app", msg)

    def test_policy_error_says_reauthorizing_will_not_help(self):
        msg = google_api.classify_refresh_error(
            Exception("invalid_grant, error_subtype: invalid_rapt"))
        self.assertIn("policy", msg)
        self.assertIn("will not help", msg)

class ScopeTest(unittest.TestCase):

    def test_drive_scope_is_not_drive_file(self):
        """drive.file cannot read state files another client created — it
        would report empty state, which looks exactly like a first run."""
        joined = " ".join(google_api.SCOPES)
        self.assertIn("auth/drive", joined)
        self.assertNotIn("drive.file", joined)

    def test_no_send_scope(self):
        """§1.3's email deliberately stays on the connector (§1.9)."""
        self.assertNotIn("gmail.send", " ".join(google_api.SCOPES))

    def test_body_read_scope_is_not_metadata_only(self):
        """gmail.metadata returns headers only and would silently break §4.5."""
        self.assertNotIn("gmail.metadata", " ".join(google_api.SCOPES))
        self.assertIn("gmail.modify", " ".join(google_api.SCOPES))

    def test_search_threads_returns_no_snippet_text(self):
        """A snippet in the return value is a body read by the back door."""
        src = open("google_api.py", encoding="utf-8").read()
        self.assertIn('"snippet_len"', src)
        self.assertNotIn('"snippet": t', src)


@unittest.skipUnless(os.path.exists("PROJECT_INSTRUCTIONS.md"), "spec docs are not public")
class GovernanceTest(unittest.TestCase):
    """The docs and the code must not disagree about who calls Gmail."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {}
        for name in ("PROJECT_INSTRUCTIONS.md", "DAILY_BRIEFING_PROMPT.md",
                     "SCHEMA_AND_STATE.md"):
            with open(name, encoding="utf-8") as fh:
                cls.docs[name] = fh.read()
        cls.all = "\n".join(cls.docs.values())

    def test_the_rule_exists_and_is_numbered(self):
        self.assertIn("### 1.9", self.docs["PROJECT_INSTRUCTIONS.md"])

    def test_step_zero_checks_credentials_before_the_sweep(self):
        p = self.docs["DAILY_BRIEFING_PROMPT.md"]
        self.assertLess(p.index("google_api.py"), p.index("STEP 1"))

    def test_prompt_does_not_assert_the_inapplicable_seven_day_rule(self):
        """This is an Internal app. A run told to expect a 7-day deadline
        would report a MAJOR that can never come true."""
        p = self.docs["DAILY_BRIEFING_PROMPT.md"]
        self.assertNotIn("7-day deadline", p)
        self.assertIn("Internal", p)

    def test_credential_files_are_never_read_into_context(self):
        self.assertIn("token.json", self.docs["PROJECT_INSTRUCTIONS.md"])
        self.assertIn("Never `cat`", self.docs["PROJECT_INSTRUCTIONS.md"])

    def test_delivery_email_still_names_the_connector(self):
        """The one operation that must NOT migrate. If a future edit routes it
        through the API, the token needs a send scope and this test fails."""
        self.assertIn("mcp__Gmail__send_message", self.all)

    def test_no_bare_connector_call_survives_as_a_primary_instruction(self):
        """Each legacy tool name may still appear, but only near a fallback
        marker — §1.9's table, a 'fallback' clause, or a never-call entry."""
        for doc, text in self.docs.items():
            for tool in ("search_files", "label_thread", "get_thread",
                         "list_labels", "unlabel_thread"):
                for line_no, line in enumerate(text.splitlines(), 1):
                    if tool not in line:
                        continue
                    window = "\n".join(
                        text.splitlines()[max(0, line_no - 6):line_no + 5])
                    self.assertTrue(
                        any(w in window for w in
                            ("fallback", "§1.9", "1.9", "NEVER", "never-call",
                             "connector")),
                        "%s:%d names %s with no fallback context"
                        % (doc, line_no, tool))


class TrashGuardTest(unittest.TestCase):
    """§1.2's file-set rule, enforced in code rather than trusted.

    This is the most destructive call in the project. It used to accept an
    opaque file id and do as it was told, so a mis-sorted list, an archive
    copy, or one of Michael's own files was an unrecoverable delete with
    nothing in between.
    """

    def test_permits_exactly_the_two_named_sets(self):
        for name in ("college_assistant_state_2026-09-10T035000Z.json",
                     "college_assistant_state_superseded_old.json",
                     "college_assistant_state_backup_2026.json",
                     "college_assistant_state_temp1.json"):
            self.assertTrue(google_api.trashable_name(name), name)

    def test_never_the_legacy_active_file(self):
        """§1.2: 'leave it alone forever.'"""
        self.assertFalse(google_api.trashable_name("college_assistant_state.json"))

    def test_never_an_archive_copy_the_ledger_or_user_content(self):
        for name in ("college_brief_2026-09-06.json",
                     "college_assistant_ledger.jsonl",
                     "ENGL001 essay draft.docx",
                     "Screenshot 2026-09-11.png"):
            self.assertFalse(google_api.trashable_name(name), name)

    def test_a_near_miss_on_the_stamp_is_refused(self):
        """The pattern is anchored and exact: no stamp, no trash."""
        self.assertFalse(google_api.trashable_name("college_assistant_state_2026-09-10.json"))
        self.assertFalse(
            google_api.trashable_name("college_assistant_state_2026-09-10T0350Z.json"))

    def test_empty_and_none_are_refused(self):
        self.assertFalse(google_api.trashable_name(""))
        self.assertFalse(google_api.trashable_name(None))

    def test_trash_raises_rather_than_deleting_a_forbidden_name(self):
        with self.assertRaises(ValueError):
            google_api.trash("some-drive-id", name="my thesis.docx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
