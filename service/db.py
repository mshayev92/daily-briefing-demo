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


# Columns added after a database was first created. CREATE TABLE IF NOT
# EXISTS never adds a column to a table that already exists, so each one is
# added here, once, when init_db() finds it missing.
ADDED_COLUMNS = (
    ("items", "status_changed_at", "TEXT"),   # 2026-10-10
    ("actions", "app_before", "TEXT"),        # 2026-10-11: application record before
    ("actions", "label", "TEXT"),             # 2026-10-11: what the undo bar says
    ("course_grade_snapshots", "graded_items", "TEXT"),  # 2026-10-17: names for grade pushes
)


def _add_missing_columns(conn):
    for table, column, decl in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                         % (table, column, decl))


def init_db(db_path=DEFAULT_DB_PATH, schema_path=SCHEMA_PATH):
    """Create the schema if it doesn't exist yet. Idempotent (IF NOT EXISTS),
    and brings an older database up to the current columns."""
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = fh.read()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        _add_missing_columns(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    init_db(path)
    print("initialized %s" % path)
