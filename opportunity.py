"""
opportunity.py — the discovery layer.

Everything else in this system reformats channels Michael is already
subscribed to: his inbox, his calendars, the campus events page he could open
himself. That is a hygiene product. It compresses and it never lies, which is
worth having, but he would have found almost all of it anyway.

This module is for the opportunities that have NO inbound channel — the ones
nobody emails an undergraduate about until he is already on the list. It does
four things the rest of the pipeline structurally cannot:

  1. Works backward from a deadline to the date he has to MOVE (`act_by`),
     because "applications close Feb 1" is not the actionable fact when the
     application needs a recommendation letter.
  2. Carries a second class of item — a LEAD — that is explicitly a hypothesis
     rather than a reported fact, with its provenance enforced in code.
  3. Ranks by obscurity rather than prominence, because a fair advertised on
     every screen on campus is worth less to him than a scholarship with forty
     applicants, and the existing relevance weights rank those backwards.
  4. Reports its own blind spots, which a system that can only describe what
     arrived is incapable of doing.

NO MODEL CALL, deliberately, for the same two reasons §12.4 gives: a lead has
to be reproducible so "why am I seeing this" has an answer, and dedup against
yesterday needs the same input to score the same way twice.

THE SAFETY DESIGN, stated once and enforced below rather than requested in
prose: §1.6 forbids inventing a fact. A lead is not an invented fact — it is a
labelled inference — but that distinction only holds if it is impossible to
build a lead whose claims are untraceable. So `Lead.validate()` REJECTS a lead
that carries no basis, a basis entry with no source, a confidence tier its
basis cannot support, or a headline asserting something no basis mentions.
The second regime is safe because it is mechanically partitioned, not because
the docs ask it to behave.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# 1. Standing requirements — what the system is TASKED with, not what he likes
# ---------------------------------------------------------------------------
#
# The quiz (§12.2) asks which categories of campus event he wants. That is a
# taste model, and a taste model can only rank what already arrived.
# Intelligence collection starts from the other end: what do we need to know?
# A requirement is a standing question. Sources get tasked against it, ranking
# scores against it, and — the part a taste model cannot do — SILENCE against
# it is reportable.

REQUIREMENT_FIELDS = ("id", "statement", "horizon_days", "keywords",
                      "active", "quiet_after_days")

# A requirement nobody has surfaced anything for in this long is a coverage
# gap worth naming. Not an error: a fact about the collection, not the world.
DEFAULT_QUIET_AFTER = 30


def requirement(id, statement, horizon_days=180, keywords=(),
                active=True, quiet_after_days=DEFAULT_QUIET_AFTER):
    """One standing intelligence requirement.

    `horizon_days` is deliberately long — 180 by default against the briefing's
    30-day item horizon. An internship deadline in November is decided by a
    resume built in September; a 30-day window structurally excludes every
    opportunity that needs preparation, which is every opportunity worth
    having.
    """
    return {"id": id, "statement": statement, "horizon_days": int(horizon_days),
            "keywords": [k.lower() for k in keywords], "active": bool(active),
            "quiet_after_days": int(quiet_after_days)}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def matches_requirement(text, req):
    """Which requirement keywords a blob of text hits. Substring, not fuzzy —
    reproducibility beats recall here, and `keywords_boost` (§12.2) already
    exists as the user's escape hatch for vocabulary no taxonomy covers."""
    hay = _norm(text)
    return [k for k in req["keywords"] if k and _norm(k) in hay]


# ---------------------------------------------------------------------------
# 2. Backward planning — the single highest-leverage field in the system
# ---------------------------------------------------------------------------
#
# Every item in the existing schema has a `date` that is a due date or an event
# start. Nothing represents "the last day you can still start this". These lead
# times are HEURISTIC DEFAULTS — judgments about how long a human process
# takes, not claims about any specific opportunity — so they are labelled as
# defaults, they are overridable per opportunity, and `act_by` always discloses
# which ones it used.

PREP_LEAD_DAYS = {
    "recommendation": 21,   # asking a professor cold, plus their turnaround
    "transcript": 7,        # registrar request
    "essay": 10,            # draft, sit on it, revise
    "portfolio": 14,
    "code_sample": 7,
    "resume": 5,
    "writing_sample": 10,
    "interview_prep": 7,
    "form": 1,
    "registration": 1,
}

