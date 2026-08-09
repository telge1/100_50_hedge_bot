"""Independent double-check of fractal_wave_fade_global_single_position_db results."""

from __future__ import annotations

from pathlib import Path

from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE, TF_RANK, TPSL_BY_TF
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    PRIMARY_FEE,
    STRATEGY_MAX_HOLD_BY_TF,
)

AUDIT_VERSION = "fractal_wave_fade_global_single_position_doublecheck_v1"

REF_DIR = Path("results/fractal_wave_fade_global_single_position_db")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_global_single_position_doublecheck")

SYMBOLS = ("APTUSDT", "DOGEUSDT")
COVERAGE_TFS = ("1m", "15m", "30m", "1h", "4h")
TF_BAR_MIN = {"1m": 1, "15m": 15, "30m": 30, "1h": 60, "4h": 240}

COMMON_START = "2022-10-19T02:48:00+00:00"
COMMON_END = "2026-08-08T04:00:00+00:00"

FEE_PCT = PRIMARY_FEE  # 0.11
PRICE_TOL = 1e-8
PCT_TOL = 1e-6
PERF_TOL = 1e-6

SAMPLE_SEED = 42

DEFINITIONS_DOC = f"""
Independent double-check / audit ({AUDIT_VERSION})

Purpose: verify that results in {REF_DIR} are causally correct and not
artificially inflated by implementation bugs. No strategy re-optimization.

Data: MySQL market_candles only via ENV_FILE={ENV_FILE}.
Reference CSVs under {REF_DIR} are read only as audit targets.

Independent path:
- Reload MySQL candles
- Reconstruct entry from signal_time → first 1m open strictly after
- Replay 1m OHLC exits with SL_FIRST and causal P5A ladder changes
- Separate event-loop (does NOT call run_global_single_position)

Frozen semantics checked (not changed):
- T0 entry: first 1m open with open_time > signal_available_at
- P5A ladder: {TPSL_BY_TF}
- Max-hold: {STRATEGY_MAX_HOLD_BY_TF}
- Fee roundtrip: {FEE_PCT}%
- Global max 1 position; next entry strictly after prior exit
- Same-bar TP+SL → SL_FIRST
"""
