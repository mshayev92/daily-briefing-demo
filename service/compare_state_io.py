"""Phase 2's "read from both, compare" harness.

Loads the newest state via the LEGACY path (state_io.load_local() against the
Drive-mirrored JSON files in the project root) and via the NEW path
(state_io_sqlite.load() against the SQLite database), then reports any
difference. Run this daily during the parallel-run period (Phase 7) before
anything depends on state_io_sqlite exclusively.

Usage:
    python3 compare_state_io.py [--db path/to.db] [--root ..]
"""

import sys

import db as dbmod
import state_io_sqlite
from migrate_state import diff as _item_and_top_diff

sys.path.insert(0, "..")
import state_io as legacy_state_io  # noqa: E402


def compare(root="..", db_path=dbmod.DEFAULT_DB_PATH):
    legacy_state, legacy_source, legacy_problems = (
        legacy_state_io.load_local(root))
    if legacy_state is None:
        return {
            "ok": False,
            "reason": "legacy load_local() returned no state from %r (%s)"
                     % (root, "; ".join(legacy_problems)),
        }

    discrepancies = _item_and_top_diff(legacy_state, db_path)
    return {
        "ok": not discrepancies,
        "legacy_source": legacy_source,
        "legacy_problems": legacy_problems,
        "discrepancies": discrepancies,
    }


if __name__ == "__main__":
    root = ".."
    db_path = dbmod.DEFAULT_DB_PATH
    if "--root" in sys.argv:
        root = sys.argv[sys.argv.index("--root") + 1]
    if "--db" in sys.argv:
        db_path = sys.argv[sys.argv.index("--db") + 1]

    result = compare(root, db_path)
    if result["ok"]:
        print("dual-read: clean — SQLite matches the legacy JSON state "
             "loaded from %r" % result.get("legacy_source"))
    else:
        print("dual-read: MISMATCH")
        for line in result.get("discrepancies") or [result.get("reason")]:
            print("  - %s" % line)
        sys.exit(1)
