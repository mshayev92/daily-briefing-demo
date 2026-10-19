"""The briefing's data sources: which are on, how often each is refreshed,
and when each last ran (2026-09-29).

Two kinds of source:

- **Scrapers** (canvas, handshake) are separate programs that write their
  own output file. `refresh_due()` runs the ones that are due; the brief
  reads their latest output on every build.
- **Pipeline sources** (gmail, calendar, weather, campus, discovery) are
  fetched by the orchestrator while it builds the brief. On a build where
  one is not due, the orchestrator skips it: items it produced earlier stay
  in state, and the calendar/weather result is reused from `source_cache`
  when it is from the same day.

The page's Sources sheet edits the settings (kv "source_settings"; only
the page writes it); the runs record status (kv "source_status"). Two timers drive it
(service/systemd/): the 06:30 morning build, and a 15-minute tick that
refreshes whatever is due and rebuilds the page when anything new came in.
If the day's build has not published, the tick retries it hourly, up to
BUILD_ATTEMPTS_PER_DAY builds a day.

Interval rule: "daily" (24 h) means once per day, at or after 06:30. A
shorter interval means that much time since the last attempt; a failed
attempt is retried after at most an hour.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time as _time, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db as dbmod
import store

TZ = ZoneInfo("America/New_York")
MORNING = _time(6, 30)
INTERVALS = (1, 2, 4, 8, 12, 24)       # hours; 24 = daily
DAILY = 24
GRACE = timedelta(minutes=5)           # timer jitter
RETRY_AFTER_FAILURE = timedelta(hours=1)
BUILD_ATTEMPTS_PER_DAY = 6             # the 06:30 build, then hourly retries
SETTINGS_KEY = "source_settings"
STATUS_KEY = "source_status"
CACHE_KEY = "source_cache:%s"
BUILD_LOG_KEY = "build_log"

HOME = os.path.expanduser("~")
CANVAS_DIR = os.environ.get("CANVAS_SCRAPER_DIR",
                            os.path.join(HOME, "projects", "canvas-scraper"))
HANDSHAKE_DIR = os.environ.get("HANDSHAKE_SCRAPER_DIR",
                               os.path.join(HOME, "projects", "handshake-scraper"))

# name -> what the Sources sheet shows. Order is the sheet's order.
SOURCES = {
    "canvas": {"label": "Canvas", "kind": "scraper",
               "feeds": "Assignments, due dates, grades and What changed "
                        "(canvas-scraper)"},
    "handshake": {"label": "Handshake", "kind": "scraper",
                  "feeds": "Internships for your goals, career fairs and "
                           "employer events (handshake-scraper)"},
    "gmail": {"label": "Email", "kind": "pipeline",
              "feeds": "Deadlines, events and opportunities from your inbox"},
    "calendar": {"label": "Calendar", "kind": "pipeline",
                 "feeds": "Today's schedule from your Class and Personal "
                          "calendars"},
    "weather": {"label": "Weather", "kind": "pipeline",
                "feeds": "Today's forecast and the umbrella line"},
    "campus": {"label": "Campus events", "kind": "pipeline",
               "feeds": "Events from calendar.umd.edu, ranked by your "
                        "preferences"},
    "discovery": {"label": "Web search", "kind": "pipeline",
                  "feeds": "Searches for the programs in your goals "
                           "(uses Claude)"},
}
SCRAPERS = [n for n, s in SOURCES.items() if s["kind"] == "scraper"]


def default_settings():
    return {name: {"enabled": True, "interval_hours": DAILY} for name in SOURCES}


def clean_settings(raw):
    """Stored/submitted settings -> a complete, valid dict. Unknown names
    are dropped; a bad interval falls back to daily."""
    out = default_settings()
    for name, s in (raw or {}).items():
        if name not in out or not isinstance(s, dict):
            continue
        out[name]["enabled"] = bool(s.get("enabled", True))
        hours = s.get("interval_hours")
        out[name]["interval_hours"] = hours if hours in INTERVALS else DAILY
    return out


def load_settings(db_path=dbmod.DEFAULT_DB_PATH):
    return clean_settings(store.kv_get(db_path, SETTINGS_KEY))


def save_settings(db_path, settings):
    """The page's write (PUT /sources)."""
    settings = clean_settings(settings)
    store.kv_set(db_path, SETTINGS_KEY, settings)
    return settings


def load_status(db_path=dbmod.DEFAULT_DB_PATH):
    return store.kv_get(db_path, STATUS_KEY, {}) or {}


