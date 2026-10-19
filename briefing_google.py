"""Daily-briefing rules on top of the shared Google client (google_api, in
~/projects/google-oauth): the Drive trash guard (§1.2) and the once-a-day
briefing email (§1.3). Credentials and the generic API live there; `root`
below is this project's folder and only holds the sent-today marker."""

import os
import re
from datetime import datetime

import google_api

# §1.2's two named sets, as patterns: a dated state file, or a legacy backup
# from the pre-2026-09-06 write scheme. Anything else is not trashable here.
# The `briefing_backup_` pattern is Phase 5's addition, for the new SQLite
# nightly backup's own retention pruning -- same guard, same reasoning: a
# filename this function does not recognize is refused, not guessed at.
TRASHABLE = (
    re.compile(r"^college_assistant_state_\d{4}-\d{2}-\d{2}T\d{6}Z\.json$"),
    re.compile(r"^college_assistant_state_superseded_.*\.json$"),
    re.compile(r"^college_assistant_state_backup_.*\.json$"),
    re.compile(r"^college_assistant_state_temp.*$"),
    re.compile(r"^briefing_backup_\d{4}-\d{2}-\d{2}T\d{6}Z\.sqlite$"),
)
# The legacy active file. §1.2: "leave it alone forever."
NEVER_TRASH = ("college_assistant_state.json",)


def trashable_name(name):
    """Does §1.2 permit this FILENAME to be trashed at all?

    Name-level only: the 14-day age test and the keep-the-14-most-recent rule
    stay with the caller, because neither can be answered from one filename.
    """
    n = str(name or "").strip()
    if not n or n in NEVER_TRASH:
        return False
    return any(rx.match(n) for rx in TRASHABLE)


def trash(file_id, name=None):
    """Move to trash. Refuses any name §1.2 does not permit.

    Passing `name` turns §1.2's file-set rule from something the caller is
    trusted to have applied into something this function enforces: a wrong
    id (a mis-sorted list, an archive copy, one of Michael's own files) used
    to be an unrecoverable delete with nothing in between.

    Called without a name it still works, for the connector-fallback path
    where only an id is in hand, but it reports `guard: "bypassed-no-name"`
    rather than passing silently.
    """
    if name is not None and not trashable_name(name):
        raise ValueError(
            "\u00a71.2 forbids trashing %r \u2014 not a dated state file and not a "
            "legacy backup. If this is genuinely old-state cleanup the name is "
            "wrong; if it is anything else the call is wrong." % name)
    google_api.trash_file(file_id)
    return {"trashed": True, "name": name,
            "guard": "enforced" if name is not None else "bypassed-no-name"}


BRIEFING_RECIPIENT = "student@terpmail.umd.edu"  # 1.3 -- hardcoded, never a parameter


def _last_sent_marker(root):
    """Guard file for the once-per-calendar-day rule (1.3), independent of
    `state.last_completed_date` -- SCHEMA_AND_STATE.md 3.6 explains why a
    single marker written after delivery is not enough on its own: a crash
    between send and state-write must still be recognized as "already sent"
    on the next invocation the same day."""
    return os.path.join(root, ".last_briefing_email_sent")


def send_daily_briefing_email(summary_line, page_url, today_iso, root=""):
    """The ONLY function permitted to send the daily briefing email.

    Every guarantee PROJECT_INSTRUCTIONS.md 1.3 asks a run to uphold is
    structural here rather than requested in a prompt: the recipient is the
    module constant above, never a parameter; the subject is derived only
    from `today_iso`; the body is exactly the summary line, a blank line,
    then the bare URL; and a second call on the same date is refused before
    it can touch the network.

    Raises RuntimeError if the send-capable token doesn't exist yet -- this
    must fail loudly, not silently degrade to "no email sent", so a missing
    scope reads as a configuration gap (5.7 exception 2) rather than a
    permanent, quiet non-delivery a run has no way to surface.
    """
    if not google_api.send_scope_ok():
        raise RuntimeError(
            "no send-capable credential -- run "
            "`remote-auth send` (or `python ~/projects/google-oauth/google_api.py --authorize-send`) once, interactively, "
            "before this function can be used. See "
            "EXECUTION_PLAN_reviewed.md 6 item 2.")

    marker = _last_sent_marker(root)
    if os.path.exists(marker):
        with open(marker, "r", encoding="utf-8") as fh:
            if fh.read().strip() == today_iso:
                return {"sent": False,
                        "reason": "already sent today (%s)" % today_iso}

    import base64
    from email.mime.text import MIMEText

    d = datetime.strptime(today_iso, "%Y-%m-%d")
    subject = "Daily Briefing — %s, %s %d" % (
        d.strftime("%A"), d.strftime("%b"), d.day)
    body = "%s\n\n%s\n" % (summary_line, page_url)

    msg = MIMEText(body)
    msg["to"] = BRIEFING_RECIPIENT
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    gmail = google_api.send_gmail_service()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(today_iso)
    return {"sent": True, "to": BRIEFING_RECIPIENT, "subject": subject}
