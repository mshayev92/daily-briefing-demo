"""
collect.py — the collection layer. §1.8 and §13.5 as code.

Everything else in the pipeline reads channels Michael is already subscribed
to. This module is the only part that goes looking, and it exists because the
opportunities with no inbound channel are the ones worth having.

THE ONE RULE THIS MODULE IS BUILT AROUND (§1.8):

    Search MAY ORIGINATE a candidate. Search MAY NEVER SUBSTITUTE for a source
    that failed. A fact crosses into the confirmed regime only when it was read
    from the page that OWNS it, with a stated date.

That is enforced here, not requested: `classify()` returns `regime="lead"` for
anything whose evidence came from a search result, an aggregator or a summary,
and can only return `regime="confirmed"` when `is_owning_url()` is true for the
page the date was read from. A deadline learned from a listicle stays a lead
forever, however plausible it looks.

NO HARDCODED CORPUS (§13.5). There is no list of fellowships in this file and
there must never be one. An earlier design named specific programmes and
deadlines from recollection; several were wrong on eligibility rather than
merely on date. `plan_queries()` builds its queries from the user's own
requirements, and `landscape_queries()` asks what CLASSES of thing exist before
any instance is hunted.

THIS MODULE NEVER FETCHES ANYTHING, AND CANNOT.

An earlier version took `search_fn` and `fetch_fn` callables and promised the
pipeline would "pass the real WebFetch-backed ones". That was impossible:
`WebFetch` and `web_search` are TOOLS available to the model, not Python
functions, and prompt step 5 forbids `curl`, `wget` and scripted HTTP outright.
So nothing could ever have supplied those callables and `harvest()` was
uncallable — while all of its tests passed, because they injected stubs. The
same self-consistent-but-wrong shape that let `block()` return nothing for
seven row templates.

The split that actually works, and the one the run performs:

    1. `plan()`      — this module says WHAT to look for.
    2. the RUN       — executes its own `web_search` / `WebFetch` tool calls
                       and writes the results to a JSON file.
    3. `ingest()`    — this module classifies what came back.

Step 2 is the only part that touches the network, it is done by the model with
its own tools, and no Python in this repository is involved. That is why this
module is fully testable offline without pretending to be.
"""

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

# §2-style caps, named once. Collection is the most expensive thing a run can
# do and the least bounded, so the ceilings are explicit rather than implied.
MAX_SEARCHES_PER_RUN = 6
MAX_FETCHES_PER_RUN = 8
MAX_LEADS_PER_RUN = 5

# Do not re-fetch a page we read this recently. A deadline page does not change
# daily, and every fetch is both a token cost and a request someone else's
# server has to serve.
CACHE_DAYS = 7

# A date on a page is worthless without knowing what it is a date FOR, so a
# candidate needs BOTH a date and a date-bearing phrase near it to be
# considered confirmable at all.
_DATE_PATTERNS = (
    r"\b(20\d{2})-(\d{2})-(\d{2})\b",
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
)
_DEADLINE_WORDS = ("deadline", "due", "closes", "close", "apply by",
                   "applications close", "submit by", "priority date")

# Aggregators, listicles and anything that republishes someone else's facts.
# A match here can never be an owning domain, whatever else is true.
NEVER_OWNING = ("medium.com", "reddit.com", "quora.com", "pinterest.com",
                "facebook.com", "x.com", "twitter.com", "linkedin.com",
                "wikipedia.org", "blogspot.com", "wordpress.com",
                "substack.com", "youtube.com")


# ---------------------------------------------------------------------------
# who owns a fact
# ---------------------------------------------------------------------------

def is_owning_url(url, owning_domains):
    """True when `url` is on a domain that OWNS the fact (§1.8).

    `owning_domains` comes from the source descriptor the run is working
    against — a department, an office, a funder, a registrar — never from a
    guess about which result looked most official.

    Host matching is exact-or-subdomain. A bare suffix test accepts
    `notumd.edu` for `umd.edu`, which is the §6.4 bug in a new place.
    """
    if not url:
        return False
    u = urlparse(str(url).strip())
    if u.scheme != "https" or not u.netloc:
        return False
    host = u.netloc.split("@")[-1].split(":")[0].lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in NEVER_OWNING):
        return False
    for d in owning_domains or ():
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def stated_date(text):
    """Return an ISO date only when the text states one NEAR a deadline word.

    A bare date on a page is not a deadline — it could be a publication date, a
    term start, a copyright year. Requiring a deadline word within a short
    window is what stops the collection layer manufacturing a due date out of
    incidental text, which would be §1.6 at its most damaging: an
    authoritative-looking wrong deadline.
    """
    t = str(text or "")
    low = t.lower()
    for pat in _DATE_PATTERNS:
        for m in re.finditer(pat, t, re.I):
            window = low[max(0, m.start() - 120):m.end() + 120]
            if not any(w in window for w in _DEADLINE_WORDS):
                continue
            g = m.groups()
            try:
                if len(g[0]) == 4 and g[0].isdigit():
                    return "%s-%s-%s" % g
                month = _MONTHS[g[0].lower()]
                return "%s-%02d-%02d" % (g[2], month, int(g[1]))
            except (KeyError, ValueError, IndexError):
                continue
    return None


