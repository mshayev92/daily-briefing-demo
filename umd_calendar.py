"""
umd_calendar.py — campus events from calendar.umd.edu, filtered to what the
user actually asked for.
"""

import re
import unicodedata
from datetime import datetime, time, timedelta

TOPIC_SLUGS = [
    "academics",
    "arts-entertainment-and-culture",
    "student-life",
    "community-engagement",
    "health-and-wellness",
    "research",
    "diversity-inclusion",
    "athletics-and-recreation",
    "spiritual-and-religious",
    "family-engagement",
]

GOAL_SIGNALS = {
    "career": ["career", "internship", "job", "resume", "recruit", "employer",
               "hiring", "interview", "co-op", "networking night", "fair"],
    "social": ["meetup", "mixer", "social", "welcome", "meet ", "make friends",
               "community", "game night"],
    "academic_support": ["tutoring", "study skills", "writing center", "advising",
                         "workshop", "study", "office hours", "review session"],
    "clubs": ["club", "student org", "organization", "first look", "involvement",
              "terplink", "chapter"],
    "arts": ["concert", "gallery", "exhibition", "performance", "theatre",
             "theater", "film", "music", "dance", "art"],
    "fitness": ["fitness", "rec ", "intramural", "yoga", "run", "climb", "swim",
                "volleyball", "basketball", "soccer", "football", "game"],
    "service": ["volunteer", "service", "donate", "drive", "cleanup", "garden"],
    "research": ["research", "seminar", "colloquium", "lab", "symposium",
                 "lecture", "thesis", "defense"],
    "free_food": ["free food", "pizza", "breakfast", "lunch provided",
                  "refreshments", "snacks", "dinner provided"],
}

ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"

_LIST_BLOCK = re.compile(
    r"\[(?P<title>[^\]\n]+)\]\((?P<url>https://calendar\.umd\.edu/[^)\s]+)\)"
    r"\s*\n+\s*(?P<start>" + ISO + r")"
    r"\s*\n+\s*(?P<end>" + ISO + r")"
    r"\s*\n+(?P<desc>.*?)"
    r"\n+\s*\[View Event\]\((?P<url2>https://calendar\.umd\.edu/[^)\s]+)\)",
    re.S,
)

_SLUG_SUFFIX = re.compile(r"-\d{1,2}$")

# A slug family only means "the same underlying event" for listings that sit
# close together in time. Without this window, `cmsc-colloquium-3` and
# `cmsc-colloquium-7` — two different talks a fortnight apart — share a family
# and dedupe() silently drops the second. A multi-day festival spans days, not
# weeks, so a week is generous for the case the family rule exists to serve.
FAMILY_WINDOW_DAYS = 7

# The comparison is STRICTLY LESS THAN, and that is the whole fix for a bug
# that ate every other occurrence of a weekly series. With `<=`, a listing
# exactly 7 days after the kept one merges into it: three weekly listings
# collapsed to two and the middle one vanished with nothing anywhere saying so.
#
# The trade, stated because it is real: a festival spanning a full Saturday to
# Saturday now renders as two rows instead of one. That error is VISIBLE and
# mildly annoying; the one it replaces was silent data loss. Between a briefing
# that shows a festival twice and a briefing that drops a seminar you meant to
# attend, the first is obviously right.
#
# TO REVERT: change `<` back to `<=` in dedupe(), and change this comment. Do
# not change one without the other, and note that §2 and §12.4 both name the
# constant.
FAMILY_MERGE_STRICT = True

# §12.2's on-campus test. The College Park *city* is 20740; 20742 is the
# university's own campus ZIP, which is why the ZIP and not the city name is
# what distinguishes an on-campus venue from a restaurant down the road.
CAMPUS_ZIPS = ("20742",)


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _slug_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def slug_family(url):
    return _SLUG_SUFFIX.sub("", _slug_of(url))


