#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
cd "${PROJECT_ROOT}" || exit 1

STOP_SCRIPT="${BOT_DIR}/scripts/stop.sh"
HARD_RESET_SCRIPT="${BOT_DIR}/scripts/hard_reset.sh"
CANCEL_SCRIPT="${BOT_DIR}/scripts/cancel_open_orders.sh"

if [[ ! -x "${STOP_SCRIPT}" ]]; then
  echo "Error: ${STOP_SCRIPT} not executable" >&2
  exit 1
fi
if [[ ! -x "${HARD_RESET_SCRIPT}" ]]; then
  echo "Error: ${HARD_RESET_SCRIPT} not executable" >&2
  exit 1
fi
if [[ ! -x "${CANCEL_SCRIPT}" ]]; then
  echo "Error: ${CANCEL_SCRIPT} not executable" >&2
  exit 1
fi

echo "[long_bot_1] WARNING: this will stop the bot process before cleaning."
echo "[long_bot_1] WARNING: this will cancel all open exchange orders for the detected symbol."
echo "[long_bot_1] WARNING: this will NOT close positions."
echo "[long_bot_1] WARNING: local per-bot state/runtime files are cleaned here."
echo "[long_bot_1] WARNING: logs are NOT cleaned in stop_with_cleanup (only on restart)."

STOP_CODE=0
set +e
"${STOP_SCRIPT}"
STOP_CODE=$?
set -e

if [[ ${STOP_CODE} -ne 0 ]]; then
  echo "[ERROR] long_bot_1 stop failed (exit_code=${STOP_CODE})" >&2
  exit 1
fi

CANCEL_CODE=0
set +e
"${CANCEL_SCRIPT}"
CANCEL_CODE=$?
set -e

if [[ ${CANCEL_CODE} -ne 0 ]]; then
  echo "[ERROR] long_bot_1 cancel open orders failed (exit_code=${CANCEL_CODE})" >&2
  exit 1
fi

RESET_CODE=0
set +e
"${HARD_RESET_SCRIPT}"
RESET_CODE=$?
set -e

if [[ ${RESET_CODE} -ne 0 ]]; then
  echo "[ERROR] long_bot_1 hard reset failed (exit_code=${RESET_CODE})" >&2
  exit 1
fi

echo "[long_bot_1] stop_with_cleanup_success"