_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


# ---------------------------------------------------------------------------
# tasking: queries come from requirements, never from a remembered list
# ---------------------------------------------------------------------------

def landscape_queries(req, institution=None):
    """What CLASSES of thing exist for this requirement (§13.5).

    Run FIRST for a requirement with no recorded signal. The point is to
    discover the offices, funders, portals and programmes that exist — which
    then become monitored sources — rather than to guess at named instances.
    """
    who = (" " + institution) if institution else ""
    stem = (req.get("statement") or "").strip()
    return [q for q in (
        ("what kinds of programs exist for: %s%s" % (stem, who)) if stem else None,
        ("%s office%s" % (_head(stem), who)) if stem else None,
        ("undergraduate opportunities%s %s" % (who, " ".join(req.get("keywords") or [])[:60])).strip(),
    ) if q and q.strip()]


def plan_queries(req, institution=None, today=None):
    """Instance-hunting queries for one requirement.

    Built from the requirement's own words plus the current academic period, so
    they stay current without anything being hardcoded. Deliberately few: the
    cap is the budget, and a broad query answered well beats six narrow ones.
    """
    kw = [k for k in (req.get("keywords") or []) if k.strip()]
    who = (" " + institution) if institution else ""
    period = _period(today or date.today())
    out = []
    if kw:
        out.append("%s%s %s deadline" % (" ".join(kw[:3]), who, period))
        out.append("%s%s application" % (" ".join(kw[:2]), who))
    stem = (req.get("statement") or "").strip()
    if stem:
        out.append("%s%s %s" % (_head(stem), who, period))
    return [q for q in out if q.strip()]


def _head(statement, words=6):
    return " ".join(str(statement or "").split()[:words])


def _period(d):
    """`fall 2026` / `spring 2027`. Cheap, and it keeps queries current without
    anything in this file needing to know a calendar."""
    d = _as_date(d)
    if d.month >= 8:
        return "fall %d" % d.year
    if d.month >= 5:
        return "summer %d" % d.year
    return "spring %d" % d.year


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# classification: the regime boundary
# ---------------------------------------------------------------------------

def classify(candidate, owning_domains):
    """Return (regime, reason). The §1.8 boundary, as one function.

    `candidate` carries `url`, `text` (what was actually read) and
    `read_from_owner` (True only when `text` came from fetching `url` itself,
    not from a search snippet describing it).
    """
    url = candidate.get("url")
    if not is_owning_url(url, owning_domains):
        return "lead", "not on a domain that owns this fact"
    if not candidate.get("read_from_owner"):
        return "lead", "described by a search result, not read from the page"
    if not stated_date(candidate.get("text")):
        return "lead", "the owning page states no date next to a deadline word"
    return "confirmed", "read from the owning page, which states a date"


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

class Budget(object):
    """Hard ceilings, decremented as they are spent. A collection layer without
    a budget is the one that quietly becomes the most expensive step."""

    def __init__(self, searches=MAX_SEARCHES_PER_RUN, fetches=MAX_FETCHES_PER_RUN):
        self.searches = int(searches)
        self.fetches = int(fetches)
        self.spent_searches = 0
        self.spent_fetches = 0
        self.hit_cap = []

    def take_search(self):
        if self.spent_searches >= self.searches:
            self.hit_cap.append("search")
            return False
        self.spent_searches += 1
        return True

    def take_fetch(self):
        if self.spent_fetches >= self.fetches:
            self.hit_cap.append("fetch")
            return False
        self.spent_fetches += 1
        return True


def fresh_in_cache(url, cache, today, days=CACHE_DAYS):
    entry = (cache or {}).get(url)
    if not entry or not entry.get("fetched_on"):
        return False
    return (_as_date(today) - _as_date(entry["fetched_on"])).days < days


