#!/usr/bin/env bash
# Continuous OB1000 FS → ClickHouse materializer.
# Symbol list: config/ob1000_live_symbols.json (OA) or OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS.
set -euo pipefail
OA_ROOT="/home/telgenbuescher/projects/orderbook_analyse"
ROOT="/home/telgenbuescher/projects/spread_recovery_hedge_short_dev"
cd "$ROOT"
# shellcheck source=/home/telgenbuescher/projects/orderbook_analyse/scripts/_ob1000_symbols.sh
source "$OA_ROOT/scripts/_ob1000_symbols.sh"
OB_SYMBOLS="$(ob1000_resolve_symbols "$OA_ROOT")"
echo "ob1000 materializer symbols=$OB_SYMBOLS" >&2
exec "$ROOT/.venv/bin/python" -m research.btc_doge_research.ob1000_materializer_runner \
  --loop \
  --symbols "$OB_SYMBOLS" \
  --interval-sec "${OB1000_MATERIALIZER_INTERVAL_SEC:-20}" \
  --warn-lag-sec "${OB1000_MATERIALIZER_WARN_LAG_SEC:-90}"
