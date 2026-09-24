"""State file I/O per SCHEMA_AND_STATE.md 3.1, plus the context projection.

Two jobs, both of which used to be done by hand:

1. **The read discipline.** 3.1's filename filter, parsed-timestamp sort and
   three-attempt fallback chain were ~900 tokens of prose describing one regex,
   one sort key and one loop. They are code now, so a run cannot get the
   `_superseded_` sort trap wrong: those names share the state prefix and `s`
   sorts above any `2026-...` timestamp, so a lexicographic sort silently
   prefers an old backup over the newest real file.

2. **The projection.** Drive reads pass the state through a base64 round-trip
   that puts it into context twice (3.1). Measured on 2026-09-09 the file was
   46,787 bytes compact. Python reads all of it; `for_context()` returns only
   what the run reasons over, so `errors`, the older `last_run_stats` entries
   and out-of-window `umd_dates` never enter context at all.

**Items are NOT windowed.** Only 4,770 of 36,647 item bytes fall outside the
30-day window, and dedupe needs the out-of-window ones to avoid re-surfacing an
item it already handled. Slicing them would trade a real correctness property
for ~1,200 tokens. Measured, then rejected.

Drive remains the durable write target. `create_file` takes content, so the
write still crosses context; that cost is not recoverable and is not pretended
away here.
"""

import json
import os
import re
from datetime import date, datetime, timezone

# 3.1: nothing else is a state file. Anchored on purpose.
STATE_RE = re.compile(
    r"^college_assistant_state_(\d{4}-\d{2}-\d{2}T\d{6}Z)\.json$"
)
LEGACY_NAME = "college_assistant_state.json"
STAMP_FMT = "%Y-%m-%dT%H%M%SZ"

# Kept in the projection because the run reasons over them.
CONTEXT_KEYS = (
    "schema_version", "last_updated", "last_completed_date", "artifact_url",
    "db_last_synced", "state_folder_id", "briefings_folder_id",
    "automation_mode", "archive_enabled", "calendars", "courses",
    "learned_patterns", "last_weather", "items", "preferences", "campus",
    "last_delivery", "requirements", "coverage", "panes", "ledger_file_id",
)


def parse_stamp(name):
    """Return the parsed timestamp for a state filename, or None if not one."""
    m = STATE_RE.match(str(name).strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), STAMP_FMT).replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def order_candidates(names):
    """Filter to real state files and sort newest first by PARSED timestamp.

    Discards `_superseded_`, `_backup_` and `_temp` by construction: they do
    not match the anchored pattern.
    """
    dated = []
    for n in names or ():
        ts = parse_stamp(n)
        if ts is not None:
            dated.append((ts, n))
    dated.sort(key=lambda p: p[0], reverse=True)
    return [n for _, n in dated]


def load(names, read_text, max_attempts=3):
    """Walk 3.1's fallback chain. Returns (state, source_name, problems).

    `read_text(name) -> str` is supplied by the caller, so this works against
    a local folder or a Drive fetch without knowing which. `state` is None only
    when every route failed, which is 3.1 step 6: start from empty, say so
    prominently, never crash.
    """
    problems = []
    for name in order_candidates(names)[:max_attempts]:
        try:
            return json.loads(read_text(name)), name, problems
        except Exception as exc:
            problems.append(
                "MAJOR: state file %s did not parse (%s)" % (name, exc))
    if LEGACY_NAME in (names or ()):
        try:
            return json.loads(read_text(LEGACY_NAME)), LEGACY_NAME, problems
        except Exception as exc:
            problems.append(
                "MAJOR: legacy %s did not parse (%s)" % (LEGACY_NAME, exc))
    problems.append(
        "MAJOR: no state file survived the filter; starting from empty state")
    return None, None, problems


def load_local(folder, read_text=None):
    """Load from a local folder. Returns (state, source_name, problems)."""
    try:
        names = os.listdir(folder)
    except OSError as exc:
        return None, None, ["MAJOR: cannot list %s (%s)" % (folder, exc)]

    def _read(name):
        with open(os.path.join(folder, name), "r", encoding="utf-8") as fh:
            return fh.read()

    return load(names, read_text or _read)


def new_name(now=None):
    """The write filename for this run, per 3.1's exact pattern."""
    return "college_assistant_state_%s.json" % (
        (now or datetime.now(timezone.utc)).strftime(STAMP_FMT))


def serialize(state):
    """Compact JSON. 3.1 makes this a MUST; pretty-printing inflates every
    future read and write by roughly a fifth."""
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


def _in_window(value, today, back=3, ahead=30):
    try:
        d = date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return True  # undated entries are kept; absence is not exclusion
    return -back <= (d - today).days <= ahead


def for_context(state, today, keep_run_stats=1):
    """The slice the run reasons over. Everything omitted is still on disk.

    Omits, with measured 2026-09-09 sizes:
      errors          3,000 bytes  the run appends; it never reads them back
      last_run_stats  1,017 bytes  only the newest is compared; run_stats.py
                                   handles the append
      umd_dates       2,589 bytes  the feature is parked
                                   (lifecycle.UMD_DEADLINES_ENABLED), so none
                                   of it is surfaced and all of it is omitted.
                                   `_in_window` and the windowing branch are
                                   kept for when it is re-enabled.
    """
    if not state:
        return {}
    out = {k: state[k] for k in CONTEXT_KEYS if k in state}

    # Preserve unknown keys: 3.1 warns that a rewrite keeping only documented
    # fields would destroy accumulated data like learned_patterns' subkeys.
    for k, v in state.items():
        if k not in out and k not in ("errors", "last_run_stats", "umd_dates"):
            out[k] = v

    stats = state.get("last_run_stats") or []
    if stats:
        out["last_run_stats"] = stats[:keep_run_stats]

    # `umd_dates` is omitted entirely while §7c is parked — nothing reads it,
    # so windowing it to 3 relevant entries would be paying for context a run
    # cannot use. Restore this branch alongside
    # lifecycle.UMD_DEADLINES_ENABLED:
    #
    #   dates = state.get("umd_dates")
    #   if isinstance(dates, list):
    #       out["umd_dates"] = [e for e in dates if _in_window(
    #           e.get("date") if isinstance(e, dict) else e, today)]
    #   elif dates is not None:
    #       out["umd_dates"] = dates
    #
    # The stored entries are NOT deleted; STEP 11 writes the full object and
    # `state.umd_dates` survives untouched for a future re-enable.

    out["_projected"] = True  # a run must never write this object back as state
    return out


def projection_savings(state, today):
    """Bytes the projection keeps out of context. For last_run_stats/notes."""
    full = len(serialize(state))
    proj = len(serialize(for_context(state, today)))
    return {"full_bytes": full, "context_bytes": proj, "saved_bytes": full - proj}
