"""Phase-3 R6 orderflow confirmation (research-only, no bot/PnL)."""

from __future__ import annotations

ANALYSIS_ID = "LIQUIDITY_LOCATION_R6_ORDERFLOW_CONFIRMATION_V1"
ANALYSIS_VERSION = "v1"

# Frozen R6 contract (from V2 — do not retune)
R6_CONTRACT = {
    "min_components": 6,
    "touch_timing": "delayed",
    "min_initial_distance_atr": 0.5,
    "distance_atr_buckets": ["0.5-1", "1-2", "2-3", ">3"],
    "event_unit": "independent_episode",
    "bid_ask_mirrored": True,
    "source_rule": "V2_R6",
    "v2_rule_id": "R6_6plus_vs_single_defense",
}

VERDICT_COMPLETE = "LIQUIDITY_LOCATION_R6_ORDERFLOW_CONFIRMATION_V1_COMPLETE"
VERDICT_COVERAGE_BLOCKED = "LIQUIDITY_LOCATION_R6_ORDERFLOW_COVERAGE_BLOCKED"
VERDICT_NO_STABLE = "LIQUIDITY_LOCATION_R6_NO_STABLE_CONFIRMATION_YET"

T3_WINDOWS_SEC = (1, 3, 5, 15, 30, 60)
T3_WINDOWS_1M = (1, 3)

PRIMARY_VARIANT = {
    "acceptance_bars": 2,
    "reclaim_horizon_bars": 6,
    "reaction_atr_mult": 0.5,
}
