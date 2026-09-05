#!/usr/bin/env bash
# PROPOSALS ONLY — DO NOT EXECUTE without explicit approval.
# No downloads/imports were run by the audit task.

set -euo pipefail
SG=/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves
cd "$SG"
export PYTHONPATH=src

# --- Pilot: BTC+DOGE 6m gap before current canonical start (2026-07-19) ---
# .venv/bin/python scripts/run_public_trades_7d_backfill.py --mode backfill \
#   --start-date 2026-01-20 --end-date-exclusive 2026-07-19 \
#   --symbols BTCUSDT,DOGEUSDT \
#   --workers 1 \
#   --run-dir results/public_trades_backfill_6m_pilot/BTC_DOGE_20260120_20260719

# --- Optional Gate-A style single day ---
# .venv/bin/python scripts/run_public_trades_7d_backfill.py --mode backfill \
#   --start-date 2026-01-20 --end-date-exclusive 2026-01-21 \
#   --symbols BTCUSDT --workers 1 \
#   --run-dir results/public_trades_backfill_6m_pilot/SMOKE_BTC_20260120

# --- OA download-only (no CH) if caching locally first ---
# OA=/home/telgenbuescher/projects/orderbook_analyse
# PYTHONPATH=$OA/src $OA/.venv/bin/python $OA/scripts/download_bybit_public_trades.py \
#   --symbol BTCUSDT --start 2026-01-20T00:00:00Z --end 2026-01-21T00:00:00Z \
#   --dest /tmp/bybit_pt_cache_NOT_USED

# --- FORBIDDEN without separate design ---
# Do not point OA ingest_public_trades_archive.py at canonical.
# Do not ALTER/OPTIMIZE/TRUNCATE public_trades_canonical.
# Do not restart collectors 1692334 / 147111 / 1661773 for this backfill.
