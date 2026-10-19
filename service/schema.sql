-- UMD Daily Briefing Pipeline — SQLite schema (migration Phase 1)
--
-- Replaces BOTH the artifact `db` key-value store (prefs/quiz, prefs/custom,
-- prefs/requirements, feedback/<id>, assignments/done, attention/resolved,
-- ai/<item-id>__<task>) AND the Drive-hosted college_assistant_state_*.json
-- blob (EXECUTION_PLAN_reviewed.md §2).
--
-- Design rule: normalize the item fields the pipeline actually filters or
-- sorts on; JSON-blob the rest (extra_json). state_io_sqlite.py reconstructs
-- the exact nested dict shape SCHEMA_AND_STATE.md §3.2/§3.3 specifies, so
-- every existing pure-Python module (lifecycle.py, schedule.py,
-- umd_calendar.py, opportunity.py, render_briefing.py, ...) runs UNCHANGED
-- against the reconstructed dict — only the storage backend moved.
--
-- `prefs` and `requirements` are the AUTHORITATIVE store now (no more
-- artifact db to be authoritative instead). PROJECT_INSTRUCTIONS.md §1.1's
-- rule stands unchanged: the orchestrator reads these tables, it never
-- writes them. Only the FastAPI PUT endpoints (Phase 6), driven by a real
-- request from Michael, write here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row. Top-level scalar/blob fields from SCHEMA_AND_STATE.md §3.2 that
-- aren't better modeled as their own table.
CREATE TABLE IF NOT EXISTS meta (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version          INTEGER NOT NULL DEFAULT 11,
    last_updated            TEXT,
    last_completed_date     TEXT,
    -- Retained through Phase 7 (parallel run): the old interactive pipeline
    -- still needs the one stable artifact_url. Becomes vestigial once the
    -- artifact is retired in Phase 8.
    artifact_url            TEXT,
    db_last_synced          TEXT,
    state_folder_id         TEXT,
    briefings_folder_id     TEXT,
    automation_mode         TEXT NOT NULL DEFAULT 'active',
    archive_enabled         INTEGER NOT NULL DEFAULT 1,
    calendars_json          TEXT NOT NULL DEFAULT '{}',
    courses_json            TEXT NOT NULL DEFAULT '{}',
    learned_patterns_json   TEXT NOT NULL DEFAULT '{}',
    umd_dates_json          TEXT NOT NULL DEFAULT '[]',
    umd_dates_source_json   TEXT,
    last_weather_json       TEXT,
    campus_json             TEXT NOT NULL DEFAULT '{}',
    coverage_json           TEXT NOT NULL DEFAULT '{}',
    panes_json              TEXT NOT NULL DEFAULT '{}',
    ledger_file_id          TEXT,
    last_delivery_json      TEXT,
    preferences_synced_at   TEXT,
    requirements_synced_at  TEXT,
    requirements_version    INTEGER DEFAULT 1
);