# Below this, "act by" and "due" are the same day and saying both is noise.
ACT_BY_MIN_GAP = 2


def compute_act_by(deadline, prerequisites=(), overrides=None):
    """Return (act_by, chain) — the date to start, and the disclosed reasoning.

    Prerequisites run in PARALLEL, not in series: the binding constraint is the
    longest one, because he can ask for a letter and request a transcript in
    the same afternoon. Summing them would manufacture urgency, and a brief
    that cries wolf gets skimmed.
    """
    d = _as_date(deadline)
    lead = dict(PREP_LEAD_DAYS)
    lead.update(overrides or {})
    used = []
    worst = 0
    for p in prerequisites or ():
        days = lead.get(p)
        if days is None:
            continue
        used.append((p, days))
        worst = max(worst, days)
    if not used:
        return None, []
    used.sort(key=lambda x: -x[1])
    return d - timedelta(days=worst), used


def act_by_line(deadline, prerequisites=(), overrides=None):
    """The user-facing disclosure. Says which assumption drove the date, so a
    wrong default is visible and correctable rather than silently authoritative.
    """
    ab, chain = compute_act_by(deadline, prerequisites, overrides)
    if not ab:
        return None
    driver, days = chain[0]
    return ("Start by %s — %s takes about %d days (assumed default)"
            % (ab.isoformat(), driver.replace("_", " "), days))


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# 3. Leads — a second epistemic regime, partitioned in code
# ---------------------------------------------------------------------------

CONFIDENCE_TIERS = ("reported", "inferred", "speculative")

# How much provenance each tier demands. `reported` claims a source SAID it, so
# it needs a source that did. `inferred` is a join across two facts, so it
# needs two. `speculative` is a hypothesis worth one email, and is allowed on
# one basis — but it can never be presented as anything else.
MIN_BASIS = {"reported": 1, "inferred": 2, "speculative": 1}


class LeadError(ValueError):
    """A lead that could mislead. Raised, never logged and shipped."""


def basis(claim, source, url=None, observed_on=None):
    """One traceable observation a lead rests on.

    `claim` is what was observed, in the source's own terms. `source` is where.
    Both are required: a basis entry without a source is the thing §1.6 exists
    to stop, and it is easier to enforce that here than to ask for it in prose.
    """
    if not (claim or "").strip():
        raise LeadError("basis with no claim")
    if not (source or "").strip():
        raise LeadError("basis %r has no source" % claim[:40])
    return {"claim": claim.strip(), "source": source.strip(),
            "url": url, "observed_on": observed_on}


def lead(id, headline, basis_list, confidence, requirement_ids=(),
         confirm_action=None, kill_criteria=None, cost_to_check="low",
         value_if_won="medium", act_by=None, deadline=None, first_seen=None):
    """A hypothesis, not a fact. Validated on construction.

    `confirm_action` and `kill_criteria` are mandatory and are the discipline
    that separates a lead from a rumour: a lead you cannot cheaply confirm and
    cannot ever rule out is a permanent low-grade anxiety, not intelligence.
    """
    L = {
        "id": id, "headline": (headline or "").strip(), "basis": list(basis_list or ()),
        "confidence": confidence, "requirement_ids": list(requirement_ids or ()),
        "confirm_action": (confirm_action or "").strip(),
        "kill_criteria": (kill_criteria or "").strip(),
        "cost_to_check": cost_to_check, "value_if_won": value_if_won,
        "act_by": act_by, "deadline": deadline,
        "first_seen": first_seen, "status": "open", "regime": "lead",
    }
    validate_lead(L)
    return L


# Words that assert a fact about the world. A headline using one has to have a
# basis that actually mentions the subject — otherwise the lead is smuggling an
# assertion in under a label that says "hypothesis".
_ASSERTIVE = re.compile(
    r"\b(is|are|will|has|have|opens?|closes?|awarded|hiring|recruiting|"
    r"accepted|announced|confirmed)\b", re.I)


