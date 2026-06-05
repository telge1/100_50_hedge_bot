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

STOP_WITH_CLEANUP="${BOT_GROUP_DIR}/shared_scripts/stop_with_cleanup.sh"
if [[ -x "${STOP_WITH_CLEANUP}" ]]; then
  "${STOP_WITH_CLEANUP}" "${BOT_NAME}"
fi

if [[ -z "${PYTHON:-}" ]]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi

CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
RUN_DIR="${BOT_DIR}/run"
RUNTIME_CONFIG_FILE="${RUN_DIR}/fixed_cycle_config.runtime.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
LOG_DIR="${BOT_DIR}/logs"
PID_FILE="${BOT_DIR}/pids/fixed_cycle_bot.pid"
SNAPSHOT_FILE="${BOT_DIR}/snapshots/fixed_cycle_wallet_snapshot.json"
AUDIT_LOG="${LOG_DIR}/generic_hedge_runtime_audit.jsonl"
RUNNER_STDOUT="${LOG_DIR}/fixed_cycle_runner.stdout.log"
RESERVED_BEST_COIN_FILE="${RUN_DIR}/reserved_best_coin.json"

HARD_RESET_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/hard_reset_bot.sh"
CLEAN_LOGS_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/clean_bot_logs.sh"
CANCEL_SCRIPT="${BOT_GROUP_DIR}/shared_scripts/cancel_open_orders.sh"
PID_FILE="${RUN_DIR}/bot.pid"
STATUS_FILE="${RUN_DIR}/status.json"
WAIT_PID_FILE="${RUN_DIR}/start_wait.pid"
CANCEL_START_FILE="${RUN_DIR}/cancel_start"

mkdir -p "${BOT_DIR}/config" "${BOT_DIR}/state" "${BOT_DIR}/logs" "${BOT_DIR}/snapshots" "${BOT_DIR}/pids" "${RUN_DIR}"

touch "${LOG_DIR}/confirmed_order_pnl_history.jsonl"

