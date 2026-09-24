"""STEP 5b: campus events from calendar.umd.edu (brief v2, 2026-09-22).

`umd_calendar.py` has classified and ranked campus events against the
preferences quiz since early September, but nothing ever fetched a page for
it once the pipeline stopped running inside an interactive session: the
page's "around campus" section said "No campus events matched your
preferences" every day while no event had been looked at. This is that
fetcher. Plain HTTP and HTML parsing, no model: listing pages for every
topic the quiz does not set to Never (two pages each), then the detail
page of each shortlisted event for its location, registration link,
organizer and audience.
"""

import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import umd_calendar

BASE = "https://calendar.umd.edu"
PAGES_PER_TOPIC = 1
UNDERGRAD_PAGES = 4            # the "Undergraduate Students" audience listing
TIMEOUT = 20
WORKERS = 3                    # the site times out under more parallel load
# Used only when the quiz's own keyword boost list is empty: terms taken
# from the kind of courses he is actually enrolled in, so a CS talk outranks
# an alumni real-estate panel.
SUBJECT_TERMS = {
    "CMSC": ("computer science", "computing", "programming", "software",
             "artificial intelligence", " ai ", "machine learning", "data science",
             "cybersecurity", "hackathon", "coding", "robotics", "quantum",
             "python", "startup", "entrepreneur", "internship", "tech "),
    "MATH": ("mathematics", " math "),
    "ECON": ("economics", "finance", "investing", "quant"),
    "PHIL": ("ethics", "philosophy"),
}
UNDERGRAD_AUDIENCE = "current-students"
_UA = "UMD-Daily-Briefing/1.0 (personal student briefing)"


def fetch(url, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read(1_500_000).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 -- one retry, then give up
            last = exc
    raise last


def with_default_boosts(prefs, course_codes):
    """The quiz answers, with keyword boosts from his course subjects added
    when he has not set any himself."""
    prefs = dict(prefs or {})
    answers = dict(prefs.get("answers") or {})
    if not answers.get("keywords_boost"):
        terms = []
        for code in course_codes or ():
            terms.extend(SUBJECT_TERMS.get(str(code)[:4].upper(), ()))
        answers["keywords_boost"] = list(dict.fromkeys(terms))
    prefs["answers"] = answers
    return prefs


def _listing_urls(topic, pages=PAGES_PER_TOPIC):
    first = "%s/category/%s" % (BASE, topic)
    return [first] + ["%s/p%d" % (first, n) for n in range(2, pages + 1)]


def collect(prefs, today, busy_blocks, known_ids=(), suppressed_ids=(),
            fetch_page=fetch, log=lambda msg: None):
    """-> (items, report). `busy_blocks` are (start, end) naive local
    datetimes of class + personal events, for the clash filter."""
    answers = (prefs or {}).get("answers") or {}
    topics_pref = answers.get("topics") or {}
    topics = [t for t in umd_calendar.TOPIC_SLUGS
              if topics_pref.get(t, "sometimes") != "never"]
    # Candidates come from the Undergraduate Students audience listing: the
    # topic listings are mostly graduate/faculty/staff workshops. The topic
    # pages are still read, for the topic tags the quiz scores against.
    jobs = [(None, u) for u in _listing_urls(UNDERGRAD_AUDIENCE, UNDERGRAD_PAGES)]
    jobs += [(t, u) for t in topics for u in _listing_urls(t)]

    def _get(job):
        topic, url = job
        try:
            return umd_calendar.parse_category_html(fetch_page(url), topic)
        except Exception as exc:  # noqa: BLE001
            log("campus listing %s failed: %s" % (url, exc))
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pages = list(pool.map(_get, jobs))
    failed = sum(1 for p in pages if p is None)
    undergrad = {e["slug"] for (t, _u), p in zip(jobs, pages)
                 if t is None and p for e in p}
    events = umd_calendar.merge_pages([e for p in pages if p for e in p])
    if undergrad:
        events = [e for e in events if e["slug"] in undergrad]
    events = umd_calendar.dedupe(events)
    umd_calendar.classify(events, prefs, today, busy_blocks=busy_blocks,
                          known_ids=known_ids, suppressed_ids=suppressed_ids)

    def _detail(url):
        return umd_calendar.detail_html_to_text(fetch_page(url))

    chosen, dropped = umd_calendar.select_and_enrich(
        events, prefs, _detail, max_fetches=10)
    kept = []
    for ev in chosen:
        audience = ev.get("audience") or []
        if audience and UNDERGRAD_AUDIENCE not in audience:
            continue          # a faculty/staff/grad-only listing
        kept.append(umd_calendar.to_item(ev, today))
    report = {"listing_pages": len(jobs), "failed_pages": failed,
              "events_seen": len(events), "shown": len(kept)}
    return kept, report


def busy_from_calendar(busy, tz):
    """step4_schedule's `busy_blocks` (Google Calendar events, ISO strings)
    -> naive local (start, end) pairs, which is what umd_calendar compares.
    All-day events are not a clash."""
    out = []
    for e in busy or ():
        if e.get("all_day"):
            continue
        try:
            s = datetime.fromisoformat(str(e.get("start")))
            t = datetime.fromisoformat(str(e.get("end")))
        except (TypeError, ValueError):
            continue
        if s.tzinfo:
            s, t = s.astimezone(tz), t.astimezone(tz)
        out.append((s.replace(tzinfo=None), t.replace(tzinfo=None)))
    return out
