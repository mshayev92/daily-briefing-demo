"""SQLite connection helper for the migrated pipeline (EXECUTION_PLAN §Phase 0/1).

One file, one connection factory. WAL mode so the FastAPI page (readers) and
the orchestrator (a writer, once a day) don't block each other.
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
DEFAULT_DB_PATH = os.path.join(HERE, "briefing.db")


def connect(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=DEFAULT_DB_PATH, schema_path=SCHEMA_PATH):
    """Create the schema if it doesn't exist yet. Idempotent (IF NOT EXISTS)."""
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = fh.read()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    init_db(path)
    print("initialized %s" % path)
