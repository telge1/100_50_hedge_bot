"""Equity + Reserve curve analysis with leverage variants (validated trades only)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_equity_curve_analysis_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_equity_curve_analysis")

START_ACTIVE = 1000.0
START_RESERVE = 0.0
CASHOUT_RATE = 0.30
COVERAGE_RATE = 1.00
EXPECTED_N_TRADES = 6476

LEVERAGES = (1, 2, 3, 5, 10)

# Known worst 10-SL streak window (for chart shading)
WORST_SL_STREAK_START = "2025-07-31 00:00:00"
WORST_SL_STREAK_END = "2025-08-01 23:59:59"

DEFINITIONS_DOC = f"""
Equity + Reserve curves ({AUDIT_VERSION})

Input: {REF_TRADES} (no strategy change).
Start ACTIVE={START_ACTIVE}, RESERVE={START_RESERVE}.
Cashout {CASHOUT_RATE:.0%} of positive raw PnL → RESERVE.
Loss reimbursement: min(|pnl|, RESERVE) → ACTIVE (coverage {COVERAGE_RATE:.0%}).

Leverage L: leveraged_net = net_return_pct * L
(fees already inside net_return_pct — not reapplied).

If ACTIVE <= 0 → CAPITAL_DEPLETED; stop compounding further trades.
TOTAL_WEALTH = ACTIVE + RESERVE.
"""