def plan(requirements, today, institution=None, budget=None):
    """Step 1. What to look for — a work order the RUN executes with its tools.

    Returns {"queries": [...], "budget": {...}, "notes": [...]}. Each query
    carries the requirement it serves so `ingest()` can attribute results
    without guessing.
    """
    budget = budget or Budget()
    today = _as_date(today)
    queries, notes = [], []
    for req in requirements or ():
        if not req.get("active", True):
            continue
        if req.get("keywords"):
            qs = plan_queries(req, institution, today)
        else:
            # Nothing to hunt with yet: map the landscape rather than guess at
            # named instances (§13.5).
            qs = landscape_queries(req, institution)
            notes.append(
                "\u201c%s\u201d has no words to watch yet, so this run maps what kinds "
                "of thing exist rather than hunting named ones."
                % (req.get("statement") or req.get("id")))
        for q in qs:
            if not budget.take_search():
                notes.append(
                    "Hit the search cap while planning, so some requirements were "
                    "not tasked at all this run.")
                break
            queries.append({"requirement_id": req["id"], "query": q})
    return {"queries": queries,
            "budget": {"searches": budget.searches, "fetches": budget.fetches},
            "notes": notes}


def ingest(results, owning_domains, today, cache=None, seen_urls=(),
           failures=()):
    """Step 3. Classify what the run's own tool calls brought back.

    `results` is what the RUN writes to JSON after its searches and fetches:

        [{"requirement_id": "research",
          "url": "https://cs.umd.edu/x",
          "title": "...",
          "text": "...",              # snippet, OR the fetched page text
          "read_from_owner": true,    # true ONLY if `text` came from fetching
          "fetched_on": "2026-09-08"} # optional, feeds the cache
         ]

    `failures` is [{"what": "...", "why": "..."}] for every search or fetch
    that did not come back. They become coverage notes and NOTHING is
    substituted for them (§1.8, §9.3) — this module has no way to fill a hole
    even if it wanted to, which is the point.
    """
    today = _as_date(today)
    cache = dict(cache or {})
    seen = set(seen_urls or ())
    out, coverage = [], []

    for f in failures or ():
        coverage.append(
            "%s failed (%s). Nothing was substituted for it — that is a hole in "
            "today's collection, not an absence of opportunities."
            % (f.get("what", "A lookup"), f.get("why", "no reason given")))

    for r in results or ():
        url = (r or {}).get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        cand = {
            "url": url,
            "title": (r.get("title") or "").strip(),
            "text": (r.get("text") or "").strip(),
            "read_from_owner": bool(r.get("read_from_owner")),
            "requirement_ids": [r["requirement_id"]] if r.get("requirement_id") else [],
            "found_by": "search",
        }
        cand["regime"], cand["regime_reason"] = classify(cand, owning_domains)
        cand["date"] = stated_date(cand["text"]) if cand["regime"] == "confirmed" else None
        if r.get("fetched_on"):
            cache[url] = {"fetched_on": _as_date(r["fetched_on"]).isoformat()}
        out.append(cand)

    return {
        "candidates": out,
        "coverage": coverage,
        "cache": cache,
        "stats": {"candidates": len(out),
                  "confirmed": sum(1 for c in out if c["regime"] == "confirmed"),
                  "leads": sum(1 for c in out if c["regime"] == "lead"),
                  "failures": len(failures or ())},
    }


def worth_fetching(url, owning_domains, cache, today):
    """Whether the RUN should spend a fetch on this search result.

    Only an owning domain can ever yield a confirmed fact (§1.8), so a fetch
    anywhere else buys nothing but tokens; and a page read within CACHE_DAYS
    does not need reading again.
    """
    return (is_owning_url(url, owning_domains)
            and not fresh_in_cache(url, cache, today))


def _short(exc):
    s = str(exc).strip().replace("\n", " ")
    return (s[:60] + "…") if len(s) > 60 else (s or exc.__class__.__name__)


def prune_cache(cache, today, days=CACHE_DAYS * 4):
    """Keep the fetch cache from becoming a ledger. It only has to answer
    'did we read this recently', so anything older than a few cycles is dead
    weight in a file that costs its tokens twice a day (§3.1)."""
    today = _as_date(today)
    return {u: e for u, e in (cache or {}).items()
            if e.get("fetched_on")
            and (today - _as_date(e["fetched_on"])).days <= days}
