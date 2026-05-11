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
BOT_SCRIPTS_DIR="${BOT_DIR}/scripts"

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

STOP_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/stop_bot.sh"
CANCEL_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/cancel_open_orders.sh"
HARD_RESET_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/hard_reset_bot.sh"

for script in "${STOP_SCRIPT}" "${CANCEL_SCRIPT}" "${HARD_RESET_SCRIPT}"; do
  if [[ ! -x "${script}" ]]; then
    echo "Error: ${script} not executable" >&2
    exit 1
  fi
done

echo "[${BOT_NAME}] WARNING: this will stop the bot process before cleaning."
echo "[${BOT_NAME}] WARNING: this will cancel all open exchange orders for the detected symbol."
echo "[${BOT_NAME}] WARNING: this will NOT close positions."
echo "[${BOT_NAME}] WARNING: local per-bot state/runtime files are cleaned here."
echo "[${BOT_NAME}] WARNING: logs are NOT cleaned in stop_with_cleanup (only on restart)."

SIDE="long"
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

set +e
 "${STOP_SCRIPT}" "${BOT_NAME}"
STOP_CODE=$?
set -e

if [[ ${STOP_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} stop failed (exit_code=${STOP_CODE})" >&2
  exit 1
fi

CANCEL_CODE=0
set +e
"${CANCEL_SCRIPT}" "${BOT_NAME}"
CANCEL_CODE=$?
set -e

if [[ ${CANCEL_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} cancel open orders failed (exit_code=${CANCEL_CODE})" >&2
  exit 1
fi

set +e
"${HARD_RESET_SCRIPT}"
RESET_CODE=$?
set -e

if [[ ${RESET_CODE} -ne 0 ]]; then
  echo "[ERROR] ${BOT_NAME} hard reset failed (exit_code=${RESET_CODE})" >&2
  exit 1
fi

echo "[${BOT_NAME}] stop_with_cleanup_success"
