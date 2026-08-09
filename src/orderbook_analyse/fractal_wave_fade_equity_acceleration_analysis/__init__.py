"""Why equity accelerates ~2024 — descriptive decomposition (no strategy change)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_equity_acceleration_analysis_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_equity_acceleration_analysis")
SYMBOLS = ("DOGEUSDT", "APTUSDT")

DEFINITIONS_DOC = f"""
Equity acceleration analysis ({AUDIT_VERSION})

Input: {REF_TRADES}. Candles: MySQL market_candles only.
No strategy change / reoptimization.

Half-year blocks by exit_time (UTC).
Additive metrics on net_return_pct (fees already inside).
Volatility: ATR14% on 1h bars; median |1h| and |4h| close-to-close returns.
MFE/MAE: causal 1m path from entry bar through exit bar (inclusive), same side definition as engine.
"""
