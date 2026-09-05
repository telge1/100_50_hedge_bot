#!/usr/bin/env bash
# Foreground Full-OB / raw-archive collector for systemd Type=simple.
# Same env contract as start_orderbook_v3_raw_archive_btc_doge.sh, but exec (no nohup).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH=src
export PYTHONUNBUFFERED=1
export OB_V3_RAW_ARCHIVE_ENABLE=true
export OB_V3_RAW_ARCHIVE_SYMBOLS=BTCUSDT,DOGEUSDT
export OB_V3_RAW_ARCHIVE_ROOT="${OB_V3_RAW_ARCHIVE_ROOT:-$ROOT/data/orderbook_raw_shadow/ob200_v3}"
export OB_V3_RAW_ARCHIVE_ROTATION="${OB_V3_RAW_ARCHIVE_ROTATION:-hour}"
export OB_V3_RAW_ARCHIVE_RETENTION_DAYS="${OB_V3_RAW_ARCHIVE_RETENTION_DAYS:-0}"
export OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB="${OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB:-20}"
export OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB="${OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB:-5}"

export OB_V3_ON_DEMAND_ENABLE="${OB_V3_ON_DEMAND_ENABLE:-true}"
export OB_V3_ON_DEMAND_KEEPER="${OB_V3_ON_DEMAND_KEEPER:-true}"
export OB_V3_ON_DEMAND_SOCKET_PATH="${OB_V3_ON_DEMAND_SOCKET_PATH:-/run/user/$(id -u)/orderbook_ob1000.sock}"

export OB_V3_FULL_BOOK_ENABLE="${OB_V3_FULL_BOOK_ENABLE:-true}"
if [[ -z "${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE:-}" && -f "$ROOT/.env" ]]; then
  _fr_line="$(grep -E '^[[:space:]]*OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=' "$ROOT/.env" | tail -n1 || true)"
  if [[ -n "${_fr_line}" ]]; then
    export OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${_fr_line#*=}"
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE%$'\r'}"
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE%\"}"
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE#\"}"
    export OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE
  fi
fi
export OB_V3_FULL_OB_FR_SYMBOLS="${OB_V3_FULL_OB_FR_SYMBOLS:-BTCUSDT,DOGEUSDT}"
export OB_V3_FULL_OB_FR_ROOT="${OB_V3_FULL_OB_FR_ROOT:-$ROOT/data/orderbook_raw_shadow/full_ob_edge_flight_recorder}"

HEALTH="logs/orderbook_v3_raw_archive_btc_doge.health.ndjson"
PIDFILE="logs/orderbook_v3_raw_archive_only.pid"
# systemd owns the process; keep pidfile in sync for operators / old scripts.
echo "$$" >"$PIDFILE"

exec .venv/bin/python -m orderbook_analyse.orderbook_v2_live \
  --mode raw-archive-only \
  --symbols BTCUSDT,DOGEUSDT \
  --confirm-raw-archive-symbols \
  --health-file "$HEALTH" \
  --log-level INFO
