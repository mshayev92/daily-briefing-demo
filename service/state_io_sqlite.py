"""SQLite-backed state I/O (EXECUTION_PLAN_reviewed.md Phase 2).

Replaces state_io.py's Drive-JSON read/write path. Deliberately does NOT
change the in-memory shape a run reasons over: `load()` reconstructs the same
nested dict SCHEMA_AND_STATE.md §3.2/§3.3 describes, so lifecycle.py,
schedule.py, umd_calendar.py, opportunity.py and render_briefing.py all run
against it completely unchanged. Only the storage backend moved.

Run this ALONGSIDE state_io.py (§ Phase 2's "read from both, compare") via
compare_state_io.py before anything depends on this module exclusively.
"""

import json
import os
import sqlite3

from state_shape import (
    ITEM_SCALAR_FIELDS, ITEM_EXTRA_FIELDS, ITEM_EXTRA_NEVER_COLLAPSE,
    ITEM_BOOL_FIELDS, ITEM_DEFAULTS, META_JSON_FIELDS, META_SCALAR_FIELDS,
)
import db as dbmod

# CONTEXT_KEYS from state_io.py, reused so for_context() behaves identically
# regardless of which backend loaded the state.
CONTEXT_KEYS = (
    "schema_version", "last_updated", "last_completed_date", "artifact_url",
    "db_last_synced", "state_folder_id", "briefings_folder_id",
    "automation_mode", "archive_enabled", "calendars", "courses",
    "learned_patterns", "last_weather", "items", "preferences", "campus",
    "last_delivery", "requirements", "coverage", "panes", "ledger_file_id",
)


def _row_to_item(row):
    d = {k: row[k] for k in ITEM_SCALAR_FIELDS if row[k] is not None}
    for k in ITEM_BOOL_FIELDS:
        if k in d:
            d[k] = bool(d[k])
    extra = json.loads(row["extra_json"] or "{}")
    for k in ITEM_EXTRA_FIELDS:
        if k not in extra:
            continue
        if k in ITEM_EXTRA_NEVER_COLLAPSE:
            # §3.1: absent means "never looked"; `[]`/`{}` means "looked,
            # found none". Preserve the value exactly, even when falsy.
            d[k] = extra[k]
        elif extra[k] not in (None, [], {}):
            d[k] = extra[k]
    return d


def _item_to_row(item):
    scalars = {k: item.get(k) for k in ITEM_SCALAR_FIELDS}
    for k in ITEM_BOOL_FIELDS:
        scalars[k] = int(bool(scalars.get(k, False)))
    scalars["times_surfaced"] = int(scalars.get("times_surfaced") or 0)
    # `scalars` already holds every column key (None when the item lacks
    # it), so setdefault() was a no-op here and one item missing e.g.
    # `regime` failed the NOT NULL constraint and aborted the WHOLE save
    # (2026-09-19 audit). Fill real defaults for None instead.
    for k, default in (("course_class", "none"), ("course_label", "System"),
                       ("regime", "confirmed"), ("status", "new")):
        if scalars.get(k) is None:
            scalars[k] = default
    extra = {k: item[k] for k in ITEM_EXTRA_FIELDS if k in item}
    scalars["extra_json"] = json.dumps(extra, separators=(",", ":"))
    return scalars


