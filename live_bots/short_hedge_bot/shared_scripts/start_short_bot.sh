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
symbol_value = os.environ.get("STATUS_SYMBOL")
if symbol_value is None:
    symbol_value = ""
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
runner_started = data["status"] == "running"
data["runner_started"] = runner_started
if runner_started:
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

cleanup_local_bot_state_if_no_runner() {
  # Wenn kein laufender Runner für diesen Bot existiert, lokale State-Dateien
  # defensiv bereinigen (active_bot_symbols-Eintrag + Runtime-/Reserved-Files).
  local existing_pid=""
  local runner_alive="false"
  if [[ -f "${PID_FILE}" ]]; then
    existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${existing_pid}" ]] && is_alive_pid_with_bot_name "${existing_pid}" "${BOT_NAME}"; then
      runner_alive="true"
    fi
  fi
  if [[ "${runner_alive}" == "true" ]]; then
    return
  fi

  # Stale PID-Datei entfernen, falls vorhanden
  if [[ -f "${PID_FILE}" ]]; then
    rm -f "${PID_FILE}"
  fi

  # Stale active_bot_symbols-Eintrag für diesen Bot entfernen
  BOT_GROUP_DIR_VALUE="${BOT_GROUP_DIR}" BOT_NAME_VALUE="${BOT_NAME}" python3 <<'PY'
import json
import os
from pathlib import Path

group_dir = Path(os.environ["BOT_GROUP_DIR_VALUE"])
state_path = group_dir / "state" / "active_bot_symbols.json"
bot_name = os.environ.get("BOT_NAME_VALUE") or ""
if not bot_name:
    raise SystemExit(0)
if not state_path.exists():
    raise SystemExit(0)
try:
    data = json.loads(state_path.read_text(encoding="utf-8") or "{}")
except Exception:
    raise SystemExit(0)
if bot_name in data:
    data.pop(bot_name, None)
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY

  # Stale Runtime-/Reserved-Files entfernen – neue Starts schreiben frische Dateien.
  rm -f "${RUNTIME_CONFIG_FILE}" "${RESERVED_BEST_COIN_FILE}"
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

ensure_no_other_runner_process() {
  # Zusätzlicher Schutz: Wenn aus irgendeinem Grund kein PID_FILE existiert,
  # aber noch ein laufender fixed_cycle_hedge_bot.runner mit diesem Bot-Namen
  # aktiv ist, soll KEIN weiterer Bot gestartet werden.
  #
  # Wir suchen bewusst breit nach dem Bot-Namen im Kommandozeilenaufruf.
  local pids
  pids="$(ps -o pid= -o args= -C python -C python3 2>/dev/null \
    | grep -F "fixed_cycle_hedge_bot.runner" \
    | grep -F -- "--bot-name ${BOT_NAME}" \
    | awk '{print $1}' \
    | tr '\n' ' ' \
    | sed 's/ *$//' || true)"
  if [[ -z "${pids// }" ]]; then
    return 0
  fi
  echo "[${BOT_NAME}] already running (PIDs=${pids}); aborting start" >&2
  exit 0
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

  echo "[${BOT_NAME}] runtime_config_prepare_started symbol=${reserved_symbol} source_config=${CONFIG_FILE} runtime_config=${RUNTIME_CONFIG_FILE}" >&2
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

  if [[ ! -f "${RUNTIME_CONFIG_FILE}" ]]; then
    echo "[${BOT_NAME}] runtime_config_prepare_failed reason=runtime_config_missing path=${RUNTIME_CONFIG_FILE}" >&2
    exit 1
  fi

  RESERVED_SYMBOL_VALUE="${reserved_symbol}" \
  RUNTIME_CONFIG_FILE_PATH="${RUNTIME_CONFIG_FILE}" \
  python3 <<'PY'
import json
import os
from pathlib import Path

runtime_config = Path(os.environ["RUNTIME_CONFIG_FILE_PATH"])
reserved_symbol = (os.environ.get("RESERVED_SYMBOL_VALUE") or "").upper()
try:
    cfg = json.loads(runtime_config.read_text(encoding="utf-8") or "{}")
except Exception as exc:  # pragma: no cover - defensive
    print(f"[runtime_config_validate] failed_to_load path={runtime_config} error={exc}", flush=True)
    raise

symbol_value = str(cfg.get("symbol") or "").upper()
if symbol_value != reserved_symbol:
    print(
        f"[runtime_config_validate] symbol_mismatch path={runtime_config} "
        f"expected={reserved_symbol} actual={symbol_value}",
        flush=True,
    )
    raise SystemExit(1)

print(
    f"[runtime_config_validate] runtime_config_validated path={runtime_config} symbol={symbol_value}",
    flush=True,
)
PY
  echo "[${BOT_NAME}] runtime_config_written path=${RUNTIME_CONFIG_FILE}" >&2
}

SIDE="short"
source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "${SIDE}"

mkdir -p "${RUN_DIR}"
rm -f "${CANCEL_START_FILE}" "${WAIT_PID_FILE}"
START_LOCK_FILE="${RUN_DIR}/start.lock"

# Global Start-Lock pro Bot, um parallele Starts sicher zu verhindern.
# Der Lock bleibt für die gesamte Dauer des Skripts gehalten.
START_LOCK_FD=0
if ! exec {START_LOCK_FD}>"${START_LOCK_FILE}"; then
  echo "[${BOT_NAME}] failed to open start lock file: ${START_LOCK_FILE}" >&2
  exit 1
fi
if ! flock -n "${START_LOCK_FD}"; then
  # Lock ist belegt – prüfen, ob es sich nur um einen stale Lock ohne echten
  # Start-/Runner-Prozess handelt. In diesem Fall darf der Lock entfernt werden.
  self_pid="$$"
  bash_pid="${BASHPID:-}"
  ppid="${PPID:-}"

  other_start_pids="$(ps -o pid= -o args= 2>/dev/null \
    | grep -F "start_short_bot.sh" \
    | grep -F -- "${BOT_NAME}" \
    | grep -v "grep" \
    | awk '{print $1}' \
    | tr '\n' ' ' \
    | sed 's/ *$//' || true)"
  waiter_pids="$(ps -o pid= -o args= 2>/dev/null \
    | grep -F "wait_for_unique_symbol.sh" \
    | grep -F -- "${BOT_NAME}" \
    | grep -v "grep" \
    | awk '{print $1}' \
    | tr '\n' ' ' \
    | sed 's/ *$//' || true)"
  runner_pids="$(ps -o pid= -o args= -C python -C python3 2>/dev/null \
    | grep -F "fixed_cycle_hedge_bot.runner" \
    | grep -F -- "--bot-name ${BOT_NAME}" \
    | grep -v "grep" \
    | awk '{print $1}' \
    | tr '\n' ' ' \
    | sed 's/ *$//' || true)"

  # PIDs um eigene Shell/Eltern bereinigen
  cleaned_other=""
  for pid in ${other_start_pids}; do
    if [[ "${pid}" != "${self_pid}" && "${pid}" != "${bash_pid}" && "${pid}" != "${ppid}" ]]; then
      cleaned_other+=" ${pid}"
    fi
  done
  other_start_pids="${cleaned_other# }"

  cleaned_waiter=""
  for pid in ${waiter_pids}; do
    if [[ "${pid}" != "${self_pid}" && "${pid}" != "${bash_pid}" && "${pid}" != "${ppid}" ]]; then
      cleaned_waiter+=" ${pid}"
    fi
  done
  waiter_pids="${cleaned_waiter# }"

  cleaned_runner=""
  for pid in ${runner_pids}; do
    if [[ "${pid}" != "${self_pid}" && "${pid}" != "${bash_pid}" && "${pid}" != "${ppid}" ]]; then
      cleaned_runner+=" ${pid}"
    fi
  done
  runner_pids="${cleaned_runner# }"

  lock_holders=""
  if command -v fuser >/dev/null 2>&1; then
    lock_holders="$(fuser "${START_LOCK_FILE}" 2>/dev/null | tr ' ' '\n' | tr '\n' ' ' | sed 's/ *$//' || true)"
  elif command -v lsof >/dev/null 2>&1; then
    lock_holders="$(lsof -t -- "${START_LOCK_FILE}" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//' || true)"
  fi
  cleaned_holders=""
  for pid in ${lock_holders}; do
    if [[ "${pid}" != "${self_pid}" && "${pid}" != "${bash_pid}" && "${pid}" != "${ppid}" ]]; then
      cleaned_holders+=" ${pid}"
    fi
  done
  lock_holders="${cleaned_holders# }"

  echo "[${BOT_NAME}] start_lock_busy_probe other_start_pids=${other_start_pids:-none} waiter_pids=${waiter_pids:-none} runner_pids=${runner_pids:-none} lock_holders=${lock_holders:-none} self_pid=${self_pid} bashpid=${bash_pid:-none} ppid=${ppid:-none}" >&2

  if [[ -z "${other_start_pids// }" && -z "${waiter_pids// }" && -z "${runner_pids// }" && -z "${lock_holders// }" && ! -f "${PID_FILE}" ]]; then
    echo "[${BOT_NAME}] stale_start_lock_removed path=${START_LOCK_FILE} reason=no_runner_no_start_process" >&2
    rm -f "${START_LOCK_FILE}" || true
    # Lock-Datei neu öffnen und Lock erneut versuchen.
    if ! exec {START_LOCK_FD}>"${START_LOCK_FILE}"; then
      echo "[${BOT_NAME}] failed to reopen start lock file: ${START_LOCK_FILE}" >&2
      exit 1
    fi
    if ! flock -n "${START_LOCK_FD}"; then
      echo "[${BOT_NAME}] start already in progress; aborting start (lock busy)" >&2
      exit 0
    fi
  else
    echo "[${BOT_NAME}] start already in progress; aborting start (lock busy)" >&2
    exit 0
  fi
fi

# Direction-neutral Pair-Leader/Follower-Start: gemeinsamer Pair-State pro Profil.
BOT_INDEX="${BOT_NAME##*_}"
PAIR_STATE_DIR="${PROJECT_ROOT}/live_bots/state"
PAIR_STATE_FILE="${PAIR_STATE_DIR}/pair_symbol_bot_${BOT_INDEX}.json"
PAIR_LOCK_FILE="${PAIR_STATE_DIR}/pair_symbol_bot_${BOT_INDEX}.lock"
mkdir -p "${PAIR_STATE_DIR}"
PAIR_LOCK_FD=0
if ! exec {PAIR_LOCK_FD}>"${PAIR_LOCK_FILE}"; then
  echo "[${BOT_NAME}] failed to open pair state lock: ${PAIR_LOCK_FILE}" >&2
  exit 1
fi
if ! flock -n "${PAIR_LOCK_FD}"; then
  echo "[${BOT_NAME}] pair_symbol_start_lock_busy; aborting start" >&2
  exit 0
fi

PAIR_SYMBOL=""
if [[ -f "${PAIR_STATE_FILE}" ]]; then
  PAIR_SYMBOL="$(python3 <<PY
import json
from pathlib import Path

path = Path(${PAIR_STATE_FILE@Q})
try:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
except Exception:
    data = {}
symbol = str(data.get("symbol") or "").strip().upper()
print(symbol)
PY
)"
fi

