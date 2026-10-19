"""Small helpers over the pipeline-owned tables added for brief v2:
`kv` (named JSON values), `llm_cache` (LLM outputs keyed by a hash of their
input) and `llm_calls` (per-call cost accounting). Each helper opens and
closes its own connection, like the rest of this service."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import db as dbmod


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    """A write time that sorts as text. One format for every writer, so two
    stamps compare with a plain string comparison."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


ERRORS_KEEP = 50                        # newest rows kept (SCHEMA_AND_STATE §3.2)
ERRORS_MAX_AGE = timedelta(days=30)     # and none older than this


def _utc_z(value):
    """An error timestamp as UTC 'Z' text, so the errors table sorts by time."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_errors(conn, rows):
    """Append error rows, then trim the table to the newest ERRORS_KEEP rows
    no older than ERRORS_MAX_AGE. The caller commits.

    Rows are only ever appended, never replaced. A writer that never loaded
    the table (notify.py, a run that crashed before it saved) therefore cannot
    erase another writer's rows: the wholesale replace in save() used to do
    exactly that (2026-10-10). Each row is {timestamp|at, source, severity,
    message}, the shape state["errors"] uses.
    """
    for r in rows:
        conn.execute(
            "INSERT INTO errors (at, source, severity, message) "
            "VALUES (?, ?, ?, ?)",
            (_utc_z(r.get("timestamp") or r.get("at")), r.get("source"),
             r.get("severity") or "MINOR", r.get("message") or ""))
    cutoff = (datetime.now(timezone.utc) - ERRORS_MAX_AGE).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM errors WHERE at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM errors WHERE seq NOT IN (SELECT seq FROM errors "
        "ORDER BY at DESC, seq DESC LIMIT ?)", (ERRORS_KEEP,))


def kv_get(db_path, key, default=None):
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute("SELECT value_json FROM kv WHERE key = ?",
                           (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else default


def kv_set(db_path, key, value):
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), _now()))
        conn.commit()
    finally:
        conn.close()


def content_hash(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def cache_get(db_path, task, text, version):
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute(
            "SELECT output_json FROM llm_cache WHERE task = ? AND "
            "input_hash = ? AND version = ?",
            (task, content_hash(text), version)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def cache_put(db_path, task, text, version, output):
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (task, input_hash, version, "
            "output_json, at) VALUES (?, ?, ?, ?, ?)",
            (task, content_hash(text), version, json.dumps(output), _now()))
        conn.commit()
    finally:
        conn.close()


def record_llm_calls(db_path, run_start, calls):
    if not calls:
        return
    conn = dbmod.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO llm_calls (run_start, label, model, ok, cost_usd, "
            "input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, seconds) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_start, c["label"], c.get("model"), int(bool(c.get("ok"))),
              c.get("cost_usd"), c.get("input_tokens"), c.get("output_tokens"),
              c.get("cache_read_tokens"), c.get("cache_write_tokens"),
              c.get("seconds")) for c in calls])
        conn.commit()
    finally:
        conn.close()


def applications_all(db_path):
    """{item_id: record} for every tracked application (applications.py's
    record shape). Read by the run; written only by app.py."""
    conn = dbmod.connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM applications").fetchall()
    finally:
        conn.close()
    return {r["item_id"]: {"stage": r["stage"], "stage_on": r["stage_on"],
                           "follow_up_on": r["follow_up_on"],
                           "draft_url": r["draft_url"],
                           "history": json.loads(r["history_json"] or "[]"),
                           "manual": json.loads(r["manual_json"])
                           if r["manual_json"] else None}
            for r in rows}


def application_put(conn, item_id, record):
    """Upsert one record on an open connection (the caller commits)."""
    conn.execute(
        "INSERT INTO applications (item_id, stage, stage_on, follow_up_on, "
        "draft_url, history_json, updated_at, manual_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET stage = excluded.stage, "
        "stage_on = excluded.stage_on, follow_up_on = excluded.follow_up_on, "
        "draft_url = excluded.draft_url, history_json = excluded.history_json, "
        "updated_at = excluded.updated_at, "
        "manual_json = COALESCE(excluded.manual_json, applications.manual_json)",
        (item_id, record["stage"], record["stage_on"],
         record.get("follow_up_on"), record.get("draft_url"),
         json.dumps(record.get("history") or []), _now(),
         json.dumps(record["manual"]) if record.get("manual") else None))



def watch_hits(db_path):
    """Confirmed things-to-watch hits, first sighting first. Read by the page
    (app.py GET /watch-hits); written by the run (watch_hits_save)."""
    conn = dbmod.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM watch_hits ORDER BY found_on, item_id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def watch_hits_save(db_path, hits, checked, live):
    """Bring watch_hits up to date after a check. `hits`: rows that meet their
    watch now. `checked`: watch ids this check ran for; their rows that were
    not hit now are removed. `live`: active watch ids; rows of any other watch
    are removed. found_on is kept from the first sighting."""
    conn = dbmod.connect(db_path)
    try:
        for h in hits:
            conn.execute(
                "INSERT INTO watch_hits (item_id, watch_id, title, finding, url, "
                "seen_on, found_on, checked_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET title = excluded.title, "
                "finding = excluded.finding, url = excluded.url, "
                "seen_on = excluded.seen_on, checked_on = excluded.checked_on",
                (h["item_id"], h["watch_id"], h["title"], h["finding"], h["url"],
                 h["seen_on"], h["found_on"], h["checked_on"]))
        hit_ids = [h["item_id"] for h in hits]
        if live:
            conn.execute("DELETE FROM watch_hits WHERE watch_id NOT IN (%s)"
                         % ",".join("?" * len(live)), tuple(live))
        else:
            conn.execute("DELETE FROM watch_hits")
        if checked:
            conn.execute(
                "DELETE FROM watch_hits WHERE watch_id IN (%s) AND item_id NOT IN (%s)"
                % (",".join("?" * len(checked)),
                   ",".join("?" * len(hit_ids)) if hit_ids else "''"),
                tuple(checked) + tuple(hit_ids))
        conn.commit()
    finally:
        conn.close()
