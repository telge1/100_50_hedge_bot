#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
if [ -z "${PYTHON:-}" ]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi
cd "${PROJECT_ROOT}" || exit 1

CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
LOG_DIR="${BOT_DIR}/logs"
PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
SNAPSHOT_FILE="${BOT_DIR}/snapshots/fixed_cycle_wallet_snapshot.json"

mkdir -p "${BOT_DIR}/config" "${BOT_DIR}/state" "${BOT_DIR}/logs" "${BOT_DIR}/snapshots" "${BOT_DIR}/pids"

HARD_RESET_SCRIPT="${BOT_DIR}/scripts/hard_reset.sh"
CLEAN_LOGS_SCRIPT="${BOT_DIR}/scripts/clean_logs.sh"

source "${PROJECT_ROOT}/scripts/load_bybit_env.sh" long_bot_2 long

echo "[long_bot_2] Running hard reset..."
set +e
"${HARD_RESET_SCRIPT}"
RESET_CODE=$?
set -e

echo "[long_bot_2] hard-reset exit code: ${RESET_CODE}"

if [[ "${RESET_CODE}" -ne 0 ]]; then
  echo "[ERROR] long_bot_2 hard-reset failed (exit_code=${RESET_CODE}); aborting start." >&2
  exit 1
fi

echo "[long_bot_2] Cleaning log files..."
"${CLEAN_LOGS_SCRIPT}"

if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${OLD_PID}" ]]; then
    if kill -0 "${OLD_PID}" 2>/dev/null; then
      CMDLINE="$(ps -p "${OLD_PID}" -o args= 2>/dev/null || true)"
      if [[ "${CMDLINE}" == *fixed_cycle_hedge_bot.runner* ]] && ([[ "${CMDLINE}" == *"${CONFIG_FILE}"* ]] || [[ "${CMDLINE}" == *"${STATE_FILE}"* ]]); then
        echo "long_bot_2 already running with PID=${OLD_PID}"
        exit 0
      else
        echo "stale PID file points to unrelated process ${OLD_PID}, removing" >&2
        rm -f "${PID_FILE}"
      fi
    else
      echo "PID ${OLD_PID} not running, removing PID file"
      rm -f "${PID_FILE}"
    fi
  fi
fi

source "${PROJECT_ROOT}/scripts/load_bybit_env.sh" long_bot_2 long

nohup "${PYTHON}" -m fixed_cycle_hedge_bot.runner \
  --strategy fixed_cycle \
  --bot-name long_bot_2 \
  --strategy-config-file "${CONFIG_FILE}" \
  --strategy-state-file "${STATE_FILE}" \
  --audit-log-file "${LOG_DIR}/generic_hedge_runtime_audit.jsonl" \
  --calc-audit-log-file "${LOG_DIR}/fixed_cycle_calc_audit.log" \
  --confirmed-pnl-history-file "${LOG_DIR}/confirmed_order_pnl_history.jsonl" \
  --log-file "${LOG_DIR}/fixed_cycle_hedge_runtime.log" \
  > "${LOG_DIR}/fixed_cycle_runner.stdout.log" 2>&1 &

PID=$!
printf "%s" "$PID" > "${PID_FILE}"
sleep 1
if ! kill -0 "${PID}" 2>/dev/null; then
  echo "[ERROR] long_bot_2 failed to stay running after start" >&2
  echo "[ERROR] Last stdout log lines:" >&2
  tail -n 80 "${LOG_DIR}/fixed_cycle_runner.stdout.log" >&2 || true
  rm -f "${PID_FILE}"
  exit 1
fi
echo "Fixed-cycle long_bot_2 started via nohup (${LOG_DIR}/fixed_cycle_runner.stdout.log)"
echo "Started long_bot_2 with PID=$PID"
