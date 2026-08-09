"""BE50 exit replay on July 2026 global-single trades (audit only)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_be50_july_2026_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_be50_july_2026")

FEE_PCT = 0.11
CASHOUT_RATE = 0.30
COVERAGE_RATE = 1.00
START_ACTIVE = 1000.0
START_RESERVE = 0.0
BE_FRAC = 0.50

DEFINITIONS_DOC = f"""
BE50 July 2026 replay ({AUDIT_VERSION})

Rule: once price reaches 50% of original TP distance from entry,
move SL to entry (break-even). TP unchanged. No partials / filters.

Price path: MySQL market_candles 1m (no 1s/tick available for July 2026).
Intrabar ties: NO optimistic bias for BE50 —
  - BE trigger vs original SL same bar → treat as SL (BE not armed in time)
  - after armed, TP vs BE same bar → treat as BE (cut winner)
Ambiguous cases flagged AMBIGUOUS_INTRABAR.

Fees: gross - {FEE_PCT}% (same as baseline). BE at entry → net ≈ -{FEE_PCT}%.
Equity: local July-only ACTIVE={START_ACTIVE}, RESERVE={START_RESERVE},
cashout {CASHOUT_RATE:.0%}, reimbursement {COVERAGE_RATE:.0%}.
"""
