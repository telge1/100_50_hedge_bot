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

PYTHON_CMD="${LONG_BOT_SYMBOL_WATCHER_PYTHON:-python3}"

CONFIG_FILE="${BOT_DIR}/config/fixed_cycle_config.json"
STATE_FILE="${BOT_DIR}/state/fixed_cycle_state.json"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "ERROR: config file missing: ${CONFIG_FILE}" >&2
  exit 1
fi

export CONFIG_FILE
export STATE_FILE

SYMBOL_INFO="$(
  "${PYTHON_CMD}" <<'PY'
import json
import os
from pathlib import Path

config_path = Path(os.environ["CONFIG_FILE"])
state_path = Path(os.environ["STATE_FILE"])

if not config_path.exists():
    raise FileNotFoundError(f"CONFIG_FILE does not exist: {config_path}")

symbol = ""
category = "linear"

if state_path.exists():
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        symbol = payload.get("symbol") or ""
        category = payload.get("category") or category
        strategy_state = payload.get("strategy_state") or {}
        cycle_state = strategy_state.get("cycle_state") or {}
        if not symbol:
            symbol = cycle_state.get("symbol") or ""
    except Exception:
        pass

with config_path.open(encoding="utf-8") as fh:
    cfg = json.loads(fh.read())
symbol = symbol or cfg.get("symbol") or ""
category = cfg.get("category") or category

print(json.dumps({"symbol": symbol, "category": category}))
PY
)"

SYMBOL=$(echo "${SYMBOL_INFO}" | "${PYTHON_CMD}" -c 'import json,sys; print(json.load(sys.stdin)["symbol"])')
CATEGORY=$(echo "${SYMBOL_INFO}" | "${PYTHON_CMD}" -c 'import json,sys; print(json.load(sys.stdin)["category"])')

if [[ -z "${SYMBOL}" ]]; then
  echo "ERROR: unable to determine symbol for ${BOT_NAME}" >&2
  exit 1
fi

source "${BOT_GROUP_DIR}/shared_scripts/load_bybit_env.sh" "${BOT_NAME}" "long"

export TARGET_SYMBOL="${SYMBOL}"
export TARGET_CATEGORY="${CATEGORY}"
export BOT_NAME="${BOT_NAME}"
export PROJECT_ROOT
export BOT_DIR

${PYTHON_CMD} <<PY
import os
import sys
import time
from pathlib import Path

project_root = Path(os.environ["PROJECT_ROOT"])
bot_dir = Path(os.environ["BOT_DIR"])
if not project_root.exists():
    print(f"PROJECT_ROOT does not exist: {project_root}", file=sys.stderr)
    sys.exit(1)
if not bot_dir.exists():
    print(f"BOT_DIR does not exist: {bot_dir}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(project_root))

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

symbol = os.environ["TARGET_SYMBOL"]
category = os.environ.get("TARGET_CATEGORY", "linear")
bot_name = os.environ["BOT_NAME"]
api_key = os.environ.get("BYBIT_API_KEY")
secret_key = os.environ.get("BYBIT_API_SECRET")

if not api_key or not secret_key:
    print("Missing API credentials", file=sys.stderr)
    sys.exit(1)

om = BybitOrderManager(api_key, secret_key)

def fetch_qty():
    positions = om.fetch_positions(symbol=symbol, category=category)
    long_qty = 0.0
    short_qty = 0.0
    for pos in positions:
        try:
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        side = str(pos.get("side") or "").capitalize()
        if side == "Buy":
            long_qty += size
        elif side == "Sell":
            short_qty += size
    return long_qty, short_qty

def close(side_label, qty):
    request_side = "Sell" if side_label == "long" else "Buy"
    position_idx = 1 if side_label == "long" else 2
    resp = om.place_reduce_market_order(
        symbol=symbol,
        side=request_side,
        qty=qty,
        position_idx=position_idx,
        category=category,
        order_link_id=None,
    )
    if not resp:
        print(f"Failed to submit reduce-only order for {side_label}", file=sys.stderr)
        sys.exit(1)

long_qty, short_qty = fetch_qty()
if long_qty <= 0 and short_qty <= 0:
    print("No open positions for symbol", symbol)
    sys.exit(0)

if long_qty > 0:
    close("long", long_qty)
if short_qty > 0:
    close("short", short_qty)

long_after, short_after = fetch_qty()
if long_after > 0 or short_after > 0:
    print(f"Positions still open long={long_after} short={short_after}", file=sys.stderr)
    sys.exit(1)

print("Positions closed for", symbol)
PY
