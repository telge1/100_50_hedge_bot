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
GROUP_RUN_DIR="${BOT_GROUP_DIR}/run"
GROUP_LOG_DIR="${BOT_GROUP_DIR}/logs"
PID_FILE="${GROUP_RUN_DIR}/safety_order_watchdog.pid"
STATUS_FILE="${RUN_DIR}/safety_order_watchdog.status.json"
GROUP_STATUS_FILE="${GROUP_RUN_DIR}/safety_order_watchdog.status.json"
NOHUP_LOG="${GROUP_LOG_DIR}/safety_order_watchdog.nohup.log"
WATCHDOG_SCRIPT="${BOT_GROUP_DIR}/watchdog/safety_order_watchdog.py"
PYTHON="${PYTHON:-python3}"

mkdir -p "${RUN_DIR}" "${LOG_DIR}" "${GROUP_RUN_DIR}" "${GROUP_LOG_DIR}"

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
  if [[ "$cmdline" == *"safety_order_watchdog.py"* && "$cmdline" == *"${WATCHDOG_SCRIPT}"* ]]; then
    return 0
  fi
  return 1
}

write_status_files() {
  local pid_value="$1"
  local status_value="$2"
  cat <<JSON > "${STATUS_FILE}"
{
  "status": "${status_value}",
  "pid": ${pid_value},
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "bot": "${BOT_NAME}",
  "mode": "global_short_watchdog",
  "log_file": "${NOHUP_LOG}"
}
JSON
  cat <<JSON > "${GROUP_STATUS_FILE}"
{
  "status": "${status_value}",
  "pid": ${pid_value},
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "scope": "all_short_bots",
  "log_file": "${NOHUP_LOG}"
}
JSON
}

if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if is_process_alive "${EXISTING_PID}"; then
    write_status_files "${EXISTING_PID}" "running"
    echo "Short safety watchdog already running (PID=${EXISTING_PID})"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

start_cmd=("${PYTHON}" "${WATCHDOG_SCRIPT}" --loop --interval 30)
nohup "${start_cmd[@]}" >> "${NOHUP_LOG}" 2>&1 &
WATCHDOG_PID="$!"
echo "${WATCHDOG_PID}" > "${PID_FILE}"
write_status_files "${WATCHDOG_PID}" "running"

echo "Started short safety watchdog (PID=${WATCHDOG_PID})"
