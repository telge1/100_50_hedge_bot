#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BOT_BASE_DIR="${PROJECT_ROOT}/live_bots/100_50_hedge_bot"

usage() {
  echo "Usage: ${BASH_SOURCE[0]} long_bot_<n> [--force]"
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

BOT_NAME=""
FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true
      shift
      ;;
    *)
      if [[ -z "${BOT_NAME}" ]]; then
        BOT_NAME="$1"
      else
        usage
      fi
      shift
      ;;
  esac
done

if [[ -z "${BOT_NAME}" || ! "${BOT_NAME}" =~ ^long_bot_[0-9]+$ ]]; then
  echo "ERROR: BOT_NAME must be like long_bot_<number>"
  usage
fi

BOT_DIR="${BOT_BASE_DIR}/${BOT_NAME}"
if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: Bot directory does not exist: ${BOT_DIR}"
  exit 1
fi

STATE_DIR="${BOT_DIR}/state"
RUN_DIR="${BOT_DIR}/run"
SNAPSHOT_DIR="${BOT_DIR}/snapshots"
LOG_DIR="${BOT_DIR}/logs"

mkdir -p "${STATE_DIR}" "${RUN_DIR}" "${SNAPSHOT_DIR}" "${LOG_DIR}"

PID_FILE="${RUN_DIR}/bot.pid"
STATUS_FILE="${RUN_DIR}/status.json"
STATE_FILE="${STATE_DIR}/fixed_cycle_state.json"
SNAPSHOT_FILE="${SNAPSHOT_DIR}/fixed_cycle_wallet_snapshot.json"

is_pid_running() {
  local pid="$1"
  if [[ -z "${pid}" ]]; then
    return 1
  fi
  if kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  return 1
}

bot_running=false
if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if is_pid_running "${pid}"; then
    cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    if [[ "${cmdline}" == *fixed_cycle_hedge_bot.runner* && "${cmdline}" == *"${BOT_NAME}"* ]]; then
      echo "Bot appears to be running (PID ${pid}); aborting dashboard registration."
      echo "Bot dir: ${BOT_DIR}"
      exit 1
    fi
  else
    echo "Stale PID file detected (${PID_FILE}); removing."
    rm -f "${PID_FILE}"
  fi
fi

if [[ -f "${STATUS_FILE}" ]]; then
  status_running="$(jq -r '.running // false' "${STATUS_FILE}" 2>/dev/null || echo "false")"
  status_label="$(jq -r '.status // ""' "${STATUS_FILE}" 2>/dev/null || echo "")"
  if [[ "${status_running}" == "true" ]] || [[ "${status_label}" =~ running|waiting ]]; then
    echo "Status file indicates bot is running/active; aborting."
    exit 1
  fi
fi

write_state() {
  cat <<'JSON' > "${STATE_FILE}"
{
  "long_qty": 0.0,
  "short_qty": 0.0,
  "snapshot": {
    "long_qty": 0.0,
    "short_qty": 0.0
  },
  "strategy_state": {
    "long_qty": 0.0,
    "short_qty": 0.0,
    "bot_state": "STOPPED"
  }
}
JSON
}

if [[ -f "${STATE_FILE}" ]]; then
  bot_state="$(jq -r '.strategy_state.bot_state // ""' "${STATE_FILE}" 2>/dev/null || echo "")"
  if [[ "${bot_state^^}" == "RUNNING" ]]; then
    echo "State file shows RUNNING; will not overwrite."
  elif [[ "${FORCE}" == "true" ]]; then
    write_state
  else
    echo "State file already exists; use --force to regenerate."
  fi
else
  write_state
fi

write_status() {
  cat <<JSON > "${STATUS_FILE}"
{
  "bot_name": "${BOT_NAME}",
  "bot_type": "long",
  "running": false,
  "start_requested": false,
  "status": "stopped",
  "status_label": "gestoppt",
  "pid": null
}
JSON
}

if [[ -f "${STATUS_FILE}" ]]; then
  status_running="$(jq -r '.running // false' "${STATUS_FILE}" 2>/dev/null || echo "false")"
  if [[ "${status_running}" == "true" ]]; then
    echo "Status file reports bot running; not overwriting."
  elif [[ "${FORCE}" == "true" ]]; then
    write_status
  else
    echo "Status file already exists; use --force to reset."
  fi
else
  write_status
fi

SNAPSHOT_SCRIPT="${PROJECT_ROOT}/scripts/update_fixed_cycle_wallet_snapshot.py"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "${PYTHON_BIN}" || ! -f "${SNAPSHOT_SCRIPT}" ]]; then
  echo "Snapshot script or python missing; aborting."
  exit 1
fi

if [[ ! -f "${STATE_FILE}" ]]; then
  echo "State file missing after creation; aborting."
  exit 1
fi

if ! "${PYTHON_BIN}" "${SNAPSHOT_SCRIPT}" \
  --bot-name "${BOT_NAME}" \
  --state-file "${STATE_FILE}" \
  --output-file "${SNAPSHOT_FILE}" \
  --force-flat; then
  echo "Snapshot generation failed."
  exit 1
fi

cat <<EOF
Dashboard registration completed
Bot name: ${BOT_NAME}
Bot dir: ${BOT_DIR}
State file: ${STATE_FILE}
Status file: ${STATUS_FILE}
Snapshot file: ${SNAPSHOT_FILE}
EOF