def validate_lead(L):
    """Reject anything that could read as more certain than it is."""
    if L.get("confidence") not in CONFIDENCE_TIERS:
        raise LeadError("confidence %r not one of %s"
                        % (L.get("confidence"), CONFIDENCE_TIERS))
    if not L.get("headline"):
        raise LeadError("lead with no headline")
    b = L.get("basis") or []
    if not b:
        raise LeadError("lead %r has no basis — §1.6" % L["id"])
    for entry in b:
        if not (entry.get("claim") or "").strip() or not (entry.get("source") or "").strip():
            raise LeadError("lead %r has a basis entry without claim+source" % L["id"])
    need = MIN_BASIS[L["confidence"]]
    if len(b) < need:
        raise LeadError("lead %r is %r but rests on %d basis entries, needs %d"
                        % (L["id"], L["confidence"], len(b), need))
    if not L.get("confirm_action"):
        raise LeadError("lead %r has no confirm_action — a lead you cannot "
                        "check is anxiety, not intelligence" % L["id"])
    if not L.get("kill_criteria"):
        raise LeadError("lead %r has no kill_criteria — it would never age out"
                        % L["id"])
    # A headline that asserts must be traceable to a basis that mentions its
    # subject. Cheap check, and it catches the real failure mode: a confident
    # sentence with a vaguely related citation under it.
    if _ASSERTIVE.search(L["headline"]):
        subj = set(_norm(L["headline"]).split()) - _STOP
        pool = set()
        for entry in b:
            pool |= set(_norm(entry["claim"]).split())
        if subj and not (subj & pool):
            raise LeadError(
                "lead %r asserts something no basis entry mentions: %r"
                % (L["id"], L["headline"][:60]))
    return True


_STOP = set("a an the is are was were be been being to of in on at for with "
            "and or but if then than that this these those it its as by from "
            "you your may might could would should will has have had do does "
            "new next now about into over under more most less least".split())


