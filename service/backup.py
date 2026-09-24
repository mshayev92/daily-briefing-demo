"""Nightly SQLite backup -> Drive (EXECUTION_PLAN_reviewed.md Phase 5).

    "Backup | Nightly `sqlite3 .backup` of the state DB, pushed to Drive via
    the existing `google_api.py` OAuth. This restores the off-machine
    redundancy that the old Drive-hosted JSON gave for free and that a
    single local SQLite file does not."

Pushed into the SAME Drive folder the old `college_assistant_state_*.json`
files lived in (`state.state_folder_id`, the `Claude Assistant` folder) --
that folder already exists, is already the off-machine copy of "the state",
and does not need a second one created for it.

RETENTION IS STILL OPEN (EXECUTION_PLAN_reviewed.md §6 item 5: "how many
nightly backups to keep on Drive before pruning"). DEFAULT_RETENTION below
mirrors this project's existing convention for the OLD state files
(PROJECT_INSTRUCTIONS.md §1.2's "keep the 14 most recent") rather than
inventing an unrelated number -- override with --keep once Michael has an
actual answer.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import db as dbmod
import state_io_sqlite
import google_api

STAMP_FMT = "%Y-%m-%dT%H%M%SZ"
NAME_PREFIX = "briefing_backup_"
NAME_SUFFIX = ".sqlite"
DEFAULT_RETENTION = 14  # provisional -- see module docstring


def make_backup(db_path=dbmod.DEFAULT_DB_PATH, dest_dir=None, now=None):
    """A consistent point-in-time copy via sqlite3's own backup API -- safe
    even with a WAL-mode writer open elsewhere, unlike a plain file copy
    (which can capture a half-written page)."""
    dest_dir = dest_dir or HERE
    stamp = (now or datetime.now(timezone.utc)).strftime(STAMP_FMT)
    name = "%s%s%s" % (NAME_PREFIX, stamp, NAME_SUFFIX)
    dest_path = os.path.join(dest_dir, name)

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return name, dest_path


def _parse_stamp(name):
    if not (name.startswith(NAME_PREFIX) and name.endswith(NAME_SUFFIX)):
        return None
    stamp = name[len(NAME_PREFIX):-len(NAME_SUFFIX)]
    try:
        return datetime.strptime(stamp, STAMP_FMT)
    except ValueError:
        return None


def prune_drive(folder_id, keep=DEFAULT_RETENTION, root=ROOT):
    """Keep the `keep` most recent backups; trash the rest through the same
    name-enforced guard §1.2 uses for state files (google_api.TRASHABLE)."""
    entries = google_api.list_folder(folder_id, NAME_PREFIX, root=root)
    dated = [(ts, e) for e in entries
            for ts in (_parse_stamp(e["name"]),) if ts is not None]
    dated.sort(key=lambda p: p[0], reverse=True)
    trashed = []
    for _ts, e in dated[keep:]:
        google_api.trash(e["id"], name=e["name"], root=root)
        trashed.append(e["name"])
    return trashed


def run(db_path=dbmod.DEFAULT_DB_PATH, keep=DEFAULT_RETENTION,
       cleanup_local=True):
    state, _source, _problems = state_io_sqlite.load(db_path)
    folder_id = (state or {}).get("state_folder_id")
    if not folder_id:
        raise RuntimeError(
            "no state_folder_id in %s -- run migrate_state.py first, or "
            "set state_folder_id by hand, before the backup has anywhere "
            "to go" % db_path)

    name, path = make_backup(db_path)
    try:
        file_id = google_api.create_file_from_path(
            folder_id, name, path, mimetype="application/x-sqlite3",
            root=ROOT)
    finally:
        if cleanup_local:
            os.remove(path)

    trashed = prune_drive(folder_id, keep=keep)
    return {"uploaded": name, "file_id": file_id, "trashed": trashed}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=dbmod.DEFAULT_DB_PATH)
    parser.add_argument("--keep", type=int, default=DEFAULT_RETENTION)
    parser.add_argument("--keep-local", action="store_true",
                        help="Don't delete the local backup file after "
                             "upload (default: delete it).")
    args = parser.parse_args()
    print(json.dumps(run(args.db, args.keep, not args.keep_local), indent=2))
