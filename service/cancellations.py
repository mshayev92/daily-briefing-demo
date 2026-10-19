"""Mark canceled classes/events as CANCELED on the calendar and in the DB.

A notification (email, announcement) that a class or event is canceled is
extracted with `cancellation: true` and the date of the canceled meeting.
This step finds the matching calendar event and renames it
"CANCELED: <title>" (gray, shown as free). It never deletes anything. The
matching DB rows get the same marker and `cancelled: true`.

The work is idempotent and retried: an item stays `cancel_pending` until its
calendar event was marked (the calendar-write token may not exist yet, or the
event may not be on the calendar yet), then becomes `calendar_marked`.
"""

import re
from datetime import datetime, time as _time, timedelta

PREFIX = "CANCELED: "
LOOKBACK_DAYS = 14
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "at", "class",
         "lecture", "section", "meeting", "canceled", "cancelled", "no"}


def is_marked(title):
    return str(title or "").upper().startswith(("CANCELED", "CANCELLED"))


def _tokens(text):
    text = re.sub(r"^\s*cancell?ed:?\s*", "", str(text or ""), flags=re.I)
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if w not in _STOP and len(w) > 1}


def _matches(item, event, course_label_of):
    """Does calendar `event` look like the thing `item` says was canceled?"""
    if event.get("all_day"):
        return False
    label = course_label_of(event)
    if item.get("course_label") not in (None, "", "UMD") and \
            label == item["course_label"]:
        # A class-calendar meeting of that course. A section keyword
        # ("discussion", "lab") in the item must also be in the title.
        want = _tokens(item.get("title")) & {"discussion", "lab", "recitation",
                                             "lecture", "seminar"}
        return not want or want <= _tokens(event.get("title"))
    a, b = _tokens(item.get("title")), _tokens(event.get("title"))
    return bool(a and b) and len(a & b) >= min(2, len(b))


def _day_bounds(day, tz):
    return (datetime.combine(day, _time.min, tzinfo=tz).isoformat(),
            datetime.combine(day, _time.max, tzinfo=tz).isoformat())


def mark_db_rows(items, cancel_item):
    """Prefix open rows for the same course/day that the notification is
    about. Never removes a row. Returns the ids changed."""
    out = []
    want = _tokens(cancel_item.get("title"))
    for it in items:
        if it is cancel_item or it.get("cancellation") or it.get("cancelled"):
            continue
        if it.get("date") != cancel_item.get("date") or \
                it.get("course_label") != cancel_item.get("course_label"):
            continue
        if it.get("kind") not in ("event", "assessment") or is_marked(
                it.get("title")):
            continue
        if len(want & _tokens(it.get("title"))) < 1:
            continue
        it["title"] = PREFIX + str(it.get("title") or "")
        it["cancelled"] = True
        out.append(it.get("id"))
    return out


def apply(items, today, tz, calendars, course_label_of, api, log):
    """Process every pending cancellation item. `api` supplies
    write_ok(), event_refs(cal_id, tmin, tmax), mark(cal_id, event_id).
    Returns {"marked": n, "pending": n}."""
    marked = pending = 0
    for it in items:
        if not it.get("cancellation") or it.get("calendar_marked"):
            continue
        try:
            day = datetime.strptime(str(it.get("date")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < today - timedelta(days=LOOKBACK_DAYS):
            continue
        it["cancelled"] = True
        mark_db_rows(items, it)
        if not api.write_ok():
            pending += 1
            it["cancel_pending"] = "calendar write token missing"
            continue
        tmin, tmax = _day_bounds(day, tz)
        hit, failed = 0, False
        for key in ("class", "personal"):
            cal = (calendars or {}).get(key)
            if not cal:
                continue
            try:
                refs = api.event_refs(cal, tmin, tmax)
                for ev in refs:
                    if not _matches(it, ev, course_label_of):
                        continue
                    if not (ev.get("canceled") or is_marked(ev.get("title"))):
                        api.mark(cal, ev["id"])
                    hit += 1           # marked now, or on an earlier run
            except Exception as exc:  # noqa: BLE001 -- retried next run
                failed = True
                log("could not mark %r canceled on the %s calendar: %s"
                    % (it.get("title"), key, exc))
        if hit and not failed:
            it["calendar_marked"] = today.isoformat()
            it.pop("cancel_pending", None)
            marked += 1
        else:
            pending += 1
            it["cancel_pending"] = "no matching calendar event yet" \
                if not failed else "calendar error"
    return {"marked": marked, "pending": pending}