# Stale-Pair-State-Check: Wenn ein Pair-Symbol existiert, aber weder Long- noch Short-Bot
# für dieses Profil laufen, den Pair-State für diesen Start ignorieren.
if [[ -n "${PAIR_SYMBOL}" ]]; then
  LONG_BOT_NAME="long_bot_${BOT_INDEX}"
  SHORT_BOT_NAME="short_bot_${BOT_INDEX}"
  LONG_PID_FILE="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/${LONG_BOT_NAME}/run/bot.pid"
  SHORT_PID_FILE="${PROJECT_ROOT}/live_bots/short_hedge_bot/${SHORT_BOT_NAME}/run/bot.pid"
  long_alive="false"
  short_alive="false"
  if [[ -f "${LONG_PID_FILE}" ]]; then
    long_pid="$(cat "${LONG_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${long_pid}" && -d "/proc/${long_pid}" ]]; then
      long_alive="true"
    fi
  fi
  if [[ -f "${SHORT_PID_FILE}" ]]; then
    short_pid="$(cat "${SHORT_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${short_pid}" && -d "/proc/${short_pid}" ]]; then
      short_alive="true"
    fi
  fi
  if [[ "${long_alive}" != "true" && "${short_alive}" != "true" ]]; then
    echo "[${BOT_NAME}] stale_pair_state_ignored symbol=${PAIR_SYMBOL} reason=no_running_bots" >&2
    # Pair-State defensiv leeren, damit Folge-Starts keinen alten Handoff mehr nutzen.
    python3 <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

path = Path(${PAIR_STATE_FILE@Q})
try:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
except Exception:
    data = {}
changed = False
if data.get("symbol"):
    data["symbol"] = ""
    changed = True
if data.get("long_running"):
    data["long_running"] = False
    changed = True
if data.get("short_running"):
    data["short_running"] = False
    changed = True
if not data.get("long_running") and not data.get("short_running") and data.get("leader_bot"):
    data["leader_bot"] = ""
    changed = True
if changed:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
    PAIR_SYMBOL=""
  fi
fi

# Konflikt-Check: Wenn bereits ein Pair-Symbol existiert, aber der Long-Bot
# (Gegenbot) mit einem anderen Symbol läuft, Start abbrechen.
CONFLICT_STATUS=0
if [[ -n "${PAIR_SYMBOL}" ]]; then
  OTHER_BOT_NAME="long_bot_${BOT_INDEX}"
  OTHER_RUNTIME_CONFIG="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/${OTHER_BOT_NAME}/run/fixed_cycle_config.runtime.json"
  OTHER_PID_FILE="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/${OTHER_BOT_NAME}/run/bot.pid"
  export PAIR_SYMBOL OTHER_BOT_NAME OTHER_RUNTIME_CONFIG OTHER_PID_FILE
  CONFLICT_MSG="$(
    python3 <<'PY'
import json
import os
from pathlib import Path

pair_symbol = os.environ.get("PAIR_SYMBOL") or ""
other_bot = os.environ.get("OTHER_BOT_NAME") or ""
runtime_path = Path(os.environ.get("OTHER_RUNTIME_CONFIG") or "")
pid_path = Path(os.environ.get("OTHER_PID_FILE") or "")

running = False
other_symbol = ""
try:
    pid_text = pid_path.read_text(encoding="utf-8").strip()
    pid = int(pid_text) if pid_text else None
except Exception:
    pid = None

if pid is not None and (Path("/proc") / str(pid)).exists():
    running = True

if running and runtime_path.exists():
    try:
        cfg = json.loads(runtime_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        cfg = {}
    other_symbol = str(cfg.get("symbol") or "").upper()

if running and other_symbol and other_symbol != pair_symbol:
    print(
        f"pair_symbol_conflict_detected pair_symbol={pair_symbol} "
        f"other_bot={other_bot} other_symbol={other_symbol}"
    )
    raise SystemExit(1)
PY
  )" || CONFLICT_STATUS=$?
  if [[ ${CONFLICT_STATUS} -ne 0 ]]; then
    if [[ -n "${CONFLICT_MSG}" ]]; then
      echo "[${BOT_NAME}] ${CONFLICT_MSG}" >&2
    else
      echo "[${BOT_NAME}] pair_symbol_conflict_detected (details unavailable)" >&2
    fi
    exit 1
  fi
fi

IS_PAIR_LEADER="false"

if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${PID_FILE}")"
  if is_alive_pid_with_bot_name "${EXISTING_PID}" "${BOT_NAME}"; then
    echo "[${BOT_NAME}] already running (PID=${EXISTING_PID})" >&2
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

# Fallback-Schutz: Falls kein gültiges PID_FILE existiert, aber noch ein
# laufender Runner-Prozess für diesen Bot im System ist, Start abbrechen.
ensure_no_other_runner_process

# Wenn kein Runner aktiv ist, lokale State-Dateien (active_bot_symbols, Runtime-/Reserved-Files)
# defensiv bereinigen, bevor ein neuer Start vorbereitet wird.
cleanup_local_bot_state_if_no_runner

FIXED_SYMBOL="${SHORT_FIXED_SYMBOL:-}"
if [[ -n "${FIXED_SYMBOL}" ]]; then
  echo "[${BOT_NAME}] fixed_symbol_env_detected symbol=$(echo "${FIXED_SYMBOL}" | tr '[:lower:]' '[:upper:]')" >&2
fi
SKIP_SYMBOL_RESERVATION="${SHORT_SKIP_SYMBOL_RESERVATION:-}"
WAIT_SYMBOL=""
WAIT_REASON="waiting_for_symbol"
WAIT_RESERVED_BY=""
if [[ -n "${FIXED_SYMBOL}" ]]; then
  WAIT_SYMBOL="$(echo "${FIXED_SYMBOL}" | tr '[:lower:]' '[:upper:]')"
  WAIT_REASON="forced_symbol"
fi

ensure_wait_pid_clean

if [[ -n "${PAIR_SYMBOL}" ]]; then
  RESERVED_SYMBOL="${PAIR_SYMBOL}"
  echo "[${BOT_NAME}] pair_symbol_adopted symbol=${RESERVED_SYMBOL} leader_bot=$(python3 <<PY
import json
from pathlib import Path

path = Path(${PAIR_STATE_FILE@Q})
try:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
except Exception:
    data = {}
print(str(data.get("leader_bot") or "unknown"))
PY
) source=pair_state"
  # Follower schreibt nur lokale Runtime-/Reserved-Files, ohne neue best_coin-Reservation.
  write_reserved_runtime_files "${RESERVED_SYMBOL}"
else
  if [[ -n "${FIXED_SYMBOL}" ]]; then
    FIXED_SYMBOL="$(echo "${FIXED_SYMBOL}" | tr '[:lower:]' '[:upper:]')"
    echo "[${BOT_NAME}] using forced symbol ${FIXED_SYMBOL}"
    RESERVED_SYMBOL="${FIXED_SYMBOL}"
    if [[ -z "${SKIP_SYMBOL_RESERVATION}" ]]; then
      write_reserved_runtime_files "${RESERVED_SYMBOL}"
    else
      echo "[${BOT_NAME}] skipping persistent reservation (SHORT_SKIP_SYMBOL_RESERVATION enabled)"
    fi
    IS_PAIR_LEADER="true"
  elif [[ -n "${SKIP_SYMBOL_RESERVATION}" ]]; then
    RESERVED_SYMBOL="${WAIT_SYMBOL}"
    if [[ -z "${RESERVED_SYMBOL}" ]]; then
      RESERVED_SYMBOL="BTCUSDT"
    fi
    echo "[${BOT_NAME}] skipping symbol reservation and using ${RESERVED_SYMBOL}"
    IS_PAIR_LEADER="true"
  else
    echo "[${BOT_NAME}] wallet capture skipped: global wallet refill watcher handles this later"

    # Bevor wir in den blockierenden Symbol-Waiter gehen, prüfen wir explizit auf
    # den "no_good_candidates"-Fall aus logs/best_coin.json. In diesem Szenario
    # wird kein Runner gestartet, sondern ein stabiler Zwischenstatus geschrieben.
    BEST_REASON=""
    BEST_COUNT=0
    mapfile -t BEST_INFO < <(python3 <<PY
import json
from pathlib import Path

best_path = Path(${PROJECT_ROOT@Q}) / "logs" / "best_coin.json"
reason = ""
count = 0
if best_path.exists():
    try:
        payload = json.loads(best_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        payload = {}
    reason = str(payload.get("reason") or "")
    try:
        count = int(payload.get("candidate_count") or 0)
    except Exception:
        count = 0
print(reason)
print(count)
PY
)
    BEST_REASON="${BEST_INFO[0]:-}"
    BEST_COUNT="${BEST_INFO[1]:-0}"

    if [[ -z "${WAIT_SYMBOL}" && "${BEST_REASON}" == "no_good_candidates" ]]; then
      echo "[${BOT_NAME}] waiting_for_symbol reason=no_good_candidates candidate_count=${BEST_COUNT}" >&2
      write_status_json "waiting_for_symbol" "no_good_candidates" "" "" "true"
      # Kein Runner-Start, kein bot.pid – Dashboard soll diesen Zwischenstatus anzeigen.
      exit 0
    fi

    write_status_json "waiting_for_symbol" "${WAIT_REASON}" "${WAIT_SYMBOL}" "${WAIT_RESERVED_BY}" "true"
    echo "$$" > "${WAIT_PID_FILE}"
    if ! "${BOT_GROUP_DIR}/shared_scripts/wait_for_unique_symbol.sh" "${BOT_NAME}"; then
      echo "[${BOT_NAME}] waiting_for_symbol (${WAIT_REASON})" >&2
      exit 1
    fi
    cleanup_wait_files

    RESERVED_SYMBOL="$(read_reserved_symbol)"
    write_reserved_runtime_files "${RESERVED_SYMBOL}"
    IS_PAIR_LEADER="true"
  fi
fi

if [[ "${IS_PAIR_LEADER}" == "true" ]]; then
  python3 <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

path = Path(${PAIR_STATE_FILE@Q})
try:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
except Exception:
    data = {}
symbol = ${RESERVED_SYMBOL@Q}
index = ${BOT_INDEX@Q}
data.update({
    "symbol": symbol,
    "leader_bot": ${BOT_NAME@Q},
    "long_bot_name": f"long_bot_{index}",
    "short_bot_name": f"short_bot_{index}",
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "short_running": True,
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[PAIR-STATE] updated pair_symbol_bot_{index}.json symbol={symbol} leader={data['leader_bot']}")
PY
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

# Start-Lock freigeben, bevor längere Hintergrundprozesse (Runner/Watchdogs) gestartet werden,
# damit diese den FD nicht erben und den Lock nicht dauerhaft halten.
if [[ -n "${START_LOCK_FD:-}" ]]; then
  flock -u "${START_LOCK_FD}" || true
  exec {START_LOCK_FD}>&- || true
fi

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

start_short_watchdog() {
  local watchdog_script="${BOT_GROUP_DIR}/shared_scripts/start_safety_order_watchdog.sh"
  if [[ -x "${watchdog_script}" ]]; then
    "${watchdog_script}" "${BOT_NAME}"
  else
    echo "[${BOT_NAME}] watchdog start script missing: ${watchdog_script}" >&2
  fi
}

start_short_wallet_refill_watchdog() {
  local watchdog_script="${BOT_GROUP_DIR}/shared_scripts/start_wallet_refill_watchdog.sh"
  if [[ -x "${watchdog_script}" ]]; then
    "${watchdog_script}" "${BOT_NAME}"
  else
    echo "[${BOT_NAME}] wallet refill watchdog missing: ${watchdog_script}" >&2
  fi
}

start_short_watchdog
start_short_wallet_refill_watchdog
