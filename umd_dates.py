"""
umd_dates.py — populate `state.umd_dates` from the office that owns the dates.

§7c refuses to supply UMD academic dates from memory, and it is right to: an
authoritative-looking wrong withdrawal deadline is the worst error this system
can make. The consequence was that `umd_dates` sat empty, no add/drop or
withdrawal deadline ever appeared, and a `System` row nagged about it every
fourteen days forever (§5.7 exception 2).

§1.8 resolves it. The Office of the University Registrar **owns** these dates,
so reading them from the registrar's own page is not "from the web" in the sense
§7c prohibits — it is the confirmed-regime path, and the prohibition on memory
and on aggregators is untouched.

THE SOURCE, verified 2026-09-09 by fetching it:

    https://registrar.umd.edu/calendars/standard-registration-dates-deadlines

Two things learned in the process, both worth keeping:

1. **The URL a search engine returned (`…/fall-and-spring-semester-dates-and-
   deadlines`) is stale and answers with bot detection.** The working URL came
   from the registrar's own `/calendars` index. Reach the owning page through
   the owner's navigation, not through a search result — which is §1.8's rule
   arriving in practice on the first real attempt.
2. **These dates change mid-semester.** Spring 2026's schedule-adjustment,
   refund and waitlist deadlines were all extended after a week of weather
   closures. So this is NOT a populate-once job; `needs_refresh()` exists
   because a cached withdrawal deadline can be wrong while looking fine.

THIS MODULE DOES NOT FETCH. The run performs the WebFetch and passes the text
in, exactly as `collect.ingest()` works and for the same reason: `WebFetch` is
a tool, not a Python function.
"""

import re
from datetime import date, datetime, timedelta

# PARKED 2026-09-10 (§7c). This module is not imported by any run. It is kept
# on disk so a re-enable needs no new code and so §0's file table matches `ls`.
# `REFRESH_AFTER_DAYS` is intentionally no longer named in the governance docs:
# the §2 constant row that carried it was removed with the step, and a tuned
# cap documented as live for a parked feature is worse than an undocumented
# one. See RATIONALE_LOG.md 2026-09-10 for the removed spec.
PARKED = True

SOURCE_URL = "https://registrar.umd.edu/calendars/standard-registration-dates-deadlines"
INDEX_URL = "https://registrar.umd.edu/calendars"

# Re-read the page when the stored copy is older than this even if the term has
# not turned over — mid-semester amendments are the reason (see above).
REFRESH_AFTER_DAYS = 30

_TERM_RE = re.compile(r"^#{3,6}\s*((?:Fall|Spring|Summer|Winter)\s+20\d{2})\s*$", re.M)
# One table row: | Event ... | Date |
_ROW_RE = re.compile(r"^\|\s*(?!Event\b)(?!-)(.+?)\s*\|\s*([^|]+?)\s*\|\s*$", re.M)
_ONE_DATE = r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(20\d{2})"
_DATE_RE = re.compile(_ONE_DATE)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# Rows that are not a student-facing deadline. Dropped rather than stored: a
# `umd_deadline` row costs a line in Coming up, and "Degree clearances due" is
# not something Michael can act on.
_SKIP = (
    "graduate student registration deadlines",
    "instructors can begin submitting final grades",
    "degree clearances due",
    "official transcripts available",
    "degree conferral date",
    "commencement",
    "mid-term grades become available",
)

# Refund-tier rows: there are five of them, they say the same thing at
# decreasing percentages, and only the ones with a real consequence are worth a
# row. 100% and the W date matter; 60/40/20/0% are noise in a daily briefing.
_SKIP_REFUND = re.compile(r"with (?:60|40|20|0)% refund", re.I)


