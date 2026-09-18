"""Frozen paths and constants for big-move case-control study."""

from __future__ import annotations

from pathlib import Path

BATCH_RUN_REL = Path("obfull_research_engine/runs/mp_edge_event_batch_v1_20260916")
ENRICH_RUN_REL = Path("obfull_research_engine/runs/mp_ob_feature_enrichment_v1_20260916")
PRICE_PATH_RUN_REL = Path("obfull_research_engine/runs/mp_price_path_4h_v1_20260916")
CONFIRM_RUN_REL = Path("obfull_research_engine/runs/mp_entry_confirmation_v1_20260916")
DEFAULT_RUN_REL = Path("obfull_research_engine/runs/mp_big_move_case_control_v1_20260916")
WALL_FILTER_REL = PRICE_PATH_RUN_REL / "frozen_wall_persistence_filter_v1.json"

NS = 1_000_000_000
TARGET_PCT = 0.41
STOP_PCT = 0.15
BIG_MFE_PCT = 0.60
VERY_BIG_MFE_PCT = 0.75
NO_EXPANSION_PCT = 0.20

# OB feature columns (exact names when present; aliases mapped in loader)
OB_FEATURE_CANDIDATES = (
    "depth_inside_1bp",
    "depth_inside_2bps",
    "depth_inside_5bps",
    "depth_beyond_1bp",
    "wall_strength_vs_local_book",
    "hit_qty",
    "hit_notional",
    "pull_qty",
    "pull_notional",
    "add_qty",
    "add_notional",
    "refill_ratio",
    "refill_latency_ms",
    "wall_present_baseline_fraction",
    "wall_present_approach_fraction",
    "wall_present_contact_fraction",
    "wall_survival_after_hits",
    "wall_recovery_fraction",
    "wall_moved_with_price",
    "wall_disappeared_before_touch",
    "level_change_rate",
    "imbalance_mean",
    "imbalance_at_touch",
    "imbalance_at_trigger",
    "spread_mean_bps",
    "aggressive_buy_notional",
    "aggressive_sell_notional",
    "net_aggressive_notional",
    "aggression_against_zone",
    "aggression_without_progress",
    "flow_flip_before_trigger",
)

MANUAL_TRIGGERS_UTC = (
    "2026-09-10T16:44:17",
    "2026-09-06T21:48:26",
    "2026-09-11T07:33:36",
)

PRIMARY_CLASS_PRIORITY = (
    "VERY_BIG_CLEAN_MOVE",
    "BIG_CLEAN_MOVE",
    "BIG_DIRTY_MOVE",
    "STOP_THEN_LATE_TARGET",
    "WRONG_WAY",
    "NO_EXPANSION",
    "LOW_FAVORABLE_EXPANSION",
    "QUALIFIED_MOVE",
    "NEUTRAL_UNCLASSIFIED",
)
