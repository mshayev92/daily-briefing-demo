"""STEP 6's maintenance, §5.1's routing, §5.8's grouping, §5.3's ordering.

Why this module exists
----------------------
All of it was prose a run had to execute by hand at the end of a 25-minute
session: date comparisons, counters, a first-match-wins table, and a substep
order that §5.9 spends a paragraph insisting on. None of it is a judgment call,
and `duration_seconds` already showed what happens to arithmetic in that
position — it came back null on three of the first five v10-era runs.

**What stays with the model.** Two things here are genuinely not mechanical,
and this module takes them as *inputs* rather than pretending to decide them:

1. `needs_attention` — §5.1's row about "an unresolved conflict, an error, an
   unclassifiable thread, or any other decision only Michael can make." A run
   sets that flag; `section_for()` then routes it. Code cannot recognise a
   novel conflict, and a heuristic that tried would either miss real ones or
   flood the section.
2. `evidence` — §5.10's four tiers. Assigned at 7-0 with the user's
   preferences and Custom Instructions in hand.

Everything else below is derivable from `kind`, `status`, `date`,
`times_surfaced` and today's date, so it is derived.

The substep order is not configurable
-------------------------------------
`run()` executes sync → overdue → expire → auto-dismiss → prune in that fixed
sequence, because §5.9 makes it a correctness property rather than a
preference: flag before the sync and something already marked done is reported
missed; expire before the flag and a deadline four days past — a weekend plus a
holiday — is expired before it is ever surfaced. Exposing an order parameter
would be re-opening a decision that is already settled.
"""

import re
from datetime import date, timedelta

import opportunity
import render_briefing

# §5.1 / §4.2 windows, in days from today.
ASSIGNMENT_WINDOW = 6
ASSESSMENT_WINDOW = 20
HORIZON = 30

# §5.1, added 2026-09-16: the five real courses, as opposed to advising/UMD/
# personal/untied. Derived from render_briefing.COURSE_PAIRS rather than
# listed again here, so this module and the gate's course-pairing check can
# never name a different five classes without one of them failing loudly.
# render_briefing.py has no imports of its own (checked before adding this),
# so importing it here carries no cycle risk.
MY_CLASS_CLASSES = set(render_briefing.COURSE_PAIRS) - {
    "advising", "umd", "personal", "none"}

EXPIRE_AFTER_DAYS = 3          # §3.4
OPPORTUNITY_UNDATED_DAYS = 21  # an opportunity with no stated deadline
AUTO_DISMISS_AT = 5            # §5.7
CONFIG_SNOOZE_DAYS = 14        # §5.7 exception 2
COMPACT_AFTER_DAYS = 7         # §3.1 bounds
DELETE_AFTER_DAYS = 35
GROUP_MIN = 3                  # §5.8

# --- Parked feature: fixed UMD academic dates (§7c) ------------------------
# Flipped off 2026-09-10 at Michael's request. `umd_dates.py`, the registrar
# URL in §2, §5.3's block 3 and the routing branch below are all left intact so
# re-enabling is this one line plus restoring STEP 7c (RATIONALE_LOG.md carries
# the removed spec verbatim).
#
# This gate is deliberately in the ROUTER, not only in the fetch. Turning off
# the refresh alone would have left the three `umd_deadline` items already in
# state — all dated 2026-09-14, all `new` — rendering for another four days
# from a source nothing was refreshing. A parked feature that keeps publishing
# stale rows is worse than one that was never built.
UMD_DEADLINES_ENABLED = False
COMING_UP_MAX = 10             # §5.3
CAMPUS_MAX = 3                 # §5.3b — the whole "Worth your time" section
# §5.1c, added 2026-09-16: default cap on Needs your attention's PRIMARY rows,
# same default-fallback role as the two above -- `service/orchestrator.py`
# reads a per-run value out of the quiz (`attention_max`) and passes it to
# order_attention()'s `cap`; this only applies when nothing overrides it.
ATTENTION_MAX = 8

# §5.5 — Heavy Day fires on LOAD, not on a row count. `HEAVY_DAY_MIN = 3` used
# to mean "3 or more items share a date", which counted a 10-minute discussion
# reply and a MATH001 midterm as the same thing: three participation posts
# raised the banner, and a midterm plus a problem set did not. Measured against
# the 2026-09-09 state, the count rule fired on the wrong day.
ITEM_WEIGHT = {"assessment": 3.0, "assignment": 1.0}
RECURRING_WEIGHT = 0.5         # a numbered/dated member of a recurring run
DEFAULT_WEIGHT = 1.0
HEAVY_DAY_LOAD = 4.0

# §5.6 — a row re-expands to full detail as its date closes in, whatever its
# surface count. Binary anti-repetition made an assessment verbose 20 days out
# and a single line the night before, which is the wrong way round.
REEXPAND_WITHIN = {"assignment": 1, "assessment": 3}

TERMINAL = ("handled", "dismissed", "expired")
OPEN = ("new", "ongoing", "unresolved", "snoozed")
ATTENTION_KINDS = ("assignment", "assessment")   # §5.9 scope


