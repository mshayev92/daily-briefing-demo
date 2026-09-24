"""Small helpers over the pipeline-owned tables added for brief v2:
`kv` (named JSON values), `llm_cache` (LLM outputs keyed by a hash of their
input) and `llm_calls` (per-call cost accounting). Each helper opens and
closes its own connection, like the rest of this service."""

import hashlib
import json
from datetime import datetime, timezone

import db as dbmod


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
