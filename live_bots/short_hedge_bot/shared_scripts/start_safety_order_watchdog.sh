#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ${BASH_SOURCE[0]} short_bot_<number>"
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

BOT_NAME="$1"
if [[ ! "${BOT_NAME}" =~ ^short_bot_[0-9]+$ ]]; then
  usage
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_GROUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_ROOT="${BOT_GROUP_DIR}/${BOT_NAME}"
RUN_DIR="${BOT_ROOT}/run"
LOG_DIR="${BOT_ROOT}/logs"
PID_FILE="${RUN_DIR}/safety_order_watchdog.pid"
STATUS_FILE="${RUN_DIR}/safety_order_watchdog.status.json"
NOHUP_LOG="${LOG_DIR}/safety_order_watchdog.nohup.log"
WATCHDOG_SCRIPT="${BOT_GROUP_DIR}/watchdog/safety_order_watchdog.py"
PYTHON="${PYTHON:-python3}"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

is_process_alive() {
  local pid="$1"
  if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$cmdline" == *"safety_order_watchdog.py"* && "$cmdline" == *"${BOT_NAME}"* ]]; then
    return 0
  fi
  return 1
}

if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if is_process_alive "${EXISTING_PID}"; then
    echo "${BOT_NAME} safety watchdog already running (PID=${EXISTING_PID})"
    exit 0
  else
    rm -f "${PID_FILE}"
  fi
fi

start_cmd=("${PYTHON}" "${WATCHDOG_SCRIPT}" --loop --bot-name "${BOT_NAME}")
nohup "${start_cmd[@]}" >> "${NOHUP_LOG}" 2>&1 &
WATCHDOG_PID="$!"
echo "${WATCHDOG_PID}" > "${PID_FILE}"
cat <<JSON > "${STATUS_FILE}"
{
  "status": "running",
  "pid": ${WATCHDOG_PID},
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "bot": "${BOT_NAME}"
}
JSON

echo "Started safety_order_watchdog for ${BOT_NAME} (PID=${WATCHDOG_PID})"