def record(db_path, name, ok, now=None, detail=None):
    """One attempt at `name`: always stamps last_attempt; a success also
    stamps last_success and clears the error."""
    now = (now or datetime.now(TZ)).isoformat(timespec="seconds")
    status = load_status(db_path)
    s = dict(status.get(name) or {})
    s["last_attempt"] = now
    s["ok"] = bool(ok)
    if ok:
        s["last_success"] = now
        s.pop("error", None)
        if detail:
            s["detail"] = detail
    else:
        s["error"] = str(detail or "failed")[:300]
    status[name] = s
    store.kv_set(db_path, STATUS_KEY, status)


def _parse(stamp):
    try:
        dt = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def next_due(setting, status, now):
    """When a source is next fetched (aware, ET), or None when it is off."""
    if not setting.get("enabled", True):
        return None
    last = _parse((status or {}).get("last_attempt"))
    if last is None:
        return now
    hours = setting.get("interval_hours", DAILY)
    if (status or {}).get("ok") is False:
        return last + min(RETRY_AFTER_FAILURE, timedelta(hours=hours)) - GRACE
    if hours >= DAILY:
        morning = datetime.combine(now.date(), MORNING, tzinfo=TZ)
        return morning if last.astimezone(TZ) < morning \
            else morning + timedelta(days=1)
    return last + timedelta(hours=hours) - GRACE


def is_due(setting, status, now):
    """Whether a source should be fetched at `now` (aware, ET)."""
    when = next_due(setting, status, now)
    return when is not None and now >= when


def due_now(db_path=dbmod.DEFAULT_DB_PATH, now=None, kind=None):
    now = now or datetime.now(TZ)
    settings, status = load_settings(db_path), load_status(db_path)
    return [n for n in SOURCES
            if (kind is None or SOURCES[n]["kind"] == kind)
            and is_due(settings[n], status.get(n), now)]


# --- same-day cache for calendar/weather ------------------------------------

def cache_put(db_path, name, today, value):
    store.kv_set(db_path, CACHE_KEY % name, {"date": today.isoformat(),
                                              "value": value})


def cache_get(db_path, name, today):
    """The cached value when it was fetched today, else None (a new day
    always needs a fresh schedule and forecast)."""
    got = store.kv_get(db_path, CACHE_KEY % name)
    if got and got.get("date") == today.isoformat():
        return got.get("value")
    return None


# --- scrapers -----------------------------------------------------------------

def handshake_queries(db_path=dbmod.DEFAULT_DB_PATH):
    """Job searches beyond the scraper's own generic defaults: one per
    keyword of each active goal (the programs he named)."""
    conn = dbmod.connect(db_path)
    try:
        rows = conn.execute("SELECT keywords_json FROM requirements "
                            "WHERE active = 1").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        for kw in json.loads(r[0] or "[]"):
            kw = str(kw).strip()
            if kw and kw.lower() not in (q.lower() for q in out):
                out.append(kw)
    return out


def scraper_command(name, db_path=dbmod.DEFAULT_DB_PATH):
    """(argv, cwd, env additions, timeout seconds) for one scraper."""
    if name == "canvas":
        return ([os.path.join(CANVAS_DIR, ".venv/bin/python"), "-m",
                 "canvas_scraper.cli.run"], CANVAS_DIR, {"DISPLAY": ":99"}, 900)
    if name == "handshake":
        argv = [os.path.join(HANDSHAKE_DIR, ".venv/bin/python"), "-m",
                "handshake_scraper"]
        for q in handshake_queries(db_path):
            argv += ["--query", q]
        return argv, HANDSHAKE_DIR, {}, 300
    raise KeyError(name)


_SECRET = re.compile(r"(cookie|authorization|csrf|session)[\w-]*\s*[:=]\s*\S.*", re.I)


def _failure_line(err):
    """One line explaining a scraper failure, without request headers.

    A Playwright error ends in an indented dump of request headers (session
    cookies); the last line of stderr is then a secret, not the reason."""
    clean = [l for l in err if not _SECRET.search(l)]
    for l in reversed(clean):
        if l and not l[0].isspace():
            return l
    return clean[-1] if clean else "failed (details withheld)"


def run_scraper(name, db_path=dbmod.DEFAULT_DB_PATH, now=None, runner=None):
    """Run one scraper, record the outcome, return True on success."""
    argv, cwd, env_add, timeout = scraper_command(name, db_path)
    runner = runner or subprocess.run
    try:
        proc = runner(argv, cwd=cwd, env={**os.environ, **env_add},
                      capture_output=True, text=True, timeout=timeout)
        ok = proc.returncode == 0
        out = (proc.stdout or "").strip().splitlines()
        err = (proc.stderr or "").strip().splitlines()
        detail = (out[-1] if ok and out else
                  (_failure_line(err) if err else "exit %d" % proc.returncode))
    except FileNotFoundError:
        ok, detail = False, "%s is not installed at %s" % (name, cwd)
    except subprocess.TimeoutExpired:
        ok, detail = False, "timed out after %d s" % timeout
    record(db_path, name, ok, now=now, detail=detail)
    print("%s refresh %s: %s" % (name, "ok" if ok else "FAILED", detail))
    return ok


