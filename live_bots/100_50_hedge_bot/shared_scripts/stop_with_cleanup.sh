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
BOT_SCRIPTS_DIR="${BOT_DIR}/scripts"
STATE_DIR="${BOT_GROUP_DIR}/state"
STATE_FILE="${STATE_DIR}/active_bot_symbols.json"
LOCK_FILE="${STATE_DIR}/active_bot_symbols.lock"
PYTHON_CMD="${LONG_BOT_SYMBOL_WATCHER_PYTHON:-python3}"
RUN_DIR="${BOT_DIR}/run"
PID_FILE="${RUN_DIR}/bot.pid"
LEGACY_PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
STATUS_FILE="${RUN_DIR}/status.json"
WAIT_PID_FILE="${RUN_DIR}/start_wait.pid"
CANCEL_START_FILE="${RUN_DIR}/cancel_start"

write_stopped_status() {
  python3 <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

path = Path("${STATUS_FILE}")
path.parent.mkdir(parents=True, exist_ok=True)

data = {
    "bot_name": "${BOT_NAME}",
    "status": "stopped",
    "start_requested": False,
    "updated_at": datetime.now(timezone.utc).isoformat()
}

path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

STOP_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/stop_bot.sh"
CANCEL_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/cancel_open_orders.sh"
CLOSE_POSITIONS_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/close_open_positions.sh"
HARD_RESET_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/hard_reset_bot.sh"

for script in "${STOP_SCRIPT}" "${CANCEL_SCRIPT}" "${CLOSE_POSITIONS_SCRIPT}" "${HARD_RESET_SCRIPT}"; do
  if [[ ! -x "${script}" ]]; then
    echo "Error: ${script} not executable" >&2
    exit 1
  fi
done

echo "[${BOT_NAME}] WARNING: this will stop the bot process before cleaning."
echo "[${BOT_NAME}] WARNING: this will cancel all open exchange orders for the detected symbol."
echo "[${BOT_NAME}] WARNING: this will close open positions using reduce-only market orders."
echo "[${BOT_NAME}] WARNING: local per-bot state/runtime files are cleaned here."
echo "[${BOT_NAME}] WARNING: logs are NOT cleaned in stop_with_cleanup (only on restart)."

if [[ -f "${WAIT_PID_FILE}" ]]; then
  WAIT_PID="$(cat "${WAIT_PID_FILE}" 2>/dev/null || true)"
  if [[ "${WAIT_PID}" =~ ^[0-9]+$ ]]; then
    touch "${CANCEL_START_FILE}"
    if kill -0 "${WAIT_PID}" 2>/dev/null; then
      echo "[${BOT_NAME}] canceling pending start (PID=${WAIT_PID})"
      kill "${WAIT_PID}" 2>/dev/null || true
      for i in {1..5}; do
        if ! kill -0 "${WAIT_PID}" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      if kill -0 "${WAIT_PID}" 2>/dev/null; then
        kill -KILL "${WAIT_PID}" 2>/dev/null || true
      fi
    fi
  fi
  rm -f "${WAIT_PID_FILE}" "${CANCEL_START_FILE}"
  write_stopped_status
  echo "[${BOT_NAME}] releasing symbol reservation..."
  set +e
  "${PYTHON_CMD}" "${BOT_GROUP_DIR}/shared_scripts/active_bot_symbols.py" \
    --bot-name "${BOT_NAME}" \
    release \
    --state-file "${STATE_FILE}" \
    --lock-file "${LOCK_FILE}"
  RELEASE_CODE=$?
  set -e

  if [[ ${RELEASE_CODE} -ne 0 ]]; then
    echo "[WARN] ${BOT_NAME} release reservation failed (exit_code=${RELEASE_CODE})" >&2
  fi

  exit 0
fi

SIDE="long"
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

set +e
 "${STOP_SCRIPT}" "${BOT_NAME}"
STOP_CODE=$?
set -e

if [[ ${STOP_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} stop failed (exit_code=${STOP_CODE})" >&2
  exit 1
fi

CANCEL_CODE=0
set +e
"${CANCEL_SCRIPT}" "${BOT_NAME}"
CANCEL_CODE=$?
set -e

if [[ ${CANCEL_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} cancel open orders failed (exit_code=${CANCEL_CODE})" >&2
  exit 1
fi

CLOSE_CODE=0
set +e
"${CLOSE_POSITIONS_SCRIPT}" "${BOT_NAME}"
CLOSE_CODE=$?
set -e

if [[ ${CLOSE_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} close positions failed (exit_code=${CLOSE_CODE})" >&2
  exit 1
fi

CANCEL_CODE_SECOND=0
set +e
"${CANCEL_SCRIPT}" "${BOT_NAME}"
CANCEL_CODE_SECOND=$?
set -e

if [[ ${CANCEL_CODE_SECOND} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} cancel open orders (second pass) failed (exit_code=${CANCEL_CODE_SECOND})" >&2
  exit 1
fi

set +e
"${HARD_RESET_SCRIPT}" "${BOT_NAME}"
RESET_CODE=$?
set -e

if [[ ${RESET_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} hard reset failed (exit_code=${RESET_CODE})" >&2
  exit 1
fi

echo "[${BOT_NAME}] stop_with_cleanup_success"

echo "[${BOT_NAME}] releasing symbol reservation..."
set +e
"${PYTHON_CMD}" "${BOT_GROUP_DIR}/shared_scripts/active_bot_symbols.py" \
  --bot-name "${BOT_NAME}" \
  release \
  --state-file "${STATE_FILE}" \
  --lock-file "${LOCK_FILE}"
RELEASE_CODE=$?
set -e

if [[ ${RELEASE_CODE} -ne 0 ]]; then
  echo "[WARN] ${BOT_NAME} release reservation failed (exit_code=${RELEASE_CODE})" >&2
fi

rm -f "${PID_FILE}" "${LEGACY_PID_FILE}"
write_stopped_status
