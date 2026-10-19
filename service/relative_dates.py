"""Keep relative-time wording true as days pass.

An email or announcement written on Monday says "review is tomorrow". The
stored text still says that on Tuesday. This module re-expresses every
relative phrase against today's date.

Each text is anchored to the day it was WRITTEN (the email's sent date, the
announcement's posted date, else the day the row first appeared). A phrase is
resolved to the real date it meant on that day, then re-worded relative to
today. The original text is never overwritten: `retime_items` keeps it in
`item["retime"]` and recomputes from it each run, so the result never
compounds and is right on any day.

Handled: tomorrow, today, tonight, yesterday, this/next/last <weekday>,
this/yesterday/tomorrow morning|afternoon|evening, "in N days". Bare weekday
names ("Thursday") and week-level phrases ("this week") are left alone: they
stay true. A phrase followed by an explicit date ("Today - Wednesday 9/16")
is left alone too.
"""

import re
from datetime import date, datetime, timedelta

# `title` is left out on purpose: titles are the keys later emails match on.
FIELDS = ("detail", "description", "notes", "next_action")

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
         "sunday"]
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_EXPLICIT = (r"(?!\W{0,4}(?:(?:%s)\b|\d{1,2}/\d{1,2}|(?:%s)[a-z]*\.? \d))"
             % ("|".join(_DAYS), _MONTHS))
_PHRASE_RE = re.compile(
    r"\b(?P<phrase>"
    r"(?P<part>this|yesterday|tomorrow)\s+(?P<tod>morning|afternoon|evening)"
    r"|(?P<when>tomorrow|today|tonight|yesterday)"
    r"|(?P<mod>this|next|last)\s+(?P<day>%s)"
    r"|in\s+(?P<n>\d{1,2})\s+days?"
    r")\b%s" % ("|".join(_DAYS), _EXPLICIT), re.I)
# After one of these a date reads without "on": "by Friday", "until Monday".
_BARE_AFTER = {"by", "on", "until", "till", "before", "through", "from",
               "since", "after"}


def _weekday(d):
    return _DAYS[d.weekday()].capitalize()


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _day_phrase(d, today):
    """How to say date `d` on day `today`."""
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    if 2 <= n <= 6:
        if _week_start(d) == _week_start(today):
            return "this " + _weekday(d)
        if _week_start(d) == _week_start(today) + timedelta(days=7):
            return "next " + _weekday(d)
        return "on " + _weekday(d)
    if -6 <= n <= -2:
        return "last " + _weekday(d)
    return "on %s, %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day)


def _target(m, anchor):
    """(real date the phrase meant on its anchor day, part of day or None)."""
    g = m.groupdict()
    if g["part"]:
        shift = {"this": 0, "yesterday": -1, "tomorrow": 1}[g["part"].lower()]
        return anchor + timedelta(days=shift), g["tod"].lower()
    if g["when"]:
        word = g["when"].lower()
        shift = {"today": 0, "tonight": 0, "tomorrow": 1,
                 "yesterday": -1}[word]
        return anchor + timedelta(days=shift), \
            "night" if word == "tonight" else None
    if g["mod"]:
        idx = _DAYS.index(g["day"].lower())
        mod = g["mod"].lower()
        if mod == "last":
            return anchor - timedelta(
                days=(anchor.weekday() - idx) % 7 or 7), None
        d = _week_start(anchor) + timedelta(days=idx)
        if mod == "this":
            return (d if d >= anchor else d + timedelta(days=7)), None
        return d + timedelta(days=7), None  # "next <weekday>": following week
    return anchor + timedelta(days=int(g["n"])), None


def _say(d, tod, today):
    base = _day_phrase(d, today)
    if tod == "night":
        return {"today": "tonight", "yesterday": "last night",
                "tomorrow": "tomorrow night"}.get(base, base + " night")
    if tod:
        return "this " + tod if base == "today" else base + " " + tod
    return base


def retime(text, anchor, today):
    """`text` written on `anchor`, re-worded for `today`. Pure."""
    if not text or not anchor or anchor == today:
        return text

    def sub(m):
        d, tod = _target(m, anchor)
        out = _say(d, tod, today)
        before = text[:m.start()].split()
        if out.startswith("on ") and before and \
                before[-1].lower().strip(",;:()") in _BARE_AFTER:
            out = out[3:]
        phrase = m.group("phrase")
        if out.lower() == phrase.lower():
            return phrase
        return out[:1].upper() + out[1:] if phrase[:1].isupper() else out

    return _PHRASE_RE.sub(sub, text)


def as_day(value):
    """A date, datetime or ISO string -> date (None when unreadable)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def retime_items(items, today):
    """Re-word the text fields of every item against `today`, in place.
    Returns how many fields changed. Idempotent within a day."""
    changed = 0
    for it in items:
        rec = it.get("retime") or {}
        orig, last = dict(rec.get("orig") or {}), rec.get("last") or {}
        edited = False                       # text replaced since we saved
        for f in FIELDS:
            cur = it.get(f)
            if isinstance(cur, str) and cur and f in orig \
                    and cur != last.get(f):
                orig[f], edited = cur, True
        anchor = None if edited else as_day(rec.get("anchor"))
        anchor = anchor or as_day(it.get("text_anchor")) \
            or as_day(it.get("first_seen"))
        if edited and not as_day(it.get("text_anchor")):
            anchor = today                   # rewritten today, by someone
        if not anchor:
            continue
        out = {}
        for f in FIELDS:
            cur = it.get(f)
            if isinstance(cur, str) and cur:
                orig.setdefault(f, cur)
                out[f] = retime(orig[f], anchor, today)
        if not any(out[f] != orig[f] for f in out):
            it.pop("retime", None)
            continue
        it["retime"] = {"anchor": anchor.isoformat(), "orig": orig,
                        "last": out}
        for f, v in out.items():
            if it.get(f) != v:
                it[f] = v
                changed += 1
    return changed
