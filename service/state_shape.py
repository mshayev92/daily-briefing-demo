"""Shared column/key lists for the state <-> SQLite mapping.

Single source of truth for both migrate_state.py (JSON -> SQLite, one-time)
and state_io_sqlite.py (SQLite -> dict -> SQLite, every run), so the two can
never drift on which fields are columns vs. which live in `extra_json`.

Reference: SCHEMA_AND_STATE.md §3.2 (top-level schema) and §3.3 (item schema).
"""

# Item fields stored as real columns in `items`. Order matches §3.3's listing.
ITEM_SCALAR_FIELDS = (
    "id", "course", "course_class", "course_label", "kind",
    "date_changed_on", "title", "detail", "notes", "date", "end_date",
    "time", "end_time", "location", "canvas_url", "organizer", "description",
    "source_url", "evidence", "regime", "act_by", "act_by_basis", "effort",
    "competition", "next_action", "confirm_action", "kill_criteria",
    "confidence", "status", "times_surfaced", "last_shown", "snooze_until",
    "supersedes", "group_id", "overdue_flagged", "needs_attention",
    "config_unfixed",
)

# §3.3 fields that are lists/dicts of variable shape. Round-tripped verbatim
# through `extra_json` rather than normalized into their own tables — none of
# them is filtered or sorted on by the pure-Python modules (lifecycle.py,
# schedule.py, opportunity.py, render_briefing.py), only read whole.
ITEM_EXTRA_FIELDS = (
    "links", "eligibility", "basis", "relevance", "extraction",
    "ai_actions", "source_refs", "requirement_ids",
    # Canvas's own submission mechanism for an assignment/quiz object, e.g.
    # `["none"]`, `["online_text_entry"]`, `["external_tool"]` -- threaded
    # through by canvas_shadow.py's `canvas_authoritative_candidates()`
    # since 2026-09-18, so lifecycle.action_tag() has real signal to tell a
    # participation clicker activity from an actual submission apart from
    # Canvas's own object type ("assignment") and a due date, which both
    # activities share.
    "submission_types",
    # 2026-09-19 audit: Canvas's own completion record for the object
    # (canvas_shadow.canvas_submission_summary()) and its point value --
    # what lets lifecycle tell "submitted on Canvas" / "0-point optional" /
    # "Canvas says missing" apart instead of guessing from a passed date.
    "canvas_submission", "points_possible",
    # Why the pipeline itself (not a click) closed an item -- e.g.
    # "canvas: submitted", "duplicate of <id>" -- so a retired row is
    # explainable from state alone.
    "handled_by", "expired_reason",
    # Brief v2 (2026-09-22). `first_seen`: the ISO date the pipeline first
    # recorded the item (drives "since you last looked"). `opportunity`: an
    # opportunity's own facts (type, organization, verbatim eligibility,
    # apply link) plus its researched `dossier`. `disagreement`: the lower-
    # authority source's differing date, shown on the row instead of only
    # being logged.
    "first_seen", "opportunity", "disagreement", "digest",
)

# §3.1: for THESE two fields specifically, absent vs. `[]`/`{}` are different
# facts ("never looked" vs. "looked, found none") and must never collapse
# into each other. Every other extra field may be omitted when falsy — this
# is the one place the "omit when equal to default" rule in §3.1 must NOT be
# applied mechanically, and §3.1 says so in exactly those words.
ITEM_EXTRA_NEVER_COLLAPSE = ("links", "extraction")

# Booleans stored as INTEGER 0/1 in SQLite; convert both directions.
ITEM_BOOL_FIELDS = ("overdue_flagged", "needs_attention", "config_unfixed")

# Defaults from §3.1's "omit when equal to default" rule — used when
# reconstructing a dict from a row that omitted a column-backed field.
ITEM_DEFAULTS = {
    "regime": "confirmed",
    "status": "new",
    "times_surfaced": 0,
    "overdue_flagged": False,
    "needs_attention": False,
    "config_unfixed": False,
    "ai_actions": [],
    "notes": "",
}

# Top-level `meta` blob columns and the state-dict key they represent.
META_JSON_FIELDS = {
    "calendars_json": "calendars",
    "courses_json": "courses",
    "learned_patterns_json": "learned_patterns",
    "umd_dates_json": "umd_dates",
    "umd_dates_source_json": "umd_dates_source",
    "last_weather_json": "last_weather",
    "campus_json": "campus",
    "coverage_json": "coverage",
    "panes_json": "panes",
    "last_delivery_json": "last_delivery",
}

META_SCALAR_FIELDS = {
    "schema_version": "schema_version",
    "last_updated": "last_updated",
    "last_completed_date": "last_completed_date",
    "artifact_url": "artifact_url",
    "db_last_synced": "db_last_synced",
    "state_folder_id": "state_folder_id",
    "briefings_folder_id": "briefings_folder_id",
    "automation_mode": "automation_mode",
    "archive_enabled": "archive_enabled",
    "ledger_file_id": "ledger_file_id",
}
