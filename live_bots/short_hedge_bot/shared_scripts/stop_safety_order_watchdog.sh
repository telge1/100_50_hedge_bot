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
PID_FILE="${RUN_DIR}/safety_order_watchdog.pid"
STATUS_FILE="${RUN_DIR}/safety_order_watchdog.status.json"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "No safety_order_watchdog PID file for ${BOT_NAME}" >&2
  exit 0
fi

read -r WATCHDOG_PID < "${PID_FILE}"
if [[ -z "${WATCHDOG_PID}" || ! "${WATCHDOG_PID}" =~ ^[0-9]+$ ]]; then
  rm -f "${PID_FILE}"
  echo "Stale PID file removed"
  exit 0
fi

if ! kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
  echo "Safety watchdog PID ${WATCHDOG_PID} not running"
  rm -f "${PID_FILE}"
  exit 0
fi

cmdline="$(ps -p "${WATCHDOG_PID}" -o args= 2>/dev/null || true)"
if [[ -z "${cmdline}" || "${cmdline}" != *"safety_order_watchdog.py"* || "${cmdline}" != *"${BOT_NAME}"* ]]; then
  echo "PID ${WATCHDOG_PID} does not belong to ${BOT_NAME} watchdog" >&2
  exit 1
fi

kill "${WATCHDOG_PID}" 2>/dev/null || true
for i in {1..5}; do
  if ! kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    break
  fi
  sleep 1
done
if kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
  echo "Safety watchdog still alive, forcing kill"
  kill -KILL "${WATCHDOG_PID}" 2>/dev/null || true
fi

rm -f "${PID_FILE}"
cat <<JSON > "${STATUS_FILE}"
{
  "status": "stopped",
  "pid": null,
  "stopped_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "bot": "${BOT_NAME}"
}
JSON

echo "Stopped safety_order_watchdog for ${BOT_NAME}"
