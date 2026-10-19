"""
applications.py — where Michael stands on each opportunity he is pursuing.

An opportunity row used to have three outcomes: new, done, or "Not for me".
Applying is a process with its own dates, so it gets its own record: the
`applications` table (service/schema.sql), one row per item, written only by
the page (service/app.py) and read by the run. The run never writes it, the
same rule `feedback` and `prefs` follow, so a click can never be lost to a
run that loaded state before it.

This module is the one definition of the stages, which of them keep a row on
the page, and what date each stage asks him to act on. Pure: no I/O.
"""

import hashlib
import re
from datetime import date, datetime, timedelta

# In order of progress. `withdrawn` covers both "stopped applying" and
# "declined the offer"; they end the same way. `confirmed` is he accepting a
# selection (or an offer) he was given: "Selected" waits on him, "Accepted"
# is his answer.
STAGES = ("applying", "submitted", "interview", "offer",
          "accepted", "confirmed", "rejected", "withdrawn")
ACTIVE = ("applying", "submitted", "interview", "offer", "accepted", "confirmed")

LABELS = {"applying": "Applying", "submitted": "Submitted",
          "interview": "Interviewing", "offer": "Offer",
          "accepted": "Selected", "confirmed": "Accepted",
          "rejected": "Not selected",
          "withdrawn": "Withdrawn"}

# Days after reaching a stage to check in if nothing has happened. Three
# weeks of silence after submitting is when a polite status email is normal;
# a week after an interview is when a thank-you has gone unanswered long
# enough to ask about timing. `applying` has no follow-up: the deadline and
# the start-by date already carry it.
FOLLOW_UP_DAYS = {"submitted": 21, "interview": 7, "accepted": 2}


def _as_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_active(app):
    return bool(app) and app.get("stage") in ACTIVE


def follow_up_on(stage, stage_on):
    """The follow-up date a stage change sets, or None."""
    days = FOLLOW_UP_DAYS.get(stage)
    d = _as_date(stage_on)
    return (d + timedelta(days=days)).isoformat() if days and d else None


def transition(prior, stage, today):
    """The stored record after moving to `stage` on `today`. `prior` is the
    existing record or None. Any stage may follow any other: he corrects a
    mistaken click the same way he records progress."""
    if stage not in STAGES:
        raise ValueError("unknown stage %r" % stage)
    today = _as_date(today)
    history = list((prior or {}).get("history") or ())
    if not prior or prior.get("stage") != stage:
        history.append({"stage": stage, "on": today.isoformat()})
    return {
        "stage": stage,
        "stage_on": today.isoformat() if not prior or prior.get("stage") != stage
        else prior.get("stage_on"),
        "follow_up_on": follow_up_on(stage, today)
        if not prior or prior.get("stage") != stage
        else prior.get("follow_up_on"),
        "draft_url": (prior or {}).get("draft_url"),
        "history": history,
    }


# A decision email's word for the stage it sets (orchestrator reads these).
NEWS_STAGE = {"selected": "accepted", "interview": "interview",
              "offer": "offer", "rejected": "rejected"}
DECIDED = ("offer", "accepted", "confirmed", "rejected", "withdrawn")


def from_news(prior, news_status, news_day):
    """The stored record after a decision email (`news_status`, sent
    `news_day`), or None when it changes nothing or the item is not tracked.
    Submitted or interviewing applications take any decision. A decided
    stage is not moved by an older email, so a later click or email wins."""
    stage = NEWS_STAGE.get(news_status)
    if not prior or stage is None or prior.get("stage") == stage:
        return None
    news = _as_date(news_day)
    if prior.get("stage") in DECIDED and news is not None and \
            _as_date(prior.get("stage_on")) and \
            news < _as_date(prior.get("stage_on")):
        return None
    return transition(prior, stage, news or prior.get("stage_on"))


CLOSED = ("rejected", "withdrawn")


def undo_stage(record):
    """The stage "Undo" returns a closed application to: the last open stage
    in its history, or applying when the history holds none."""
    stage = (record or {}).get("stage")
    history = [h.get("stage") for h in (record or {}).get("history") or ()]
    while history and history[-1] == stage:
        history.pop()
    for past in reversed(history):
        if past not in CLOSED:
            return past
    return "applying"


def _short(d):
    return "%s %d" % (d.strftime("%b"), d.day)


