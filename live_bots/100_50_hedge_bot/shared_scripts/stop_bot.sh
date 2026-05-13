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

RUN_DIR="${BOT_DIR}/run"
PID_FILE="${RUN_DIR}/bot.pid"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"

CONFIG_REL=$(realpath --relative-to="${PROJECT_ROOT}" "${CONFIG_FILE}")
STATE_REL=$(realpath --relative-to="${PROJECT_ROOT}" "${STATE_FILE}")

if [[ ! -f "${PID_FILE}" ]]; then
  echo "${BOT_NAME} PID file not found; nothing to stop"
  exit 0
fi

PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
if [[ -z "${PID}" || ! "${PID}" =~ ^[0-9]+$ ]]; then
  rm -f "${PID_FILE}"
  echo "${BOT_NAME} PID file empty or invalid, removed"
  exit 0
fi

if ! kill -0 "${PID}" 2>/dev/null; then
  rm -f "${PID_FILE}"
  echo "no ${BOT_NAME} process with PID=${PID}"
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
echo "${BOT_NAME} stopped"
