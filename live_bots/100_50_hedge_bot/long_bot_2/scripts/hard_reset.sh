#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
cd "${PROJECT_ROOT}" || exit 1

PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
CONFIG_REL="live_bots/100_50_hedge_bot/long_bot_2/config/fixed_cycle_config.json"
STATE_REL="live_bots/100_50_hedge_bot/long_bot_2/state/fixed_cycle_state.json"

if [[ -f "${PID_FILE}" ]]; then
  PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -z "${PID}" || ! "${PID}" =~ ^[0-9]+$ ]]; then
    rm -f "${PID_FILE}"
    echo "[long_bot_2] PID file empty/invalid, removed"
  else
    if ! kill -0 "${PID}" 2>/dev/null; then
      rm -f "${PID_FILE}"
      echo "[long_bot_2] process ${PID} not running, PID file removed"
    else
      CMDLINE="$(ps -p "${PID}" -o args= 2>/dev/null || true)"
      if [[ "${CMDLINE}" != *fixed_cycle_hedge_bot.runner* ]]; then
        echo "[long_bot_2] PID ${PID} points to unrelated process, aborting hard reset" >&2
        exit 1
      fi
      if [[ "${CMDLINE}" != *"${CONFIG_FILE}"* && "${CMDLINE}" != *"${STATE_FILE}"* && "${CMDLINE}" != *"${CONFIG_REL}"* && "${CMDLINE}" != *"${STATE_REL}"* ]]; then
        echo "[long_bot_2] PID ${PID} command line lacks config/state path" >&2
        echo "CMDLINE=${CMDLINE}" >&2
        exit 1
      fi
      echo "[long_bot_2] stopping existing process ${PID}"
      kill "${PID}" 2>/dev/null || true
      for i in {1..10}; do
        if ! kill -0 "${PID}" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      if kill -0 "${PID}" 2>/dev/null; then
        echo "[long_bot_2] sending SIGKILL to ${PID}"
        kill -KILL "${PID}" 2>/dev/null || true
        sleep 1
      fi
      if kill -0 "${PID}" 2>/dev/null; then
        echo "[long_bot_2] PID ${PID} still alive after SIGKILL" >&2
        exit 1
      fi
    rm -f "${PID_FILE}"
    fi
  fi
fi

rm -vf "${STATE_FILE}"
echo "[long_bot_2] state file removed"

echo "[long_bot_2] exchange cleanup skipped (per-bot hard reset does not cancel global orders)"
echo "[long_bot_2] hard_reset_success"
exit 0
