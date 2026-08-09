"""Manual 10-trade audit report for chart review (no strategy change)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_manual_10_trade_report_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
HIST_EQUITY = Path("results/fractal_wave_fade_cashout_reimbursement_analysis/equity_paths.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_manual_10_trade_report")

START_ACTIVE = 1000.0
START_RESERVE = 0.0
CASHOUT_RATE = 0.30
COVERAGE_RATE = 1.00
TARGET_N = 10
FEE_PCT = 0.11

PRIMARY_WINDOW_START = "2026-07-01 00:00:00"
PRIMARY_WINDOW_END = "2026-07-31 23:59:59"

DEFINITIONS_DOC = f"""
Manual 10-trade audit report ({AUDIT_VERSION})

Input trades: {REF_TRADES}
Candles: MySQL market_candles only.
No new strategy / reoptimization / full backtest.

Selection: last calendar month of history (July 2026 primary),
~10 trades evenly spaced within winners and losers (5/5 when possible),
merged chronologically. Not performance cherry-picking.

Local equity: ACTIVE={START_ACTIVE}, RESERVE={START_RESERVE},
cashout={CASHOUT_RATE:.0%}, loss reimbursement coverage={COVERAGE_RATE:.0%},
ALL_NEGATIVE — same semantics as cashout_reimbursement analysis,
applied ONLY to the selected sample starting at 1000.
"""
