#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_ROOT}/../../.." && pwd)"
SAFETY_PID="${BOT_ROOT}/run/safety_order_watchdog.pid"
WALLET_PID="${BOT_ROOT}/run/wallet_refill_watchdog.pid"
SAFETY_LOG="${BOT_ROOT}/logs/safety_order_watchdog.nohup.log"
WALLET_LOG="${BOT_ROOT}/logs/wallet_refill_watchdog.nohup.log"
STATUS_FILE="${BOT_ROOT}/run/hedge_guard_watchers_status.json"

write_status() {
  local safety_status=$1
  local wallet_status=$2
  cat <<EOF > "${STATUS_FILE}"
{
  "status": "stopped",
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

stop_watcher() {
  local pid_file=$1
  local expected=$2
  if [ ! -f "${pid_file}" ]; then
    echo "stopped"
    return
  fi
  local pid
  pid="$(cat "${pid_file}")"
  if [ -z "${pid}" ] || [ ! -d "/proc/${pid}" ]; then
    rm -f "${pid_file}"
    echo "stopped"
    return
  fi
  if ! grep -q "${expected}" "/proc/${pid}/cmdline" 2>/dev/null; then
    rm -f "${pid_file}"
    echo "stopped"
    return
  fi
  kill "${pid}"
  local timeout=5
  while [ "${timeout}" -gt 0 ] && [ -d "/proc/${pid}" ]; do
    sleep 1
    timeout=$((timeout - 1))
  done
  if [ -d "/proc/${pid}" ]; then
    kill -9 "${pid}"
  fi
  rm -f "${pid_file}"
  echo "stopped"
}

cd "${BOT_ROOT}"
safety_status="$(stop_watcher "${SAFETY_PID}" "safety_order_watchdog.py")"
wallet_status="$(stop_watcher "${WALLET_PID}" "wallet_refill_watchdog.py")"
write_status "${safety_status}" "${wallet_status}"
