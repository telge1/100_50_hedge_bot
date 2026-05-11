#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ${BASH_SOURCE[0]} long_bot_<number>" >&2
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

BOT_NAME="$1"
if [[ ! "${BOT_NAME}" =~ ^long_bot_[0-9]+$ ]]; then
  usage
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_GROUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_GROUP_DIR}/../.." && pwd)"
BOT_DIR="${BOT_GROUP_DIR}/${BOT_NAME}"

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

if [[ -z "${PYTHON:-}" ]]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi

CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
LOG_DIR="${BOT_DIR}/logs"
PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
SNAPSHOT_FILE="${BOT_DIR}/snapshots/fixed_cycle_wallet_snapshot.json"
AUDIT_LOG="${LOG_DIR}/generic_hedge_runtime_audit.jsonl"
RUNNER_STDOUT="${LOG_DIR}/fixed_cycle_runner.stdout.log"

mkdir -p "${BOT_DIR}/config" "${BOT_DIR}/state" "${BOT_DIR}/logs" "${BOT_DIR}/snapshots" "${BOT_DIR}/pids"

HARD_RESET_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/hard_reset_bot.sh"
CLEAN_LOGS_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/clean_bot_logs.sh"

SIDE="long"
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

echo "[${BOT_NAME}] Running hard reset..."
set +e
 "${HARD_RESET_SCRIPT}" "${BOT_NAME}"
RESET_CODE=$?
set -e

echo "[${BOT_NAME}] hard-reset exit code: ${RESET_CODE}"

if [[ "${RESET_CODE}" -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} hard-reset failed (exit_code=${RESET_CODE}); aborting start." >&2
  exit 1
fi

echo "[${BOT_NAME}] Cleaning log files..."
 "${CLEAN_LOGS_SCRIPT}" "${BOT_NAME}"

if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${OLD_PID}" ]]; then
    if kill -0 "${OLD_PID}" 2>/dev/null; then
      CMDLINE="$(ps -p "${OLD_PID}" -o args= 2>/dev/null || true)"
      if [[ "${CMDLINE}" == *fixed_cycle_hedge_bot.runner* ]] && ([[ "${CMDLINE}" == *"${CONFIG_FILE}"* ]] || [[ "${CMDLINE}" == *"${STATE_FILE}"* ]]); then
        echo "${BOT_NAME} already running with PID=${OLD_PID}"
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

# refresh creds before launch
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

nohup "${PYTHON}" -m fixed_cycle_hedge_bot.runner \
  --strategy fixed_cycle \
  --bot-name "${BOT_NAME}" \
  --strategy-config-file "${CONFIG_FILE}" \
  --strategy-state-file "${STATE_FILE}" \
  --audit-log-file "${AUDIT_LOG}" \
  --calc-audit-log-file "${LOG_DIR}/fixed_cycle_calc_audit.log" \
  --confirmed-pnl-history-file "${LOG_DIR}/confirmed_order_pnl_history.jsonl" \
  --log-file "${LOG_DIR}/fixed_cycle_hedge_runtime.log" \
  > "${RUNNER_STDOUT}" 2>&1 &

PID=$!
printf "%s" "$PID" > "${PID_FILE}"
sleep 1
if ! kill -0 "${PID}" 2>/dev/null; then
  echo "[ERROR] ${BOT_NAME} failed to stay running after start" >&2
  echo "[ERROR] Last stdout log lines:" >&2
  tail -n 80 "${RUNNER_STDOUT}" >&2 || true
  rm -f "${PID_FILE}"
  exit 1
fi
echo "Fixed-cycle ${BOT_NAME} started via nohup (${RUNNER_STDOUT})"
echo "Started ${BOT_NAME} with PID=$PID"
