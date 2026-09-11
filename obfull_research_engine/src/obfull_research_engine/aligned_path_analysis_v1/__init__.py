"""Causal path analysis after the frozen high-conviction ALIGNED classification.

Descriptive only. Does not recompute HC classes or rewrite the HC run.
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "aligned_path_analysis_v1"
STATUS = "RESEARCH_PATH_ANALYSIS_ONLY"
SIGNAL_STATUS = "NOT_A_TRADING_SIGNAL"
FROZEN_HC_RUN_KEY = "hcv1_62ffdd8f3e439a9b"
FROZEN_HC_BLIND_HASH = "1242c67884cdb96cc96f8570c6984b470d7014cb03b3978653cff44920de17e7"
FROZEN_HC_CONFIG_SHA256 = "7608498b99ae2b75469d4f0e992cf63fb120b83f52b288024ad9089cac3f5a54"
HORIZONS_S = (5, 15, 30, 60, 300, 900, 1800)
MARK_PCTS = (0.05, 0.10, 0.20, 0.30, 0.50, 0.75, 1.00)
# First-move floor: ignore sub-basis-point ticks when labeling PROFIT/ADVERSE first.
FIRST_MOVE_FLOOR_PCT = 0.01
# Match public-trade outcome contract: last event may sit up to 60s before horizon end.
PATH_COMPLETE_SLACK_MS = 60_000
RELEVANT_MARK_PCT = 0.10
CHOP_EXPANSION_PCT = 0.20
CHOP_MIN_ZERO_CROSSES = 3
MAX_HORIZON_S = 1800
SYMBOL = "BTCUSDT"

SEQUENCE_CLASSES = (
    "DIRECT_PROFIT_HELD",
    "DIRECT_PROFIT_GIVEN_BACK",
    "ADVERSE_THEN_PROFIT",
    "ADVERSE_NO_RECOVERY",
    "CHOP_AROUND_ENTRY",
    "FLAT_OR_INSUFFICIENT_DATA",
)

COMPARE_GROUPS = (
    "ALIGNED",
    "HC_STRICT",
    "HC_WITH_OI_MIXED",
    "ALIGNED_BUT_CONFLICTING",
)
