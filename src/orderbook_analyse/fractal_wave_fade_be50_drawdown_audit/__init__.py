"""Drawdown-distribution audit on existing BE50 full-backtest equity."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_be50_drawdown_audit_v1"
REF_DIR = Path("results/fractal_wave_fade_be50_full_backtest")
EQUITY_CSV = REF_DIR / "equity_comparison.csv"
TRADES_CSV = REF_DIR / "full_trade_comparison.csv"
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_be50_drawdown_audit")

START_EQUITY = 1000.0
EXPECTED_BE50_MAX_DD = -15.13428703864581
MAX_DD_TOL = 1e-6

THRESHOLDS = [2.0, 3.0, 5.0, 7.5, 10.0, 12.0, 13.0, 14.0, 15.0]
COMPARE_THRESHOLDS = [5.0, 10.0, 12.0, 14.0, 15.0, 20.0]