def load(db_path=dbmod.DEFAULT_DB_PATH):
    """Mirrors state_io.load_local()'s return shape: (state, source, problems).

    `state` is None only when the database file itself doesn't exist yet —
    the SQLite equivalent of SCHEMA_AND_STATE.md §3.1 step 6 (start empty).
    A schema that exists but is freshly initialized (no meta row) also counts
    as empty state, and is not an error.
    """
    problems = []
    if not os.path.exists(db_path):
        problems.append(
            "MAJOR: %s does not exist; starting from empty state" % db_path)
        return None, None, problems

    conn = dbmod.connect(db_path)
    try:
        meta_row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if meta_row is None:
            problems.append(
                "MAJOR: %s has no meta row; starting from empty state"
                % db_path)
            return None, None, problems

        state = {}
        for col, key in META_SCALAR_FIELDS.items():
            state[key] = meta_row[col]
        state["archive_enabled"] = bool(meta_row["archive_enabled"])
        for col, key in META_JSON_FIELDS.items():
            raw = meta_row[col]
            state[key] = json.loads(raw) if raw is not None else (
                {} if key not in ("umd_dates",) else [])

        state["items"] = [_row_to_item(r) for r in
                          conn.execute("SELECT * FROM items")]

        quiz_row = conn.execute(
            "SELECT * FROM prefs WHERE doc_id = 'quiz'").fetchone()
        custom_row = conn.execute(
            "SELECT * FROM prefs WHERE doc_id = 'custom'").fetchone()
        state["preferences"] = {
            "synced_at": meta_row["preferences_synced_at"],
            "quiz": (json.loads(quiz_row["body_json"]) if quiz_row
                     else {"version": 1, "answers": {}}),
            "custom": (json.loads(custom_row["body_json"]) if custom_row
                       else {"version": 1, "text": ""}),
        }

        req_rows = conn.execute("SELECT * FROM requirements").fetchall()
        state["requirements"] = {
            "synced_at": meta_row["requirements_synced_at"],
            "version": meta_row["requirements_version"] or 1,
            "entries": [
                {
                    "id": r["id"],
                    "statement": r["statement"],
                    "horizon_days": r["horizon_days"],
                    "keywords": json.loads(r["keywords_json"] or "[]"),
                    "active": bool(r["active"]),
                    "quiet_after_days": r["quiet_after_days"],
                }
                for r in req_rows
            ],
        }

        stats_rows = conn.execute(
            "SELECT * FROM run_stats ORDER BY seq ASC LIMIT 7").fetchall()
        state["last_run_stats"] = [
            {
                "start_time": r["start_time"],
                "duration_seconds": r["duration_seconds"],
                "emails_processed": r["emails_processed"],
                "items_found": r["items_found"],
                "items_changed": r["items_changed"],
                "delivery": {
                    "email": bool(r["delivery_email"]),
                    "drive": bool(r["delivery_drive"]),
                    "artifact": bool(r["delivery_artifact"]),
                },
                "error_count": r["error_count"],
                "bytes_staged": r["bytes_staged"],
                "bytes_state": r["bytes_state"],
                "bytes_page": r["bytes_page"],
                "skipped": json.loads(r["skipped_json"] or "[]"),
            }
            for r in stats_rows
        ]

        err_rows = conn.execute(
            "SELECT * FROM errors ORDER BY seq ASC LIMIT 50").fetchall()
        # Real production state (not just SCHEMA_AND_STATE.md's abbreviated
        # example) uses `timestamp`, not `at`, on each error entry. Match the
        # actual data rather than the doc's shorthand.
        state["errors"] = [
            {"timestamp": r["at"], "source": r["source"],
             "severity": r["severity"], "message": r["message"]}
            for r in err_rows
        ]

        return state, db_path, problems
    finally:
        conn.close()


