#!/usr/bin/env bash
# Expose the FastAPI service over the tailnet (Phase 0).
#
# BLOCKED as of 2026-09-15: this tailnet has Serve disabled at the account
# level. Running `tailscale serve` returns:
#
#   Serve is not enabled on your tailnet.
#   To enable, visit:
#            https://login.tailscale.com/f/serve?node=YOUR_NODE_ID
#
# That's a one-time account-level toggle only Michael can approve (it's not
# a sudo/permission issue on this box — `sudo tailscale serve` hit the same
# wall after prompting for a password this session doesn't have). Visit the
# URL above once, then this script works.
set -euo pipefail

PORT="${1:-8811}"
tailscale serve --bg "$PORT"
tailscale serve status
