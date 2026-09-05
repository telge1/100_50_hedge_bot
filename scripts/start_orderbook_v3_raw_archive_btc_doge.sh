#!/usr/bin/env bash
# Shadow raw-archive collector for BTCUSDT+DOGEUSDT with optional OB1000 on-demand.
# Loads project .env (incl. OB_V3_ON_DEMAND_*). Does not touch universe51 / OI collectors.
#
# Preferred production path: systemd user unit
#   bybit-full-ob-raw-archive-btc-doge.service
# (foreground wrapper: scripts/run_orderbook_v3_raw_archive_btc_doge_foreground.sh)
# This nohup helper remains for manual/emergency start only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if systemctl --user is-active --quiet bybit-full-ob-raw-archive-btc-doge.service 2>/dev/null; then
  echo "systemd unit bybit-full-ob-raw-archive-btc-doge.service already active; refuse nohup double-start" >&2
  exit 1
fi

export PYTHONPATH=src
export PYTHONUNBUFFERED=1
export OB_V3_RAW_ARCHIVE_ENABLE=true
export OB_V3_RAW_ARCHIVE_SYMBOLS=BTCUSDT,DOGEUSDT
export OB_V3_RAW_ARCHIVE_ROOT="${OB_V3_RAW_ARCHIVE_ROOT:-$ROOT/data/orderbook_raw_shadow/ob200_v3}"
export OB_V3_RAW_ARCHIVE_ROTATION="${OB_V3_RAW_ARCHIVE_ROTATION:-hour}"
export OB_V3_RAW_ARCHIVE_RETENTION_DAYS="${OB_V3_RAW_ARCHIVE_RETENTION_DAYS:-0}"
export OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB="${OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB:-20}"
export OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB="${OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB:-5}"

# Prefer explicit on-demand enable; .env also loaded by collector settings.
export OB_V3_ON_DEMAND_ENABLE="${OB_V3_ON_DEMAND_ENABLE:-true}"
export OB_V3_ON_DEMAND_KEEPER="${OB_V3_ON_DEMAND_KEEPER:-true}"
export OB_V3_ON_DEMAND_SOCKET_PATH="${OB_V3_ON_DEMAND_SOCKET_PATH:-/run/user/$(id -u)/orderbook_ob1000.sock}"

# Full-OB Edge Flight Recorder shadow pilot (BTC/DOGE only).
# Do NOT pre-export ENABLE=false: that blocks collector dotenv (.env) from enabling FR.
# Prefer an already-exported value; else adopt ENABLE from .env if present; else leave unset.
export OB_V3_FULL_BOOK_ENABLE="${OB_V3_FULL_BOOK_ENABLE:-true}"
if [[ -z "${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE:-}" && -f "$ROOT/.env" ]]; then
  _fr_line="$(grep -E '^[[:space:]]*OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=' "$ROOT/.env" | tail -n1 || true)"
  if [[ -n "${_fr_line}" ]]; then
    export OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${_fr_line#*=}"
    # strip optional quotes / CR
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE%$'\r'}"
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE%\"}"
    OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE#\"}"
    export OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE
  fi
fi
export OB_V3_FULL_OB_FR_SYMBOLS="${OB_V3_FULL_OB_FR_SYMBOLS:-BTCUSDT,DOGEUSDT}"
export OB_V3_FULL_OB_FR_ROOT="${OB_V3_FULL_OB_FR_ROOT:-$ROOT/data/orderbook_raw_shadow/full_ob_edge_flight_recorder}"
_fr_echo="${OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE:-unset(dotenv)}"

LOG="logs/orderbook_v3_raw_archive_btc_doge.nohup.log"
HEALTH="logs/orderbook_v3_raw_archive_btc_doge.health.ndjson"
PIDFILE="logs/orderbook_v3_raw_archive_only.pid"

if [[ -f "$PIDFILE" ]]; then
  old="$(tr -d ' \n' < "$PIDFILE" || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "collector already running pid=$old" >&2
    exit 1
  fi
fi

nohup .venv/bin/python -m orderbook_analyse.orderbook_v2_live \
  --mode raw-archive-only \
  --symbols BTCUSDT,DOGEUSDT \
  --confirm-raw-archive-symbols \
  --health-file "$HEALTH" \
  --log-level INFO \
  >>"$LOG" 2>&1 &
NEWPID=$!
echo "$NEWPID" >"$PIDFILE"
echo "started pid=$NEWPID log=$LOG health=$HEALTH fr_enable=${_fr_echo} fr_root=${OB_V3_FULL_OB_FR_ROOT}"