def refresh_due(db_path=dbmod.DEFAULT_DB_PATH, now=None):
    """Run every scraper that is due. -> names that refreshed OK."""
    return [n for n in due_now(db_path, now, kind="scraper")
            if run_scraper(n, db_path, now)]


def built_today(db_path, now):
    """Whether today's morning build has happened."""
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute("SELECT last_completed_date FROM meta").fetchone()
    except Exception:  # noqa: BLE001
        row = None
    finally:
        conn.close()
    return bool(row) and row[0] == now.date().isoformat()


def next_fetch(name, setting, status, now, built):
    """next_due() as the Sources sheet shows it: a pipeline source is only
    fetched by a build, and the day's first build is the 06:30 one."""
    when = next_due(setting, status, now)
    if when is None:
        return None
    when = max(when, now)
    if SOURCES[name]["kind"] == "pipeline" and not built:
        morning = datetime.combine(now.date(), MORNING, tzinfo=TZ)
        when = max(when, morning)
    return when


def _attempts_today(db_path, now):
    """(builds started today, when the last one started)."""
    try:
        log = store.kv_get(db_path, BUILD_LOG_KEY, {}) or {}
    except sqlite3.Error:      # no kv table yet: no build has run
        log = {}
    if log.get("date") != now.date().isoformat():
        return 0, None
    return int(log.get("n") or 0), _parse(log.get("last"))


def record_build_attempt(db_path=dbmod.DEFAULT_DB_PATH, now=None):
    """Count one build against today. run_daily.sh calls this before every
    live build, so a build that dies on startup still counts."""
    now = now or datetime.now(TZ)
    n, _ = _attempts_today(db_path, now)
    store.kv_set(db_path, BUILD_LOG_KEY, {
        "date": now.date().isoformat(), "n": n + 1,
        "last": now.isoformat(timespec="seconds")})
    return n + 1


def retry_left(db_path=dbmod.DEFAULT_DB_PATH, now=None):
    """Whether today's page is still unbuilt and another build may run. When
    False after a failure, the failure is final for the day: the orchestrator
    then sends the day's email as a failure notice."""
    now = now or datetime.now(TZ)
    if built_today(db_path, now):
        return False
    return _attempts_today(db_path, now)[0] < BUILD_ATTEMPTS_PER_DAY


def build_wanted(db_path=dbmod.DEFAULT_DB_PATH, now=None, refreshed=()):
    """Whether a 15-minute tick should rebuild the page.

    Once the day's page has published: only when a scraper just brought new
    data or a pipeline source is due. Before it has published (the morning
    build failed, or never ran): build again, hourly, up to
    BUILD_ATTEMPTS_PER_DAY times a day, so a failed morning does not leave
    yesterday's page up all day. The day's email goes out with the first
    publish (orchestrator step10), so retries never send a second one.
    """
    now = now or datetime.now(TZ)
    if now.time() < MORNING:
        return False
    if built_today(db_path, now):
        return bool(refreshed) or bool(due_now(db_path, now, kind="pipeline"))
    attempts, last = _attempts_today(db_path, now)
    if attempts == 0:
        # The morning build has not started. Both timers fire at 06:30, and
        # a tick that wins the lock must not build ahead of it, so a tick
        # waits an hour before it catches up.
        return now >= datetime.combine(now.date(), MORNING, TZ) \
            + RETRY_AFTER_FAILURE
    return attempts < BUILD_ATTEMPTS_PER_DAY and (
        last is None or now >= last + RETRY_AFTER_FAILURE)


def main(argv=None):
    """CLI for run_daily.sh:
        sources.py refresh        run due scrapers; print which refreshed
        sources.py tick           refresh, exit 0 if a rebuild is wanted
        sources.py attempt        count one build against today (before it)
        sources.py status         settings + last runs, as JSON
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("refresh", "tick", "attempt", "status"))
    ap.add_argument("--db", default=dbmod.DEFAULT_DB_PATH)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({"settings": load_settings(args.db),
                          "status": load_status(args.db)}, indent=2))
        return 0
    if args.cmd == "attempt":
        record_build_attempt(args.db)
        return 0
    refreshed = refresh_due(args.db)
    if args.cmd == "refresh":
        return 0
    return 0 if build_wanted(args.db, refreshed=refreshed) else 10


if __name__ == "__main__":
    sys.exit(main())
