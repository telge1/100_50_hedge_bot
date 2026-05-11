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

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"
AUDIT_LOG="${BOT_DIR}/logs/generic_hedge_runtime_audit.jsonl"
CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
PYTHON_CMD="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON_CMD}" ]]; then
  echo "[${BOT_NAME}] ERROR: ${PYTHON_CMD} not found or not executable" >&2
  exit 1
fi

set +e
SYMBOL_OUTPUT=$(
  "${PYTHON_CMD}" - "${STATE_FILE}" "${AUDIT_LOG}" "${CONFIG_FILE}" "${BOT_DIR}" <<'PY'
import json
import sys
from pathlib import Path

def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return None

def symbol_from_dict(candidate):
    if not isinstance(candidate, dict):
        return None
    for key in ("symbol", "base_symbol", "coin"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return None

state_path = Path(sys.argv[1])
audit_path = Path(sys.argv[2])
config_path = Path(sys.argv[3])
bot_dir = Path(sys.argv[4])

symbol = None
category = None
method = None

state = load_json(state_path)
if state:
    symbol = state.get("symbol") or state.get("strategy_state", {}).get("cycle_state", {}).get("symbol")
    category = state.get("category") or state.get("strategy_state", {}).get("category")
    if symbol:
        method = "state"

if not symbol:
    last_line = None
    if audit_path.exists():
        with audit_path.open(encoding="utf-8") as stream:
            for line in stream:
                trimmed = line.strip()
                if trimmed:
                    last_line = trimmed
    if last_line:
        try:
            entry = json.loads(last_line)
        except json.JSONDecodeError:
            entry = {}
        if isinstance(entry, dict):
            symbol = entry.get("symbol") or symbol_from_dict(entry.get("snapshot"))
            if not category:
                category = entry.get("category")
            if symbol:
                method = "audit_log"

config = load_json(config_path) or {}
if not symbol:
    symbol = config.get("symbol")
    if symbol:
        method = "config"
if not category:
    category = config.get("category")

best_coin_relative = config.get("best_coin_file") or "logs/best_coin.json"
best_coin_path = (bot_dir / best_coin_relative).resolve()
best_coin = load_json(best_coin_path)
if not symbol and best_coin:
    if isinstance(best_coin, dict):
        symbol = symbol_from_dict(best_coin)
    elif isinstance(best_coin, list):
        for entry in best_coin:
            sym = symbol_from_dict(entry if isinstance(entry, dict) else None)
            if sym:
                symbol = sym
                break
    if symbol and method is None:
        method = "best_coin"

if not symbol:
    sys.exit(1)

symbol = symbol.upper()
category = (category or "linear")
method = method or "unknown"
print(symbol)
print(category)
print(method)
PY
)
PY_EXIT=$?
set -e

if [[ ${PY_EXIT} -ne 0 || -z "${SYMBOL_OUTPUT:-}" ]]; then
  echo "[${BOT_NAME}] ERROR: unable to determine active symbol for cancellation" >&2
  exit 1
fi

mapfile -t SYMBOL_LINES <<< "${SYMBOL_OUTPUT}"

SYMBOL="${SYMBOL_LINES[0]:-}"
CATEGORY="${SYMBOL_LINES[1]:-linear}"
METHOD="${SYMBOL_LINES[2]:-unknown}"

if [[ -z "${SYMBOL}" ]]; then
  echo "[${BOT_NAME}] ERROR: symbol detection returned empty string" >&2
  exit 1
fi

CATEGORY="${CATEGORY:-linear}"
METHOD="${METHOD:-unknown}"

echo "[${BOT_NAME}] cancel_open_orders detected symbol=${SYMBOL} via ${METHOD}"

set +e
"${PYTHON_CMD}" -m fixed_cycle_hedge_bot.cleanup \
  --config-file "${CONFIG_FILE}" \
  --symbol "${SYMBOL}" \
  --category "${CATEGORY}"
CLEANUP_EXIT=$?
set -e

if [[ ${CLEANUP_EXIT} -ne 0 ]]; then
  echo "[${BOT_NAME}] ERROR: cleanup cancellation failed (exit_code=${CLEANUP_EXIT})" >&2
  exit ${CLEANUP_EXIT}
fi

echo "[${BOT_NAME}] cancel_open_orders_success symbol=${SYMBOL} category=${CATEGORY}"
