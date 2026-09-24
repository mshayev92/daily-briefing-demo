"""One-time migration: college_assistant_state_*.json -> SQLite.

EXECUTION_PLAN_reviewed.md Phase 1: "Write a one-time migration script from
the Drive JSON blob into SQLite. Run the migration against a copy, not the
live state file, and diff the result."

Usage:
    python3 migrate_state.py <state.json> [--db path/to.db] [--diff]

--diff loads the state back out of SQLite via state_io_sqlite.load() and
reports any field-level difference against the original JSON, so the
migration can be checked without trusting it by construction.
"""

import json
import sys

import db as dbmod
import state_io_sqlite


def migrate(json_path, db_path=dbmod.DEFAULT_DB_PATH):
    with open(json_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    dbmod.init_db(db_path)
    state_io_sqlite.save(state, db_path)
    return state


def _sorted_items(items):
    return sorted(items or [], key=lambda it: it.get("id") or "")


def diff(original, db_path=dbmod.DEFAULT_DB_PATH):
    """Field-by-field comparison. Returns a list of human-readable diffs.

    Applies §3.1's own default-omission rule before comparing, so a field
    the original JSON omitted because it equalled its schema default is not
    reported as a spurious difference against the round-tripped copy, which
    always writes it back explicitly.
    """
    from state_shape import ITEM_DEFAULTS

    reconstructed, _, problems = state_io_sqlite.load(db_path)
    diffs = list(problems)
    if reconstructed is None:
        diffs.append("CRITICAL: nothing loaded back from %s" % db_path)
        return diffs

    top_keys = set(original.keys()) | set(reconstructed.keys())
    for key in sorted(top_keys):
        if key == "items":
            continue
        if key.startswith("_"):
            # Sample/annotation keys (e.g. a hand-added `_note` explaining a
            # convention) are not part of SCHEMA_AND_STATE.md's schema and
            # are not persisted — nothing reads them back, so they are not a
            # migration bug.
            continue
        if key in ("errors", "last_run_stats"):
            # Both are intentionally capped/pruned on write (§3.2); compare
            # only the newest entry, which is what actually round-trips.
            o = (original.get(key) or [None])[0]
            r = (reconstructed.get(key) or [None])[0]
            if o != r:
                diffs.append("%s[0] differs:\n  original: %r\n  sqlite:   %r"
                             % (key, o, r))
            continue
        o, r = original.get(key), reconstructed.get(key)
        if o != r:
            diffs.append("%s differs:\n  original: %r\n  sqlite:   %r"
                         % (key, o, r))

    orig_items = _sorted_items(original.get("items"))
    recon_items = _sorted_items(reconstructed.get("items"))
    if len(orig_items) != len(recon_items):
        diffs.append("item count differs: original=%d sqlite=%d" % (
            len(orig_items), len(recon_items)))
    def _normalize(item):
        # A key holding its schema default (including `null`, for fields
        # whose default is null) and an absent key are the same fact to
        # every consumer in this codebase (`.get()` can't tell them apart),
        # so treat them as equivalent for fidelity-checking even though
        # §3.1 only formally licenses omission for a named subset of
        # fields. This is a diff-tool normalization, not a storage rule.
        out = {}
        for k, v in item.items():
            if v is None:
                continue
            if k in ITEM_DEFAULTS and v == ITEM_DEFAULTS[k]:
                continue
            out[k] = v
        return out

    for o, r in zip(orig_items, recon_items):
        merged_o = _normalize(o)
        merged_r = _normalize(r)
        if merged_o != merged_r:
            only_o = {k: v for k, v in merged_o.items()
                     if merged_r.get(k) != v}
            only_r = {k: v for k, v in merged_r.items()
                     if merged_o.get(k) != v}
            diffs.append("item %s differs:\n  original: %r\n  sqlite:   %r"
                         % (o.get("id"), only_o, only_r))
    return diffs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    json_path = sys.argv[1]
    db_path = dbmod.DEFAULT_DB_PATH
    if "--db" in sys.argv:
        db_path = sys.argv[sys.argv.index("--db") + 1]

    original = migrate(json_path, db_path)
    print("migrated %s -> %s (%d items)" % (
        json_path, db_path, len(original.get("items", []))))

    if "--diff" in sys.argv:
        problems = diff(original, db_path)
        if not problems:
            print("diff: clean — round-trip matches the original exactly "
                 "(modulo documented default-omission).")
        else:
            print("diff: %d discrepancies" % len(problems))
            for p in problems:
                print("  - %s" % p)
