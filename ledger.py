"""
ledger.py — §3.7. The system's long memory.

`items[]` is a working set pruned at 35 days, which is right for a file read
and written every run and wrong as the only memory a discovery system has.
Recurrence detection needs multiple years and revealed preference needs every
disposition ever recorded.

So: one JSONL file, append-only, METADATA ONLY. No titles, no descriptions, no
snippets. Keeping it narrow is what lets it stay unpruned for years while
costing almost nothing, and it is also why it holds nothing worth reading on
its own — it is an index, not an archive. The Drive briefing copies are the
archive.

Append-only is not a style preference. A rewrite means reading the whole file
into context to change one line, which is the cost this file exists to avoid,
and it means a partial write can destroy history that cannot be reconstructed.
"""

import json
import os
import re
from datetime import date, datetime

FILENAME = "college_assistant_ledger.jsonl"

DISPOSITIONS = ("surfaced", "handled", "dismissed", "snoozed",
                "killed", "confirmed", "acted", "expired")

# Everything a ledger line may carry. Anything not here is dropped on write
# rather than stored, so the file cannot quietly grow into a second state file.
FIELDS = ("id", "family", "kind", "date", "source", "requirement_ids",
          "disposition", "on", "regime")


def line(item, disposition, on, family=None):
    """One ledger row from an item. Metadata only, by construction."""
    if disposition not in DISPOSITIONS:
        raise ValueError("unknown disposition %r" % disposition)
    row = {
        "id": item.get("id"),
        "family": family or _family(item.get("id")),
        "kind": item.get("kind"),
        "date": item.get("date"),
        "source": _source_of(item),
        "requirement_ids": list(item.get("requirement_ids") or ()),
        "disposition": disposition,
        "on": _iso(on),
        "regime": item.get("regime") or "confirmed",
    }
    return {k: v for k, v in row.items() if k in FIELDS and v not in (None, [], "")}


def _family(item_id):
    """The recurring identity: the id with its trailing date removed.

    §3.3 ids end in a date (`campus-first-look-fair-2026-09-15`), and
    recurrence detection needs to know that this year's instance and last
    year's are the same thing. Stripping the date is what gives it that, and it
    is done here rather than stored twice.
    """
    if not item_id:
        return None
    # TWO date shapes, because live state has both. Items minted before
    # 2026-09 end in a compact `-20260910`; §3.3's current form is
    # `-2026-09-10`. Stripping only the hyphenated form left every legacy
    # item's family equal to its whole id, so recurrence could never match one
    # year's instance to the next — silently, since a family of "the whole id"
    # looks perfectly well-formed.
    s = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", str(item_id))
    return re.sub(r"-\d{8}$", "", s)


def _source_of(item):
    refs = item.get("source_refs") or ()
    for r in refs:
        s = (r or {}).get("source")
        if s:
            return s
    return item.get("source")


def _iso(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def serialize(rows):
    """The text to APPEND. Compact, one object per line, trailing newline —
    so a later append cannot corrupt the last record written."""
    return "".join(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n"
                   for r in rows)


def parse(text):
    """Read a ledger. A malformed line is SKIPPED, not fatal.

    A years-old append-only file will eventually contain one bad line from an
    interrupted write, and losing the whole history to it would be the worst
    possible trade. `bad` is returned so a run can report the count rather
    than discover it silently.
    """
    rows, bad = [], 0
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                bad += 1
        except ValueError:
            bad += 1
    return rows, bad


def default_path(dir_path):
    """Where the ledger lives: alongside whatever `briefing.db` is in use
    (real account data in `service/`, or a scratch copy under a test's own
    temp dir), never a fixed path -- a run against a copy of the db must
    never write into the real account's history."""
    return os.path.join(dir_path, FILENAME)


def append(path, rows):
    """Append `rows` (each already shaped by `line()`) to the ledger at
    `path`, one `write()` call per row rather than one call for the whole
    batch. A local-filesystem append with O_APPEND is atomic up to
    PIPE_BUF (4KiB on Linux) per write(); a single JSONL row is always well
    under that, and the orchestrator's daily batch (one write per active
    item) and the live service's one-line writes can therefore interleave
    freely without ever corrupting a line -- order across processes is not
    guaranteed, and nothing here needs it to be."""
    if not rows:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(serialize([row]))


def read(path):
    """Parse the ledger at `path`. Returns ([], 0) if it does not exist yet
    -- a run's first day has no history, and that is not an error."""
    if not os.path.exists(path):
        return [], 0
    with open(path, "r", encoding="utf-8") as fh:
        return parse(fh.read())


def revealed_preference(rows, min_events=3):
    """What he actually does, per requirement — not what the quiz says.

    Recommenders learn from behaviour, and the quiz is a stated preference that
    behaviour routinely contradicts. Returned as counts and a rate, never as a
    recommendation: the run reports "you have dismissed 6 of 7 arts events",
    and a human decides whether that is a signal or a bad fortnight.

    `min_events` exists because a 1-of-1 dismissal rate is noise wearing a
    percentage sign.
    """
    acted_on = {"handled", "acted", "confirmed"}
    ignored = {"dismissed", "killed", "expired"}
    tally = {}
    for r in rows or ():
        for rid in (r.get("requirement_ids") or ["(none)"]):
            t = tally.setdefault(rid, {"acted": 0, "ignored": 0, "surfaced": 0})
            d = r.get("disposition")
            if d in acted_on:
                t["acted"] += 1
            elif d in ignored:
                t["ignored"] += 1
            elif d == "surfaced":
                t["surfaced"] += 1
    out = {}
    for rid, t in tally.items():
        decided = t["acted"] + t["ignored"]
        if decided < min_events:
            continue
        out[rid] = dict(t, decided=decided,
                        act_rate=round(t["acted"] / float(decided), 2))
    return out


def families_seen(rows):
    """{family: [dates]} — the input `opportunity.detect_recurrence()` wants."""
    out = {}
    for r in rows or ():
        fam, d = r.get("family"), r.get("date")
        if fam and d:
            out.setdefault(fam, []).append(d)
    for fam in out:
        out[fam] = sorted(set(out[fam]))
    return out