def _parse_iso(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def _day(d):
    """`Tue, Sep 8` without %-d, which is a glibc extension and fails on
    macOS. Same helper and same reason as reminder_url._day."""
    return "%s, %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day)


def item_id_for(ev):
    """`campus-<family>-<YYYY-MM-DD>` — §3.3's `<course>-<slug>-<date>` shape.

    The date is load-bearing twice over. Without it a weekly series that reuses
    its URL slug produces the SAME id every week forever: next week's occurrence
    reconciles against last week's item, so it never surfaces as new, its
    `times_surfaced` keeps climbing toward auto-dismissal, and a single Mark
    done hides every future instance through the db store.

    It also removes the need for the `family@date` hack dedupe() used to give
    far-apart same-family listings distinct identities — the date does that
    now — so the `@` never reaches an id, which §3.3 requires punctuation-free.
    """
    fam = ev["family"].split("@", 1)[0]
    fam = re.sub(r"[^a-z0-9]+", "-", fam.lower()).strip("-")
    return "campus-%s-%s" % (fam, ev["start"].date().isoformat())


def parse_category_page(text, source_slug=None):
    events = []
    for m in _LIST_BLOCK.finditer(text or ""):
        url = m.group("url2") or m.group("url")
        start = _parse_iso(m.group("start"))
        end = _parse_iso(m.group("end"))
        desc = " ".join(m.group("desc").split())
        all_day = start.time() == datetime.min.time() and end.time() == datetime.min.time()
        events.append({
            "title": m.group("title").strip(),
            "url": url,
            "slug": _slug_of(url),
            "family": slug_family(url),
            "start": start,
            "end": end,
            "all_day": all_day,
            "multi_day": all_day and (end.date() - start.date()).days >= 1,
            "description": desc,
            "topics": [source_slug] if source_slug else [],
            "location": None,
            "address": None,
            "registration_url": None,
            "organizer": None,
            "organizer_email": None,
            "cost": None,
            "fields_absent": [],
        })
    return events


_HTML_EVENT = re.compile(r"<umd-element-event\b.*?</umd-element-event>", re.S)
_HTML_SLOT = r'<[^>]*slot="%s"[^>]*>(.*?)</(?:div|p)>'


def _slot(block, name):
    m = re.search(_HTML_SLOT % name, block, re.S)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    return " ".join(html_unescape(text).split())


def html_unescape(s):
    import html as _h
    return _h.unescape(s or "")


def parse_category_html(html, source_slug=None):
    """calendar.umd.edu's category LISTING page (the live HTML, 2026-09)
    -> the same event dicts `parse_category_page()` builds from the old
    fetched-markdown form. Each listing is one <umd-element-event> custom
    element with slot="headline" / "start-date-iso" / "end-date-iso" /
    "text" children."""
    events = []
    for block in _HTML_EVENT.findall(html or ""):
        m = re.search(r'slot="headline".*?href="(https://calendar\.umd\.edu/[^"?]+)'
                      r'(?:\?[^"]*)?"', block, re.S)
        start_s, end_s = _slot(block, "start-date-iso"), _slot(block, "end-date-iso")
        title = _slot(block, "headline")
        if not (m and title and re.match(ISO, start_s) and re.match(ISO, end_s)):
            continue
        url = m.group(1)
        start, end = _parse_iso(start_s), _parse_iso(end_s)
        all_day = start.time() == datetime.min.time() and end.time() == datetime.min.time()
        events.append({
            "title": title, "url": url, "slug": _slug_of(url),
            "family": slug_family(url), "start": start, "end": end,
            "all_day": all_day,
            "multi_day": all_day and (end.date() - start.date()).days >= 1,
            "description": _slot(block, "text"),
            "topics": [source_slug] if source_slug else [],
            "location": None, "address": None, "registration_url": None,
            "organizer": None, "organizer_email": None, "cost": None,
            "fields_absent": [],
        })
    return events


def detail_html_to_text(html):
    """An event DETAIL page's HTML -> the markdown-ish text
    enrich_from_detail() and parse_tags_from_detail() were written against
    (## / ### headings, [label](url) links). The page's filter sidebar is
    cut first: it lists every topic on the site as links, which would
    otherwise read as this event's own topics."""
    t = html or ""
    t = re.sub(r"<umd-calendar-sidebar\b.*?</umd-calendar-sidebar>", " ", t,
               flags=re.S)
    start = t.find("<umd-calendar-item-details")
    if start < 0:
        start = max(t.find("<main"), 0)
    t = t[start:]
    end = t.find("<umd-element-footer")
    if end > 0:
        t = t[:end]
    t = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)

    def _inner(x):
        return " ".join(html_unescape(re.sub(r"<[^>]+>", " ", x)).split())

    def _link(m):
        href = m.group(1)
        if href.startswith("/"):
            href = "https://calendar.umd.edu" + href
        if not href.startswith("https://"):
            return _inner(m.group(2))
        return "[%s](%s)" % (_inner(m.group(2)), href)

    t = re.sub(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', _link, t, flags=re.S)
    t = re.sub(r"<h2[^>]*>(.*?)</h2>", lambda m: "\n## %s\n" % _inner(m.group(1)),
               t, flags=re.S)
    t = re.sub(r"<h[34][^>]*>(.*?)</h[34]>",
               lambda m: "\n### %s\n" % _inner(m.group(1)), t, flags=re.S)
    t = re.sub(r"<br\s*/?>|</(p|div|li)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


def merge_pages(pages):
    by_slug = {}
    for ev in pages:
        cur = by_slug.get(ev["slug"])
        if cur is None:
            by_slug[ev["slug"]] = dict(ev)
        else:
            for t in ev["topics"]:
                if t and t not in cur["topics"]:
                    cur["topics"].append(t)
    return list(by_slug.values())


def dedupe(events):
    """Collapse redundant listings.

    Two rules, in order:
      1. Same slug family AND within FAMILY_WINDOW_DAYS of the instance already
         kept -> the same underlying event. Keep the earliest and record how
         many others there were, so a multi-day festival is one row, not four.
         Beyond that window the family is a coincidence of numbering, and the
         listing is kept as a separate event.
      2. Same normalized title on the same date -> duplicate submission.
    """
    events = sorted(events, key=lambda e: (e["start"], e["slug"]))
    kept = {}
    for ev in events:
        rep = kept.get(ev["family"])
        if rep is not None and \
                (ev["start"].date() - rep["start"].date()).days < FAMILY_WINDOW_DAYS:
            rep["other_instances"] = rep.get("other_instances", 0) + 1
            for t in ev["topics"]:
                if t not in rep["topics"]:
                    rep["topics"].append(t)
            continue
        ev = dict(ev)
        ev.setdefault("other_instances", 0)
        if rep is not None:
            # Same family, but too far apart to be the same thing. Give it its
            # own identity so downstream ids and state do not collide.
            ev["family"] = "%s@%s" % (ev["family"], ev["start"].date().isoformat())
        kept[ev["family"]] = ev

    out, seen_title = [], set()
    for ev in sorted(kept.values(), key=lambda e: e["start"]):
        tkey = (_norm(ev["title"]), ev["start"].date())
        if tkey in seen_title:
            continue
        seen_title.add(tkey)
        out.append(ev)
    return out


def _haystack(ev):
    return " ".join([
        ev["title"], ev.get("description") or "", " ".join(ev.get("topics") or []),
        " ".join(ev.get("units") or []),
    ]).lower()


def _overlaps_busy(ev, busy_blocks):
    """Does the event collide with anything already on his calendars?

    Renamed from `_overlaps_class` on 2026-09-10 because the name was the bug.
    `avoid_class_conflicts` is answered "yes", and every exclusion it made was
    measured against ACADEMIC calendars only — four campus events were dropped
    on 2026-09-09 for clashing with classes, while any non-class commitment was
    invisible. That makes both directions wrong: an event during a standing
    commitment survives the filter, and the timeline's free-time labels are
    optimistic. Callers must pass class AND personal blocks (STEP 4).
    """
    for cs, ce in busy_blocks or []:
        if ev["start"] < ce and cs < ev["end"]:
            return True
    return False


# The old name, kept so nothing that still imports it breaks silently.
_overlaps_class = _overlaps_busy


def classify(events, prefs, today, class_blocks=None, known_ids=None,
             suppressed_ids=None, busy_blocks=None):
    """`busy_blocks` is class + personal (§2.2, STEP 4). `class_blocks` is
    accepted as the old name for the same argument; when both are given they
    are unioned, so an existing caller keeps working and gains nothing wrong."""
    a = (prefs or {}).get("answers", {}) or {}
    topics = a.get("topics") or {}
    goals = a.get("goals") or []
    units = a.get("units") or []
    boost = [k.lower() for k in (a.get("keywords_boost") or [])]
    block = [k.lower() for k in (a.get("keywords_block") or [])]
    horizon = int(a.get("horizon_days") or 14)
    free_only = str(a.get("free_only", "no")) == "yes"
    avoid_conflicts = str(a.get("avoid_class_conflicts", "yes")) == "yes"

    busy = list(busy_blocks or []) + list(class_blocks or [])
    known_ids = set(known_ids or ())
    suppressed_ids = set(suppressed_ids or ())
    horizon_end = today + timedelta(days=horizon)

    for ev in events:
        ev["item_id"] = item_id_for(ev)
        hay = _haystack(ev)
        why, score = [], 0

        if ev["item_id"] in suppressed_ids:
            ev.update(evidence="exclude", score=0,
                      why="Already dismissed or marked done.")
            continue
        if ev["end"].date() < today:
            ev.update(evidence="exclude", score=0, why="Already finished.")
            continue
        if ev["start"].date() > horizon_end:
            ev.update(evidence="exclude", score=0,
                      why="Beyond your %d-day campus-event horizon." % horizon)
            continue
        hit = next((k for k in block if k and k in hay), None)
        if hit:
            ev.update(evidence="exclude", score=0,
                      why='Matches your excluded term "%s".' % hit)
            continue
        never = [t for t in ev["topics"] if topics.get(t) == "never"]
        if never and not any(topics.get(t) == "always" for t in ev["topics"]):
            ev.update(evidence="exclude", score=0,
                      why="Topic %s is set to Never." % never[0])
            continue
        # `on_campus_only` is deliberately NOT applied here. A list page states
        # no venue at all, so there is nothing to test against; the branch that
        # used to sit here read a `location_is_offcampus` key that nothing in
        # this module ever wrote, so it never fired. The filter runs once, in
        # select_and_enrich, where a real stated address exists — which is what
        # §12.2's "only on a stated fact" requires anyway.
        # `free_only` is deliberately NOT applied here either, for the same
        # reason as `on_campus_only` above: parse_category_page never sets
        # `cost`, so this test could only ever read None and was dead code of
        # exactly the species the 2026-09-07 audit removed two comments up.
        # §12.2 puts the filter after enrichment, where a stated cost exists.
        if avoid_conflicts and not ev["all_day"] and _overlaps_busy(ev, busy):
            ev.update(evidence="exclude", score=0,
                      why="Clashes with something already on your calendar.")
            continue
        if ev["item_id"] in known_ids:
            ev.update(evidence="exclude", score=0,
                      why="Already tracked from another source.")
            continue

        if not ev.get("title") or not ev.get("start"):
            ev.update(evidence="insufficient", score=0,
                      why="The listing did not state a title or a date.")
            continue

        always = [t for t in ev["topics"] if topics.get(t) == "always"]
        if always:
            score += 4
            why.append("you asked to always see %s" % always[0].replace("-", " "))
        matched_goals = [g for g in goals
                         if any(w in hay for w in GOAL_SIGNALS.get(g, []))]
        if matched_goals:
            score += 2 * min(len(matched_goals), 2)
            why.append("matches your interest in %s"
                       % ", ".join(g.replace("_", " ") for g in matched_goals[:2]))
        unit_hit = [u for u in units if u in (ev.get("units") or [])]
        if unit_hit:
            score += 2
            why.append("run by a unit you follow")
        bhit = [k for k in boost if k and k in hay]
        if bhit:
            score += 3
            why.append('matches "%s"' % bhit[0])
        if ev["multi_day"]:
            score -= 2
        days_out = (ev["start"].date() - today).days
        if 0 <= days_out <= 3:
            score += 1

        ev["score"] = score
        ev["evidence"] = "include" if score >= 6 else "consider"
        if not why:
            why.append("an upcoming campus event in a topic you allow")
        ev["why"] = "Why you're seeing this: " + "; ".join(why) + "."
    return events


def select(events, prefs, limit=None):
    a = (prefs or {}).get("answers", {}) or {}
    cap = int(a.get("max_events") if a.get("max_events") is not None else 2) \
        if limit is None else limit
    if cap <= 0:
        return []
    ranked = sorted(
        [e for e in events if e.get("evidence") in ("include", "consider")],
        key=lambda e: (-e.get("score", 0), e["start"]),
    )
    return ranked[:cap]


def to_item(ev, today):
    links = []
    if ev.get("registration_url"):
        links.append({"label": "Register", "url": ev["registration_url"]})
    links.append({"label": "More info", "url": ev["url"]})
    return {
        "id": ev["item_id"],
        "course": None,
        "course_class": "none",
        # §12.4: campus events render with the existing "Campus" label. Setting
        # it here rather than at the call site is what stops build_reminder_url
        # emitting a details block with no label prefix.
        "course_label": "Campus",
        "kind": "event",
        "title": ev["title"],
        "detail": (ev.get("description") or "")[:160],
        "notes": ev.get("why", ""),
        "date": ev["start"].date().isoformat(),
        "end_date": (ev["end"].date().isoformat()
                     if ev["end"].date() != ev["start"].date() else None),
        "time": None if ev["all_day"] else ev["start"].strftime("%H:%M"),
        "end_time": None if ev["all_day"] else ev["end"].strftime("%H:%M"),
        "location": ev.get("address") or ev.get("location"),
        "links": links,
        "organizer": ev.get("organizer"),
        "confidence": "confirmed",
        "status": "new",
        "times_surfaced": 0,
        "last_shown": today.isoformat(),
        "snooze_until": None,
        "supersedes": None,
        "group_id": None,
        "ai_actions": [],
        "overdue_flagged": False,
        # §3.3 requires `evidence` on EVERY item; `relevance.evidence` is this
        # module's own copy and the section model reads the top-level one.
        # Prompt 7-0 sets it for every candidate, but leaving it absent here
        # meant a campus item was the one kind that arrived without it.
        "evidence": ev.get("evidence"),
        "relevance": {"score": ev.get("score", 0),
                      "evidence": ev.get("evidence"),
                      "why": ev.get("why", "")},
        "extraction": {
            "enriched_at": None,
            "fields_absent": ev.get("fields_absent", []),
        },
        "source_refs": [{"source": "umd_calendar", "url": ev["url"],
                         "slug": ev["slug"]}],
    }


_DETAIL_TIMES = re.compile(
    r"-\s*(?P<start>\d{1,2}:\d{2}\s*[ap]m)\s*(?:To\s*)?[–-]\s*(?P<end>\d{1,2}:\d{2}\s*[ap]m)",
    re.I)


def _parse_clock(s):
    """`2:00 pm` -> a time, or None. Only ever called on text the page stated."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([ap])m", str(s or ""), re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).lower()
    return time(h + (12 if ap == "p" else 0), mi)


_MD_LINK = re.compile(r"\[(?P<label>[^\]\n]+)\]\((?P<url>https://[^)\s]+)\)")
_EMAIL = re.compile(r"<([^@<>\s]+@[^@<>\s]+\.[a-z]{2,})>", re.I)
_ADDRESS = re.compile(
    r"^(?P<addr>\d{2,6}\s+[A-Z][^\n]{4,60}(?:College Park|Md|MD)[^\n]{0,20})$",
    re.M)

_LINK_NOISE = ("add to calendar", "get directions", "skip site navigation",
               "advanced search", "submit an event", "umd homepage",
               "academic calendar", "alumni calendar", "all events",
               "places", "campus units", "view event")

REGISTER_WORDS = ("rsvp", "register", "registration", "sign up", "sign-up",
                  "tickets", "apply now", "application", "terplink")


def enrich_from_detail(text, ev):
    ev = dict(ev)
    text = text or ""

    # A listing whose category page carried midnight-to-midnight stamps looks
    # all-day, but its detail page often states the real hours. Those used to be
    # written to `stated_time_text`, which nothing ever read — so a genuine
    # 2:00–5:00 pm was fetched and then thrown away, and the event reached the
    # calendar as all-day. Promote it instead; the source stated it, so this is
    # capture, not inference (§1.6).
    m = _DETAIL_TIMES.search(text)
    if m and ev.get("all_day"):
        t0 = _parse_clock(m.group("start"))
        t1 = _parse_clock(m.group("end"))
        if t0 and t1:
            ev["start"] = ev["start"].replace(hour=t0.hour, minute=t0.minute)
            ev["end"] = ev["end"].replace(hour=t1.hour, minute=t1.minute)
            if ev["end"] <= ev["start"]:
                ev["end"] = ev["start"] + timedelta(hours=1)
            ev["all_day"] = False
            ev["multi_day"] = False
            ev["stated_time_text"] = "%s-%s" % (m.group("start"), m.group("end"))

    loc_section = text.split("## Location", 1)
    if len(loc_section) > 1:
        head = loc_section[1].split("\n## ", 1)[0][:600]
        lm = re.search(r"###\s*\[([^\]]+)\]", head) or re.search(r"###\s*([^\n]+)", head)
        if lm:
            ev["location"] = lm.group(1).strip()
        am = _ADDRESS.search(head)
        if am:
            ev["address"] = " ".join(am.group("addr").split())

    for lm in _MD_LINK.finditer(text):
        label = lm.group("label").strip()
        url = lm.group("url")
        low = label.lower()
        if any(n in low for n in _LINK_NOISE):
            continue
        if url.startswith("https://calendar.umd.edu/"):
            continue
        if any(w in low for w in REGISTER_WORDS) or "terplink.umd.edu" in url:
            ev["registration_url"] = url
            break

    con = text.split("## Contact", 1)
    if len(con) > 1:
        head = con[1][:400]
        om = re.search(r"###\s*([^\n]+)", head)
        if om:
            ev["organizer"] = om.group(1).strip()
        em = _EMAIL.search(head)
        if em:
            ev["organizer_email"] = em.group(1)

    if re.search(r"\bfree\b", text, re.I):
        ev["cost"] = "Free"

    ev["fields_absent"] = [f for f in
                           ("location", "address", "registration_url",
                            "organizer", "cost")
                           if not ev.get(f)]
    ev["enriched"] = True
    return ev


_TAG_SECTION = re.compile(r"###\s*(?P<name>Event Topics|Schools and Units|Tags|Audience)\s*\n(?P<body>.*?)(?=\n###|\n##|\Z)", re.S)
_TAG_LINK = re.compile(r"\[[^\]]+\]\(https://calendar\.umd\.edu/category/(?P<slug>[a-z0-9\-]+)\)")


def parse_tags_from_detail(text):
    topics, units, audience = [], [], []
    for m in _TAG_SECTION.finditer(text or ""):
        slugs = [x.group("slug") for x in _TAG_LINK.finditer(m.group("body"))]
        name = m.group("name")
        if name == "Event Topics":
            topics += slugs
        elif name == "Schools and Units":
            units += slugs
        elif name == "Audience":
            audience += slugs
    dedup = lambda xs: list(dict.fromkeys(xs))
    return dedup(topics), dedup(units), dedup(audience)


def select_and_enrich(events, prefs, fetch_detail, cap=None, max_fetches=5):
    a = (prefs or {}).get("answers", {}) or {}
    topics_pref = a.get("topics") or {}
    units_pref = a.get("units") or []
    on_campus_only = str(a.get("on_campus_only", "no")) == "yes"
    free_only = str(a.get("free_only", "no")) == "yes"
    if cap is None:
        cap = int(a.get("max_events") if a.get("max_events") is not None else 2)
    if cap <= 0:
        return [], []

    ranked = sorted(
        [e for e in events if e.get("evidence") in ("include", "consider")],
        key=lambda e: (-e.get("score", 0), e["start"]),
    )

    chosen, dropped, fetches = [], [], 0
    for ev in ranked:
        if len(chosen) >= cap or fetches >= max_fetches:
            break
        try:
            text = fetch_detail(ev["url"])
            fetches += 1
        except Exception as exc:
            ev = dict(ev)
            ev["fetch_error"] = str(exc)
            ev["fields_absent"] = ["location", "address", "registration_url",
                                   "organizer", "cost"]
            chosen.append(ev)
            continue

        full = enrich_from_detail(text, ev)
        topics, units, audience = parse_tags_from_detail(text)
        full["topics"] = list(dict.fromkeys((full.get("topics") or []) + topics))
        full["units"] = units
        full["audience"] = audience

        never = [t for t in full["topics"] if topics_pref.get(t) == "never"]
        always = [t for t in full["topics"] if topics_pref.get(t) == "always"]
        if never and not always:
            full["evidence"] = "exclude"
            full["why"] = "Topic %s is set to Never." % never[0]
            dropped.append(full)
            continue
        if free_only and full.get("cost") and "free" not in str(full["cost"]).lower():
            full["evidence"] = "exclude"
            full["why"] = "Listing states a cost (%s)." % full["cost"]
            dropped.append(full)
            continue
        addr = (full.get("address") or "").strip()
        if on_campus_only and addr and not addr.endswith(CAMPUS_ZIPS):
            full["evidence"] = "exclude"
            full["why"] = "Not on the College Park campus."
            dropped.append(full)
            continue

        if always:
            full["score"] = full.get("score", 0) + 4
            full["why"] = full["why"].rstrip(".") + \
                "; you asked to always see %s." % always[0].replace("-", " ")
        if [u for u in units_pref if u in units]:
            full["score"] = full.get("score", 0) + 2
            full["why"] = full["why"].rstrip(".") + "; run by a unit you follow."
        chosen.append(full)

    chosen.sort(key=lambda e: e["start"])
    return chosen, dropped
