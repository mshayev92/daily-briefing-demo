"""STEP 4's timeline arithmetic and STEP 5's umbrella window.

The timeline is fed by TWO calendars
------------------------------------
`Class` and the personal calendar ("<your name>"), merged and sorted
together — see PROJECT_INSTRUCTIONS.md 2.2.

**Personal events are timeline-only.** They never become `items[]` entries, so
they cannot reach Assignments, Assessments, Coming up or Needs your attention;
§5.1 already routes `kind: "personal"` to *not rendered* and this is the same
rule from the other end. They also get no Add-reminder link (already on a
calendar), no AI actions and no rating.

**They DO count for gaps, and that is the point of including them.** A run that
computed free time from classes alone would tell the reader he has "2 hours 10
minutes free" during an appointment. That is not a display nicety, it is a
false statement about his day, which is what §1.6 exists to prevent. The
Tight Transition count follows for the same reason: two back-to-back
commitments are pressure whether or not both are lectures.

Why this module exists
----------------------
§5.5 ends the gap rule with "get the arithmetic right, and never invent a
location for a transition that has none." A rule that has to ask is a rule
that belongs in code. Both computations here are subtraction and a threshold,
done at the point in a long run where `duration_seconds` historically came
back null.

The location rule is enforced structurally rather than requested: `gap_label()`
can only name buildings that were passed in from the events' own `location`
fields, so there is nothing to invent. A transition with a missing location on
either side gets the duration and no names.
"""

from datetime import datetime, time, timedelta

TIGHT_MINUTES = 20            # §5.5 — under this is "tight"
TIGHT_ALERT_MIN = 3           # 3+ tight gaps raises the banner
POP_THRESHOLD = 40            # STEP 5 — below this there is no umbrella note
DAYLIGHT_START = time(7, 0)   # 7 AM–9 PM when there are no events
DAYLIGHT_END = time(21, 0)


def _dt(value):
    """ISO datetime → datetime, or None. Tolerates a trailing Z."""
    if isinstance(value, datetime):
        return value
    s = str(value or "").strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _clean(events):
    """Timed events with parseable start and end, in time order.

    Carries `course_class` / `course_label` straight through, because the
    timeline is now fed by TWO calendars — `Class` and the personal one — and
    the pip colour plus the row tag are how the reader tells them apart. The
    template already accepts `personal` / `Personal`, so nothing downstream
    needed changing for this.

    An all-day event is dropped: it has no position on a timeline of a single
    day, and rendering it at midnight would invent a time and manufacture a
    nine-hour "gap" before the first class.
    """
    out = []
    for e in events or ():
        if e.get("all_day"):
            continue
        start, end = _dt(e.get("start")), _dt(e.get("end"))
        if start and end:
            out.append({"start": start, "end": end,
                        "title": e.get("title") or "",
                        "location": (e.get("location") or "").strip(),
                        "course_class": e.get("course_class") or "none",
                        "course_label": e.get("course_label") or "UMD"})
    out.sort(key=lambda e: e["start"])
    return out


