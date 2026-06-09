#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ${BASH_SOURCE[0]} short_bot_<number>" >&2
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
PID_FILE="${BOT_GROUP_DIR}/run/wallet_refill_watchdog.pid"
WATCHDOG_SCRIPT="${BOT_GROUP_DIR}/watchdog/wallet_refill_watchdog.py"

is_valid_short_watchdog_pid() {
  local pid="$1"
  if [[ -z "${pid}" || ! "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [[ ! -d "/proc/${pid}" ]]; then
    return 1
  fi
  local cmdline
  cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
  [[ "${cmdline}" == *"${WATCHDOG_SCRIPT}"* && "${cmdline}" == *"short_hedge_bot/watchdog/wallet_refill_watchdog.py"* ]]
}

terminate_pid() {
  local pid="$1"
  if ! is_valid_short_watchdog_pid "${pid}"; then
    return 1
  fi
  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    for i in {1..5}; do
      if ! kill -0 "${pid}" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill -KILL "${pid}" 2>/dev/null || true
    fi
  fi
  return 0
}

terminate_running_short_wallet_watchdogs() {
  local pattern="${BOT_GROUP_DIR}/watchdog/wallet_refill_watchdog.py"
  local stopped=0
  while read -r candidate; do
    if [[ -z "${candidate}" ]]; then
      continue
    fi
    if terminate_pid "${candidate}"; then
      stopped=1
    fi
  done < <(pgrep -f "${pattern}" 2>/dev/null || true)
  return "${stopped}"
}

if [[ -f "${PID_FILE}" ]]; then
  WATCHDOG_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${WATCHDOG_PID}" ]]; then
    terminate_pid "${WATCHDOG_PID}"
  fi
  rm -f "${PID_FILE}"
else
  terminate_running_short_wallet_watchdogs >/dev/null || true
fi

echo "Stopped short wallet refill watchdog"
