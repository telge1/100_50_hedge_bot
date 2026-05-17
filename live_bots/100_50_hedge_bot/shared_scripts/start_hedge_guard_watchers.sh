#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_ROOT}/../../.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

SAFETY_PID="${BOT_ROOT}/run/safety_order_watchdog.pid"
WALLET_PID="${BOT_ROOT}/run/wallet_refill_watchdog.pid"
SAFETY_LOG="${BOT_ROOT}/logs/safety_order_watchdog.nohup.log"
WALLET_LOG="${BOT_ROOT}/logs/wallet_refill_watchdog.nohup.log"
STATUS_FILE="${BOT_ROOT}/run/hedge_guard_watchers_status.json"

mkdir -p "${BOT_ROOT}/run" "${BOT_ROOT}/logs"

write_status() {
  local safety_status=$1
  local wallet_status=$2
  cat <<EOF > "${STATUS_FILE}"
{
  "status": "$3",
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "watchers": {
    "safety_order_watchdog": {
      "status": "${safety_status}",
      "pid": $(cat "${SAFETY_PID}" 2>/dev/null || echo null),
      "log_file": "${SAFETY_LOG}"
    },
    "wallet_refill_watchdog": {
      "status": "${wallet_status}",
      "pid": $(cat "${WALLET_PID}" 2>/dev/null || echo null),
      "log_file": "${WALLET_LOG}"
    }
  }
}
EOF
}

start_watcher() {
  local name=$1
  local pid_file=$2
  shift 2
  local expected=$1
  shift
  local cmd=("$@")
  if [ -f "${pid_file}" ]; then
    local existing_pid
    existing_pid="$(cat "${pid_file}")"
    if [ -n "${existing_pid}" ] && [ -d "/proc/${existing_pid}" ]; then
      if grep -q "${expected}" /proc/${existing_pid}/cmdline 2>/dev/null; then
        return 0
      fi
    fi
    rm -f "${pid_file}"
  fi
  local log_file
  if [ "${name}" = "safety_order_watchdog" ]; then
    log_file="${SAFETY_LOG}"
  else
    log_file="${WALLET_LOG}"
  fi
  nohup "${cmd[@]}" >> "${log_file}" 2>&1 &
  echo "$!" > "${pid_file}"
}

start_watcher "safety_order_watchdog" "${SAFETY_PID}" \
  "safety_order_watchdog.py" \
  "${PYTHON_BIN}" "${BOT_ROOT}/watchdog/safety_order_watchdog.py" --loop --interval 30
sleep 1
start_watcher "wallet_refill_watchdog" "${WALLET_PID}" \
  "wallet_refill_watchdog.py" \
  "${PYTHON_BIN}" "${BOT_ROOT}/watchdog/wallet_refill_watchdog.py" \
    --loop --interval 30 --no-rebaseline-on-start --enable-transfer --no-transfer-dry-run \
    --transfer-config-file config/config.yaml --transfer-coin USDT \
    --min-transfer-amount 1 --transfer-cooldown-seconds 600
sleep 1

status_from_pid() {
  local pid_file=$1
  local expected=$2
  if [ ! -f "${pid_file}" ]; then
    echo "stopped"
    return
  fi
  local pid
  pid="$(cat "${pid_file}")"
  if [ -z "${pid}" ] || [ ! -d "/proc/${pid}" ]; then
    echo "stopped"
    return
  fi
  if ! grep -q "${expected}" "/proc/${pid}/cmdline" 2>/dev/null; then
    echo "error"
    return
  fi
  echo "running"
}

safety_status="$(status_from_pid "${SAFETY_PID}" "safety_order_watchdog.py")"
wallet_status="$(status_from_pid "${WALLET_PID}" "wallet_refill_watchdog.py")"
overall="stopped"
if [ "${safety_status}" = "running" ] && [ "${wallet_status}" = "running" ]; then
  overall="running"
elif [ "${safety_status}" = "running" ] || [ "${wallet_status}" = "running" ]; then
  overall="partial"
fi
write_status "${safety_status}" "${wallet_status}" "${overall}"