def as_date(value):
    """ISO date prefix → date, or None. Never raises: a malformed date is a
    data problem for the caller to report, not a crash mid-maintenance."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _days(value, today):
    d = as_date(value)
    return None if d is None else (d - today).days


# --- STEP 6 substeps --------------------------------------------------------

def release_snoozes(items, today):
    """6.2 — a snooze whose date has passed returns as `new` at full detail.

    §3.4: `snoozed → new` is the only transition that reopens anything, and
    the reset of `times_surfaced` is what "full detail regardless" means.
    """
    changed = []
    for it in items:
        if it.get("status") != "snoozed":
            continue
        until = as_date(it.get("snooze_until"))
        if until is not None and until <= today:
            it["status"] = "new"
            it["times_surfaced"] = 0
            it.pop("snooze_until", None)
            changed.append(it.get("id"))
    return changed


def sync_canvas_completion(items):
    """6.1b (2026-09-19 audit) -- Canvas's own submission record closes an
    item, the same way a Done click does.

    Before this, an assignment submitted on Canvas stayed `new`/`ongoing`
    until its date passed, then was flagged "may have been missed" -- a
    false alarm about work Canvas already holds. `canvas_submission` is set
    only on canvas-scraper-sourced items (canvas_shadow.
    canvas_submission_summary()), so an email- or calendar-only item is
    never touched here.
    """
    closed = []
    for it in items:
        if it.get("status") not in OPEN:
            continue
        sub = it.get("canvas_submission") or {}
        if not sub.get("settled"):
            continue
        it["status"] = "handled"
        it["handled_by"] = "canvas: %s" % (sub.get("state") or "submitted")
        closed.append(it.get("id"))
    return closed


# Canvas submission types that mean "nothing to hand in online": a
# no-submission participation grade, or an on-paper, in-class quiz. A passed
# date on one of these is not something the student can still act on.
_IN_PERSON_SUBMISSION_TYPES = frozenset(("none", "on_paper", "not_graded"))


def overdue_is_actionable(item):
    """Would "may have been missed" be TRUE and USEFUL for this item?

    The 2026-09-19 audit found every row in the real Needs-your-attention
    list was a false alarm: an in-class paper quiz, a no-submission
    participation grade, and a 0-point optional project. Checked in order:

    1. Canvas says `missing` -- always actionable (Canvas's own verdict).
    2. Canvas holds a submission/grade -- never (sync_canvas_completion()
       normally closes these first; this is the belt to its braces).
    3. Worth 0 points -- an optional item cannot be "missed".
    4. Canvas says there is nothing to submit (`none`/`on_paper`) -- it
       happened in class; nothing to catch up on after the fact.
    5. An assessment with no Canvas verdict -- a quiz/exam is taken in the
       room; the pipeline cannot know whether he attended, and flagging
       every past quiz every week is noise, not signal.
    6. Otherwise (an ordinary assignment with an online submission, or no
       Canvas record at all) -- actionable, as before.
    """
    sub = item.get("canvas_submission") or {}
    if sub.get("missing"):
        return True
    if sub.get("settled"):
        return False
    pts = item.get("points_possible")
    if isinstance(pts, (int, float)) and pts == 0:
        return False
    types = item.get("submission_types")
    if isinstance(types, (list, tuple)) and types and \
            set(types) <= _IN_PERSON_SUBMISSION_TYPES:
        return False
    if item.get("kind") == "assessment":
        return False
    return True


def flag_overdue(items, today):
    """6.3 / §5.9 — a due date that passed while the item was still open.

    Fires once, guarded by `overdue_flagged`. Scoped to assignment/assessment:
    an event you did not attend is not actionable afterwards.

    **Flagging resets `times_surfaced` to 0** (2026-09-11). The count it
    carries was accrued as an ordinary coursework row — "PHIL001 quiz, Friday"
    in Assessments — and what it becomes here is a different row in a different
    section saying a different thing: the date passed and no completion was
    seen. Left alone, the two most-surfaced rows on the 2026-09-11 state were
    flagged and auto-dismissed inside the same STEP 6, so §5.9's "surfaced once
    in Needs your attention" would have been satisfied by a row nobody ever
    saw. This is §5.7 exception 1 — the issue evolved — applied at the moment
    it evolves, and it also restores full detail under §5.6, which is right for
    a row whose meaning just changed.
    """
    flagged = []
    for it in items:
        if it.get("kind") not in ATTENTION_KINDS:
            continue
        if it.get("status") not in ("new", "ongoing"):
            continue
        if it.get("overdue_flagged"):
            continue
        d = _days(it.get("date"), today)
        if d is not None and d < 0:
            if not overdue_is_actionable(it):
                # Not a miss worth a row; it retires on its date instead,
                # like any other past coursework (expire()).
                continue
            it["status"] = "unresolved"
            it["overdue_flagged"] = True
            it["needs_attention"] = True
            it["times_surfaced"] = 0
            flagged.append(it.get("id"))
    return flagged


def retract_overdue(items):
    """6.3b -- withdraw an earlier "may have been missed" flag that
    overdue_is_actionable() now says was never true (the item was submitted
    on Canvas after all, or is an in-class/optional/0-point one). Expired
    rather than dismissed: the pipeline, not Michael, closed it."""
    retracted = []
    for it in items:
        if not it.get("overdue_flagged") or it.get("status") != "unresolved":
            continue
        if overdue_is_actionable(it):
            continue
        it["status"] = "expired"
        it["expired_reason"] = "overdue flag withdrawn: nothing to act on"
        retracted.append(it.get("id"))
    return retracted


def record_surfaced(items, rendered_ids, today):
    """7f bookkeeping (§5.6/§5.7/§14.3): `times_surfaced` +1 and `last_shown`
    = today for every item the published page actually showed.

    Nothing in the orchestrator did this after the move off the interactive
    pipeline (found in the 2026-09-19 audit), which silently disabled every
    rule keyed to the counter: auto-dismiss of attention rows at 5 surfaces,
    lead aging at 3, and the `New` chip (every row stayed "new" forever).
    Counted at most once per calendar day, so a repeat run the same morning
    does not age anything twice.
    """
    ids = set(rendered_ids or ())
    stamp = today.isoformat()
    bumped = []
    for it in items:
        if it.get("id") not in ids:
            continue
        # A lead minted this morning already carries last_shown=today
        # (opportunity.to_item()) with a zero count -- that first showing
        # still counts.
        if it.get("last_shown") == stamp and int(
                it.get("times_surfaced") or 0) > 0:
            continue
        it["times_surfaced"] = int(it.get("times_surfaced") or 0) + 1
        it["last_shown"] = stamp
        bumped.append(it.get("id"))
    return bumped


def expire(items, today):
    """6.4 / §3.4 — more than 3 days past date while still open.

    **Skips anything rendering in Needs your attention.** §3.4 is explicit:
    expiring an attention item on its date deletes the row precisely when it
    matters, after ~3 surfaces rather than the 5 §5.7 intends. Those age out on
    the surface counter only.

    **Also skips leads** (§14.3/§14.4). A lead's `date` field is its `act_by`
    or `deadline` if it has either — usually neither, since most search-found
    leads carry no known date at all — so this function's `date`-based rule
    is the wrong model for it either way. `age_leads()` below is the single
    place a lead retires, on ITS OWN two rules (its own act_by passed, or
    LEAD_MAX_SURFACES reached), never this one.
    """
    expired = []
    for it in items:
        if it.get("status") not in OPEN:
            continue
        if in_attention(it):
            continue
        if it.get("regime") == "lead":
            continue
        d = _days(it.get("date"), today)
        if d is not None and d < -EXPIRE_AFTER_DAYS:
            it["status"] = "expired"
            expired.append(it.get("id"))
        elif d is not None and d < 0 and it.get("kind") == "opportunity":
            # A closed application is over the day after its deadline.
            it["status"] = "expired"
            it["expired_reason"] = "deadline passed"
            expired.append(it.get("id"))
        elif d is None and it.get("kind") == "opportunity":
            seen = _days(it.get("first_seen"), today)
            if seen is not None and seen < -OPPORTUNITY_UNDATED_DAYS:
                it["status"] = "expired"
                it["expired_reason"] = "no deadline, %d days old" % (-seen)
                expired.append(it.get("id"))
    return expired


_LEAD_TERMINAL = TERMINAL + ("confirmed", "killed")


def age_leads(items, today):
    """6.4b — §14.3: a lead is never `in_attention()` (so `auto_dismiss()`
    never reaches it) and `expire()` now skips it by regime too, so nothing
    else ever retires an unconfirmed lead — without this it would sit in
    `items[]` forever, since `compact_and_prune()`'s own delete rule also
    needs a `date` or a terminal status neither an open, dateless lead has.

    `opportunity.age_leads()` is the single source of truth for when a lead
    goes stale (its own act_by passed, or LEAD_MAX_SURFACES=3 reached — §14.3
    is deliberately tighter than §5.7's 5). It returns COPIES rather than
    mutating in place, unlike every other function in this file, so this
    just writes its verdict back onto the SAME dicts `run()` mutates
    everywhere else.
    """
    leads = [it for it in items if it.get("regime") == "lead"
             and it.get("status") not in _LEAD_TERMINAL]
    if not leads:
        return []
    aged = {L["id"]: L for L in opportunity.age_leads(leads, today)}
    expired = []
    for it in items:
        L = aged.get(it.get("id"))
        if L and L.get("status") == "expired" and it.get("status") != "expired":
            it["status"] = "expired"
            it["expired_reason"] = L.get("expired_reason")
            expired.append(it["id"])
    return expired


def auto_dismiss(items, today, changed_ids=()):
    """6.5 / §5.7 — an ATTENTION item, `times_surfaced >= 5`, still open.

    Both exceptions are implemented, and the second one matters more than it
    looks: a `System` row about an unfixed configuration gap gets snoozed 14
    days instead of dismissed, so it keeps coming back until the config is
    actually fixed. A config row is only finished when the config is.

    **Scope is attention rows only** (fixed 2026-09-11). This used to walk
    every open item, which is not what §5.7 says in its title, its body or
    prompt step 6.5 — and the difference is not academic. §3.4 deliberately
    exempts attention rows from date-expiry *because* this counter is the only
    thing that retires them; an ordinary coursework row has a date and expires
    on it, so applying the counter to it as well means the row dies for having
    been READ five times rather than for being over. On the 2026-09-11 state
    that was 15 still-live rows, including a MATH001 midterm two weeks out and
    three PHIL001 quizzes — all still open, all still dated in the future. The
    run that found it reproduced §5.7's documented scope by hand rather than
    calling this function. `in_attention()` is the same predicate `expire()`
    uses, so the two halves of "expires by date XOR expires by counter" now
    read from one definition and cannot drift apart.

    **`unresolved` is in the status set** (2026-09-11). §5.7 listed only `new`
    and `ongoing`, which left §5.9's overdue rows retired by nothing at all:
    `expire()` skips them by §3.4, this counter did not see them, and the only
    thing that eventually removed one was the 35-day delete in
    `compact_and_prune()`. On the 2026-09-11 state that was two rows — a
    MATH001 quiz and a PHIL001 post, both marked overdue on 2026-09-10, at 6
    and 9 surfaces — on course to lead "Needs your attention" every morning
    until mid-October. §5.9 and §3.4 both say plainly that an attention row
    ages out on this counter; §5.7's status list was the only text that
    disagreed, and it has been corrected to match.
    """
    out = {"dismissed": [], "reset": [], "snoozed": []}
    changed = set(changed_ids or ())
    for it in items:
        if it.get("status") not in ("new", "ongoing", "unresolved"):
            continue
        if not in_attention(it):
            continue
        if int(it.get("times_surfaced") or 0) < AUTO_DISMISS_AT:
            continue
        iid = it.get("id")
        if iid in changed:                       # exception 1: issue evolved
            it["times_surfaced"] = 1
            out["reset"].append(iid)
        elif it.get("course_label") == "System" and it.get("config_unfixed"):
            it["status"] = "snoozed"             # exception 2: config gap
            it["snooze_until"] = (today + timedelta(
                days=CONFIG_SNOOZE_DAYS)).isoformat()
            it["times_surfaced"] = 0
            out["snoozed"].append(iid)
        else:
            it["status"] = "dismissed"
            out["dismissed"].append(iid)
    return out


def compact_and_prune(items, today):
    """6.6 / §3.1 — drop detail from settled items, delete the ancient.

    Returns (kept_items, report). Deletion is by date, not by status, which is
    why a `handled` item from last week survives compaction but one from two
    months ago does not.
    """
    kept, report = [], {"compacted": [], "deleted": []}
    for it in items:
        d = _days(it.get("date"), today)
        status = it.get("status")

        if d is not None and d < -DELETE_AFTER_DAYS:
            report["deleted"].append(it.get("id")); continue
        if d is None and status in ("dismissed", "handled"):
            shown = _days(it.get("last_shown"), today)
            if shown is not None and shown < -DELETE_AFTER_DAYS:
                report["deleted"].append(it.get("id")); continue

        if status in ("handled", "expired") and d is not None \
                and d < -COMPACT_AFTER_DAYS:
            if it.pop("notes", None) is not None:
                report["compacted"].append(it.get("id"))
            for ref in it.get("source_refs") or ():
                if isinstance(ref, dict):
                    ref.pop("snippet", None)
        kept.append(it)
    return kept, report


def run(items, today, synced_handled=(), synced_dismissed=(),
        changed_ids=()):
    """STEP 6 in its required order. Returns (items, report).

    `synced_handled` / `synced_dismissed` are the ids the artifact db reported
    at substep 6.1 — passed in because only the Artifact tool can read them.
    """
    report = {}
    handled, dismissed = set(synced_handled or ()), set(synced_dismissed or ())
    report["synced"] = []
    for it in items:                                    # 6.1
        iid = it.get("id")
        if iid in handled and it.get("status") not in TERMINAL:
            it["status"] = "handled"; report["synced"].append(iid)
        elif iid in dismissed and it.get("status") not in TERMINAL:
            it["status"] = "dismissed"; report["synced"].append(iid)

    report["canvas_closed"] = sync_canvas_completion(items)  # 6.1b
    report["released"] = release_snoozes(items, today)  # 6.2
    report["overdue"] = flag_overdue(items, today)      # 6.3 — after sync
    report["overdue_retracted"] = retract_overdue(items)  # 6.3b
    report["expired"] = expire(items, today)            # 6.4 — after overdue
    report["lead_expired"] = age_leads(items, today)    # 6.4b — §14.3
    report.update(auto_dismiss(items, today, changed_ids))   # 6.5
    items, pruned = compact_and_prune(items, today)     # 6.6
    report.update(pruned)
    return items, report


# --- §5.1 routing -----------------------------------------------------------

def in_attention(item):
    """Would this item render in Needs your attention?

    Used by `expire()` before routing runs, so it cannot call `section_for()`.
    """
    if item.get("status") == "unresolved" or item.get("overdue_flagged"):
        return True
    if item.get("needs_attention"):
        return True
    return (item.get("evidence") == "insufficient"
            and item.get("kind") in ATTENTION_KINDS)


def section_for(item, today):
    """§5.1's table, top to bottom, first match wins. None = not rendered."""
    status = item.get("status")
    kind = item.get("kind")
    evidence = item.get("evidence")

    if status in TERMINAL:
        return None
    if status == "snoozed":
        until = as_date(item.get("snooze_until"))
        if until is None or until > today:
            return None                      # unexpired snooze
    if evidence == "exclude":
        return None
    if kind == "opportunity":
        # Brief v2: something to apply to or compete in. Routed on its own
        # (never an obligation, never campus filler); an unknown deadline
        # is normal for these, a passed one retires it.
        d = _days(item.get("date"), today)
        return "opportunities" if d is None or d >= 0 else None
    if evidence == "insufficient":
        return "attention" if kind in ATTENTION_KINDS else None
    if kind == "personal":
        # Not rendered, except as a proposed sender-override row — which the
        # run raises explicitly by setting needs_attention.
        return "attention" if item.get("needs_attention") else None
    if in_attention(item):
        return "attention"
    if item.get("regime") == "lead":
        # §14.3/§14.4 -- `regime` is what the renderer partitions on, stored
        # not derived, per DISCOVERY.md. `confirmed`/`killed` are how a lead
        # LEAVES this regime for good (graduated, or the hypothesis was
        # tested and was wrong); both stop it rendering here even though the
        # generic `TERMINAL` check above doesn't know about either -- it is
        # scoped to the confirmed regime's own three statuses. A lead is
        # otherwise never gated on `date`: it has no assignment-style window,
        # and an unset `act_by` (the common case -- see opportunity.lead())
        # must not make it fall through to the `d is None` check below.
        return None if status in ("confirmed", "killed") else "leads"

    d = _days(item.get("date"), today)
    if d is None:
        return None
    # §5.1, updated 2026-09-16: Assignments/Assessments are scoped to the five
    # tracked classes (MY_CLASS_CLASSES) only -- a course-less "UMD" deadline
    # used to land in Assignments right alongside real coursework, with no
    # pill-free way to tell them apart. Everything NOT tied to a real class
    # surfaces in Coming up instead, if it is within the 30-day horizon at
    # all -- this replaces the old "past its window" overflow rule, which used
    # to route a distant CLASS item into Coming up too. It no longer does: a
    # class assignment/assessment past its own window simply does not render
    # until it enters it, because Coming up is now specifically the "not my
    # classes" view and a mix of the two was the whole complaint.
    if kind == "assignment":
        if item.get("course_class") in MY_CLASS_CLASSES:
            return "assignments" if 0 <= d <= ASSIGNMENT_WINDOW else None
        return "coming_up" if 0 <= d <= HORIZON else None
    if kind == "assessment":
        if item.get("course_class") in MY_CLASS_CLASSES:
            return "assessments" if 0 <= d <= ASSESSMENT_WINDOW else None
        return "coming_up" if 0 <= d <= HORIZON else None
    if kind == "umd_deadline":
        # Parked (see UMD_DEADLINES_ENABLED). Not rendered, and deliberately
        # not mutated: the items stay in state so flipping the flag back on
        # restores them rather than needing a re-fetch.
        return "coming_up" if UMD_DEADLINES_ENABLED and 0 <= d <= HORIZON \
            else None
    # §5.3b — a campus-calendar event is an opportunity, not an obligation, and
    # gets its own capped section rather than competing for Coming up's rows.
    # Under §5.3's old drop rule it lost that competition every time: on
    # 2026-09-09 Coming up ran to 11 rows and the entire campus block was
    # demoted, and by 2026-09-10 step 5b had started SKIPPING fetches because
    # the compositor already knew it would discard the result. A subsystem
    # whose output is structurally unreachable is worse than one that is off.
    if _is_campus(item) and kind in ("event", "advising") and 0 <= d <= HORIZON:
        return "campus"
    if kind in ("event", "advising") and 0 <= d <= HORIZON:
        return "coming_up"
    return None


def assign_sections(items, today):
    """{section: [items]}. No item appears twice — §5.1's closing rule holds
    by construction here, because each item is routed exactly once."""
    out = {"assignments": [], "assessments": [], "coming_up": [],
           "attention": [], "campus": [], "leads": [], "opportunities": []}
    for it in items:
        sec = section_for(it, today)
        if sec:
            out[sec].append(it)
    return out


# --- §6.5 ai_actions -------------------------------------------------------

STUDY_MIN_DAYS = 7             # §6.5 — an assessment inside a week has no plan
# Titles that make an assessment "substantial" without a judgment call. §6.5
# names midterm/final/unit exam explicitly; "otherwise substantial" stays with
# the model via the `substantial` flag below.
_SUBSTANTIAL = re.compile(r"\b(midterm|final|unit\s+exam|exam)\b", re.I)
_LOW_STAKES = re.compile(r"\bquiz\b", re.I)
# §5.5's cheapest weight tier. Deliberately NOT §5.8's recurrence detector,
# which was the first attempt: that matcher covers `Homework N` and
# `Problem Set N` as well, so a problem set weighed the same as a discussion
# reply and a midterm-plus-problem-set day came to 3.5 and stayed silent. What
# makes a row cheap is the KIND OF WORK named in it, not that it repeats.
#
# `peer\s+instruction`/clicker/iclicker/top\s*hat added 2026-09-18, reviewing
# real briefs against real Canvas data: MATH001's "Peer Instruction N" rows
# are an in-class iClicker activity (submission_types == ["external_tool"],
# no take-home component at all) that this pattern did not catch, so they
# weighed a full 1.0 -- the same as an actual problem set -- for both heavy-
# day purposes and (now) action_tag() below.
_LOW_EFFORT = re.compile(
    r"\b(participation|discussion|reply|response\s+post|check-?in|"
    r"attendance|poll|survey|peer\s+instruction|clicker|iclicker|"
    r"top\s*hat)\b", re.I)


def assign_ai_actions(buckets, today):
    """Set `ai_actions` on every rendered item. Call after assign_sections().

    Takes the bucket map rather than a flat list because the section override
    is part of the rule, and an item's section is not recoverable from the
    item alone once `evidence` and `needs_attention` are in play.

    This is §6.5's earned/never tables, minus the one test that needs
    judgment.

    Returns the list to store in `item["ai_actions"]`. **An empty list is the
    common case and is correct** — measured on 2026-09-10, 1 of 50 rendered
    rows earned anything.

    Three of the four rules are mechanical and are decided here:

      `study`  assessment, >= STUDY_MIN_DAYS out, and substantial. Title
               matching covers midterm/final/exam; a `quiz` is excluded as
               low-stakes per §6.5. Set `item["substantial"] = True` to force
               it for something the title does not reveal — that is §6.5's
               "or otherwise substantial", and it is the model's call.
      `email`  attention rows, except a `System` housekeeping row. There is
               nobody to email about the pipeline's own plumbing.
      section  Coming up and Worth your time earn nothing; a grouped row earns
               nothing. The template gives none of them a place to put the
               button, so this overrides everything above.

    The fourth stays with the model, because it cannot be derived:

      `steps`  "plausibly more than one sitting." Pass
               `item["multi_sitting"] = True`. A problem set with parts and a
               single discussion post are indistinguishable by title, length
               or kind, and §6.5's own test — asked to decompose a task with
               no parts, a model can only produce filler — is exactly the
               judgment a pattern-matcher would get wrong in the expensive
               direction: a filler answer looks like a working feature.
    """
    for section, rows in buckets.items():
        for it in rows:
            if it.get("is_group") or section in ("coming_up", "campus"):
                it["ai_actions"] = []
                continue
            if section == "attention":
                it["ai_actions"] = (
                    [] if it.get("course_label") == "System" else ["email"])
                continue
            if section == "assessments":
                d = _days(it.get("date"), today)
                title = "%s %s" % (it.get("title") or "", it.get("detail") or "")
                substantial = bool(it.get("substantial")) or (
                    _SUBSTANTIAL.search(title)
                    and not _LOW_STAKES.search(title))
                it["ai_actions"] = (
                    ["study"] if d is not None and d >= STUDY_MIN_DAYS
                    and substantial else [])
                continue
            if section == "assignments":
                # The one judgment left. Absent the flag, nothing is earned —
                # §6.5's default is empty, so a run that forgets to set it
                # under-offers rather than offering filler.
                it["ai_actions"] = (
                    ["steps"] if it.get("multi_sitting") else [])
                continue
            it["ai_actions"] = []
    return buckets


# --- action_tag / is_fyi (2026-09-18) ---------------------------------------
#
# Added after reviewing two real generated briefs (2026-09-17 and 2026-09-18)
# against the underlying Canvas data rather than against the code's own
# assumptions. "Today — coursework" (and Assignments/Assessments generally)
# was showing "Peer Instruction 2 - 3P", "Participation 3" and similar rows
# with exactly the same visual weight as "Reading List and Reflection" or a
# real problem set, because every one of them carries `kind: "assignment"`
# and a due date -- the only two things `section_for()` looks at. Michael's
# call: don't filter these out (a participation activity with a real due
# date is still a real due date), just stop presenting them as if they were
# the same KIND of obligation. `action_tag()` is that label; nothing reads
# it to decide whether a row renders, only what badge it wears.

_IN_CLASS_RE = re.compile(
    r"\b(peer\s+instruction|clicker|iclicker|top\s*hat|in-?class)\b", re.I)

ACTION_TAGS = {
    "no_submission": "No submission required",
    "in_class": "In-class, nothing to upload",
    "take": "Take it",
    "submit": "Submit",
}


def action_tag(item):
    """A short, honest label for what an assignment/assessment row actually
    needs from Michael -- grounded in real signal, checked in this order:

    1. Canvas's own `submission_types` (threaded through by
       canvas_shadow.py) -- `["none"]` is Canvas stating, unambiguously,
       that there is nothing to hand in. This is checked BEFORE any title
       keyword because it is a fact about the object, not a guess about its
       name: a course could title a real submission "Participation" and a
       true no-submission entry could be titled anything.
    2. An in-class/participation keyword in the title or detail (the same
       vocabulary item_weight() already treats as cheap, §5.5) -- covers
       the common case where Canvas's own field doesn't exist (a Gmail-
       sourced row has no `submission_types` at all) or is ambiguous
       (`external_tool` covers both an in-class clicker AND a real
       Gradescope upload; the title is what tells those two apart).
    3. `kind` -- an assessment defaults to "take it" (you don't "submit" an
       exam the way you submit a problem set, §6.1's own Mark-done rule);
       an assignment defaults to "submit".

    Returns "" for anything that isn't an assignment/assessment -- a
    coming-up/campus/attention row earns no tag, because none of the above
    means anything for anything else.
    """
    kind = item.get("kind")
    if kind not in ("assignment", "assessment"):
        return ""
    types = item.get("submission_types")
    if isinstance(types, (list, tuple)) and list(types) == ["none"]:
        return ACTION_TAGS["no_submission"]
    title = "%s %s" % (item.get("title") or "", item.get("detail") or "")
    if _IN_CLASS_RE.search(title) or _LOW_EFFORT.search(title):
        return ACTION_TAGS["in_class"]
    if kind == "assessment":
        return ACTION_TAGS["take"]
    return ACTION_TAGS["submit"]


# A coming-up/event/advising row that says Michael has to DO something by a
# date -- apply, register, request a ticket -- as opposed to one that is only
# announcing that something is happening. Reviewing the 2026-09-18 brief's
# Further out section against the underlying items: "Apex Fund ...
# applications DUE 9/25" and "SGA election registration closes" are real
# deadlines wearing an `event` kind; "Elevate Town Hall" and a bare career-
# fair announcement are not -- nothing in either says Michael owes anyone
# anything by showing up or not.
_REQUIRES_ACTION_RE = re.compile(
    r"\b(appl(?:y|ication|ications)|regist(?:er|ration)|rsvp|"
    r"sign[\s-]?up|deadline|due|submit|enroll|renew(?:al)?|"
    r"request(?:s|ed)?)\b", re.I)


def is_fyi(item):
    """True when a coming-up/event/advising row reads as informational
    rather than owed. Assignments and assessments are never FYI -- they
    always carry their own due date and were never the rows crowding
    Further out; the review that added this found the event/advising rows
    were.

    A keyword read, not language understanding, and it is written to err
    toward "not FYI": surfacing a plain announcement alongside real
    deadlines costs one line of visibility, while calling a real deadline
    FYI and demoting it costs a missed one. render_briefing.py demotes an
    FYI row into Further out's own disclosed secondary list -- same
    "demoted, not dropped" treatment §5.3/§5.1c already give Coming up's and
    Needs your attention's overflow -- it never removes it.
    """
    if item.get("kind") in ("assignment", "assessment"):
        return False
    text = "%s %s" % (item.get("title") or "", item.get("detail") or "")
    return not bool(_REQUIRES_ACTION_RE.search(text))


# --- §5.8 grouping ----------------------------------------------------------

_NUM = re.compile(r"\b\d+\b")
_DATEISH = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_PATTERNS = (
    r"homework", r"hw", r"problem\s*set", r"week(?:ly)?", r"participation",
    r"discussion",
)


def title_pattern(title):
    """The recurring shape of a title: numbers and dates blanked out.

    §5.8 detects "any title where only a number or date differs", so the
    pattern is the title with those removed — `Homework 4` and `Homework 11`
    both reduce to `homework`.
    """
    t = str(title or "").lower()
    t = _DATEISH.sub(" ", t)
    t = _NUM.sub(" ", t)
    return " ".join(t.split())


def group_key(item):
    """(course, pattern) or None when the title has nothing recurring in it.

    Requires a number or date to have actually been removed. Without that
    check, three unrelated one-off items from one course with similar wording
    would collapse into a bogus group.
    """
    title = str(item.get("title") or "")
    pattern = title_pattern(title)
    if not pattern:
        return None
    if not (_NUM.search(title) or _DATEISH.search(title)):
        return None
    course = item.get("course_label") or item.get("course_class") or ""
    return (course, pattern)


def build_groups(section_items, today):
    """Collapse recurring runs of 3+. Returns (rows, groups).

    `rows` is the section in **display order with groups substituted inline** —
    §5.8 is explicit that a grouped row sorts by its earliest upcoming date
    exactly like an ordinary row, and is not appended after the ungrouped
    ones. Collapsing rows is a density decision and must never reorder a
    section.
    """
    buckets = {}
    for it in section_items:
        k = group_key(it)
        if k:
            buckets.setdefault(k, []).append(it)

    groups, grouped_ids = [], set()
    for (course, pattern), members in buckets.items():
        dated = [m for m in members if as_date(m.get("date"))]
        if len(dated) < GROUP_MIN:
            continue
        dated.sort(key=lambda m: as_date(m.get("date")))
        gid = "group-%s-%s" % (str(course).lower(),
                               re.sub(r"[^a-z0-9]+", "-", pattern).strip("-"))
        for m in dated:
            m["group_id"] = gid          # every member stays a distinct item
            grouped_ids.add(id(m))
        groups.append({
            "group_id": gid,
            "course_label": course,
            "title": members[0].get("title"),
            "pattern": pattern,
            "dates": [as_date(m["date"]) for m in dated],
            "count": len(dated),
            "earliest": as_date(dated[0]["date"]),
            "members": [m.get("id") for m in dated],
            "is_group": True,
        })

    rows = [it for it in section_items if id(it) not in grouped_ids]
    rows.extend(groups)
    rows.sort(key=lambda r: (r.get("earliest") if r.get("is_group")
                             else as_date(r.get("date"))) or today)
    return rows, groups


def group_dates_label(group):
    """§5.8 / 7f: `Next: Sep 11, 18, 25 · 3 in the window`.

    Dates after the first drop the month when it has not changed, which is
    what the spec's example shows. Built without strftime's `%-d`/`%#d` (the
    day-of-month-without-zero-padding flags), which have no single spelling
    that works across every platform — the same reason reminder_url.py and
    umd_calendar.py hand-format dates instead of using them.
    """
    dates = list(group.get("dates") or ())
    if not dates:
        return ""
    parts, last_month = [], None
    for d in dates:
        stamp = "%s %d" % (d.strftime("%b"), d.day)
        parts.append(stamp if d.month != last_month else str(d.day))
        last_month = d.month
    return "Next: %s · %d in the window" % (", ".join(parts),
                                            group.get("count", len(dates)))


# --- §5.5 heavy day, §5.3 ordering -----------------------------------------

def item_weight(item):
    """§5.5 — how much of a day one item actually costs.

    Three tiers, all derivable from fields already stored, so nothing new has
    to be typed each morning:

      3.0  an assessment — a midterm is not one row's worth of a day
      1.0  an ordinary assignment
      0.5  a numbered or dated member of a recurring run (`Discussion 4`,
           `Weekly Participation 7`). §5.8 already detects the pattern to
           collapse those rows; the same detector says they are cheap.

    A `substantial` or `multi_sitting` flag — the two the model already sets
    for §6.5's AI actions — pulls an assignment up to an assessment's weight,
    because that flag is exactly the statement "this is more than one sitting".
    """
    kind = item.get("kind")
    title = "%s %s" % (item.get("title") or "", item.get("detail") or "")
    if kind == "assessment":
        # §6.5 already calls a quiz low-stakes; weighting it like a midterm
        # would contradict the rule one screen away.
        return DEFAULT_WEIGHT if _LOW_STAKES.search(title) \
            else ITEM_WEIGHT["assessment"]
    if kind == "assignment":
        if item.get("substantial") or item.get("multi_sitting"):
            return ITEM_WEIGHT["assessment"]
        if _LOW_EFFORT.search(title):
            return RECURRING_WEIGHT
    return ITEM_WEIGHT.get(kind, DEFAULT_WEIGHT)


def day_load(items):
    """Total §5.5 weight of one date's items."""
    return round(sum(item_weight(i) for i in items), 2)


def heavy_day(assignments, assessments, min_load=HEAVY_DAY_LOAD):
    """§5.5 — the soonest date whose LOAD reaches `min_load`, or None.

    At most one banner. Load, not row count: three discussion replies come to
    1.5 and raise nothing, while a midterm plus a problem set comes to 4.0 and
    does. `count` is still returned because the banner names the items.
    """
    by_date = {}
    for it in list(assignments) + list(assessments):
        d = as_date(it.get("date"))
        if d:
            by_date.setdefault(d, []).append(it)
    qualifying = sorted(d for d, v in by_date.items()
                        if day_load(v) >= min_load)
    if not qualifying:
        return None
    d = qualifying[0]
    rows = sorted(by_date[d], key=lambda i: -item_weight(i))
    return {"date": d, "titles": [i.get("title") for i in rows],
            "count": len(rows), "load": day_load(rows)}


def _has_gcal(item):
    return any(str(r.get("kind") or r.get("source") or "").startswith("gcal")
               for r in (item.get("source_refs") or ())
               if isinstance(r, dict))


def _is_campus(item):
    """A campus-calendar listing, or (brief v2) an event one of the many
    listed in a digest email -- club meetings and socials belong in the
    capped, ranked campus section, not among things he owes anyone."""
    if item.get("digest"):
        return True
    return any("umd_calendar" in str(r.get("kind") or r.get("source") or "")
               for r in (item.get("source_refs") or ())
               if isinstance(r, dict))


def coming_up_block(item):
    """§5.3's blocks. Lower sorts first.

    Block 4 (campus) is normally EMPTY since §5.3b gave campus events their own
    section — `section_for()` routes them to `campus` before Coming up sees
    them. The branch is kept because it costs nothing and a campus item reached
    by some other path should still sort last rather than lead the section.
    """
    if item.get("kind") == "umd_deadline":
        return 3
    if _is_campus(item):
        return 4
    return 2 if _has_gcal(item) else 1


def relevance_score(item):
    """The campus ranker's score, whatever shape it survived in. Always a number.

    `relevance` is a **dict** — `{score, evidence, why}` — set by
    `umd_calendar.to_item()` and specified that way in SCHEMA_AND_STATE §3.3.
    `order_campus()` read it as a bare number for a while and raised
    `TypeError: bad operand type for unary -: 'dict'` on any run with two or
    more campus items to sort. A v7 item migrated forward carries
    `relevance: null`, and an old hand-written row may carry a bare int, so all
    three shapes resolve here rather than at three call sites.
    """
    rel = item.get("relevance")
    if isinstance(rel, dict):
        rel = rel.get("score")
    try:
        return float(rel or 0)
    except (TypeError, ValueError):
        return 0.0


def order_campus(items, today, cap=CAMPUS_MAX, demoted_ids=()):
    """§5.3b — the "Worth your time" section. Returns `(rows, held_back)`.

    Ranked by the campus ranker's own score where it survived onto the item
    (`relevance.score`, via `relevance_score()`), soonest date breaking ties,
    then cut at `cap`. The cut is
    real — there is no disclosure toggle here, because a section of optional
    things does not earn a second tier — so the surplus is returned rather than
    discarded, and step 5b reports its size in `state.campus.held_back` and the
    footer. Filtering that reports nothing is indistinguishable from filtering
    that broke.

    **`demoted_ids` is §15's rating demotion, and it had to move here.**
    `feedback.demoted()` used to route a twice-thumbed-down family into
    `order_coming_up()`'s `secondary` — the Coming-up disclosure list. Since
    §5.3b that is a `Campus`-labelled row inside `#section-coming-up`, which
    gate check 27 fails, so the old route would have blocked publication
    outright the first time a family earned two thumbs down. Demoted events
    sort to the BOTTOM of this section instead: they render last if there is
    room and fall into `held_back` if there is not.

    That is a real narrowing of §15's promise that nothing is ever suppressed
    outright, and it is recorded rather than glossed: with three rows and no
    toggle there is nowhere to put a second tier. What preserves the intent is
    that the reader is still told — `held_back` reaches the footer, and
    `feedback.summary()` names the family whose ranking he changed. A count he
    can see is not a silent withholding.
    """
    demoted = set(demoted_ids or ())
    rows = sorted(items, key=lambda i: (i.get("id") in demoted,
                                        -relevance_score(i),
                                        as_date(i.get("date")) or today))
    return rows[:cap], rows[cap:]


def order_coming_up(items, today, cap=COMING_UP_MAX):
    """§5.3 — four blocks, soonest first within each, then the overflow split.

    Returns `(primary, secondary)`.

    **This used to DROP the overflow and return it as `dropped`.** Over the cap
    it discarded block 4 (campus) and then block 2 (calendar-sourced) outright,
    so a plausible thing was simply never shown and the reader could not know
    it existed. The rows now move to `secondary` and render behind a
    `<details>` toggle instead: the section still leads with what is
    recommended, and nothing is silently withheld.

    Blocks 1 and 3 are never demoted — block 1 is the only actionable block and
    block 3 is a registrar deadline — so a `primary` longer than `cap` is
    correct rather than a bug.
    """
    rows = sorted(items, key=lambda i: (coming_up_block(i),
                                        as_date(i.get("date")) or today))
    primary, secondary = list(rows), []
    for block in (4, 2):
        if len(primary) <= cap:
            break
        keep = [r for r in primary if coming_up_block(r) != block]
        secondary.extend(r for r in primary if coming_up_block(r) == block)
        primary = keep
    # Demoted rows keep §5.3's ordering among themselves so the expanded list
    # is not a differently-sorted second section.
    secondary.sort(key=lambda i: (coming_up_block(i),
                                  as_date(i.get("date")) or today))
    return primary, secondary


def more_label(secondary):
    """The `<details>` summary text. Says WHAT is hidden, not just how many.

    "Show 4 more" tells the reader nothing about whether it is worth opening;
    naming the kind does, and the two blocks that get demoted are exactly the
    two that are informational rather than owed.
    """
    n = len(secondary or ())
    if not n:
        return ""
    campus = sum(1 for r in secondary if _is_campus(r))
    cal = n - campus
    parts = []
    if cal:
        parts.append("%d already on your calendar" % cal)
    if campus:
        parts.append("%d campus event%s" % (campus, "" if campus == 1 else "s"))
    return "%d more — %s" % (n, ", ".join(parts))


def order_attention(items, today, cap=ATTENTION_MAX):
    """§5.1c, added 2026-09-16 — "Needs your attention"'s primary/secondary
    split. Returns `(primary, secondary)`, same shape as `order_coming_up()`
    and `order_campus()`.

    Two independent reasons land a row in `secondary`, and they do not
    collapse into one number the way `order_campus()`'s cap does:

    1. `course_label == "System"` — a pipeline housekeeping notice ("Gradescope
       needs a password set"), never a real course or person matter. These are
       demoted UNCONDITIONALLY, regardless of `cap` and regardless of how many
       there are. A System row has never earned a place ahead of a real
       decision Michael has to make, so there is no count at which it should
       start rendering inline.
    2. Plain cap overflow — an ordinary (non-System) row beyond `cap`, the same
       kind of demotion `order_coming_up()` applies to its calendar-sourced
       block. Preserves whatever order `assign_sections()` handed in (already
       date-relevant by construction — an overdue/unresolved item is not
       given a synthetic urgency score here, because attention items do not
       carry one specific enough to rank on; inventing one would be a guess
       dressed up as a sort).

    Unlike `order_campus()`, this is not asserted as a hard row-count cap by
    the render gate: an unresolved conflict or a failed source is exactly the
    kind of thing that must never be silently hidden just to satisfy a count,
    so `primary` longer than `cap` is tolerated the same way `order_coming_up`
    tolerates it for its own never-demoted blocks. Only the System half of
    this split is a real invariant (render_briefing.py's check 29).
    """
    system = [i for i in items if i.get("course_label") == "System"]
    rest = [i for i in items if i.get("course_label") != "System"]
    primary, overflow = rest[:cap], rest[cap:]
    return primary, overflow + system


def attention_more_label(secondary):
    """The attention `<details>` summary text — same "name what's hidden"
    rule as `more_label()`, not just a count (§5.1c)."""
    n = len(secondary or ())
    if not n:
        return ""
    system = sum(1 for r in secondary if r.get("course_label") == "System")
    other = n - system
    parts = []
    if other:
        parts.append("%d waiting on you" % other)
    if system:
        parts.append("%d system notice%s" % (system, "" if system == 1 else "s"))
    return "%d more — %s" % (n, ", ".join(parts))


# --- §5.6 detail level, §5.0 change strip -----------------------------------

def full_detail(item, today):
    """§5.6 — does this row render at full detail this morning?

    True on the first surface, as before, and ALSO once the date is close:
    an assignment inside 1 day, an assessment inside 3. Binary anti-repetition
    gave the fullest version of an assessment 20 days out and a single line the
    night before it was due, which is exactly inverted.

    The `New` chip is NOT this function — the chip means `times_surfaced == 0`
    and nothing else (§5.6, gate check 20). A re-expanded row is fully written
    and unchipped, which is the honest pairing: it is not new, it is imminent.
    """
    if int(item.get("times_surfaced") or 0) == 0:
        return True
    window = REEXPAND_WITHIN.get(item.get("kind"))
    if window is None:
        return False
    d = _days(item.get("date"), today)
    return d is not None and 0 <= d <= window


def summarize_changes(report, rendered, today, moved_ids=()):
    """§5.0 — the counts behind the change strip.

    `items_changed` has been measured into `last_run_stats` since v9 and then
    thrown away: nothing on the page said what moved, so a 79-item corpus had
    to be re-read every morning to find the two rows that were different.

    `report` is `run()`'s. `rendered` is the flat list of items that actually
    reached the page. `moved_ids` comes from STEP 3's reconcile — the one input
    code cannot derive, because only the dedup pass sees a date change.

    `resolved` counts what MICHAEL settled (the artifact db sync), not what the
    pipeline aged out on its own. An expiry is not an accomplishment and must
    not be reported as one.
    """
    ids = {i.get("id") for i in rendered or ()}
    moved = ids & set(moved_ids or ())
    return {
        "new": sum(1 for i in rendered or ()
                   if int(i.get("times_surfaced") or 0) == 0),
        "moved": len(moved),
        "resolved": len(set(report.get("synced") or ())),
        "overdue": len(set(report.get("overdue") or ())),
    }


# --- §9.6 budget skips ------------------------------------------------------

def skip_row(step, what, today, because="to stay inside this run's budget"):
    """§9.6 — one `System` attention row for a step that truncated itself.

    On 2026-09-10 the links/extraction backfill (6.7/6.8) was skipped to
    conserve tool calls and two of three campus pages were never fetched. Both
    were logged to `state.errors` at INFO and neither reached the page, so the
    briefing looked complete and was not. §9.3 forbids a blind briefing; a run
    that quietly does less than it says it does is the same failure with the
    evidence filed somewhere Michael does not read.

    `config_unfixed` is deliberately NOT set: this is a per-run event, not an
    unfixed configuration gap, so §5.7's 14-day snooze must not apply. It
    surfaces fresh, and it stops appearing the moment the step stops skipping.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(step).lower()).strip("-")
    return {
        "id": "attention-skipped-%s-%s" % (slug, today.isoformat()),
        # `event` rather than an invented `system` kind: the enum in §3.3 is
        # closed, and routing does not read it here — `needs_attention` sends
        # the row to attention before `section_for()` reaches any kind branch.
        "kind": "event",
        "course_class": "none",
        "course_label": "System",
        "regime": "confirmed",
        "status": "new",
        "date": today.isoformat(),
        "times_surfaced": 0,
        "needs_attention": True,
        "title": "%s did not finish this run" % step,
        "detail": "%s was skipped %s." % (what, because),
        "notes": "Not an error — a deliberate truncation, reported because a "
                 "shorter run must not look like a complete one (§9.6).",
    }


# --- 7f formats -------------------------------------------------------------

def days_out_meta(value, today):
    """7f: `today` · `tomorrow · Mon, Sep 7` · `in 5 days · Fri, Sep 11`."""
    d = as_date(value)
    if d is None:
        return ""
    n = (d - today).days
    try:
        stamp = d.strftime("%a, %b ") + str(d.day)
    except ValueError:
        stamp = d.isoformat()
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow · %s" % stamp
    if n < 0:
        return "%d day%s ago · %s" % (-n, "" if n == -1 else "s", stamp)
    return "in %d days · %s" % (n, stamp)
