#!/usr/bin/env bash
# Restart Spread Recovery dashboard (same env as prior manual/Cursor starts).
# Does not use systemd while the user unit is in failed state; use setsid for SSH detach.
set -euo pipefail
ROOT="/home/telgenbuescher/projects/spread_recovery_hedge_short_dev"
DASH="$ROOT/dashboard"
VENV="$ROOT/.venv/bin/python"
export PYTHONPATH="$ROOT:$DASH:/home/telgenbuescher/projects/pools+ob+delta_bot:/home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short"
export POOLS_OB_DELTA_BOT_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"

if pgrep -f "$ROOT/.venv/bin/python app.py" >/dev/null 2>&1 || ss -tlnp 2>/dev/null | grep -q ':3000.*python'; then
  echo "Dashboard already listening on :3000"; exit 1
fi
mkdir -p "$DASH/logs"
cd "$DASH"
setsid "$VENV" app.py >>logs/dashboard_tf1d_local.log 2>&1 &
echo $! >"$DASH/logs/dashboard.pid"
echo "Started dashboard pid=$(cat "$DASH/logs/dashboard.pid") — log: $DASH/logs/dashboard_tf1d_local.log"
