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
STATE_DIR="${BOT_GROUP_DIR}/state"
ACTIVE_SYMBOLS_STATE_FILE="${STATE_DIR}/active_bot_symbols.json"
ACTIVE_SYMBOLS_LOCK_FILE="${STATE_DIR}/active_bot_symbols.lock"
PYTHON_CMD="${LONG_BOT_SYMBOL_WATCHER_PYTHON:-python3}"
SYMBOL_MANAGER_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/active_bot_symbols.py"

release_symbol_reservation() {
  set +e
  "${PYTHON_CMD}" "${SYMBOL_MANAGER_SCRIPT}" \
    --bot-name "${BOT_NAME}" \
    release \
    --state-file "${ACTIVE_SYMBOLS_STATE_FILE}" \
    --lock-file "${ACTIVE_SYMBOLS_LOCK_FILE}"
  RELEASE_CODE=$?
  set -e
  if [[ ${RELEASE_CODE} -ne 0 ]]; then
    echo "[WARN] ${BOT_NAME} release reservation failed (exit_code=${RELEASE_CODE})" >&2
  fi
}

write_status_stopped() {
  local reason="$1"
  local symbol="${2:-}"
  python3 <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

reason = ${reason@Q}
symbol = ${symbol@Q}

path = Path("${STATUS_FILE}")
path.parent.mkdir(parents=True, exist_ok=True)

data = {
    "bot_name": "${BOT_NAME}",
    "status": "stopped",
    "start_requested": False,
    "pid": None,
    "symbol": symbol,
    "reason": reason,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}

path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

stale_cleanup_exit() {
  local reason="$1"
  local exit_code="${2:-0}"
  release_symbol_reservation
  write_status_stopped "$reason"
  exit "${exit_code}"
}

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

RUN_DIR="${BOT_DIR}/run"
PID_FILE="${RUN_DIR}/bot.pid"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
BOT_STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
STATUS_FILE="${RUN_DIR}/status.json"

CONFIG_REL=$(realpath --relative-to="${PROJECT_ROOT}" "${CONFIG_FILE}")
STATE_REL=$(realpath --relative-to="${PROJECT_ROOT}" "${BOT_STATE_FILE}")

if [[ ! -f "${PID_FILE}" ]]; then
  echo "${BOT_NAME} PID file not found; nothing to stop"
  stale_cleanup_exit "stale_status_cleanup_no_runner" 0
fi

PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
if [[ -z "${PID}" || ! "${PID}" =~ ^[0-9]+$ ]]; then
  rm -f "${PID_FILE}"
  echo "${BOT_NAME} PID file empty or invalid, removed"
  stale_cleanup_exit "stale_status_cleanup_no_runner" 0
fi

if ! kill -0 "${PID}" 2>/dev/null; then
  rm -f "${PID_FILE}"
  echo "no ${BOT_NAME} process with PID=${PID}"
  stale_cleanup_exit "stale_status_cleanup_no_runner" 0
fi

CMDLINE="$(ps -p "${PID}" -o args= 2>/dev/null || true)"
if [[ -z "${CMDLINE}" ]] || [[ "${CMDLINE}" != *fixed_cycle_hedge_bot.runner* ]]; then
  echo "PID ${PID} does not look like fixed_cycle_hedge_bot.runner" >&2
  stale_cleanup_exit "stale_status_cleanup_no_runner" 1
fi
if [[ "${CMDLINE}" != *"--bot-name ${BOT_NAME}"* ]]; then
  echo "PID ${PID} command line lacks --bot-name ${BOT_NAME}" >&2
  stale_cleanup_exit "stale_status_cleanup_no_runner" 1
fi

if [[ "${CMDLINE}" != *"${CONFIG_FILE}"* && "${CMDLINE}" != *"${BOT_STATE_FILE}"* && "${CMDLINE}" != *"${CONFIG_REL}"* && "${CMDLINE}" != *"${STATE_REL}"* ]]; then
  echo "PID ${PID} command line lacks config/state path" >&2
  echo "CMDLINE=${CMDLINE}" >&2
  stale_cleanup_exit "stale_status_cleanup_no_runner" 1
fi

echo "Stopping ${BOT_NAME} (PID=${PID})..."
kill "${PID}" 2>/dev/null || true
for i in {1..10}; do
  if ! kill -0 "${PID}" 2>/dev/null; then
    break
  fi
  sleep 1
done
if kill -0 "${PID}" 2>/dev/null; then
  echo "${BOT_NAME} still running, sending SIGKILL"
  kill -KILL "${PID}" 2>/dev/null || true
  sleep 1
fi
if kill -0 "${PID}" 2>/dev/null; then
  PROC_STATE="$(ps -p "${PID}" -o stat= 2>/dev/null || true)"
  if [[ "${PROC_STATE}" == Z* ]]; then
    echo "${BOT_NAME} PID=${PID} is zombie after stop; removing PID file"
  else
    echo "${BOT_NAME} PID=${PID} still alive after SIGKILL (state=${PROC_STATE})" >&2
    exit 1
  fi
fi
rm -f "${PID_FILE}"
release_symbol_reservation
write_status_stopped "stopped_via_stop_bot"
echo "${BOT_NAME} stopped"