write_status_json() {
  local status="$1"
  local reason="$2"
  local symbol="$3"
  local reserved_by="$4"
  local start_requested="$5"
  STATUS_STATUS="${status:-}" \
  STATUS_REASON="${reason:-}" \
  STATUS_SYMBOL="${symbol:-}" \
  STATUS_RESERVED="${reserved_by:-}" \
  STATUS_REQUESTED="${start_requested:-}" \
  STARTED_PID="${STARTED_PID:-}" \
  STATUS_FILE_PATH="${STATUS_FILE}" \
  BOT_NAME_VALUE="${BOT_NAME}" \
  python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["STATUS_FILE_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)

data = {
    "bot_name": os.environ.get("BOT_NAME_VALUE") or "",
    "status": os.environ.get("STATUS_STATUS") or "unknown",
    "updated_at": datetime.now(timezone.utc).isoformat()
}
symbol_value = os.environ.get("STATUS_SYMBOL") or ""
if symbol_value:
    data["symbol"] = symbol_value
reason_value = os.environ.get("STATUS_REASON") or ""
if reason_value:
    data["reason"] = reason_value
reserved_value = os.environ.get("STATUS_RESERVED") or ""
if reserved_value:
    data["reserved_by"] = reserved_value
start_requested_raw = os.environ.get("STATUS_REQUESTED")
if start_requested_raw is not None:
    data["start_requested"] = str(start_requested_raw).lower() in ("1","true","yes")
else:
    data["start_requested"] = False
if data["status"] == "running":
    pid_value = os.environ.get("STARTED_PID")
    if pid_value and pid_value.isdigit():
        data["pid"] = int(pid_value)

long_qty_env = os.environ.get("STATUS_LONG_QTY")
if long_qty_env:
    try:
        data["long_qty"] = float(long_qty_env)
    except ValueError:
        data["long_qty"] = long_qty_env
short_qty_env = os.environ.get("STATUS_SHORT_QTY")
if short_qty_env:
    try:
        data["short_qty"] = float(short_qty_env)
    except ValueError:
        data["short_qty"] = short_qty_env
open_order_count_env = os.environ.get("STATUS_OPEN_ORDER_COUNT")
if open_order_count_env:
    try:
        data["open_order_count"] = int(open_order_count_env)
    except ValueError:
        data["open_order_count"] = open_order_count_env

path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

exchange_flat_check() {
  local symbol="$1"
  local config_file="$2"
  if [[ -z "${symbol}" || -z "${config_file}" ]]; then
    echo "[${BOT_NAME}] ERROR: exchange flat check missing symbol/config" >&2
    return 2
  fi
  if [[ ! -f "${config_file}" ]]; then
    echo "[${BOT_NAME}] ERROR: runtime config not found at ${config_file}" >&2
    return 2
  fi

  local payload
  if ! payload="$(
    "${PYTHON:-python3}" "${SCRIPT_DIR}/check_exchange_flat.py" \
      --symbol "${symbol}" \
      --config "${config_file}" \
      2>&1
  )"; then
    echo "[${BOT_NAME}] ERROR: exchange flat check failed: ${payload}" >&2
    return 2
  fi

  export EXCHANGE_FLAT_PAYLOAD="${payload}"
mapfile -t flat_values < <(python3 - <<PY
import json
import math
import os
import sys

payload_raw = os.environ.get("EXCHANGE_FLAT_PAYLOAD") or ""
try:
    payload = json.loads(payload_raw)
except json.JSONDecodeError:
    payload = {}
    print("[DEBUG] exchange_flat payload invalid or empty, falling back to defaults", file=sys.stderr)

def normalize(value, integer=False):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "0"
    if abs(num) < 1e-9:
        return "0"
    if integer:
        return str(int(num))
    if math.isfinite(num) and num.is_integer():
        return str(int(num))
    return str(num)

print(normalize(payload.get("long_qty", 0.0)))
print(normalize(payload.get("short_qty", 0.0)))
print(normalize(payload.get("open_order_count", 0), integer=True))
print(payload.get("flat", False))
PY
  )
  LONG_QTY="${flat_values[0]:-0}"
  SHORT_QTY="${flat_values[1]:-0}"
  OPEN_ORDERS="${flat_values[2]:-0}"
  FLAT_OK="${flat_values[3]:-False}"
  export STATUS_LONG_QTY="${LONG_QTY}"
  export STATUS_SHORT_QTY="${SHORT_QTY}"
  export STATUS_OPEN_ORDER_COUNT="${OPEN_ORDERS}"

  if [[ "${FLAT_OK}" != "True" ]]; then
    return 1
  fi
  return 0
}

cleanup_wait_files() {
  rm -f "${WAIT_PID_FILE}" "${CANCEL_START_FILE}" >/dev/null 2>&1
}

trap cleanup_wait_files EXIT

ensure_wait_pid_clean() {
  if [[ ! -f "${WAIT_PID_FILE}" ]]; then
    return
  fi
  EXISTING_WAIT_PID="$(cat "${WAIT_PID_FILE}" 2>/dev/null || true)"
  if [[ -z "${EXISTING_WAIT_PID}" || ! "${EXISTING_WAIT_PID}" =~ ^[0-9]+$ ]]; then
    rm -f "${WAIT_PID_FILE}"
    return
  fi
  if kill -0 "${EXISTING_WAIT_PID}" 2>/dev/null; then
    echo "[${BOT_NAME}] stopping stale waiter PID=${EXISTING_WAIT_PID}" >&2
    kill "${EXISTING_WAIT_PID}" 2>/dev/null || true
    for i in {1..5}; do
      if ! kill -0 "${EXISTING_WAIT_PID}" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if kill -0 "${EXISTING_WAIT_PID}" 2>/dev/null; then
      kill -KILL "${EXISTING_WAIT_PID}" 2>/dev/null || true
    fi
  fi
  rm -f "${WAIT_PID_FILE}" "${CANCEL_START_FILE}"
}

is_alive_pid_with_bot_name() {
  local candidate="$1"
  local bot_name="$2"
  if [[ -z "${candidate//[0-9]/}" && -d "/proc/${candidate}" ]]; then
    local cmdline
    cmdline="$(tr '\0' ' ' < "/proc/${candidate}/cmdline" 2>/dev/null || true)"
    if [[ "${cmdline}" == *fixed_cycle_hedge_bot.runner* && "${cmdline}" == *"--bot-name ${bot_name}"* ]]; then
      return 0
    fi
  fi
  return 1
}

read_reserved_symbol() {
  if [[ -n "${SHORT_SKIP_SYMBOL_RESERVATION:-}" ]]; then
    if [[ -n "${SHORT_FIXED_SYMBOL:-}" ]]; then
      echo "${SHORT_FIXED_SYMBOL}" | tr '[:lower:]' '[:upper:]'
    else
      echo "XRPUSDT"
    fi
    return
  fi
  python3 <<PY
import json
from pathlib import Path

state_path = Path("${BOT_GROUP_DIR}/state/active_bot_symbols.json")
bot_name = "${BOT_NAME}"
symbol = ""
if state_path.exists():
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        payload = {}
    entry = payload.get(bot_name) or {}
    symbol = str(entry.get("symbol") or "").upper()
print(symbol)
PY
}

write_reserved_runtime_files() {
  local reserved_symbol="$1"
  if [[ -z "${reserved_symbol}" ]]; then
    echo "[${BOT_NAME}] ERROR: reserved symbol missing after wait" >&2
    exit 1
  fi

  RESERVED_SYMBOL_VALUE="${reserved_symbol}" \
  SOURCE_CONFIG_FILE="${CONFIG_FILE}" \
  RUNTIME_CONFIG_FILE_PATH="${RUNTIME_CONFIG_FILE}" \
  RESERVED_BEST_COIN_FILE_PATH="${RESERVED_BEST_COIN_FILE}" \
  python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

reserved_symbol = os.environ["RESERVED_SYMBOL_VALUE"]
source_config = Path(os.environ["SOURCE_CONFIG_FILE"])
runtime_config = Path(os.environ["RUNTIME_CONFIG_FILE_PATH"])
reserved_best_coin_file = Path(os.environ["RESERVED_BEST_COIN_FILE_PATH"])

config = json.loads(source_config.read_text(encoding="utf-8"))
config["symbol"] = reserved_symbol
config["best_coin_file"] = str(reserved_best_coin_file)

runtime_config.parent.mkdir(parents=True, exist_ok=True)
runtime_config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

reserved_best_coin_file.write_text(
    json.dumps(
        {
            "symbol": reserved_symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "reserved_symbol",
            "reason": "bot_reservation",
            "candidate_count": 1,
            "candidates": [{"symbol": reserved_symbol}],
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
PY
}

SIDE="short"
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

mkdir -p "${RUN_DIR}"
rm -f "${CANCEL_START_FILE}" "${WAIT_PID_FILE}"
if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${PID_FILE}")"
  if is_alive_pid_with_bot_name "${EXISTING_PID}" "${BOT_NAME}"; then
    echo "[${BOT_NAME}] already running (PID=${EXISTING_PID})" >&2
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

WAIT_SYMBOL="${SHORT_FIXED_SYMBOL:-XRPUSDT}"
WAIT_REASON="forced_symbol"
WAIT_RESERVED_BY=""

ensure_wait_pid_clean

FIXED_SYMBOL="${SHORT_FIXED_SYMBOL:-}"
SKIP_SYMBOL_RESERVATION="${SHORT_SKIP_SYMBOL_RESERVATION:-}"
if [[ -n "${FIXED_SYMBOL}" ]]; then
  FIXED_SYMBOL="$(echo "${FIXED_SYMBOL}" | tr '[:lower:]' '[:upper:]')"
  echo "[${BOT_NAME}] using forced symbol ${FIXED_SYMBOL}"
  RESERVED_SYMBOL="${FIXED_SYMBOL}"
  if [[ -z "${SKIP_SYMBOL_RESERVATION}" ]]; then
    write_reserved_runtime_files "${RESERVED_SYMBOL}"
  else
    echo "[${BOT_NAME}] skipping persistent reservation (SHORT_SKIP_SYMBOL_RESERVATION enabled)"
  fi
elif [[ -n "${SKIP_SYMBOL_RESERVATION}" ]]; then
  RESERVED_SYMBOL="${WAIT_SYMBOL}"
  if [[ -z "${RESERVED_SYMBOL}" ]]; then
    RESERVED_SYMBOL="BTCUSDT"
  fi
  echo "[${BOT_NAME}] skipping symbol reservation and using ${RESERVED_SYMBOL}"
else
  echo "[${BOT_NAME}] wallet capture skipped: global wallet refill watcher handles this later"

  write_status_json "waiting_for_symbol" "${WAIT_REASON}" "${WAIT_SYMBOL}" "${WAIT_RESERVED_BY}" "true"
  echo "$$" > "${WAIT_PID_FILE}"
  if ! "${BOT_GROUP_DIR}/shared_scripts/wait_for_unique_symbol.sh" "${BOT_NAME}"; then
    echo "[${BOT_NAME}] waiting_for_symbol (${WAIT_REASON})" >&2
    exit 1
  fi
  cleanup_wait_files

  RESERVED_SYMBOL="$(read_reserved_symbol)"
  write_reserved_runtime_files "${RESERVED_SYMBOL}"
fi

source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

if ! exchange_flat_check "${RESERVED_SYMBOL}" "${RUNTIME_CONFIG_FILE}"; then
  write_status_json \
    "start_blocked" \
    "exchange_flat_check_failed" \
    "${RESERVED_SYMBOL}" \
    "" \
    "false"
  exit 1
fi

if [[ "${STATUS_LONG_QTY:-0}" != "0" ]] || [[ "${STATUS_SHORT_QTY:-0}" != "0" ]] || [[ "${STATUS_OPEN_ORDER_COUNT:-0}" != "0" ]]; then
  echo "[fixed_cycle_start_preflight_exchange_not_flat_blocked] symbol=${RESERVED_SYMBOL} long_qty=${STATUS_LONG_QTY:-0} short_qty=${STATUS_SHORT_QTY:-0} open_order_count=${STATUS_OPEN_ORDER_COUNT:-0}" >&2
  write_status_json \
    "start_blocked" \
    "exchange_not_flat" \
    "${RESERVED_SYMBOL}" \
    "" \
    "false"
  exit 1
fi

echo "[fixed_cycle_start_preflight_exchange_flat_ok] symbol=${RESERVED_SYMBOL} long_qty=${STATUS_LONG_QTY:-0} short_qty=${STATUS_SHORT_QTY:-0} open_order_count=${STATUS_OPEN_ORDER_COUNT:-0}"
STATE_DIR="${BOT_DIR}/state"
if [[ -d "${STATE_DIR}" ]]; then
  rm -f "${STATE_DIR}"/*.json
fi

if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${OLD_PID}" ]]; then
    if kill -0 "${OLD_PID}" 2>/dev/null; then
      CMDLINE="$(ps -p "${OLD_PID}" -o args= 2>/dev/null || true)"
      if [[ "${CMDLINE}" == *fixed_cycle_hedge_bot.runner* ]] && ([[ "${CMDLINE}" == *"${CONFIG_FILE}"* ]] || [[ "${CMDLINE}" == *"${STATE_FILE}"* ]]); then
        echo "${BOT_NAME} already running with PID=${OLD_PID}"
        exit 0
      else
        echo "stale PID file points to unrelated process ${OLD_PID}, removing" >&2
        rm -f "${PID_FILE}"
      fi
    else
      echo "PID ${OLD_PID} not running, removing PID file"
      rm -f "${PID_FILE}"
    fi
  fi
fi

# refresh creds before launch
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

rotate_log() {
  local file="$1"
  if [[ -f "${file}" ]]; then
    mv "${file}" "${file}.prev"
  fi
}

rotate_log "${LOG_DIR}/fixed_cycle_hedge_runtime.log"
rotate_log "${LOG_DIR}/fixed_cycle_calc_audit.log"

nohup "${PYTHON}" -m fixed_cycle_hedge_bot.runner \
  --strategy fixed_cycle \
  --bot-name "${BOT_NAME}" \
  --strategy-config-file "${RUNTIME_CONFIG_FILE}" \
  --strategy-state-file "${STATE_FILE}" \
  --audit-log-file "${AUDIT_LOG}" \
  --calc-audit-log-file "${LOG_DIR}/fixed_cycle_calc_audit.log" \
  --confirmed-pnl-history-file "${LOG_DIR}/confirmed_order_pnl_history.jsonl" \
  --log-file "${LOG_DIR}/fixed_cycle_hedge_runtime.log" \
  > "${RUNNER_STDOUT}" 2>&1 &

PID=$!
printf "%s" "$PID" > "${PID_FILE}"
sleep 1
if ! kill -0 "${PID}" 2>/dev/null; then
  echo "[ERROR] ${BOT_NAME} failed to stay running after start" >&2
  echo "[ERROR] Last stdout log lines:" >&2
  tail -n 80 "${RUNNER_STDOUT}" >&2 || true
  rm -f "${PID_FILE}"
  exit 1
fi
echo "Fixed-cycle ${BOT_NAME} started via nohup (${RUNNER_STDOUT})"
echo "Started ${BOT_NAME} with PID=$PID"
CURRENT_SYMBOL="${RESERVED_SYMBOL}"
STARTED_PID="$PID" write_status_json "running" "" "${CURRENT_SYMBOL}" "" "true"
echo "[block_marker] bot_restart timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ") symbol=${CURRENT_SYMBOL}" >> "${LOG_DIR}/fixed_cycle_hedge_runtime.log"
echo "[block_marker] bot_restart timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ") symbol=${CURRENT_SYMBOL}" >> "${LOG_DIR}/fixed_cycle_calc_audit.log"
unset STARTED_PID
