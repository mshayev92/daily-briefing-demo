"""Tests for the briefing's Gmail governance rules and Drive trash guard.

The generic Google client (health, scopes, refresh errors) is tested in
~/projects/google-oauth. These cover what stays project-specific.
"""

import unittest

import briefing_google
import google_api


@__import__("unittest").skipUnless(__import__("os").path.exists("PROJECT_INSTRUCTIONS.md"), "spec docs are not public")
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
            self.assertTrue(briefing_google.trashable_name(name), name)

    def test_never_the_legacy_active_file(self):
        """§1.2: 'leave it alone forever.'"""
        self.assertFalse(briefing_google.trashable_name("college_assistant_state.json"))

    def test_never_an_archive_copy_the_ledger_or_user_content(self):
        for name in ("college_brief_2026-09-06.json",
                     "college_assistant_ledger.jsonl",
                     "ENGL001 essay draft.docx",
                     "Screenshot 2026-09-11.png"):
            self.assertFalse(briefing_google.trashable_name(name), name)

    def test_a_near_miss_on_the_stamp_is_refused(self):
        """The pattern is anchored and exact: no stamp, no trash."""
        self.assertFalse(briefing_google.trashable_name("college_assistant_state_2026-09-10.json"))
        self.assertFalse(
            briefing_google.trashable_name("college_assistant_state_2026-09-10T0350Z.json"))

    def test_empty_and_none_are_refused(self):
        self.assertFalse(briefing_google.trashable_name(""))
        self.assertFalse(briefing_google.trashable_name(None))

    def test_trash_raises_rather_than_deleting_a_forbidden_name(self):
        with self.assertRaises(ValueError):
            briefing_google.trash("some-drive-id", name="my thesis.docx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
