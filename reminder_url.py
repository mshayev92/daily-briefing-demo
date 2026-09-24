"""
reminder_url.py — build the "Add reminder" Google Calendar prefill URL.

Instructions §6.3 is the authority; this is that section as code. It exists
because the two failures §6.3 documents — reminders arriving as one run-on
line, and a registration link that was in the source never reaching the event
— are both mechanical, and a mechanical failure deserves a mechanical fix
rather than another paragraph telling a run to remember.

The `dates` shape table is the subtle part: a deadline is not a meeting, and a
missing end time must never be silently invented into a fact.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
BASE = "https://calendar.google.com/calendar/render?action=TEMPLATE"
DETAILS_CAP = 1200


def _day(d):
    """`Tue, Sep 8`. Written out rather than via strftime %-d, which is a glibc
    extension: the suite and the pipeline both fail on macOS with %-d."""
    return "%s, %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day)


def _clock(t):
    """`4:00 PM`. Same reason — %-I is not portable."""
    return "%d:%02d %s" % (t.hour % 12 or 12, t.minute,
                           "AM" if t.hour < 12 else "PM")


def _utc(d, t):
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _https(url):
    return isinstance(url, str) and url.startswith("https://")


def _is_essential(line, assumptions):
    """Lines that must survive truncation (§6.3).

    Links are the least replaceable part; `When:` is the only place a deadline's
    real time or an assumed end time survives; an assumption line is a required
    disclosure, not commentary.
    """
    # A LABELLED link line — "Register: https://…" — is essential. A prose
    # line that merely quotes a URL is not, and matching a bare ": https://"
    # anywhere in the line made any such sentence undroppable, which spends the
    # budget on commentary instead of on the instructions block.
    return (line.startswith(("When:", "Where:", "Organizer:", "Source:",
                             "Added from"))
            or re.match(r"^[A-Za-z][A-Za-z ]{0,20}: https://", line) is not None
            or line in assumptions)


def _fit(details, assumptions):
    """Trim `details` to DETAILS_CAP, dropping prose from the END first.

    §6.3: "truncating at a line boundary … drop prose from the instructions
    block first — never drop the links." The previous implementation discarded
    every prose line at once and then unconditionally dropped the final line
    with `rsplit`, so a 1200-character reminder collapsed to ~116 characters and
    lost its summary line and its provenance line while leaving ~1080
    characters of budget unused. This removes one prose line at a time, keeps
    the essentials, and only hard-truncates if the essentials alone overflow.
    """
    if len(details) <= DETAILS_CAP:
        return details

    def joined(ls):
        return re.sub(r"\n{3,}", "\n\n", "\n".join(l for l in ls if l is not None)).strip()

    keep = details.split("\n")
    for i in range(len(keep) - 1, -1, -1):
        if len(joined(keep)) <= DETAILS_CAP:
            break
        if keep[i] is None or _is_essential(keep[i], assumptions):
            continue
        # Prefer trimming this prose line to dropping it: a note cut short is
        # more use than a note that vanished, and dropping a single long line
        # can leave most of the budget unspent.
        room = DETAILS_CAP - len(joined(keep[:i] + [None] + keep[i + 1:]))
        line = keep[i]
        if room >= 80 and len(line) > room:
            cut = line[:room - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
            keep[i] = (cut + "…") if cut else None
        else:
            keep[i] = None

    out = joined(keep)
    if len(out) > DETAILS_CAP:
        # Even the essentials overflow: now, and only now, cut at a line boundary.
        out = out[:DETAILS_CAP].rsplit("\n", 1)[0]
    return out


def build_reminder_url(item, generated_on, source_label=None):
    """item: the items[] dict (§3.3). Returns (url, assumptions[])."""
    assumptions = []
    date = item["date"] if isinstance(item["date"], str) else item["date"].isoformat()
    d = datetime.strptime(date, "%Y-%m-%d").date()
    end_d = d
    if item.get("end_date"):
        end_d = datetime.strptime(item["end_date"], "%Y-%m-%d").date()

    kind = item.get("kind")
    time_s, end_s = item.get("time"), item.get("end_time")
    is_deadline = kind in ("assignment", "assessment")

    if time_s and end_s and not is_deadline:
        t0 = datetime.strptime(time_s, "%H:%M").time()
        t1 = datetime.strptime(end_s, "%H:%M").time()
        dates = "%s/%s" % (_utc(d, t0), _utc(end_d, t1))
        when = "%s, %s–%s ET" % (_day(d), _clock(t0), _clock(t1))
    elif time_s and not is_deadline:
        t0 = datetime.strptime(time_s, "%H:%M").time()
        start = datetime.combine(d, t0, tzinfo=ET)
        fin = start + timedelta(hours=1)
        dates = "%s/%s" % (start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
                           fin.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        when = "%s, %s ET" % (_day(d), _clock(t0))
        assumptions.append("End time not stated — 1 hour assumed.")
    else:
        # A deadline with a time is ALL-DAY, with the real time preserved in
        # the details. A deadline is not a meeting (§6.3).
        dates = "%s/%s" % (d.strftime("%Y%m%d"),
                           (end_d + timedelta(days=1)).strftime("%Y%m%d"))
        span = _day(d) if end_d == d else "%s – %s" % (_day(d), _day(end_d))
        if time_s:
            t0 = datetime.strptime(time_s, "%H:%M").time()
            when = "Due %s, %s ET" % (span, _clock(t0))
        else:
            when = span

    title = item["title"]
    label = item.get("course") or ""
    text = ("%s — %s" % (label, title)) if label else title

    lines = []
    head = item.get("detail") or ""
    lines.append("%s%s" % ((item.get("course_label") or "") + " · " if item.get("course_label") else "", head))
    lines.append("")

    link_lines = []
    for l in item.get("links") or []:
        if _https(l.get("url")):
            link_lines.append("%s: %s" % (l.get("label") or "Link", l["url"]))
    if _https(item.get("canvas_url")):
        link_lines.append("Canvas: %s" % item["canvas_url"])
    # Two links with the same URL collapse to one.
    seen, uniq = set(), []
    for l in link_lines:
        u = l.split(": ", 1)[-1]
        if u not in seen:
            seen.add(u)
            uniq.append(l)
    if uniq:
        lines += uniq + [""]

    body = []
    if item.get("notes"):
        # One list element per LINE. Appending the whole block as a single
        # element put it back beyond the reach of the one-line-at-a-time trim
        # below — _fit could only drop the entire note or none of it, which is
        # the exact all-or-nothing behaviour §6.3's truncation order replaced.
        body += [l for l in str(item["notes"]).split("\n") if l.strip()]
    for a in assumptions:
        body.append(a)
    if body:
        lines += body + [""]

    lines.append("When: %s" % when)
    if item.get("location"):
        lines.append("Where: %s" % item["location"])
    if item.get("organizer"):
        lines.append("Organizer: %s" % item["organizer"])
    if source_label:
        lines.append("Source: %s" % source_label)
    lines.append("Added from your Daily Briefing, %s" % _day(generated_on.date()
                 if hasattr(generated_on, "date") else generated_on))

    details = _fit(("\n".join(lines)).strip(), assumptions)

    params = {"text": text, "dates": dates, "details": details}
    loc = item.get("location")
    join = next((l["url"] for l in (item.get("links") or [])
                 if (l.get("label") or "").lower() == "join" and _https(l.get("url"))), None)
    if join:
        params["location"] = join          # calendar clients surface this as the join target
    elif loc:
        params["location"] = loc

    return BASE + "&" + urlencode(params), assumptions
