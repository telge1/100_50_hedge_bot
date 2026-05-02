#!/bin/bash

set -uo pipefail

PROJECT_ROOT="/home/telgenbuescher/projects/spread_recovery_hedge"
PYTHON_CMD="${PROJECT_ROOT}/.venv/bin/python"
BEST_COIN_FILE="${PROJECT_ROOT}/logs/best_coin.json"

cleanup_symbol=""
if [[ -f "${BEST_COIN_FILE}" ]]; then
  cleanup_symbol="$("${PYTHON_CMD}" - <<PY
import json
from pathlib import Path

path = Path("${BEST_COIN_FILE}")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
symbol = data.get("symbol")
if symbol:
    print(symbol.upper())
PY
)"
  cleanup_symbol=${cleanup_symbol//$'\n'/}
  cleanup_symbol=${cleanup_symbol//$'\r'/}
fi

echo "Stopping fixed cycle bot via hard-reset..."
cd "${PROJECT_ROOT}" || exit 1

if [[ -n "${cleanup_symbol}" ]]; then
  echo "Canceling open orders for symbol ${cleanup_symbol}"
  BOT_CONTROL_CLEANUP_SYMBOL="${cleanup_symbol}" BOT_CONTROL_PYTHON="${PYTHON_CMD}" ./bot_control.sh hard-reset
else
  echo "No dynamic symbol detected; falling back to config defaults"
  BOT_CONTROL_PYTHON="${PYTHON_CMD}" ./bot_control.sh hard-reset
fi

RESET_CODE=$?
echo "hard-reset exit code: ${RESET_CODE}"
