#!/usr/bin/env bash
# The unattended daily run: refresh Canvas -> build + deliver the briefing
# -> back up the state DB. Invoked by systemd (systemd/daily-briefing.timer);
# safe to run by hand too.
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
CANVAS_DIR="${CANVAS_SCRAPER_DIR:-$HOME/canvas-scraper}"
mkdir -p "$LOG_DIR"

# `claude` (the orchestrator's LLM calls) lives in ~/.local/bin; systemd's
# default PATH does not include it.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export TZ="America/New_York"

exec >>"$LOG_DIR/daily-$(date +%F).log" 2>&1

# One run at a time: a manual run and the timer must never interleave (two
# concurrent wholesale state saves would each discard the other's work).
exec 9>"$LOG_DIR/.daily.lock"
if ! flock -n 9; then
    echo "$(date -Is) another run holds the lock -- exiting"
    exit 0
fi

echo "=== $(date -Is) daily briefing run"

# 1. Canvas refresh -- best effort. The scraper drives a headed browser, so
#    it needs an X display; reuse :99 when an Xvfb is already serving it,
#    otherwise start a private one for the duration. A failure (expired
#    Canvas login, network) is logged and the briefing proceeds on the last
#    scrape -- the orchestrator itself flags the data as stale when it is.
if [ "${SKIP_CANVAS_REFRESH:-0}" != "1" ] && [ -x "$CANVAS_DIR/.venv/bin/python" ]; then
    XVFB_PID=""
    if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
        if command -v Xvfb >/dev/null 2>&1; then
            Xvfb :99 -screen 0 1280x720x24 >/dev/null 2>&1 &
            XVFB_PID=$!
            sleep 2
        fi
    fi
    echo "--- canvas refresh"
    (cd "$CANVAS_DIR" && DISPLAY=:99 timeout 900 .venv/bin/python -m canvas_scraper.cli.run) \
        && echo "canvas refresh ok" \
        || echo "canvas refresh FAILED (exit $?) -- continuing with the last scrape"
    [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
fi

# 2. The briefing itself.
echo "--- orchestrator --live"
cd "$HERE" || exit 1
"$PY" orchestrator.py --live
rc=$?
echo "orchestrator exit $rc"

# 3. Off-machine backup of the post-run state (Drive). Never affects rc.
echo "--- backup"
"$PY" backup.py || echo "backup FAILED (exit $?)"

# Keep a month of daily logs.
find "$LOG_DIR" -maxdepth 1 -name 'daily-*.log' -mtime +30 -delete 2>/dev/null
echo "=== $(date -Is) done (rc=$rc)"
exit "$rc"
