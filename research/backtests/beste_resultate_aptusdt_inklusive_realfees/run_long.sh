#!/usr/bin/env bash
set -euo pipefail

cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev || exit 1

PYTHONPATH=. python3 -m research.backtests.run_original_hedge_backtest \
  --symbol APTUSDT \
  --direction long \
  --limit 50000 \
  --multi-start \
  --start-step-candles 250 \
  --window-candles 15000 \
  --max-starts 120 \
  --fill-model conservative \
  --config-source live \
  --use-live-short-tp-relief \
  --output-dir research/backtests/results/verify_live_relief_start4000
