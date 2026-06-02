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
PROJECT_ROOT="$(cd "${BOT_GROUP_DIR}/../.." && pwd)"
BOT_DIR="${BOT_GROUP_DIR}/${BOT_NAME}"

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

RUN_DIR="${BOT_DIR}/run"
PID_FILE="${RUN_DIR}/bot.pid"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
CYCLE_STATE_FILE="${BOT_DIR}/state/fixed_cycle_cycle_state.json"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
CONFIG_REL="$(realpath --relative-to="${PROJECT_ROOT}" "${CONFIG_FILE}")"
STATE_REL="$(realpath --relative-to="${PROJECT_ROOT}" "${STATE_FILE}")"

if [[ -f "${PID_FILE}" ]]; then
  PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -z "${PID}" || ! "${PID}" =~ ^[0-9]+$ ]]; then
    rm -f "${PID_FILE}"
    echo "[${BOT_NAME}] PID file empty/invalid, removed"
  else
    if ! kill -0 "${PID}" 2>/dev/null; then
      rm -f "${PID_FILE}"
      echo "[${BOT_NAME}] process ${PID} not running, PID file removed"
    else
      CMDLINE="$(ps -p "${PID}" -o args= 2>/dev/null || true)"
      if [[ "${CMDLINE}" != *fixed_cycle_hedge_bot.runner* ]]; then
        echo "[${BOT_NAME}] PID ${PID} points to unrelated process, aborting hard reset" >&2
        exit 1
      fi
      if [[ "${CMDLINE}" != *"${CONFIG_FILE}"* && "${CMDLINE}" != *"${STATE_FILE}"* && "${CMDLINE}" != *"${CONFIG_REL}"* && "${CMDLINE}" != *"${STATE_REL}"* ]]; then
        echo "[${BOT_NAME}] PID ${PID} command line lacks config/state path" >&2
        echo "CMDLINE=${CMDLINE}" >&2
        exit 1
      fi
      echo "[${BOT_NAME}] stopping existing process ${PID}"
      kill "${PID}" 2>/dev/null || true
      for i in {1..10}; do
        if ! kill -0 "${PID}" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      if kill -0 "${PID}" 2>/dev/null; then
        echo "[${BOT_NAME}] sending SIGKILL to ${PID}"
        kill -KILL "${PID}" 2>/dev/null || true
        sleep 1
      fi
      if kill -0 "${PID}" 2>/dev/null; then
        echo "[${BOT_NAME}] PID ${PID} still alive after SIGKILL" >&2
        exit 1
      fi
      rm -f "${PID_FILE}"
    fi
  fi
fi

rm -vf "${STATE_FILE}"
rm -vf "${CYCLE_STATE_FILE}"
echo "[${BOT_NAME}] state file removed"

echo "[${BOT_NAME}] exchange cleanup skipped (per-bot hard reset does not cancel global orders)"
echo "[${BOT_NAME}] hard_reset_success"