def _parse_one(m):
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def parse_dates_page(text):
    """Return {"term": "Fall 2026", "entries": [...]} from the fetched page.

    Each entry: {title, date, end_date, detail}. `end_date` is set only for a
    row stating a range (`Aug 31, 2026 (Mon) - Sep 14, 2026 (Mon)`), which the
    briefing renders as `Sep 12 – Sep 14` per §3.3.
    """
    text = str(text or "")
    term = None
    tm = _TERM_RE.search(text)
    if tm:
        term = tm.group(1)

    entries, seen = [], set()
    for m in _ROW_RE.finditer(text):
        cell, datecell = m.group(1), m.group(2)
        dates = list(_DATE_RE.finditer(datecell))
        if not dates:
            continue
        # The event cell is a bold title followed by a long explanation. Keep
        # the title; keep one trimmed line of the explanation as detail.
        bold = re.match(r"\s*\*\*(.+?)\*\*", cell)
        title = (bold.group(1) if bold else cell.split("|")[0]).strip()
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)     # unwrap links
        title = re.sub(r"[*_]+", "", title).strip(" -–—")
        if not title:
            continue
        low = title.lower()
        if any(s in low for s in _SKIP) or _SKIP_REFUND.search(low):
            continue
        rest = cell[bold.end():] if bold else ""
        rest = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", rest)
        rest = re.sub(r"\s+", " ", re.sub(r"[*_]+", "", rest)).strip(" -–—")
        key = (title, _parse_one(dates[0]).isoformat())
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "title": title,
            "date": _parse_one(dates[0]).isoformat(),
            "end_date": (_parse_one(dates[1]).isoformat()
                         if len(dates) > 1 else None),
            "detail": rest[:160] or None,
        })
    return {"term": term, "entries": entries, "source_url": SOURCE_URL}


def to_umd_dates(parsed, fetched_on):
    """The `state.umd_dates` shape (§3.2), plus the provenance block.

    Returns (umd_dates, source_record). Every entry is `confirmed`: it came
    from the owning page with a stated date, which is exactly §1.8's bar.
    """
    return (
        [{k: v for k, v in e.items() if v is not None} for e in parsed["entries"]],
        {"url": parsed.get("source_url") or SOURCE_URL,
         "term": parsed.get("term"),
         "fetched_on": _iso(fetched_on),
         "count": len(parsed["entries"])},
    )


def needs_refresh(state, today):
    """Whether this run should re-read the page. Cheap, and rarely true.

    Refresh when there is nothing stored, when the stored copy predates
    REFRESH_AFTER_DAYS, or when everything stored is already in the past —
    that last one is what rolls the term over without needing to know the
    academic calendar's own shape.
    """
    today = _as_date(today)
    src = (state or {}).get("umd_dates_source") or {}
    stored = (state or {}).get("umd_dates") or []
    if not stored or not src.get("fetched_on"):
        return True, "nothing stored yet"
    if (today - _as_date(src["fetched_on"])).days >= REFRESH_AFTER_DAYS:
        return True, ("stored copy is %d days old and these dates get amended "
                      "mid-semester" % (today - _as_date(src["fetched_on"])).days)
    if all(_as_date(e["date"]) < today for e in stored if e.get("date")):
        return True, "every stored date is in the past, so the term has turned over"
    return False, "current"


def upcoming(umd_dates, today, days=30):
    """The §7c filter: entries inside the Coming-up horizon, soonest first."""
    today = _as_date(today)
    end = today + timedelta(days=days)
    out = [e for e in (umd_dates or [])
           if e.get("date") and today <= _as_date(e["date"]) <= end]
    return sorted(out, key=lambda e: e["date"])


def to_item(entry, today):
    """One Coming-up row (§7c): `umd_deadline`, class `umd`, label `UMD`, and
    NO Add-reminder button — it is informational, and §6.3 omits the control
    for this kind."""
    return {
        "id": "umd-%s-%s" % (re.sub(r"[^a-z0-9]+", "-",
                                    entry["title"].lower()).strip("-")[:40],
                             entry["date"]),
        "course": "UMD", "course_class": "umd", "course_label": "UMD",
        "kind": "umd_deadline", "regime": "confirmed",
        "title": entry["title"],
        "detail": entry.get("detail") or entry["title"],
        "date": entry["date"],
        "end_date": entry.get("end_date"),
        "confidence": "confirmed",
        "evidence": "include",
        "status": "new",
        "times_surfaced": 0,
        "last_shown": _as_date(today).isoformat(),
        "ai_actions": [],
        "links": [],
        "source_refs": [{"source": "registrar", "url": SOURCE_URL}],
    }


def _iso(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
