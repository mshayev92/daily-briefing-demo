"""Handshake (umd.joinhandshake.com) -> briefing items (2026-09-29).

Reads ~/projects/handshake-scraper/output/latest.json, written by that scraper on
its own schedule (sources.py), the way canvas_shadow.py reads
canvas-scraper's. Plain code, no model:

- **Jobs become leads only when vetted for a goal.** A listing becomes a
  lead only if a goal's program-name search found it AND its title or
  employer names that program. Generic-search hits (internships at large,
  unvetted for his eligibility) never become leads (2026-10-09). Confirming
  a lead promotes it to an opportunity with the tracker and "Draft my
  application" (orchestrator._promote_confirmed_leads).
- **Events become campus items** (ids `campus-hs-...`, so the campus
  de-duplication and dismissal handling apply): every career fair, every
  event he registered for or saved, and up to EVENTS_MAX others whose
  title, host or category matches his interests.
"""

import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import opportunity

TZ = ZoneInfo("America/New_York")
LATEST = os.path.join(os.environ.get("HANDSHAKE_SCRAPER_DIR", os.path.join(
    os.path.expanduser("~"), "projects", "handshake-scraper")), "output",
    "latest.json")
EVENT_WINDOW_DAYS = 14
EVENTS_MAX = 5                 # interest-matched events, beyond fairs/registered
MAX_EVENT_SPAN_DAYS = 3        # a "month-long" listing is a program, not an event
JOB_MIN_DAYS_LEFT = 2          # a listing closing sooner is not worth raising


class NoHandshakeScrapeError(Exception):
    pass


def load(path=None):
    path = path or LATEST
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise NoHandshakeScrapeError("no Handshake scrape at %s" % path) from exc


