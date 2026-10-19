#!/usr/bin/env bash
# The unattended run: refresh due scrapers -> build + deliver the briefing
# -> back up the state DB. Two timers invoke it (service/systemd/):
#   run_daily.sh          06:30 morning build (daily-briefing.timer)
#   run_daily.sh --tick   every 15 min: refresh what the Sources settings
#                         say is due, rebuild if anything new came in
#                         (briefing-sources.timer)
# Safe to run by hand too.
#
# Until the 2026-09-19 audit NOTHING scheduled this pipeline: every briefing
# ever delivered was a hand-started `orchestrator.py --live`, and
# canvas-scraper's output was only as fresh as the last time someone ran
# `canvas refresh` manually. This script is the one place that sequence
# lives.
#
# Exit status is the orchestrator's (non-zero = a CRITICAL error; the
# orchestrator has already logged it to briefing.db and, on a crash, sent
# the day's email as a failure notice).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/venv/bin/python"
LOG_DIR="$ROOT/logs"
MODE=morning
[ "${1:-}" = "--tick" ] && MODE=tick
mkdir -p "$LOG_DIR"

# `claude` (the orchestrator's LLM calls) lives in ~/.local/bin; systemd's
# default PATH does not include it.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export TZ="America/New_York"

exec >>"$LOG_DIR/daily-$(date +%F).log" 2>&1

# One run at a time: a manual run and the timers must never interleave (two
# concurrent wholesale state saves would each discard the other's work).
# The morning build waits for a tick in progress (a Canvas refresh can take
# 15 min); a tick that finds the lock taken just skips its turn.
exec 9>"$LOG_DIR/.daily.lock"
if [ "$MODE" = tick ]; then flock -n 9; else flock -w 2400 9; fi || {
    echo "$(date -Is) another run holds the lock -- exiting ($MODE)"
    exit 0
}

# A tick that finds nothing due stays out of the log.
[ "$MODE" = morning ] && echo "=== $(date -Is) daily briefing run (morning)"

# 1. Scrapers (canvas-scraper, handshake-scraper) that are due under the
#    page's Sources settings (sources.py). Best effort: a failure (expired
#    UMD login, network) is recorded for the Sources sheet and the brief
#    proceeds on the last scrape -- the orchestrator flags stale data.
#    A tick builds the page only when something new came in or a pipeline
#    source is due, and never before the morning build (which sends the
#    day's email).
cd "$HERE" || exit 1
if [ "$MODE" = "tick" ]; then
    "$PY" sources.py tick
    want=$?
    # Deadlines approach whether or not anything is due: check on every tick,
    # before the early exit. Never affects the exit status.
    "$PY" notify.py run || echo "notify FAILED (exit $?)"
    [ "$want" = 10 ] && exit 0
    [ "$want" = 0 ] || { echo "sources tick FAILED (exit $want)"; exit "$want"; }
    echo "=== $(date -Is) tick: rebuilding with new data"
elif [ "${SKIP_CANVAS_REFRESH:-0}" != "1" ]; then
    "$PY" sources.py refresh || echo "sources refresh FAILED (exit $?)"
fi

# 2. The briefing itself, fetching only the sources that are due.
echo "--- orchestrator --live --due-only"
"$PY" sources.py attempt
"$PY" orchestrator.py --live --due-only
rc=$?
echo "orchestrator exit $rc"

# 2b. App notifications after the build: the events its new data produced,
#     and the brief-ready push once the day's page has published. notify.py
#     checks the publish itself, so a build that fails here sends no brief,
#     and a retry that publishes later does. Best effort.
"$PY" notify.py run || echo "notify FAILED (exit $?)"

# 3. Off-machine backup of the post-run state (Drive), once a day: Drive
#    keeps the 14 newest, so hourly rebuilds would push out two weeks of
#    history. Never affects rc.
STAMP="$LOG_DIR/.last_backup_date"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$(date +%F)" ]; then
    echo "--- backup"
    "$PY" backup.py && date +%F >"$STAMP" || echo "backup FAILED (exit $?)"
fi

# Keep a month of daily logs.
find "$LOG_DIR" -maxdepth 1 -name 'daily-*.log' -mtime +30 -delete 2>/dev/null
echo "=== $(date -Is) done (rc=$rc)"
exit "$rc"
