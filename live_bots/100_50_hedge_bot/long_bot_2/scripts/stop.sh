#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
cd "${PROJECT_ROOT}" || exit 1

PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
CONFIG_REL="live_bots/100_50_hedge_bot/long_bot_2/config/fixed_cycle_config.json"
STATE_REL="live_bots/100_50_hedge_bot/long_bot_2/state/fixed_cycle_state.json"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "long_bot_2 PID file not found; nothing to stop"
  exit 0
fi

PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
if [[ -z "${PID}" || ! "${PID}" =~ ^[0-9]+$ ]]; then
  rm -f "${PID_FILE}"
  echo "long_bot_2 PID file empty or invalid, removed"
  exit 0
fi

if ! kill -0 "${PID}" 2>/dev/null; then
  rm -f "${PID_FILE}"
  echo "no long_bot_2 process with PID=${PID}"
  exit 0
fi

CMDLINE="$(ps -p "${PID}" -o args= 2>/dev/null || true)"
if [[ -z "${CMDLINE}" ]] || [[ "${CMDLINE}" != *fixed_cycle_hedge_bot.runner* ]]; then
  echo "PID ${PID} does not look like fixed_cycle_hedge_bot.runner" >&2
  exit 1
fi
if [[ "${CMDLINE}" != *"${CONFIG_FILE}"* && "${CMDLINE}" != *"${STATE_FILE}"* && "${CMDLINE}" != *"${CONFIG_REL}"* && "${CMDLINE}" != *"${STATE_REL}"* ]]; then
  echo "PID ${PID} command line lacks config/state path" >&2
  echo "CMDLINE=${CMDLINE}" >&2
  exit 1
fi

echo "Stopping long_bot_2 (PID=${PID})..."
kill "${PID}" 2>/dev/null || true
for i in {1..10}; do
  if ! kill -0 "${PID}" 2>/dev/null; then
    break
  fi
  sleep 1
done
if kill -0 "${PID}" 2>/dev/null; then
  echo "long_bot_2 still running, sending SIGKILL"
  kill -KILL "${PID}" 2>/dev/null || true
  sleep 1
fi
if kill -0 "${PID}" 2>/dev/null; then
  PROC_STATE="$(ps -p "${PID}" -o stat= 2>/dev/null || true)"
  if [[ "${PROC_STATE}" == Z* ]]; then
    echo "long_bot_2 PID=${PID} is zombie after stop; removing PID file"
  else
    echo "long_bot_2 PID=${PID} still alive after SIGKILL (state=${PROC_STATE})" >&2
    exit 1
  fi
fi
rm -f "${PID_FILE}"
echo "long_bot_2 stopped"