def next_step(app, deadline, act_by, today):
    """The one line an application row shows under its title: what to do
    next and by when. `deadline`/`act_by` are the opportunity's own dates."""
    if not is_active(app):
        return ""
    today = _as_date(today)
    stage = app["stage"]
    since = _as_date(app.get("stage_on"))
    head = LABELS[stage] + (" since %s" % _short(since) if since else "")
    if stage == "applying":
        due = _as_date(deadline)
        start = _as_date(act_by)
        if due is not None and due < today:
            return "%s · the deadline (%s) has passed; mark it submitted " \
                   "or withdrawn" % (head, _short(due))
        if start is not None and start >= today and (due is None or start < due):
            return "%s · start by %s, due %s" % (
                head, _short(start), _short(due)) if due else \
                "%s · start by %s" % (head, _short(start))
        if due is not None:
            n = (due - today).days
            return "%s · due %s" % (head, "today" if n == 0 else
                                    "tomorrow" if n == 1 else _short(due))
        return "%s · no deadline stated" % head
    fu = _as_date(app.get("follow_up_on"))
    if stage == "confirmed":
        return head
    if stage == "accepted":
        # Selected is a reminder to answer, not a status to wait on.
        if fu is None:
            return "%s · reply to confirm your place" % head
        if fu <= today:
            return "%s · reply to confirm now" % head
        return "%s · reply to confirm by %s" % (head, _short(fu))
    if stage == "offer":
        return "%s · reply by %s" % (head, _short(fu)) if fu else \
            "%s · decide and reply" % head
    if fu is None:
        return head
    if fu <= today:
        return "%s · follow up now: no word since %s" % (
            head, _short(since) if since else "then")
    return "%s · follow up %s if no word" % (head, _short(fu))


def follow_up_due(app, today):
    fu = _as_date((app or {}).get("follow_up_on"))
    return is_active(app) and fu is not None and fu <= _as_date(today)


# Stages whose follow-up date he may set by hand (for an offer it is the
# date to reply by).
FOLLOW_UP_STAGES = ("submitted", "interview", "offer", "accepted")


def set_follow_up(record, on):
    """`record` with its follow-up moved to `on` (ISO date, or None to
    clear). Raises ValueError for a stage that has no follow-up."""
    if not record or record.get("stage") not in FOLLOW_UP_STAGES:
        raise ValueError("a follow-up date needs a submitted, interviewing "
                         "or offer stage")
    d = _as_date(on)
    if on and d is None:
        raise ValueError("not a date: %r" % on)
    return dict(record, follow_up_on=d.isoformat() if d else None)


# --- applications the brief never showed ------------------------------------
#
# Added by hand from the page ("Track an application"). The record carries
# what he typed (`manual`); the run turns it into an ordinary opportunity
# item the first time it sees it, so research, drafting and the tracker
# treat it like any other.

def manual_id(title, today):
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")[:40]
    digest = hashlib.sha256(("%s|%s" % (title, today)).encode("utf-8"))
    return "manual-%s-%s" % (slug or "application", digest.hexdigest()[:8])


def clean_manual(title, organization="", url="", deadline=""):
    """What he typed -> the stored `manual` dict. Raises ValueError."""
    title = " ".join(str(title or "").split())[:140]
    if not title:
        raise ValueError("a name is required")
    url = str(url or "").strip()
    if url and not url.startswith("https://"):
        raise ValueError("the link must start with https://")
    d = _as_date(deadline)
    if deadline and d is None:
        raise ValueError("not a date: %r" % deadline)
    return {"title": title,
            "organization": " ".join(str(organization or "").split())[:100],
            "url": url[:500], "deadline": d.isoformat() if d else None}


def manual_item(item_id, manual, first_seen):
    """The opportunity item a hand-added application stands for."""
    url = manual.get("url") or ""
    opp = {"type": "other", "apply_url": url} if url else {"type": "other"}
    if manual.get("organization"):
        opp["organization"] = manual["organization"]
    return {
        "id": item_id, "course": None, "course_class": "umd",
        "course_label": "UMD", "kind": "opportunity",
        "title": manual["title"], "date": manual.get("deadline"),
        "organizer": manual.get("organization") or None,
        "source_url": url, "links": [{"label": "Apply", "url": url}] if url else [],
        "confidence": "confirmed", "evidence": "include",
        "regime": "confirmed", "status": "new", "times_surfaced": 0,
        "source_refs": [{"source": "manual"}],
        "first_seen": _as_date(first_seen).isoformat(), "opportunity": opp,
    }
