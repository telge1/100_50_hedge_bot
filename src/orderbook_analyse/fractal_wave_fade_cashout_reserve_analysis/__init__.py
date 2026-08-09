"""Cashout / reserve capital-path analysis on validated global-single trades."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_cashout_reserve_analysis_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_cashout_reserve_analysis")

START_ACTIVE = 1000.0
START_RESERVE = 0.0
CASHOUT_RATES = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50)
EXPECTED_N_TRADES = 6476

DEFINITIONS_DOC = f"""
Cashout / reserve analysis ({AUDIT_VERSION})

Input: fixed validated trades from {REF_TRADES}.
No signal regeneration, no strategy change, no re-optimization.

Start: active={START_ACTIVE} USDT, reserve={START_RESERVE} USDT.
Sizing (normalized 100% of ACTIVE only):
  trade_pnl = active_equity * net_return_pct / 100

On winning trade (trade_pnl > 0):
  cashout = trade_pnl * cashout_rate
  active += trade_pnl - cashout
  reserve += cashout

On losing / flat trade (trade_pnl <= 0):
  active += trade_pnl
  reserve unchanged

Reserve is irreversible (never decreases, never redeployed).
Total wealth = active + reserve.

Cashout rates tested: {list(CASHOUT_RATES)}.
0% must reproduce prior full compounding on active equity.
"""
