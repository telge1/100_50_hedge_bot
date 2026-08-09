"""July 2026 SL loser causal audit (no strategy change)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_loser_audit_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_loser_audit")

# MFE-before-SL classification vs final TP distance
IMMEDIATE_MFE_FRAC = 0.15   # <15% of TP → immediate failure
PARTIAL_MFE_FRAC = 0.50     # 15–50% partial; 50–85% still partial; >=85% near TP
NEAR_TP_MFE_FRAC = 0.85

DEFINITIONS_DOC = f"""
Loser audit ({AUDIT_VERSION})

Input: July 2026 trades from global-single trades.csv (Reason=SL).
Signals reconstructed from MySQL waves + frozen annotate (Tier A).
1m path for MFE/MAE until SL (causal, no lookahead).

MFE classes vs final TP distance:
  IMMEDIATE_FAILURE: MFE/TP < {IMMEDIATE_MFE_FRAC}
  PARTIAL_FADE_THEN_FAIL: [{IMMEDIATE_MFE_FRAC}, {NEAR_TP_MFE_FRAC})
  NEAR_TP_THEN_FAIL: MFE/TP >= {NEAR_TP_MFE_FRAC}

No strategy change / no filters implemented.
"""
