#!/usr/bin/env bash
set -euo pipefail

cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev || exit 1

PYTHONPATH=. python3 -m research.backtests.run_original_hedge_backtest \
  --symbol APTUSDT \
  --direction short \
  --limit 50000 \
  --multi-start \
  --start-step-candles 250 \
  --window-candles 15000 \
  --max-starts 120 \
  --fill-model conservative \
  --config-source live \
  --output-dir research/backtests/results/short_fee_corrected_full120
