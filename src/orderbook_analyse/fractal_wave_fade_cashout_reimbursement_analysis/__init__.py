"""Cashout + loss-reimbursement capital-path analysis (validated trades only)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_cashout_reimbursement_analysis_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_cashout_reimbursement_analysis")

START_ACTIVE = 1000.0
START_RESERVE = 0.0
EXPECTED_N_TRADES = 6476

CASHOUT_RATES = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50)
COVERAGE_RATES = (0.50, 0.75, 1.00)  # fraction of loss eligible for reimbursement

# Known worst SL streak from prior analysis (will be re-detected)
KNOWN_WORST_SL_START = 4972
KNOWN_WORST_SL_END = 4981

DEFINITIONS_DOC = f"""
Cashout + loss reimbursement ({AUDIT_VERSION})

Input: fixed trades from {REF_TRADES}. No strategy change.

Start: ACTIVE={START_ACTIVE}, RESERVE={START_RESERVE}.
Sizing: trade_pnl = ACTIVE_before * net_return_pct / 100

On profit (pnl > 0):
  cashout = pnl * cashout_rate
  ACTIVE += pnl - cashout
  RESERVE += cashout

On loss (pnl < 0), primary ALL_NEGATIVE_TRADES_REIMBURSED:
  ACTIVE += pnl   # apply loss first
  target = min(abs(pnl) * coverage_rate, RESERVE)
  ACTIVE += target
  RESERVE -= target

Invariants:
  - Reserve never negative
  - Cashout only on profits
  - Reimbursement only on losses
  - Reserve↔Active transfer leaves TOTAL_WEALTH unchanged
  - Real economic P&L lives in TOTAL_WEALTH (= ACTIVE + RESERVE)

0% cashout + any coverage ≡ no reserve activity ≡ prior full compounding.
"""
