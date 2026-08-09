"""Full-history BE50 vs baseline A/B (exit-only replay on fixed trades)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_be50_full_backtest_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_be50_full_backtest")

FEE_PCT = 0.11
CASHOUT_RATE = 0.30
COVERAGE_RATE = 1.00
START_ACTIVE = 1000.0
START_RESERVE = 0.0
BE_FRAC = 0.50
EXPECTED_N = 6476

DEFINITIONS_DOC = f"""
Full BE50 A/B ({AUDIT_VERSION})

Fixed trade list: {REF_TRADES} (global-single, frozen entries).
Only exit rule changes under BE50; entries/sequence unchanged.

BE50: arm when 50% of original TP distance reached → SL=entry.
Fees {FEE_PCT}%. Equity: ACTIVE={START_ACTIVE}, RESERVE={START_RESERVE},
cashout {CASHOUT_RATE:.0%}, reimbursement {COVERAGE_RATE:.0%}.

Price path: MySQL 1m. Conservative intrabar (no optimistic BE50).
TRUE_SL_STREAK = consecutive SL only.
NON_WINNER_STREAK = consecutive SL+BE.
"""
