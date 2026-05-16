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

STATE_DIR="${BOT_GROUP_DIR}/state"
STATE_FILE="${STATE_DIR}/active_bot_symbols.json"
LOCK_FILE="${STATE_DIR}/active_bot_symbols.lock"
BEST_COIN_FILE="${PROJECT_ROOT}/logs/best_coin.json"
RUN_DIR="${BOT_GROUP_DIR}/${BOT_NAME}/run"
CANCEL_START_FILE="${RUN_DIR}/cancel_start"
POLL_SECONDS="${LONG_BOT_SYMBOL_WAIT_POLL_SECONDS:-5}"
TIMEOUT_SECONDS="${LONG_BOT_SYMBOL_WAIT_TIMEOUT_SECONDS:-0}"
PYTHON_CMD="${LONG_BOT_SYMBOL_WATCHER_PYTHON:-python3}"

mkdir -p "${STATE_DIR}"
mkdir -p "${RUN_DIR}"

run_pid=""
cleanup_run_pid() {
  if [[ -n "${run_pid}" ]]; then
    kill "${run_pid}" 2>/dev/null || true
  fi
}
trap cleanup_run_pid EXIT

echo "[${BOT_NAME}] state_file=${STATE_FILE} lock_file=${LOCK_FILE}"
echo "[${BOT_NAME}] running cleanup_stale_symbol_reservations before reservation"
 "${PYTHON_CMD}" "${BOT_GROUP_DIR}/shared_scripts/cleanup_stale_symbol_reservations.py" \
  --state-file "${STATE_FILE}" \
  --lock-file "${LOCK_FILE}" \
  --bot-group-dir "${BOT_GROUP_DIR}" \
  --log-prefix "${BOT_NAME}" \
|| echo "[${BOT_NAME}] cleanup_stale_symbol_reservations failed; continuing" >&2

 "${PYTHON_CMD}" "${BOT_GROUP_DIR}/shared_scripts/active_bot_symbols.py" \
  reserve \
  --bot-name "${BOT_NAME}" \
  --best-coin-file "${BEST_COIN_FILE}" \
  --state-file "${STATE_FILE}" \
  --lock-file "${LOCK_FILE}" \
  --bot-group-dir "${BOT_GROUP_DIR}" \
  --cleanup-script "${BOT_GROUP_DIR}/shared_scripts/cleanup_stale_symbol_reservations.py" \
  --poll-seconds "${POLL_SECONDS}" \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  --source "start_long_bot" \
  --pid "$$" &

run_pid=$!
while kill -0 "${run_pid}" 2>/dev/null; do
  if [[ -f "${CANCEL_START_FILE}" ]]; then
    echo "[${BOT_NAME}] start canceled; terminating wait loop" >&2
    kill "${run_pid}" 2>/dev/null || true
    wait "${run_pid}" 2>/dev/null
    exit 20
  fi
  sleep 1
done

wait "${run_pid}"
trap - EXIT
