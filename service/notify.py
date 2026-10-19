"""App notifications: decide what is worth telling Michael, and send it.

Six kinds (the only ones -- notifications stay trusted by staying rare):
  deadline     an assignment/exam/application deadline < 24 h away (and a
               second, high priority push < 3 h away for exams and dated
               opportunities)
  grade        a course's Canvas grade posted or changed
  opportunity  a new opportunity/lead on the brief
  followup     an application follow-up date is today
  source       a scraper's login/run has failed on two ticks (once per
               outage, plus once on recovery)
  brief        the day's page published (by the morning build, or a retry
               that succeeded later); the push carries the weather
               (umbrella verdict, temp now/high/low) and a due count

Two delivery paths, one ledger (`notifications` table):
  * phone: `send_pending` posts to the self-hosted ntfy (tailnet only, bearer
    token, see reference-ntfy-selfhosted), so a message carries the full text
    of the event. Tapping it opens briefing://open.
  * Mac: the app polls GET /api/notifications and posts local notifications
    with full text -- that traffic stays on the tailnet.

Quiet hours and the daily cap apply to ntfy; events held back stay in the
ledger and go out (coalesced) later. Nothing here may fail a run: `run`
catches everything and records it in `errors`.

    notify.py run               collect events, then push what is due
    notify.py test              send a test push
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db as dbmod
import sources
import store
import weather

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import applications as applications_mod  # noqa: E402
import render_briefing  # noqa: E402

TZ = ZoneInfo("America/New_York")
SETTINGS_KEY = "notify_settings"
STATE_KEY = "notify_state"
TOPIC_FILE = os.path.expanduser("~/.config/daily-briefing/ntfy-topic")
TOKEN_FILE = os.path.expanduser("~/.config/ntfy/token")


def auth():
    """Bearer header for the self-hosted server (deny-all by default)."""
    try:
        with open(TOKEN_FILE) as f:
            return {"Authorization": "Bearer " + f.read().strip()}
    except OSError:
        return {}


NTFY_SERVER = os.environ.get("BRIEFING_NTFY_SERVER",
                             "http://127.0.0.1:2586")

KINDS = ("deadline", "grade", "opportunity", "followup", "source", "brief")
KIND_LABEL = {"deadline": "Deadline soon", "grade": "Grade update",
              "opportunity": "New opportunity", "followup": "Follow-up due",
              "source": "Source needs you", "brief": "Brief ready"}
PUSH_LINES = 4      # events listed in one push before "+N more"

ACTIVE = ("new", "ongoing", "open")
DEADLINE_KINDS = ("assignment", "assessment", "umd_deadline")
OPPORTUNITY_KINDS = ("opportunity", "lead")
SOON = timedelta(hours=24)
IMMINENT = timedelta(hours=3)
GRADE_WINDOW = timedelta(hours=36)


def default_settings():
    return {"kinds": {k: True for k in KINDS}, "quiet": ["22:00", "07:00"],
            "cap": 6}


def clean_settings(raw):
    out = default_settings()
    raw = raw if isinstance(raw, dict) else {}
    kinds = raw.get("kinds") if isinstance(raw.get("kinds"), dict) else {}
    for k in KINDS:
        if k in kinds:
            out["kinds"][k] = bool(kinds[k])
    quiet = raw.get("quiet")
    if isinstance(quiet, (list, tuple)) and len(quiet) == 2 and \
            all(_hhmm(q) for q in quiet):
        out["quiet"] = [quiet[0], quiet[1]]
    cap = raw.get("cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= 30:
        out["cap"] = cap
    return out


def _hhmm(s):
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def load_settings(db_path=dbmod.DEFAULT_DB_PATH):
    return clean_settings(store.kv_get(db_path, SETTINGS_KEY))


def save_settings(db_path, settings):
    settings = clean_settings(settings)
    store.kv_set(db_path, SETTINGS_KEY, settings)
    return settings


def in_quiet(now, quiet):
    """True when `now` falls inside the quiet window (which may wrap
    midnight)."""
    start, end = (datetime.strptime(q, "%H:%M").time() for q in quiet)
    t = now.timetz().replace(tzinfo=None)
    if start == end:
        return False
    return start <= t < end if start < end else t >= start or t < end


# --- collecting ---------------------------------------------------------------

def _due_at(row):
    """When an item is due: its date + time, end of day when no time."""
    if not row["date"]:
        return None
    try:
        day = datetime.strptime(row["date"], "%Y-%m-%d")
    except ValueError:
        return None
    hhmm = row["time"] or "23:59"
    try:
        t = datetime.strptime(hhmm, "%H:%M").time()
    except ValueError:
        t = datetime.strptime("23:59", "%H:%M").time()
    return datetime.combine(day.date(), t, tzinfo=TZ)


def _ev(key, kind, item_id, title, body, priority="default", expires=None):
    return {"key": key, "kind": kind, "item_id": item_id, "title": title,
            "body": body, "priority": priority,
            "expires_at": expires.isoformat(timespec="seconds")
            if expires else None}


def _fmt_due(due, now):
    day = "today" if due.date() == now.date() else \
        "tomorrow" if due.date() == (now + timedelta(days=1)).date() else \
        due.strftime("%a %b %-d")
    return "%s %s" % (day, due.strftime("%-I:%M %p").lstrip("0"))


def _page_hidden(conn):
    """Ids the published page leaves off as near-duplicates (orchestrator
    step9); a push for one would announce a row the page itself hides."""
    try:
        row = conn.execute("SELECT value_json FROM kv WHERE key = ?",
                           ("page_hidden_ids",)).fetchone()
    except Exception:  # noqa: BLE001 -- no kv table in an old db
        return set()
    return set(json.loads(row[0]) if row and row[0] else ())


def deadline_events(conn, now):
    out = []
    hidden = _page_hidden(conn)
    rows = conn.execute(
        "SELECT id, kind, date, time, act_by, title, course_label FROM items "
        "WHERE status IN (%s) AND (date IS NOT NULL OR act_by IS NOT NULL)"
        % ",".join("?" * len(ACTIVE)), ACTIVE).fetchall()
    for r in rows:
        if r["id"] in hidden:
            continue
        if r["kind"] in DEADLINE_KINDS:
            due = _due_at(r)
            stakes = r["kind"] == "assessment"
        elif r["kind"] in OPPORTUNITY_KINDS and r["act_by"]:
            due = _due_at({"date": r["act_by"], "time": None})
            stakes = True
        else:
            continue
        if due is None or due <= now:
            continue
        left = due - now
        title = render_briefing.shown_title(r["title"], r["date"])
        label = "%s: %s" % (r["course_label"], title) \
            if r["course_label"] else title
        body = "%s, due %s" % (label, _fmt_due(due, now))
        if left <= SOON:
            out.append(_ev("deadline24:%s:%s" % (r["id"], due.date()),
                           "deadline", r["id"], "Deadline soon", body,
                           expires=due))
        if left <= IMMINENT and stakes:
            out.append(_ev("deadline3:%s:%s" % (r["id"], due.date()),
                           "deadline", r["id"], "Due in under 3 hours", body,
                           "high", expires=due))
    return out


def grade_events(conn, now):
    """A course whose newest snapshot differs from the one before it."""
    out = []
    courses = [r[0] for r in conn.execute(
        "SELECT DISTINCT course_label FROM course_grade_snapshots")]
    for c in courses:
        rows = conn.execute(
            "SELECT captured_at, current_score, current_grade, graded_count, "
            "graded_items FROM course_grade_snapshots WHERE course_label = ? "
            "ORDER BY seq DESC LIMIT 2", (c,)).fetchall()
        if len(rows) < 2:
            continue
        new, old = rows
        try:
            at = datetime.fromisoformat(new["captured_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - at > GRADE_WINDOW or new["current_score"] is None:
            continue
        if new["current_score"] == old["current_score"] and \
                new["graded_count"] == old["graded_count"]:
            continue
        now_s, was = new["current_score"], old["current_score"]
        named = _newly_graded(old["graded_items"], new["graded_items"])
        if named:
            body = "%s · %s · course grade %s%%" % (c, named, _num(now_s))
            if was is not None and was != now_s:
                body += " (was %s%%)" % _num(was)
            out.append(_ev("grade:%s:%s" % (c, new["captured_at"]), "grade",
                           None, "Grade update", body))
            continue
        body = "%s: %s%%" % (c, _num(now_s))
        if was is not None and was != now_s:
            body += " (was %s%%)" % _num(was)
        elif new["graded_count"] and old["graded_count"] is not None \
                and new["graded_count"] > old["graded_count"]:
            more = new["graded_count"] - old["graded_count"]
            body += ", %d more item%s graded" % (more, "" if more == 1 else "s")
        out.append(_ev("grade:%s:%s" % (c, new["captured_at"]), "grade", None,
                       "Grade update", body))
    return out


def _num(x):
    return ("%.1f" % x).rstrip("0").rstrip(".")


def _newly_graded(old_json, new_json, shown=3):
    """'Digital Forum 10/10, Quiz 5 8/10': the items graded since the older
    snapshot, or "" when either snapshot predates graded_items (2026-10-17)
    or nothing new was graded (a regrade keeps the course-score wording)."""
    try:
        old = {tuple(x)[0] for x in json.loads(old_json)}
        new = [x for x in json.loads(new_json) if x and x[0] not in old]
    except (TypeError, ValueError):
        return ""
    parts = ["%s %s/%s" % (n, _num(s), _num(p)) if p else n for n, s, p in new[:shown]]
    if len(new) > shown:
        parts.append("%d more" % (len(new) - shown))
    return ", ".join(parts)


def opportunity_events(conn, now):
    out = []
    hidden = _page_hidden(conn)
    for r in conn.execute(
            "SELECT id, title FROM items WHERE kind IN (%s) AND status IN ('new','open')"
            % ",".join("?" * len(OPPORTUNITY_KINDS)), OPPORTUNITY_KINDS):
        if r["id"] in hidden:
            continue
        out.append(_ev("opp:%s" % r["id"], "opportunity", r["id"],
                       "New opportunity", r["title"]))
    return out


def followup_events(conn, now):
    out = []
    today = now.date()
    for r in conn.execute("SELECT item_id, stage, follow_up_on, manual_json "
                          "FROM applications").fetchall():
        rec = {"stage": r["stage"], "follow_up_on": r["follow_up_on"]}
        if not applications_mod.follow_up_due(rec, today):
            continue
        item = conn.execute("SELECT title FROM items WHERE id = ?",
                            (r["item_id"],)).fetchone()
        title = item["title"] if item else \
            (json.loads(r["manual_json"] or "{}").get("title") or r["item_id"])
        out.append(_ev("followup:%s:%s" % (r["item_id"], r["follow_up_on"]),
                       "followup", r["item_id"], "Follow-up due", title))
    return out


def source_events(conn, db_path, now, state):
    """Two failed ticks in a row -> one alert; recovery -> one all-clear."""
    out = []
    status = store.kv_get(db_path, "source_status", {}) or {}
    tracked = state.setdefault("sources", {})
    for name, s in status.items():
        t = tracked.setdefault(name, {"fails": 0, "seen": None, "alerted": False})
        if s.get("ok") is False:
            if s.get("last_attempt") != t["seen"]:
                t["fails"] += 1
                t["seen"] = s.get("last_attempt")
            if t["fails"] >= 2 and not t["alerted"]:
                t["alerted"] = True
                out.append(_ev("source:%s:%s" % (name, t["seen"]), "source",
                               None, "Source needs you",
                               "%s is failing: %s" % (name, (s.get("error") or "")[:120]),
                               "high"))
        elif s.get("ok"):
            if t["alerted"]:
                out.append(_ev("sourceok:%s:%s" % (name, s.get("last_success")),
                               "source", None, "Source recovered",
                               "%s is working again" % name))
            t.update(fails=0, seen=None, alerted=False)
    return out


def brief_event(conn, now, db_path=dbmod.DEFAULT_DB_PATH):
    n = len(glance(conn, now)["attention"])
    body = "%d due in the next 2 days" % n if n else "Nothing due in the next 2 days"
    try:
        wx = weather.push_text(sources.cache_get(db_path, "weather", now.date()))
    except Exception:  # noqa: BLE001 -- weather never blocks the brief push
        wx = None
    if wx:
        body = "%s\n%s" % (wx, body)
    return _ev("brief:%s" % now.date(), "brief", None, "Brief ready", body)


def glance(conn, now, limit=3):
    """What the widget and the Dock badge show."""
    due = []
    for r in conn.execute(
            "SELECT id, kind, date, time, title, course_label FROM items "
            "WHERE kind IN (%s) AND status IN (%s) AND date IS NOT NULL"
            % (",".join("?" * len(DEADLINE_KINDS)), ",".join("?" * len(ACTIVE))),
            DEADLINE_KINDS + ACTIVE):
        d = _due_at(r)
        if d and d > now:
            due.append({"id": r["id"], "title": r["title"],
                        "course": r["course_label"],
                        "due": d.isoformat(timespec="minutes")})
    due.sort(key=lambda x: x["due"])
    soon = [x for x in due
            if datetime.fromisoformat(x["due"]) - now <= timedelta(days=2)]
    page = conn.execute("SELECT rendered_at FROM rendered_page WHERE id = 1"
                        ).fetchone() if _has(conn, "rendered_page") else None
    return {"next": due[:limit], "attention": soon,
            "attention_count": len(soon),
            "built_at": page[0] if page else None,
            "unread": conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
            ).fetchone()[0]}


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                        (table,)).fetchone() is not None


# --- ledger + push ---------------------------------------------------------------

def record(conn, events, now, settings, silent=()):
    """Insert events not seen before. Kinds switched off are not recorded at
    all. `silent` kinds (first run's baseline) are recorded as already
    pushed so they never flood the phone."""
    fresh = 0
    stamp = now.isoformat(timespec="seconds")
    for e in events:
        if not settings["kinds"].get(e["kind"], True):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications (key, kind, item_id, title, "
            "body, priority, created_at, expires_at, pushed_at, read_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["key"], e["kind"], e["item_id"], e["title"], e["body"],
             e["priority"], stamp, e["expires_at"],
             stamp if e["kind"] in silent else None,
             stamp if e["kind"] in silent else None))
        fresh += cur.rowcount
    conn.commit()
    return fresh


def _topic():
    try:
        with open(TOPIC_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def post(topic, title, body, priority):
    req = urllib.request.Request(
        "%s/%s" % (NTFY_SERVER.rstrip("/"), topic), data=body.encode(),
        method="POST",
        headers={"Title": title, "Priority": priority,
                 "Click": "briefing://open", "Tags": "briefing", **auth()})
    urllib.request.urlopen(req, timeout=8).read()


def _lines(rs):
    """The body of a push: one line per event, the rest folded into "+N more"."""
    lines = [r["body"] for r in rs[:PUSH_LINES]]
    if len(rs) > PUSH_LINES:
        lines.append("+%d more" % (len(rs) - PUSH_LINES))
    return "\n".join(lines)


def _batch(kind, rs):
    """(title, body, priority, rows) for the events of one kind: a lone event
    keeps its own title, several share the kind's label with a count."""
    title = rs[0]["title"] if len(rs) == 1 else "%s · %d" % (KIND_LABEL[kind], len(rs))
    priority = "high" if any(r["priority"] == "high" for r in rs) else "default"
    return title, _lines(rs), priority, rs


# The item statuses an event of each kind needs at release time: the same
# filter the collector used when it recorded the event.
OPEN_FOR = {"deadline": ACTIVE, "opportunity": ("new", "open")}


def _still_true(conn, row, db_path):
    """Whether a held event still describes things at release time. A push
    held overnight for an item handled or dismissed since must not go out
    (2026-10-10), and neither may a failure alert for a source that recovered."""
    kind = row["kind"]
    if kind in OPEN_FOR and row["item_id"]:
        item = conn.execute("SELECT status FROM items WHERE id = ?",
                            (row["item_id"],)).fetchone()
        return item is not None and item["status"] in OPEN_FOR[kind]
    if kind == "source":
        name = row["key"].split(":")[1]
        s = (store.kv_get(db_path, "source_status", {}) or {}).get(name) or {}
        if row["key"].startswith("sourceok:"):
            return s.get("ok") is True
        return s.get("ok") is False
    return True


def send_pending(conn, db_path, now, settings, state, poster=post):
    """Push held events to the phone: nothing in quiet hours, at most `cap`
    pushes a day, one push per kind (or a single summary when several kinds
    are waiting)."""
    topic = _topic()
    if not topic:
        return 0
    if in_quiet(now, settings["quiet"]):
        return 0
    stamp = now.isoformat(timespec="seconds")
    # Stale held events (deadline passed while quiet) are dropped, not pushed.
    conn.execute("UPDATE notifications SET pushed_at = ? WHERE pushed_at IS NULL "
                 "AND expires_at IS NOT NULL AND expires_at <= ?", (stamp, stamp))
    rows = conn.execute("SELECT * FROM notifications WHERE pushed_at IS NULL "
                        "ORDER BY seq").fetchall()
    rows = [r for r in rows if settings["kinds"].get(r["kind"], True)]
    # Events no longer true are closed without a push, so they are not
    # re-checked on every tick either.
    gone = {r["seq"] for r in rows if not _still_true(conn, r, db_path)}
    conn.executemany("UPDATE notifications SET pushed_at = ? WHERE seq = ?",
                     [(stamp, seq) for seq in gone])
    rows = [r for r in rows if r["seq"] not in gone]
    conn.commit()
    if not rows:
        return 0
    used = state.setdefault("pushes", {})
    if used.get("date") != str(now.date()):
        used.update(date=str(now.date()), n=0)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    # The brief push is the one message read at a glance on the way out the
    # door, so it is never folded into a summary.
    briefs = by_kind.pop("brief", [])
    batches = [("Brief ready", briefs[-1]["body"], "default", briefs)
               ] if briefs else []
    if len(by_kind) > 3:
        rest = [r for rs in by_kind.values() for r in rs]
        batches.append(("Briefing · %d updates" % len(rest), _lines(rest),
                        "default", rest))
    else:
        batches += [_batch(k, rs) for k, rs in by_kind.items()]
    sent = 0
    for title, body, priority, rs in batches:
        if used["n"] >= settings["cap"]:
            break
        try:
            poster(topic, title, body, priority)
        except Exception as e:  # noqa: BLE001 -- retry on the next tick
            _log_error(conn, now, "push failed: %s" % e)
            break
        used["n"] += 1
        sent += 1
        conn.executemany("UPDATE notifications SET pushed_at = ? WHERE seq = ?",
                         [(stamp, r["seq"]) for r in rs])
        conn.commit()
    return sent


def _log_error(conn, now, message):
    store.append_errors(conn, [{
        "timestamp": now.isoformat(timespec="seconds"), "source": "notify",
        "severity": "warning", "message": message[:300]}])
    conn.commit()


def run(db_path=dbmod.DEFAULT_DB_PATH, now=None, poster=post):
    """Collect, record, push. Never raises."""
    now = now or datetime.now(TZ)
    conn = dbmod.connect(db_path)
    try:
        dbmod.init_db(db_path)
        settings = load_settings(db_path)
        state = store.kv_get(db_path, STATE_KEY, {}) or {}
        first = not state.get("baselined")
        events = []
        for name, fn in (("deadline", deadline_events), ("grade", grade_events),
                         ("opportunity", opportunity_events),
                         ("followup", followup_events)):
            try:
                events += fn(conn, now)
            except Exception as e:  # noqa: BLE001
                _log_error(conn, now, "%s events: %s" % (name, e))
        try:
            events += source_events(conn, db_path, now, state)
        except Exception as e:  # noqa: BLE001
            _log_error(conn, now, "source events: %s" % e)
        # Gated on the publish itself (meta.last_completed_date), not on the
        # run: the day's brief waits for a page that built, so a failed build
        # never announces yesterday's page (2026-10-10). The key is per day,
        # so it goes out once.
        if sources.built_today(db_path, now):
            events.append(brief_event(conn, now, db_path))
        # First run: what already exists is not news.
        silent = ("opportunity", "grade") if first else ()
        record(conn, events, now, settings, silent=silent)
        state["baselined"] = True
        sent = send_pending(conn, db_path, now, settings, state, poster)
        store.kv_set(db_path, STATE_KEY, state)
        return sent
    except Exception as e:  # noqa: BLE001
        try:
            _log_error(conn, now, "run: %s" % e)
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        conn.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("run", "test"))
    ap.add_argument("--db", default=dbmod.DEFAULT_DB_PATH)
    args = ap.parse_args(argv)
    if args.cmd == "test":
        topic = _topic()
        if not topic:
            print("no topic file: %s" % TOPIC_FILE)
            return 1
        post(topic, "Briefing test", "Test push", "default")
        print("sent")
        return 0
    # Silent when nothing went out: run_daily.sh calls this every 15 minutes,
    # and "pushed 0" x96 a day buried the real log lines.
    sent = run(args.db)
    if sent:
        print("pushed %d" % sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
