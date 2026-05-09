#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${PYTHON:-}" ]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi
cd "${PROJECT_ROOT}" || exit 1

BOT_DIR="live_bots/100_50_hedge_bot/long_bot_1"
PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"

mkdir -p \
  "${BOT_DIR}/config" \
  "${BOT_DIR}/state" \
  "${BOT_DIR}/logs" \
  "${BOT_DIR}/snapshots" \
  "${BOT_DIR}/pids"

if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "long_bot_1 already running with PID=${OLD_PID}"
    exit 0
  fi
fi

nohup "${PYTHON}" -m fixed_cycle_hedge_bot.runner \
  --strategy fixed_cycle \
  --bot-name long_bot_1 \
  --strategy-config-file live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json \
  --strategy-state-file live_bots/100_50_hedge_bot/long_bot_1/state/fixed_cycle_state.json \
  --audit-log-file live_bots/100_50_hedge_bot/long_bot_1/logs/generic_hedge_runtime_audit.jsonl \
  --calc-audit-log-file live_bots/100_50_hedge_bot/long_bot_1/logs/fixed_cycle_calc_audit.log \
  --confirmed-pnl-history-file live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl \
  --log-file live_bots/100_50_hedge_bot/long_bot_1/logs/fixed_cycle_hedge_runtime.log \
  > live_bots/100_50_hedge_bot/long_bot_1/logs/fixed_cycle_runner.stdout.log 2>&1 &
PID=$!
echo "$PID" > "${PID_FILE}"
echo "Fixed-cycle long_bot_1 started via nohup (live_bots/100_50_hedge_bot/long_bot_1/logs/fixed_cycle_runner.stdout.log)"
echo "Started long_bot_1 with PID=$PID"
