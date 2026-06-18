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
PID_FILE="${BOT_GROUP_DIR}/run/wallet_refill_watchdog.pid"
LOG_DIR="${BOT_GROUP_DIR}/logs"
NOHUP_LOG="${LOG_DIR}/wallet_refill_watchdog.nohup.log"
WATCHDOG_SCRIPT="${BOT_GROUP_DIR}/watchdog/wallet_refill_watchdog.py"
# Einheitliche Transfer-/Credential-Config für alle Bots über die 100_50_hedge_bot-Gruppe.
TRANSFER_CONFIG="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/config/config.yaml"
PYTHON="${PYTHON:-python3}"

mkdir -p "${BOT_GROUP_DIR}/run" "${LOG_DIR}"

if [[ ! -f "${TRANSFER_CONFIG}" ]]; then
  echo "[ERROR] Short transfer config missing: ${TRANSFER_CONFIG}" >&2
  exit 1
fi

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

reconstruct_pid_from_process() {
  local pattern="${BOT_GROUP_DIR}/watchdog/wallet_refill_watchdog.py"
  while read -r candidate; do
    if is_valid_short_watchdog_pid "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
  done < <(pgrep -f "${pattern}" 2>/dev/null || true)
  return 1
}

reuse_existing_watchdog() {
  if [[ -f "${PID_FILE}" ]]; then
    local running_pid
    running_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if is_valid_short_watchdog_pid "${running_pid}"; then
      echo "Short wallet refill watchdog already running (PID=${running_pid})"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi
  local found
  if found="$(reconstruct_pid_from_process)"; then
    echo "${found}" > "${PID_FILE}"
    echo "Short wallet refill watchdog already running (PID=${found})"
    return 0
  fi
  return 1
}

CMD=(
  "${PYTHON}"
  "${WATCHDOG_SCRIPT}"
  --loop
  --interval 30
  --no-rebaseline-on-start
  --enable-transfer
  --no-transfer-dry-run
  --transfer-config-file "${TRANSFER_CONFIG}"
  --transfer-coin USDT
  --min-transfer-amount 1
  --transfer-cooldown-seconds 600
)

if reuse_existing_watchdog; then
  exit 0
fi

nohup "${CMD[@]}" >> "${NOHUP_LOG}" 2>&1 &
WATCHDOG_PID="$!"
echo "${WATCHDOG_PID}" > "${PID_FILE}"

sleep 1

if ! is_valid_short_watchdog_pid "${WATCHDOG_PID}"; then
  echo "Failed to start short wallet refill watchdog"
  exit 1
fi

echo "Started short wallet refill watchdog (PID=${WATCHDOG_PID})"
