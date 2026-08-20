#!/usr/bin/env bash
# Optional watcher — do NOT install cron automatically.
# Suggested crontab (document only):
# */5 * * * * cd /home/telgenbuescher/projects/orderbook_analyse && bash scripts/watch_market_data_health.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH=src
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/results/market_data_health_watch/$STAMP"
mkdir -p "$OUT" "$ROOT/results/market_data_health_watch"
PYTHONPATH=src python scripts/run_market_data_health_audit.py \
  --symbols DOGEUSDT,APTUSDT,BTCUSDT \
  --lookback-minutes 60 \
  --output-dir "$OUT" \
  --log-level WARNING || true
ln -sfn "$OUT" "$ROOT/results/market_data_health_latest" || true
echo "wrote $OUT"
