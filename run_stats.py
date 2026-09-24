"""Assemble one `last_run_stats` entry. Deterministic, no I/O beyond stat().

Exists because `duration_seconds` was written as null on three of the first
five v10-era runs despite STEP 11 requiring a real number. The subtraction was
a manual step at the end of a 33-minute run; the four numbers below are now
computed rather than remembered. See RATIONALE_LOG.md 2026-09-09.

`bytes_staged` is the total size of the project files this run's Python opened
from disk. Under local execution nothing is copied, so this measures what
stayed OUT of context. A drop toward zero means a script stopped importing
something it needs, not that a saving was found (SCHEMA_AND_STATE.md 3.2).
"""

import os
from datetime import datetime, timezone

# The files every run opens, per prompt step 8a's table. Conditional ones
# (umd_calendar, opportunity, collect, umd_dates) are passed by the caller,
# because only the run knows which conditions held.
ALWAYS = (
    "briefing_artifact_template.html",
    "render_briefing.py",
    "reminder_url.py",
    "ledger.py",
    # Step 0's credential check, the sweep, and both Drive writes (§1.9).
    # Counted here and not in extra_modules because there is no run that
    # skips it: a run with no mail and no Drive still calls health().
    "google_api.py",
    # Step 6's maintenance and step 7's routing; step 4's arithmetic. Both run
    # on every run, including a paused one — a paused run still publishes.
    "lifecycle.py",
    "schedule.py",
    # §15. Imported by build_row on every run that renders a rateable row, and
    # by step 6.1b on every run without exception.
    "feedback.py",
)


def _utc(ts):
    """Parse an ISO-8601 UTC timestamp. Accepts a trailing Z."""
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _size(path):
    """Size in bytes, or 0 if absent. Absence is the caller's problem to report."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def total_bytes(paths):
    return sum(_size(p) for p in paths)


def entry(start_time, emails_processed, items_found, items_changed,
          delivery, error_count, state_path, page_path,
          extra_modules=(), root="", now=None, skipped=()):
    """Build the entry. `duration_seconds` cannot come back null from here.

    start_time     ISO-8601 UTC string, as stored at step 0.
    delivery       {"email": bool, "drive": bool, "artifact": bool}
    extra_modules  conditional files this run actually imported, e.g.
                   umd_calendar.py when max_events > 0 (step 8a's table).
    root           directory holding the project files; "" means cwd.
    skipped        §9.6 — the steps that truncated themselves this run, as
                   `["step5b: 2 of 3 category pages", "step6.7: link backfill"]`.
                   Recorded because runs are 23–33 minutes long and already
                   sacrificing work to stay inside budget: on 2026-09-10 the
                   backfill was skipped and two campus pages were never
                   fetched, both at INFO in `errors`, and the page said
                   nothing. Every entry here must ALSO have raised a System
                   row via `lifecycle.skip_row()` — this field is the trend
                   line, the row is what Michael actually reads.
    """
    end = now or datetime.now(timezone.utc)
    start = _utc(start_time)
    duration = (end - start).total_seconds()
    if duration < 0:
        # Clock skew or a bad start_time. Report 0.0, never None: the field
        # exists to show runs getting slower, and null tells you nothing.
        duration = 0.0

    staged = [os.path.join(root, f) for f in ALWAYS]
    staged += [os.path.join(root, f) for f in extra_modules]

    delivery = delivery or {}
    return {
        "start_time": start_time,
        "duration_seconds": round(duration, 1),
        "emails_processed": int(emails_processed),
        "items_found": int(items_found),
        "items_changed": int(items_changed),
        "delivery": {
            "email": bool(delivery.get("email")),
            "drive": bool(delivery.get("drive")),
            "artifact": bool(delivery.get("artifact")),
        },
        "error_count": int(error_count),
        "bytes_staged": total_bytes(staged),
        "bytes_state": _size(state_path),
        "bytes_page": _size(page_path),
        "skipped": [str(x) for x in (skipped or ())],
    }


def prepend(stats, new_entry, keep=7):
    """Newest first, capped. `stats` may be None or absent."""
    out = [new_entry] + list(stats or [])
    return out[:keep]