def age_hours(scrape, now):
    try:
        at = datetime.fromisoformat(scrape["scraped_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (now - at).total_seconds() / 3600.0


def _local(ts):
    try:
        return datetime.fromisoformat(ts).astimezone(TZ) if ts else None
    except ValueError:
        return None


def _words(text):
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


_STOP = {"the", "of", "and", "for", "program", "programs", "internship",
         "internships", "undergraduate", "undergraduates", "summer", "umd",
         "research", "university", "maryland", "scholars", "scholarship"}


def names_program(job, query):
    """A goal search's hit counts only when the listing names the program:
    every distinctive word of the query (a word Handshake titles are full
    of, like "internship", does not count) appears in its title or
    employer."""
    want = _words(query) - _STOP
    have = _words(job.get("title")) | _words(job.get("employer"))
    return bool(want) and want <= have


def _pay_text(pay):
    if not pay or pay.get("min") is None:
        return None
    lo, hi = pay["min"], pay.get("max")
    fmt = lambda v: "{:,.0f}".format(v) if v >= 1000 else "%g" % v  # noqa: E731
    amount = "$" + fmt(lo) + ("" if not hi or hi == lo else "-" + fmt(hi))
    return "%s %s" % (amount, pay.get("per") or "") if pay.get("per") else amount


def job_lead(job, today, goal_ids=()):
    """One Handshake listing -> a lead item, or None when it closes too soon."""
    closes = _local(job.get("expires_at"))
    if closes and (closes.date() - today).days < JOB_MIN_DAYS_LEFT:
        return None
    places = job.get("locations") or []
    where = "Remote" if job.get("remote") else (
        "; ".join(places[:2]) + (" and %d more" % (len(places) - 2)
                                 if len(places) > 2 else "")) \
        or ("Hybrid" if job.get("hybrid") else None)
    starts = job.get("start_date")
    facts = [f for f in (job.get("job_type"), job.get("employment_type"), where,
                         _pay_text(job.get("pay")),
                         "starts %s" % starts
                         if starts and starts >= today.isoformat() else None)
             if f]
    claim = "%s at %s on Handshake%s" % (
        job["title"], job.get("employer") or "an employer",
        (": " + "; ".join(facts)) if facts else "")
    basis = [opportunity.basis(claim, "Handshake listing", url=job["url"],
                               observed_on=today.isoformat())]
    deadline = closes.date().isoformat() if closes else None
    value = "medium"
    L = opportunity.lead(
        id="lead-handshake-%s" % job["id"],
        headline="%s — %s" % (job["title"], job.get("employer")) if job.get("employer")
        else job["title"],
        basis_list=basis, confidence="reported",
        requirement_ids=list(goal_ids),
        confirm_action="Open the listing and check you meet its requirements",
        kill_criteria="It needs more experience or class standing than you have",
        cost_to_check="low", value_if_won=value, deadline=deadline)
    opportunity.validate_lead(L)
    item = opportunity.to_item(L, today)
    item["score"] = opportunity.expected_value(
        value_if_won=value, cost_to_check="low",
        requirement_hits=len(L["requirement_ids"]))
    item["source_refs"] = [{"source": "handshake", "id": job["id"]}]
    item["links"] = [{"label": "Open in Handshake", "url": job["url"]}]
    # to_item() leaves these out; confirming the lead carries them to the
    # opportunity it becomes.
    item["requirement_ids"] = list(L["requirement_ids"])
    item["opportunity"] = {"apply_url": job["url"], "deadline": deadline,
                           "type": "internship", "organization": job.get("employer")}
    return item


def job_leads(scrape, today, goals):
    """`goals`: [(requirement id, [keyword, ...])] of the active goals.
    Only listings a goal's program search found, naming that program."""
    by_query = {kw.lower(): rid for rid, kws in goals for kw in kws}
    out = []
    for job in scrape.get("jobs") or ():
        goal_ids = []
        for q in job.get("matched_queries") or ():
            rid = by_query.get(q.lower())
            if rid is not None and names_program(job, q) and rid not in goal_ids:
                goal_ids.append(rid)
        if not goal_ids:
            continue
        item = job_lead(job, today, goal_ids)
        if item:
            out.append(item)
    return out


# The campus section ranks by this against calendar.umd.edu's own scores
# (umd_calendar.classify, typically 10-14): what he signed up for first, a
# career fair about level with that ranker's best, an interest match below.
SCORE_REGISTERED, SCORE_FAIR, SCORE_INTEREST = 20, 14, 11


def event_item(ev, today, why, score):
    start, end = _local(ev.get("start")), _local(ev.get("end"))
    all_day = bool(start and start.hour == 0 and start.minute == 0)
    label = "Career fair" if ev["kind"] == "career_fair" else "Handshake event"
    notes = [why]
    if ev.get("employers_count"):
        notes.append("%d employers" % ev["employers_count"])
    reg_end = _local(ev.get("registration_ends"))
    if reg_end and not ev.get("registered"):
        notes.append("register by %s" % reg_end.strftime("%b %-d"))
    medium = {"VIRTUAL": "Virtual", "IN_PERSON": "In person",
              "HYBRID": "Hybrid"}.get(ev.get("medium"))
    return {
        "id": "campus-hs-%s" % ev["id"].replace("_", "-"),
        "course": None, "course_class": "none", "course_label": "Campus",
        "kind": "event", "title": ev["name"],
        "detail": " · ".join(x for x in (label, ev.get("host"), medium) if x)[:160],
        "notes": "; ".join(notes),
        "date": start.date().isoformat() if start else None,
        "end_date": (end.date().isoformat()
                     if start and end and end.date() != start.date() else None),
        "time": None if all_day or not start else start.strftime("%H:%M"),
        "end_time": None if all_day or not end else end.strftime("%H:%M"),
        "location": medium,
        "links": [{"label": "Open in Handshake", "url": ev["url"]}],
        "organizer": ev.get("host"),
        "confidence": "confirmed", "status": "new", "times_surfaced": 0,
        "last_shown": today.isoformat(), "snooze_until": None,
        "supersedes": None, "group_id": None, "ai_actions": [],
        "overdue_flagged": False, "evidence": "include",
        "relevance": {"score": score, "evidence": "include", "why": why},
        "source_url": ev["url"],
        "source_refs": [{"source": "handshake", "url": ev["url"]}],
    }


def event_items(scrape, today, keywords, blocked=()):
    """Career fairs and registered/saved events always; other events only
    when they match `keywords` (the campus quiz's boosts, or his course
    subjects), soonest first, EVENTS_MAX of them."""
    keywords = [k.lower() for k in keywords if k and k.strip()]
    blocked = [k.strip().lower() for k in blocked if k and k.strip()]
    window_end = today + timedelta(days=EVENT_WINDOW_DAYS)
    must, maybe = [], []
    for ev in scrape.get("events") or ():
        start, end = _local(ev.get("start")), _local(ev.get("end"))
        if not start or (end and end.date() < today):
            continue
        text = " %s %s %s " % (ev.get("name"), ev.get("host"),
                               " ".join(ev.get("categories") or ()))
        text = text.lower()
        if any(b in text for b in blocked):
            continue
        if ev.get("registered") or ev.get("favorited"):
            must.append(event_item(ev, today, "You registered for this on Handshake"
                                   if ev.get("registered") else "Saved on Handshake",
                                   SCORE_REGISTERED))
            continue
        if start.date() > window_end or start.date() < today:
            continue           # not started-and-running programs, not far out
        if ev.get("kind") == "career_fair":
            must.append(event_item(ev, today, "Career fair on Handshake", SCORE_FAIR))
            continue
        if end and (end - start).days > MAX_EVENT_SPAN_DAYS:
            continue
        hits = [k for k in keywords if k in text]
        if hits:
            maybe.append(event_item(ev, today, "Matches your interests (%s)"
                                    % ", ".join(h.strip() for h in hits[:2]),
                                    SCORE_INTEREST))
    maybe.sort(key=lambda it: (it["date"] or "", it["time"] or ""))
    return must + maybe[:EVENTS_MAX]