def gaps(events):
    """Gaps between consecutive events. Returns a list of dicts.

    Each carries `minutes`, `tight`, the two locations (possibly empty) and the
    window, so callers never recompute a duration from a label.
    """
    evs = _clean(events)
    out = []
    for a, b in zip(evs, evs[1:]):
        minutes = int((b["start"] - a["end"]).total_seconds() // 60)
        if minutes < 0:
            # Overlapping calendar entries. Not a gap; reporting a negative
            # one as "free time" would be worse than omitting it.
            continue
        out.append({
            "minutes": minutes,
            "tight": minutes < TIGHT_MINUTES,
            "from_location": a["location"],
            "to_location": b["location"],
            "from_title": a["title"],
            "to_title": b["title"],
            "start": a["end"],
            "end": b["start"],
        })
    return out


def gap_label(gap):
    """7f's `{{GAP_LABEL}}`: `25 minutes free` · `15 minutes, Tydings to Key Hall`.

    Names buildings only when BOTH sides have one in their own location field
    (§5.5). This is why the rule cannot be violated here: an absent location is
    an absent string, and there is no branch that substitutes anything.
    """
    minutes = gap["minutes"]
    if not gap["tight"]:
        return "%d minutes free" % minutes
    a, b = gap.get("from_location"), gap.get("to_location")
    if a and b and a != b:
        return "%d minutes, %s to %s" % (minutes, a, b)
    return "%d minutes" % minutes


def tight_transition_alert(events):
    """§5.5 — count today's tight gaps; 3+ returns the count, else None."""
    n = sum(1 for g in gaps(events) if g["tight"])
    return n if n >= TIGHT_ALERT_MIN else None


def timeline(events):
    """Today's events interleaved with labeled gaps, in order.

    The shape STEP 4 describes: events in time order, each gap labeled between
    them. Returned as data so the composer never does the arithmetic.
    """
    evs = _clean(events)
    if not evs:
        return []
    gs = gaps(events)
    out, gi = [], 0
    for i, e in enumerate(evs):
        out.append({"type": "event", "title": e["title"], "start": e["start"],
                    "end": e["end"], "location": e["location"],
                    "course_class": e["course_class"],
                    "course_label": e["course_label"]})
        if i < len(evs) - 1 and gi < len(gs):
            g = gs[gi]; gi += 1
            out.append({"type": "gap", "label": gap_label(g), **g})
    return out


def free_windows(events, day_start=DAYLIGHT_START, day_end=DAYLIGHT_END):
    """The windows the umbrella note may name.

    With events: the gaps between them (STEP 5's "actual gap between today's
    Class events"). Without events: the whole daylight span, because a rainy
    Saturday still deserves the warning.
    """
    evs = _clean(events)
    if not evs:
        return [{"start": day_start, "end": day_end, "kind": "daylight"}]
    return [{"start": g["start"].time(), "end": g["end"].time(),
             "kind": "gap", "after": g["from_title"]} for g in gaps(events)]


def umbrella(hourly, events, threshold=POP_THRESHOLD):
    """STEP 5 — the umbrella note, or None.

    `hourly` is [{"hour": datetime|int, "pop": int}] from the AccuWeather
    hourly call. Returns {"pop","start_hour","end_hour","after","text"} or
    None, and None means the renderer deletes `div.weather` entirely.

    Below the threshold this returns None rather than a reassuring string:
    §1.6 — an absent umbrella note is not a claim that it will not rain, and a
    note invented from stale state would be a fabrication about a future hour.
    """
    readings = []
    for h in hourly or ():
        pop = h.get("pop")
        raw = h.get("hour")
        hour = raw.hour if hasattr(raw, "hour") else (
            int(raw) if str(raw).isdigit() else None)
        if pop is None or hour is None:
            continue
        readings.append((hour, int(pop)))
    if not readings:
        return None

    best = None
    for window in free_windows(events):
        lo, hi = window["start"].hour, window["end"].hour
        inside = [(h, p) for h, p in readings if lo <= h <= hi
                  and p >= threshold]
        if not inside:
            continue
        peak = max(p for _, p in inside)
        span = (min(h for h, _ in inside), max(h for h, _ in inside))
        cand = {"pop": peak, "start_hour": span[0], "end_hour": span[1],
                "after": window.get("after"), "kind": window["kind"]}
        # Earliest qualifying window wins: the note exists to change a
        # decision made on the way out of the door.
        if best is None or cand["start_hour"] < best["start_hour"]:
            best = cand
    if best is None:
        return None
    best["text"] = umbrella_text(best)
    return best


def _clock(hour):
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12 or 12
    return "%d %s" % (h, suffix)


def umbrella_text(note):
    """`Umbrella: 60% chance between 4 and 6 PM, in your gap after PHIL001.`"""
    start, end = note["start_hour"], note["end_hour"]
    if start == end:
        window = "around %s" % _clock(start)
    elif (start < 12) == (end < 12):
        window = "between %d and %s" % (start % 12 or 12, _clock(end))
    else:
        window = "between %s and %s" % (_clock(start), _clock(end))
    text = "Umbrella: %d%% chance %s" % (note["pop"], window)
    if note.get("after"):
        text += ", in your gap after %s" % note["after"]
    return text + "."