-- items[] — SCHEMA_AND_STATE.md §3.3. Columns the pipeline filters/sorts on
-- are real columns; everything else lives in extra_json:
--   links[], eligibility{}, basis[], relevance{}, extraction{}, ai_actions[],
--   source_refs[], requirement_ids[]
CREATE TABLE IF NOT EXISTS items (
    id                 TEXT PRIMARY KEY,
    course             TEXT,
    course_class       TEXT NOT NULL,
    course_label       TEXT NOT NULL,
    kind               TEXT NOT NULL,
    date_changed_on    TEXT,
    title              TEXT NOT NULL,
    detail             TEXT,
    notes              TEXT,
    date               TEXT,
    end_date           TEXT,
    time               TEXT,
    end_time           TEXT,
    location           TEXT,
    canvas_url         TEXT,
    organizer          TEXT,
    description        TEXT,
    source_url         TEXT,
    evidence           TEXT,
    regime             TEXT NOT NULL DEFAULT 'confirmed',
    act_by             TEXT,
    act_by_basis       TEXT,
    effort             TEXT,
    competition        TEXT,
    next_action        TEXT,
    confirm_action     TEXT,
    kill_criteria      TEXT,
    confidence         TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    -- When the page last wrote `status` (store.stamp()). save() uses it to
    -- keep a page edit made while a run held a stale copy of the item.
    status_changed_at  TEXT,
    times_surfaced     INTEGER NOT NULL DEFAULT 0,
    last_shown         TEXT,
    snooze_until       TEXT,
    supersedes         TEXT,
    group_id           TEXT,
    overdue_flagged    INTEGER NOT NULL DEFAULT 0,
    needs_attention    INTEGER NOT NULL DEFAULT 0,
    config_unfixed     INTEGER NOT NULL DEFAULT 0,
    extra_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_group ON items(group_id);

-- prefs/quiz and prefs/custom (CAMPUS_AND_PREFERENCES.md §12.1). AUTHORITATIVE
-- post-migration. The pipeline reads; only a user PUT via the FastAPI page
-- (Phase 6) writes.
CREATE TABLE IF NOT EXISTS prefs (
    doc_id      TEXT PRIMARY KEY CHECK (doc_id IN ('quiz', 'custom')),
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT,
    body_json   TEXT NOT NULL
);

-- prefs/requirements (DISCOVERY.md §13.2), one row per standing requirement.
-- Same read-only-to-the-pipeline rule as `prefs`.
CREATE TABLE IF NOT EXISTS requirements (
    id                TEXT PRIMARY KEY,
    statement         TEXT NOT NULL,
    horizon_days      INTEGER NOT NULL DEFAULT 180,
    keywords_json     TEXT NOT NULL DEFAULT '[]',
    active            INTEGER NOT NULL DEFAULT 1,
    quiet_after_days  INTEGER NOT NULL DEFAULT 30
);

-- §15 ratings. One row per item id (family/organizer stamped at click time,
-- per-instance id as the key — matches the artifact db's
-- `feedback/<item-id>` shape). Read-only to the pipeline; written only by the
-- FastAPI rating endpoint.
CREATE TABLE IF NOT EXISTS feedback (
    item_id    TEXT PRIMARY KEY,
    rating     TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    family     TEXT NOT NULL,
    organizer  TEXT,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_family ON feedback(family);

-- Audit trail for Mark done / Resolved clicks. `items.status` is what the
-- pipeline actually reads (§6.1/§6.2); this table is provenance only, in case
-- "when did I click that" is ever asked. Written only by the FastAPI
-- done/resolved endpoints (Phase 6).
CREATE TABLE IF NOT EXISTS done_resolved (
    item_id  TEXT NOT NULL,
    action   TEXT NOT NULL CHECK (action IN ('done', 'resolved')),
    at       TEXT NOT NULL,
    PRIMARY KEY (item_id, action)
);

-- last_run_stats[], as rows instead of a capped array. Keep the newest 7 at
-- read time (state_io_sqlite.py), matching run_stats.prepend(keep=7); rows
-- older than that are pruned at write time rather than carried forever.
CREATE TABLE IF NOT EXISTS run_stats (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time         TEXT NOT NULL,
    duration_seconds   REAL,
    emails_processed   INTEGER,
    items_found        INTEGER,
    items_changed      INTEGER,
    delivery_email     INTEGER,
    delivery_drive     INTEGER,
    delivery_artifact  INTEGER,
    error_count        INTEGER,
    bytes_staged       INTEGER,
    bytes_state        INTEGER,
    bytes_page         INTEGER,
    skipped_json       TEXT NOT NULL DEFAULT '[]'
);

-- errors[], as rows. Keep newest 50 / 30 days at read time, per §3.2.
CREATE TABLE IF NOT EXISTS errors (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    source    TEXT,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL
);

-- §6.5's saved AI answers: `ai/<item-id>__<task>` in the old artifact db.
-- Read AND written by the orchestrator's own compose step (unlike prefs/
-- feedback, this one the pipeline owns) plus by the FastAPI hand-off
-- endpoint once Phase 6 builds it.
CREATE TABLE IF NOT EXISTS ai_answers (
    item_id       TEXT NOT NULL,
    task          TEXT NOT NULL,
    context_hash  TEXT NOT NULL,
    answer        TEXT NOT NULL,
    at            TEXT NOT NULL,
    PRIMARY KEY (item_id, task)
);

-- "Plan my week" — one page-level answer, keyed on a hash of the whole
-- workload list (§6.5).
CREATE TABLE IF NOT EXISTS plan_my_week (
    context_hash  TEXT PRIMARY KEY,
    answer        TEXT NOT NULL,
    at            TEXT NOT NULL
);

-- Grades + trends (2026-09-19). One row per (course, run) where a live
-- Canvas read actually succeeded that run -- a failed/stale read simply
-- inserts nothing, so the most recent row is always the most recent
-- GENUINE observation, never a synthetic placeholder. Append-only, like
-- the JSONL ledger (ledger.py): rows are never UPDATEd or DELETEd, so a later Canvas
-- recalculation (assignment added/removed/reweighted) shows up as a new,
-- honestly-dated data point rather than erasing what was true before.
-- Read/written by service/grades.py + orchestrator.py's grades step via
-- plain SQL (same pattern as `rendered_page` in step9_publish) -- this is
-- NOT part of the big nested `state` dict, so state_shape.py/
-- state_io_sqlite.py/migrate_state.py need no changes for it.
CREATE TABLE IF NOT EXISTS course_grade_snapshots (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,   -- run timestamp, UTC ISO
    course_label     TEXT NOT NULL,   -- e.g. "PHIL001" -- one of COURSE_PAIRS' 5 real classes
    -- Canvas's own computed_current_score/grade: the grade counting ONLY
    -- graded work (what Canvas's Grades page shows by default). Never an
    -- estimate this pipeline invented.
    current_score    REAL,
    current_grade    TEXT,
    -- Canvas's own computed_final_score/grade: the grade if every ungraded
    -- item counted as a zero right now -- official, but only meaningful
    -- late in a term when most work is graded; stored for completeness,
    -- deliberately NOT surfaced on the page early in a term (see grades.py).
    final_score      REAL,
    final_grade      TEXT,
    graded_count     INTEGER NOT NULL DEFAULT 0,  -- gradable items with a posted grade
    gradable_count   INTEGER NOT NULL DEFAULT 0,  -- published, points_possible > 0
    missing_count    INTEGER NOT NULL DEFAULT 0,  -- Canvas's own `missing` flag, never inferred
    source           TEXT NOT NULL DEFAULT 'canvas_scraper',
    -- 2026-10-17: JSON [[name, score, points], ...] of the graded items, so a
    -- grade push can name what was graded. NULL in older rows.
    graded_items     TEXT
);
CREATE INDEX IF NOT EXISTS idx_course_grade_snapshots_course
    ON course_grade_snapshots(course_label, captured_at);

-- 2026-09-22 (brief v2). Small pipeline-owned key/value store for state that
-- is not an item: the "What changed" baseline (which Canvas changes were
-- already reported on an earlier day's brief) and the page's last-visit
-- time for "since you last looked".
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Summarize once, reuse forever: every LLM output that is a pure function
-- of its input text is stored under (task, sha256 of the input, prompt
-- version). Unchanged input never reaches a model twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    task          TEXT NOT NULL,
    input_hash    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    output_json   TEXT NOT NULL,
    at            TEXT NOT NULL,
    PRIMARY KEY (task, input_hash, version)
);

-- One row per `claude -p` call a run made: what each step actually costs.
CREATE TABLE IF NOT EXISTS llm_calls (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_start          TEXT NOT NULL,
    label              TEXT NOT NULL,
    model              TEXT,
    ok                 INTEGER NOT NULL,
    cost_usd           REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    seconds            REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_start);

-- 2026-09-25. Where Michael stands on each opportunity he is pursuing
-- (applications.py owns the stages). Written ONLY by the page
-- (POST /applications/{id}, and the Draft action's doc link); the run reads
-- it and never writes it, like `feedback` -- so a click made while a run is
-- in flight is never overwritten. No foreign key to items: a tracked row
-- outlives the item's normal pruning (lifecycle keeps active ones), and a
-- closed one may outlive the item harmlessly.
CREATE TABLE IF NOT EXISTS applications (
    item_id       TEXT PRIMARY KEY,
    stage         TEXT NOT NULL,
    stage_on      TEXT NOT NULL,
    follow_up_on  TEXT,
    draft_url     TEXT,
    history_json  TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL,
    -- An application added by hand from the page (the brief never showed
    -- it): {title, organization, url, deadline}. NULL for the rest.
    manual_json   TEXT
);

-- 2026-09-29 (app notifications). Every event notify.py decided is worth
-- telling Michael about. The primary key is the dedup ledger (an event is
-- recorded once, ever); the rowid is the cursor the Mac app polls with.
-- pushed_at: sent to the phone through ntfy (NULL = held or not sent).
-- read_at: opened in an app. expires_at: a held event that is stale by
-- release time is dropped rather than pushed.
CREATE TABLE IF NOT EXISTS notifications (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    item_id     TEXT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    pushed_at   TEXT,
    read_at     TEXT
);

-- 2026-10-10 (things to watch). A confirmed hit on a watch: its own section on
-- the page, not an opportunity row. One row per watch item (item_id is
-- watch-<id>[-<item slug>]). The run keeps a row while its item still meets
-- the watch and removes it when it no longer does or the watch is paused or
-- removed. found_on: first day it met the watch.
CREATE TABLE IF NOT EXISTS watch_hits (
    item_id     TEXT PRIMARY KEY,
    watch_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    finding     TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    seen_on     TEXT NOT NULL DEFAULT 'page',
    found_on    TEXT NOT NULL,
    checked_on  TEXT NOT NULL
);

-- 2026-10-10 (undo). One row per page action that changed an item's status,
-- with the status it replaced. POST /undo puts the newest row not yet undone
-- back to before_status; undone_at marks it. Pruned to the newest 200.
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    before_status TEXT NOT NULL,
    after_status  TEXT NOT NULL,
    at            TEXT NOT NULL,
    undone_at     TEXT,
    -- An application write (stage, follow-up date, track, draft): the
    -- applications record it replaced, JSON; 'null' when there was none.
    -- NULL for an action that did not touch the application.
    app_before    TEXT,
    -- The words the undo bar shows ("Status: Applying").
    label         TEXT
);
