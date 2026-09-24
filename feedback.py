"""Thumbs up / down on a suggested row, and what a later run does with it.

Why this exists
---------------
Campus events, leads and opportunities are *suggestions* — the pipeline ranks
them by keyword overlap and obscurity and has no idea whether any of it landed.
Done and Resolved say "this is finished", not "this was worth showing". Nothing
in the system carried the difference until now.

What a rating is, and is not
----------------------------
A rating is the USER's data, written client-side to the artifact db at
`feedback/<item-id>`. **The pipeline reads it and never writes it** — the same
rule §1.1 applies to `prefs/quiz`, `prefs/custom` and `prefs/requirements`, for
the same reason: a run that can edit the record can overwrite something he
meant, with no trace.

Ratings generalise by FAMILY, not by item
-----------------------------------------
Rating one instance of "Résumé Lab drop-in" down should affect the next one,
and item ids are per-instance (§3.3). So a rating is filed under the item's
family — the campus-calendar slug family where there is one, otherwise the
ledger's id family — and under its organizer. `weight()` then scores an
unseen event by what its family and organizer have earned before.

Two deliberate limits
---------------------
**A single thumbs-down does not suppress anything.** `MIN_DOWN` requires a
second one before a family is demoted at all, because one bad instance of a
recurring series is ordinary and the reader clicking down once is not asking
for the series to disappear.

**A demoted event sorts to the bottom of its own section, not off the page.**
`weight()` returns a bounded score adjustment, and `demoted()` marks a family
for the last rows of "Worth your time" (§5.3b) via
`lifecycle.order_campus(demoted_ids=...)`.

It used to drop into `order_coming_up()`'s `secondary`, and that route is now
a bug rather than a policy: campus events left Coming up on 2026-09-10, so a
demoted one landing in that section's disclosure list is a `Campus` row in
`#section-coming-up` — which gate check 27 fails, blocking publication the
first time any family earned its second thumbs-down. The demotion had to move
to the section the events actually render in.

With three rows and no disclosure toggle, a demoted event can be pushed out of
the section entirely, into `held_back`. That is narrower than "nothing is ever
suppressed outright" and is stated here rather than quietly enjoyed: what
preserves the intent is that the reader is told — `held_back` is counted in the
footer and `summary()` names the family whose ranking he changed. Silently
withholding a plausible thing is the defect the 2026-09-10 Coming-up change
removed; a reported count is not that.
"""

UP = "up"
DOWN = "down"
VALID = (UP, DOWN)

# A second down before a family is demoted at all (see the docstring).
MIN_DOWN = 2
# Score adjustment ceiling, in the same units as umd_calendar's score. Bounded
# so feedback tilts the ranking and never dominates the keyword and obscurity
# signals that put an event in front of the reader for a stated reason.
MAX_WEIGHT = 3.0
PER_UP = 1.0
PER_DOWN = 1.5          # a down is worth more: it costs the reader attention


def family_of(item):
    """The key a rating generalises over. None when there is nothing stable.

    Campus events carry a calendar slug family; everything else falls back to
    the ledger's id family, which strips the trailing date. Both are already
    the project's notion of "the same recurring thing".
    """
    for ref in (item.get("source_refs") or ()):
        if not isinstance(ref, dict):
            continue
        url = ref.get("url") or ""
        if "calendar.umd.edu" in url:
            try:
                import umd_calendar
                fam = umd_calendar.slug_family(url)
            except Exception:
                fam = None
            if fam:
                return "campus:" + fam
    iid = item.get("id")
    if not iid:
        return None
    try:
        import ledger
        fam = ledger._family(str(iid))
    except Exception:
        fam = None
    return ("item:" + fam) if fam else None


def organizer_of(item):
    org = (item.get("organizer") or "").strip().lower()
    return ("org:" + org) if org else None


def aggregate(docs):
    """`{doc_id: {rating, family, organizer}}` -> tallies per key.

    `docs` is what the db `feedback` collection returned. A doc whose rating is
    not in VALID is ignored rather than guessed at: the store is user-writable
    and a malformed value is not a licence to invent an opinion.
    """
    out = {}
    for body in (docs or {}).values():
        if not isinstance(body, dict):
            continue
        rating = body.get("rating")
        if rating not in VALID:
            continue
        for key in (body.get("family"), body.get("organizer")):
            if not key:
                continue
            tally = out.setdefault(key, {UP: 0, DOWN: 0})
            tally[rating] += 1
    return out


def weight(item, tallies):
    """Bounded score adjustment for one candidate. 0.0 when nothing is known.

    Positive promotes, negative demotes. Never returns anything that could
    remove an item — see the docstring's second limit.
    """
    if not tallies:
        return 0.0
    total = 0.0
    for key in (family_of(item), organizer_of(item)):
        t = tallies.get(key)
        if not t:
            continue
        total += PER_UP * t[UP]
        if t[DOWN] >= MIN_DOWN:
            total -= PER_DOWN * t[DOWN]
    return max(-MAX_WEIGHT, min(MAX_WEIGHT, total))


def demoted(item, tallies):
    """True when this candidate belongs at the BOTTOM of Worth your time
    rather than in the recommended rows — pass the ids to
    `lifecycle.order_campus(demoted_ids=...)` (§5.3b). Never `secondary`:
    see the module docstring."""
    return weight(item, tallies) <= -PER_DOWN * MIN_DOWN


def summary(tallies):
    """One line for the briefing footer, or "". Says what was learned, so a
    ranking change the reader caused is never invisible to him."""
    fams = [(k, v) for k, v in (tallies or {}).items()
            if k.startswith(("campus:", "item:"))]
    ups = sum(v[UP] for _, v in fams)
    downs = sum(v[DOWN] for _, v in fams)
    if not (ups or downs):
        return ""
    bits = []
    if ups:
        bits.append("%d rated useful" % ups)
    if downs:
        bits.append("%d rated not useful" % downs)
    return " · ".join(bits)
