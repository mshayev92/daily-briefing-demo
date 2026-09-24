#!/usr/bin/env bash
# Install/refresh the user-level systemd units for the Daily Briefing.
#   ./install.sh            install + enable timer and page server
#   ./install.sh --remove   disable and remove them
# Units reference ~/daily-briefing (a symlink to this project), which keeps
# the space in "Documents/Daily Briefing" out of systemd's ExecStart.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS="daily-briefing.service daily-briefing.timer briefing-page.service"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now daily-briefing.timer briefing-page.service || true
    for u in $UNITS; do rm -f "$UNIT_DIR/$u"; done
    systemctl --user daemon-reload
    exit 0
fi

[ -e "$HOME/daily-briefing" ] || ln -s "$(dirname "$(dirname "$HERE")")" "$HOME/daily-briefing"
mkdir -p "$UNIT_DIR"
for u in $UNITS; do install -m 0644 "$HERE/$u" "$UNIT_DIR/$u"; done
systemctl --user daemon-reload
systemctl --user enable --now daily-briefing.timer
systemctl --user enable --now briefing-page.service
# User units only run while the user has a session unless lingering is on.
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "NOTE: could not enable linger; run 'sudo loginctl enable-linger $USER' so the timer fires without a login session"
systemctl --user list-timers daily-briefing.timer --no-pager