def render_lead(L):
    """The user-facing block. Every lead SHOWS its basis, its confidence, what
    would confirm it and what would kill it — because an opportunity the reader
    cannot audit is one they cannot act on, and §12.4 already establishes that
    an opaque recommendation is not one Michael can correct."""
    lines = ["%s  [%s]" % (L["headline"], L["confidence"].upper())]
    for entry in L["basis"]:
        lines.append("  · %s — %s" % (entry["claim"], entry["source"]))
    if L.get("act_by"):
        lines.append("  Act by: %s" % L["act_by"])
    elif L.get("deadline"):
        lines.append("  Deadline: %s" % L["deadline"])
    lines.append("  To confirm: %s" % L["confirm_action"])
    lines.append("  Drop it if: %s" % L["kill_criteria"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Ranking by obscurity, and by expected value
# ---------------------------------------------------------------------------
#
# The existing relevance model boosts topic matches and followed units, which
# systematically surfaces the MOST ADVERTISED opportunities. A career fair in
# the Xfinity Center is on every screen on campus; telling him about it adds
# nothing. That is anti-alpha, and it is the ranking bug most worth fixing.

# Signals that an opportunity is CROWDED. Each costs it points.
CROWDING_SIGNALS = {
    "mass_email": 3,        # went to everyone
    "front_page": 3,        # on the university or college front page
    "large_venue": 2,       # stadium, ballroom, arena
    "drop_in": 2,           # no application, no cap
    "recurring_weekly": 1,  # happens again next week; missing it costs little
}

# Signals that it is THIN — few competitors, or a real filter on entry.
SCARCITY_SIGNALS = {
    "application_required": 3,
    "capped_seats": 2,
    "departmental_only": 2,   # buried on one department's page
    "first_occurrence": 2,    # no established audience yet
    "narrow_eligibility": 2,  # a filter he happens to pass
    "no_marketing_seen": 2,   # found by us, not pushed to him
}

VALUE_POINTS = {"low": 1, "medium": 3, "high": 6, "transformative": 10}
COST_POINTS = {"trivial": 1, "low": 2, "medium": 4, "high": 8}


def crowding_score(signals):
    """Negative is thin (good for him), positive is crowded."""
    s = set(signals or ())
    return (sum(v for k, v in CROWDING_SIGNALS.items() if k in s)
            - sum(v for k, v in SCARCITY_SIGNALS.items() if k in s))


def expected_value(value_if_won="medium", cost_to_check="low",
                   signals=(), requirement_hits=0):
    """A ratio, not a probability. Deliberately.

    Estimating "you have a 12% chance at this fellowship" would be a fabricated
    number wearing a decimal point. Bands and a divisor are honest about being
    a heuristic, and they still order a list correctly, which is all ranking
    needs to do.
    """
    v = VALUE_POINTS.get(value_if_won, 3)
    c = COST_POINTS.get(cost_to_check, 2)
    thin = -crowding_score(signals)          # thin field -> bonus
    return round((v * 2 + thin + 2 * requirement_hits) / float(c), 2)


def rank(candidates, key="score"):
    """Highest expected value first, then soonest act_by, then soonest
    deadline. Ties broken deterministically by id so two runs agree."""
    def sort_key(c):
        ab = c.get("act_by") or c.get("deadline") or "9999-12-31"
        return (-float(c.get(key) or 0), str(ab), str(c.get("id") or ""))
    return sorted(candidates, key=sort_key)


# ---------------------------------------------------------------------------
# 5. Entity joins — the value is in the edge, not the item
# ---------------------------------------------------------------------------
#
# The mechanism behind trigger-event selling, link analysis and most
# investigative journalism: two individually boring facts become a lead when
# connected. (professor got a grant) x (professor teaches your course) is a
# lead. Neither half is.

def join_leads(edges, affinity, opportunity_kinds, today, min_affinity=1):
    """Find newly-created edges between something he cares about and something
    that bears opportunity.

    `edges`   : [{a, b, kind, first_seen, source}]
    `affinity`: {entity_id: weight} — where his attention already is, which is
                observed from behaviour, not declared in a quiz.
    `opportunity_kinds`: edge kinds that mean "a door opened" — funded, hiring,
                teaching, hosting, electing.
    Returns candidate join descriptions, NOT leads: the caller supplies the
    basis entries and the headline, because only the caller knows what the
    sources actually said. This module refuses to write a claim it cannot cite.
    """
    out = []
    for e in edges or ():
        if e.get("kind") not in opportunity_kinds:
            continue
        for near, far in ((e.get("a"), e.get("b")), (e.get("b"), e.get("a"))):
            w = (affinity or {}).get(near, 0)
            if w < min_affinity:
                continue
            age = None
            if e.get("first_seen"):
                age = (_as_date(today) - _as_date(e["first_seen"])).days
            out.append({
                "id": "join-%s-%s-%s" % (_norm(near).replace(" ", "-"),
                                         e.get("kind"),
                                         _norm(far).replace(" ", "-")),
                "near": near, "far": far, "edge_kind": e.get("kind"),
                "affinity": w, "age_days": age, "source": e.get("source"),
                # Fresh edges are the whole point: an opportunity is uncrowded
                # for as long as nobody else has noticed the door opened.
                "freshness_bonus": 2 if (age is not None and age <= 14) else 0,
            })
    return out


# ---------------------------------------------------------------------------
# 6. Recurrence — last year's calendar predicts this year's
# ---------------------------------------------------------------------------

RECURRENCE_WINDOW_DAYS = 21     # how far ahead to warn
RECURRENCE_TOLERANCE_DAYS = 10  # how much annual drift to allow


def detect_recurrence(ledger, today, window_days=RECURRENCE_WINDOW_DAYS,
                      tolerance=RECURRENCE_TOLERANCE_DAYS):
    """From a multi-year ledger, name what is about to come round again.

    This is the cheapest possible way to get ahead of a deadline: something
    that surfaced on Sept 14 last year will surface again within days of it,
    and knowing that a fortnight early is the difference between applying and
    reading about it afterwards. It needs history — which is why a 35-day item
    prune is right for the working set and wrong as the system's only memory.
    """
    today = _as_date(today)
    by_family = {}
    for row in ledger or ():
        fam = row.get("family") or row.get("id")
        if not fam or not row.get("date"):
            continue
        by_family.setdefault(fam, []).append(_as_date(row["date"]))
    out = []
    for fam, dates in by_family.items():
        dates = sorted(dates)
        past = [d for d in dates if d < today]
        if not past:
            continue
        for d in past:
            # Project each prior sighting forward a whole number of years.
            for years in range(1, 5):
                try:
                    proj = d.replace(year=d.year + years)
                except ValueError:                 # Feb 29
                    proj = d.replace(year=d.year + years, day=28)
                delta = (proj - today).days
                if 0 <= delta <= window_days:
                    if any(abs((x - proj).days) <= tolerance for x in dates if x >= today):
                        continue                   # already surfaced this cycle
                    out.append({
                        "family": fam, "expected_around": proj.isoformat(),
                        "days_out": delta, "seen_on": d.isoformat(),
                        "prior_sightings": len(past),
                        "confidence": "inferred" if len(past) > 1 else "speculative",
                    })
                    break
    return sorted(out, key=lambda r: (r["days_out"], r["family"]))


# ---------------------------------------------------------------------------
# 7. Coverage gaps — reporting what the system CANNOT see
# ---------------------------------------------------------------------------
#
# §9.3 makes the run say so when a source FAILS. Nothing makes it say anything
# about a source it never had. That is the more dangerous silence, because a
# briefing with no gaps section looks complete — which is exactly the failure
# mode §9.3 exists to prevent, one level up.

def coverage_gaps(requirements, ledger, today, monitored_sources=(),
                  known_unmonitored=(), run_events=()):
    """Requirements with nothing against them, and sources we simply lack.

    `run_events` is what THIS run actually tried and did not get:
    `[{"source": "umd_calendar", "outcome": "skipped", "detail": "..."}]`,
    with `outcome` one of `failed`, `skipped` or `partial`. It is the honest
    core of this section and it did not exist before 2026-09-10: with an empty
    `state.coverage.last_signal` the section printed the same four static
    source names every day forever, which trains the reader to skip a section
    whose entire purpose is to be read. A fixed disclaimer is not coverage
    reporting.
    """
    today = _as_date(today)
    last_hit = {}
    for row in ledger or ():
        for rid in row.get("requirement_ids") or ():
            d = _as_date(row["date"]) if row.get("date") else None
            if d and (rid not in last_hit or d > last_hit[rid]):
                last_hit[rid] = d
    gaps = []
    for req in requirements or ():
        if not req.get("active"):
            continue
        seen = last_hit.get(req["id"])
        quiet = (today - seen).days if seen else None
        if seen is None or quiet >= req["quiet_after_days"]:
            gaps.append({
                "requirement_id": req["id"], "statement": req["statement"],
                "last_signal": seen.isoformat() if seen else None,
                "quiet_days": quiet,
                # The wording matters. "Nothing is happening" would be a claim
                # about the world we cannot support; this is a claim about our
                # own collection, which we can.
                "note": ("No signal in %d days — that is a statement about "
                         "coverage, not about whether anything exists"
                         % quiet) if quiet is not None else
                        ("No signal ever recorded for this requirement"),
            })
    unmonitored = [s for s in known_unmonitored if s not in set(monitored_sources or ())]
    run_gaps = []
    for ev in run_events or ():
        src = ev.get("source") or "an unnamed source"
        outcome = (ev.get("outcome") or "failed").lower()
        verb = {"failed": "did not answer this morning",
                "skipped": "was not read this morning",
                "partial": "was only partly read this morning"}.get(
                    outcome, "did not answer this morning")
        note = "%s %s" % (src, verb)
        if ev.get("detail"):
            note += " \u2014 %s" % ev["detail"]
        run_gaps.append({"source": src, "outcome": outcome, "note": note})
    return {"quiet_requirements": gaps, "unmonitored_sources": unmonitored,
            "run_gaps": run_gaps}


def coverage_notes(gaps, roster_cap=4):
    """The `.coverage-note` lines for §13.4, in the order they should read.

    This run's own gaps first — they are the only part that changes day to day
    and the only part that describes this morning. Quiet requirements next.
    The standing unmonitored roster LAST, and only when this run had no gaps of
    its own to report: repeating "I do not watch TerpLink" under a real failure
    buries the real failure under a permanent one.
    """
    gaps = gaps or {}
    notes = [g["note"] for g in gaps.get("run_gaps") or ()]
    notes += ["%s — %s" % (g["statement"], g["note"])
              for g in gaps.get("quiet_requirements") or ()]
    roster = list(gaps.get("unmonitored_sources") or ())
    if roster and not notes:
        shown = roster[:roster_cap]
        notes.append("Not watched at all: %s. Nothing failed this morning; "
                     "these are simply outside what this briefing collects."
                     % ", ".join(shown))
    return notes


# ---------------------------------------------------------------------------
# 8. Lead lifecycle
# ---------------------------------------------------------------------------
#
# Reuses §3.4's discipline: one-way into terminal states, nothing reopens
# except a snooze. A lead additionally has `killed`, which `dismissed` does not
# capture — dismissed means he chose not to; killed means the hypothesis was
# tested and was wrong, and that is worth remembering so it is never re-raised.

LEAD_STATUSES = ("open", "confirmed", "killed", "acted", "expired", "dismissed")
LEAD_MAX_SURFACES = 3   # tighter than §5.7's 5: an unconfirmed guess earns less patience


def age_leads(leads, today, max_surfaces=LEAD_MAX_SURFACES):
    """Retire leads that went stale, and never re-raise a killed one."""
    today = _as_date(today)
    out = []
    for L in leads or ():
        L = dict(L)
        if L.get("status") in ("killed", "acted", "confirmed", "dismissed"):
            out.append(L)
            continue
        db = L.get("act_by") or L.get("deadline")
        if db and _as_date(db) < today:
            L["status"] = "expired"
            L["expired_reason"] = "act_by passed unconfirmed"
        elif (L.get("times_surfaced") or 0) >= max_surfaces:
            L["status"] = "expired"
            L["expired_reason"] = "surfaced %d times without being confirmed or acted on" \
                                  % L["times_surfaced"]
        out.append(L)
    return out


def to_item(L, today):
    """Convert a lead to an ordinary items[] row so it inherits dedup, snoozing,
    times_surfaced and auto-dismiss unchanged — exactly the choice §12.4 made
    for campus events, and for the same reason: no parallel pipeline.

    `regime: "lead"` is what the renderer keys the separate section on. The two
    regimes must never be blended in one list, which is the whole point.
    """
    return {
        "id": L["id"],
        "course": None, "course_class": "none", "course_label": "Lead",
        "kind": "lead", "regime": "lead",
        "title": L["headline"],
        "detail": "%s · to confirm: %s" % (L["confidence"], L["confirm_action"]),
        "notes": render_lead(L),
        "date": L.get("act_by") or L.get("deadline"),
        "act_by": L.get("act_by"),
        "confidence": "inferred",
        # `confidence_tier` is a SEPARATE field from `confidence` above, not a
        # rename: `confidence` here means "was this item's DATE inferred"
        # (read by orchestrator._prep_row() for its own, unrelated "· inferred
        # date" detail suffix, used across every section) and is deliberately
        # left alone. `confidence_tier` is what render_briefing.build_row()
        # actually reads for the lead's own reported/inferred/speculative tier
        # (template field reference: "{{CONFIDENCE}} EXACTLY one of: reported
        # inferred speculative") -- the two concepts share the word
        # "confidence" but nothing else, and blending them into one key was
        # the reason a lead has never rendered with any tier text at all.
        "confidence_tier": L.get("confidence"),
        # Never empty -- validate_lead() already requires at least one basis
        # entry (§14.1) -- but build_row() raises ValueError on a lead with no
        # `basis` key at all (§14.1's "never empty" is enforced there, not
        # merely stated), and this dict never carried the key before, so
        # EVERY lead reaching the renderer would have crashed the whole
        # publish. Never actually hit until leads had a section to land in.
        "basis": list(L.get("basis") or ()),
        # {{SOURCE_URL}}: "a basis entry's url; the line is deleted if none"
        # (template field reference). The first one with a url is as good a
        # single representative link as any -- basis entries beyond it are
        # still shown inline via {{BASIS_ITEMS}}.
        "source_url": next((b["url"] for b in L.get("basis", ())
                            if b.get("url")), ""),
        # {{CONFIRM_ACTION}}/{{KILL_CRITERIA}}: "Mandatory on a lead (§14.3)"
        # per the template's own field reference -- `detail` above embeds a
        # truncated echo of `confirm_action`, but the row has DEDICATED
        # fields for both and reads them by these exact keys, which this
        # dict never carried; both rendered as empty `<b>` labels with
        # nothing after them until now.
        "confirm_action": L["confirm_action"],
        "kill_criteria": L["kill_criteria"],
        "evidence": "consider",
        "status": L.get("status", "new"),
        "times_surfaced": L.get("times_surfaced", 0),
        "last_shown": _as_date(today).isoformat(),
        "ai_actions": [],
        "links": [{"label": "More info", "url": b["url"]}
                  for b in L.get("basis", ()) if b.get("url")],
        "source_refs": [{"source": "opportunity", "basis": b["source"]}
                        for b in L.get("basis", ())],
    }
