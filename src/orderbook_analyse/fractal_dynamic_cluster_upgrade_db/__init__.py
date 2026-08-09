"""Dynamic P5 cluster exit-upgrade research (MySQL SoT, frozen confluence)."""

from __future__ import annotations

from pathlib import Path

# Reuse frozen confluence constants
from orderbook_analyse.fractal_signal_confluence_db import (  # noqa: F401
    APT_IS_END,
    ENV_FILE,
    FEE_PCT,
    MAX_HOLD_BY_TF,
    MIN_SAMPLE,
    SIGNAL_TFS,
    SYMBOLS,
    TF_RANK,
    TPSL_BY_TF,
    TPSL_EXTRA_4H,
    VERY_SMALL,
)

AUDIT_VERSION = "fractal_dynamic_cluster_upgrade_db_v1"

POLICIES = ("P0", "P5A", "P5B", "P5C")
CONFLICT_POLICIES = ("C1", "C2", "C3")

PROFIT_BUCKETS = (
    ("lt0", -1e9, 0.0),
    ("0_0.5", 0.0, 0.5),
    ("0.5_1", 0.5, 1.0),
    ("1_2", 1.0, 2.0),
    ("2_4", 2.0, 4.0),
    ("gt4", 4.0, 1e9),
)

TIME_BUCKETS = (
    ("0_15m", 0, 15),
    ("15_30m", 15, 30),
    ("30_60m", 30, 60),
    ("1_2h", 60, 120),
    ("2_4h", 120, 240),
    ("gt4h", 240, 1e9),
)

DEFINITIONS_DOC = """
Dynamic cluster upgrade (P5) — frozen confluence + fixed TPSL

Cluster / windows / signals: identical to fractal_signal_confluence_db
(see that package DEFINITIONS; reconstructed from MySQL market_candles).

Entry: first signal of cluster (T0). One trade per cluster. No second position.

Base TPSL by TF (unchanged research candidates):
  15m: TP1.0 / SL1.0
  30m: TP2.0 / SL1.5
  1h:  TP2.0 / SL1.5
  4h:  TP4.0 / SL2.0  (+ sensitivity TP6 / SL3)

P0: exit plan frozen at first-signal TF for whole trade.

P5: while trade open, if a higher TF same-side signal confirms (acted at its T0 entry),
upgrade exit plan:
  P5A FULL: TP and SL → higher TF plan; max_hold extends to higher TF hold
  P5B TP ONLY: TP → higher TF; SL unchanged
  P5C TP + NEVER LOOSEN SL: TP → higher TF; SL distance = min(old, new)

Upgrade only if trade still open under current plan at upgrade T0
(no retrospective upgrade if original TP/SL already hit).

Conflict after entry (higher TF opposite):
  C1: close immediately
  C2: keep trade, freeze further upgrades
  C3: close (higher-TF dominance); no reverse trade

Fees 0.11% roundtrip once at final exit. SL_FIRST same-bar.
HIGHEST_TF_ONLY: retrospective diagnostic oracle only.
"""
