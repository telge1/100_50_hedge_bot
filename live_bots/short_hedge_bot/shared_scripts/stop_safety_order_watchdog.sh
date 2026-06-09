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
STATUS_FILE="${RUN_DIR}/safety_order_watchdog.status.json"
GROUP_RUN_DIR="${BOT_GROUP_DIR}/run"
PID_FILE="${GROUP_RUN_DIR}/safety_order_watchdog.pid"
GROUP_STATUS_FILE="${GROUP_RUN_DIR}/safety_order_watchdog.status.json"
WATCHDOG_SCRIPT="${BOT_GROUP_DIR}/watchdog/safety_order_watchdog.py"

write_bot_status() {
  cat <<JSON > "${STATUS_FILE}"
{
  "status": "stopped",
  "pid": null,
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "bot": "${BOT_NAME}",
  "mode": "global_short_watchdog"
}
JSON
}

write_group_status() {
  local status_value="$1"
  local pid_value="$2"
  cat <<JSON > "${GROUP_STATUS_FILE}"
{
  "status": "${status_value}",
  "pid": ${pid_value},
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "scope": "all_short_bots"
}
JSON
}

is_runner_alive() {
  local candidate="$1"
  local bot_name="$2"
  if [[ -z "${candidate}" || ! "${candidate}" =~ ^[0-9]+$ || ! -d "/proc/${candidate}" ]]; then
    return 1
  fi
  local cmdline
  cmdline="$(tr '\0' ' ' < "/proc/${candidate}/cmdline" 2>/dev/null || true)"
  [[ "${cmdline}" == *"fixed_cycle_hedge_bot.runner"* && "${cmdline}" == *"--bot-name ${bot_name}"* ]]
}

other_active_short_bots_exist() {
  local excluded_bot="$1"
  local bot_dir
  for bot_dir in "${BOT_GROUP_DIR}"/short_bot_*; do
    [[ -d "${bot_dir}" ]] || continue
    local bot_basename
    bot_basename="$(basename "${bot_dir}")"
    if [[ "${bot_basename}" == "${excluded_bot}" ]]; then
      continue
    fi
    local bot_pid_file="${bot_dir}/run/bot.pid"
    [[ -f "${bot_pid_file}" ]] || continue
    local bot_pid
    bot_pid="$(cat "${bot_pid_file}" 2>/dev/null || true)"
    if is_runner_alive "${bot_pid}" "${bot_basename}"; then
      return 0
    fi
  done
  return 1
}

is_watchdog_alive() {
  local pid="$1"
  if [[ -z "${pid}" || ! "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
  [[ "${cmdline}" == *"safety_order_watchdog.py"* && "${cmdline}" == *"${WATCHDOG_SCRIPT}"* ]]
}

if [[ ! -f "${PID_FILE}" ]]; then
  write_bot_status
  write_group_status "stopped" "null"
  echo "No safety_order_watchdog PID file for ${BOT_NAME}" >&2
  exit 0
fi

read -r WATCHDOG_PID < "${PID_FILE}"
if [[ -z "${WATCHDOG_PID}" || ! "${WATCHDOG_PID}" =~ ^[0-9]+$ ]]; then
  rm -f "${PID_FILE}"
  write_bot_status
  write_group_status "stopped" "null"
  echo "Stale PID file removed"
  exit 0
fi

if other_active_short_bots_exist "${BOT_NAME}"; then
  write_bot_status
  if is_watchdog_alive "${WATCHDOG_PID}"; then
    write_group_status "running" "${WATCHDOG_PID}"
    echo "Kept short safety watchdog running for other active short bots"
  else
    rm -f "${PID_FILE}"
    write_group_status "stopped" "null"
    echo "Short safety watchdog was not running; kept stopped state for remaining bots" >&2
  fi
  exit 0
fi

if ! is_watchdog_alive "${WATCHDOG_PID}"; then
  echo "Safety watchdog PID ${WATCHDOG_PID} not running"
  rm -f "${PID_FILE}"
  write_bot_status
  write_group_status "stopped" "null"
  exit 0
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
write_bot_status
write_group_status "stopped" "null"

echo "Stopped short safety watchdog"