def save(state, db_path=dbmod.DEFAULT_DB_PATH):
    """Write the full state dict back into SQLite. One transaction.

    Mirrors STEP 11's "one write per run" discipline: items are replaced
    wholesale from the in-memory list (the run already holds the true set
    after lifecycle.run()'s compaction/pruning), everything else is upserted.
    """
    dbmod.init_db(db_path)
    conn = dbmod.connect(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO meta (id, schema_version, last_updated,
                last_completed_date, artifact_url, db_last_synced,
                state_folder_id,
                briefings_folder_id, automation_mode, archive_enabled,
                calendars_json, courses_json, learned_patterns_json,
                umd_dates_json, umd_dates_source_json, last_weather_json,
                campus_json, coverage_json, panes_json, ledger_file_id,
                last_delivery_json, preferences_synced_at,
                requirements_synced_at, requirements_version)
            VALUES (1, :schema_version, :last_updated, :last_completed_date,
                :artifact_url, :db_last_synced, :state_folder_id,
                :briefings_folder_id,
                :automation_mode, :archive_enabled, :calendars_json,
                :courses_json, :learned_patterns_json, :umd_dates_json,
                :umd_dates_source_json, :last_weather_json, :campus_json,
                :coverage_json, :panes_json, :ledger_file_id,
                :last_delivery_json, :preferences_synced_at,
                :requirements_synced_at, :requirements_version)
            ON CONFLICT(id) DO UPDATE SET
                schema_version=excluded.schema_version,
                last_updated=excluded.last_updated,
                last_completed_date=excluded.last_completed_date,
                artifact_url=excluded.artifact_url,
                db_last_synced=excluded.db_last_synced,
                state_folder_id=excluded.state_folder_id,
                briefings_folder_id=excluded.briefings_folder_id,
                automation_mode=excluded.automation_mode,
                archive_enabled=excluded.archive_enabled,
                calendars_json=excluded.calendars_json,
                courses_json=excluded.courses_json,
                learned_patterns_json=excluded.learned_patterns_json,
                umd_dates_json=excluded.umd_dates_json,
                umd_dates_source_json=excluded.umd_dates_source_json,
                last_weather_json=excluded.last_weather_json,
                campus_json=excluded.campus_json,
                coverage_json=excluded.coverage_json,
                panes_json=excluded.panes_json,
                ledger_file_id=excluded.ledger_file_id,
                last_delivery_json=excluded.last_delivery_json,
                preferences_synced_at=excluded.preferences_synced_at,
                requirements_synced_at=excluded.requirements_synced_at,
                requirements_version=excluded.requirements_version
        """, {
            "schema_version": state.get("schema_version", 11),
            "last_updated": state.get("last_updated"),
            "last_completed_date": state.get("last_completed_date"),
            "artifact_url": state.get("artifact_url"),
            "db_last_synced": state.get("db_last_synced"),
            "state_folder_id": state.get("state_folder_id"),
            "briefings_folder_id": state.get("briefings_folder_id"),
            "automation_mode": state.get("automation_mode", "active"),
            "archive_enabled": int(bool(state.get("archive_enabled", True))),
            "calendars_json": json.dumps(state.get("calendars") or {}),
            "courses_json": json.dumps(state.get("courses") or {}),
            "learned_patterns_json": json.dumps(
                state.get("learned_patterns") or {}),
            "umd_dates_json": json.dumps(state.get("umd_dates") or []),
            "umd_dates_source_json": json.dumps(
                state.get("umd_dates_source")) if state.get(
                "umd_dates_source") else None,
            "last_weather_json": json.dumps(
                state.get("last_weather")) if state.get(
                "last_weather") else None,
            "campus_json": json.dumps(state.get("campus") or {}),
            "coverage_json": json.dumps(state.get("coverage") or {}),
            "panes_json": json.dumps(state.get("panes") or {}),
            "ledger_file_id": state.get("ledger_file_id"),
            "last_delivery_json": json.dumps(
                state.get("last_delivery")) if state.get(
                "last_delivery") else None,
            "preferences_synced_at": (state.get("preferences") or {}).get(
                "synced_at"),
            "requirements_synced_at": (state.get("requirements") or {}).get(
                "synced_at"),
            "requirements_version": (state.get("requirements") or {}).get(
                "version", 1),
        })

        # Lost-update guard (2026-09-19 audit). A run loads state, works for
        # a minute or two, then replaces `items` wholesale -- so a Done /
        # Resolved / Confirmed / Killed click made on the live page DURING
        # that window used to be silently reverted. Inside this same
        # transaction, a disposition the page wrote to the DB wins over the
        # run's in-memory copy whenever the run still had that item open.
        # (The pipeline itself never reopens a closed item except via an
        # expired snooze, and a snooze is not one of these statuses.)
        user_closed = {
            r[0]: r[1] for r in conn.execute(
                "SELECT id, status FROM items WHERE status IN "
                "('handled', 'dismissed', 'confirmed', 'killed')")}
        for item in state.get("items") or []:
            db_status = user_closed.get(item.get("id"))
            if db_status and item.get("status") in (
                    None, "new", "ongoing", "unresolved", "open"):
                item["status"] = db_status

        conn.execute("DELETE FROM items")
        for item in state.get("items") or []:
            row = _item_to_row(item)
            cols = list(row.keys())
            conn.execute(
                "INSERT INTO items (%s) VALUES (%s)" % (
                    ", ".join(cols), ", ".join(":" + c for c in cols)),
                row)

        # `state["preferences"]["quiz"/"custom"]` and `state["requirements"]`
        # are only ever populated from a read (STEP 0.7 / the FastAPI PUT
        # endpoints in Phase 6) — writing them back here just persists what
        # the run already loaded, never anything the pipeline itself decided.
        prefs = state.get("preferences") or {}
        for doc_id in ("quiz", "custom"):
            body = prefs.get(doc_id)
            if body is None:
                continue
            # Same lost-update guard: the page's PUT /prefs/* stamps
            # updated_at; if the stored copy is newer than the one this run
            # loaded, the user changed it mid-run and it is kept.
            cur = conn.execute("SELECT updated_at FROM prefs WHERE doc_id = ?",
                               (doc_id,)).fetchone()
            if cur and cur[0] and (body.get("updated_at") or "") < cur[0]:
                continue
            conn.execute("""
                INSERT INTO prefs (doc_id, version, updated_at, body_json)
                VALUES (:doc_id, :version, :updated_at, :body_json)
                ON CONFLICT(doc_id) DO UPDATE SET
                    version=excluded.version, updated_at=excluded.updated_at,
                    body_json=excluded.body_json
            """, {"doc_id": doc_id, "version": body.get("version", 1),
                  "updated_at": body.get("updated_at"),
                  "body_json": json.dumps(body)})

        conn.execute("DELETE FROM requirements")
        for req in (state.get("requirements") or {}).get("entries") or []:
            conn.execute("""
                INSERT INTO requirements (id, statement, horizon_days,
                    keywords_json, active, quiet_after_days)
                VALUES (:id, :statement, :horizon_days, :keywords_json,
                    :active, :quiet_after_days)
            """, {
                "id": req.get("id"),
                "statement": req.get("statement"),
                "horizon_days": req.get("horizon_days", 180),
                "keywords_json": json.dumps(req.get("keywords") or []),
                "active": int(bool(req.get("active", True))),
                "quiet_after_days": req.get("quiet_after_days", 30),
            })

        # `last_run_stats` and `errors` arrive already assembled and capped by
        # the caller (run_stats.prepend(keep=7), §3.2's keep-50/drop-30-days
        # rule) — a full replace here, in list order, mirrors how `items` is
        # handled rather than accumulating duplicates across runs.
        conn.execute("DELETE FROM run_stats")
        for r in state.get("last_run_stats") or []:
            conn.execute("""
                INSERT INTO run_stats (start_time, duration_seconds,
                    emails_processed, items_found, items_changed,
                    delivery_email, delivery_drive, delivery_artifact,
                    error_count, bytes_staged, bytes_state, bytes_page,
                    skipped_json)
                VALUES (:start_time, :duration_seconds, :emails_processed,
                    :items_found, :items_changed, :delivery_email,
                    :delivery_drive, :delivery_artifact, :error_count,
                    :bytes_staged, :bytes_state, :bytes_page, :skipped_json)
            """, {
                "start_time": r.get("start_time"),
                "duration_seconds": r.get("duration_seconds"),
                "emails_processed": r.get("emails_processed"),
                "items_found": r.get("items_found"),
                "items_changed": r.get("items_changed"),
                "delivery_email": int(bool(
                    (r.get("delivery") or {}).get("email"))),
                "delivery_drive": int(bool(
                    (r.get("delivery") or {}).get("drive"))),
                "delivery_artifact": int(bool(
                    (r.get("delivery") or {}).get("artifact"))),
                "error_count": r.get("error_count"),
                "bytes_staged": r.get("bytes_staged"),
                "bytes_state": r.get("bytes_state"),
                "bytes_page": r.get("bytes_page"),
                "skipped_json": json.dumps(r.get("skipped") or []),
            })

        conn.execute("DELETE FROM errors")
        for err in state.get("errors") or []:
            conn.execute(
                "INSERT INTO errors (at, source, severity, message) "
                "VALUES (:at, :source, :severity, :message)",
                {"at": err.get("timestamp") or err.get("at"),
                 "source": err.get("source"),
                 "severity": err.get("severity"), "message": err.get(
                     "message", "")})

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def for_context(state, today, keep_run_stats=1):
    """Identical projection logic to state_io.py — pure dict operation, no
    I/O, so it is imported unchanged rather than duplicated."""
    import state_io as legacy_state_io
    return legacy_state_io.for_context(state, today, keep_run_stats)
