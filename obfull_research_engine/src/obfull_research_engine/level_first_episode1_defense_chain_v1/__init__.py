"""Episode-1 Defense Chain mechanics (event-time, epoch-isolated).

Builds on EPISODE1_WITHIN_EPOCH_WALL_MIGRATION_STRUCTURE_PROVEN without
modifying frozen packages.

generation_count ≠ relevant_wall_count — short-lived churn is visible but
not automatically a chain node.

Statuses:
  WALL_RELEVANCE_NOT_OUTCOME_CALIBRATED
  DEFENSE_CHAIN_NOT_CALIBRATED
  chain_status = CENSORED_BY_EPOCH_BOUNDARY (Episode 1)
"""

from __future__ import annotations

AUDIT_ID = "LEVEL_FIRST_EPISODE1_DEFENSE_CHAIN_V1"
SCHEMA_VERSION = "level_first_episode1_defense_chain_v1"
RUN_PREFIX = "dch1_"

SYMBOL = "BTCUSDT"
TICK_SIZE = 0.1
ORIGINAL_WALL_PRICE = 79780.0
ZONE_LOW = 79774.75
ZONE_HIGH = 79775.25
ZONE_ID = "pc_7775a856ab22f006"
EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"

EPOCH4 = 4
EPOCH5 = 5
EPOCH4_COVERAGE_START = "2026-09-06T20:19:02.300Z"
EPOCH4_COVERAGE_END = "2026-09-06T20:19:59.800Z"
ASK_WALL_BREACH = "2026-09-06T20:19:41.900Z"
EPOCH5_CHECKPOINT = "2026-09-06T20:19:59.873Z"

# Research defaults for relevance at first visibility — NOT Episode-1 outcome-tuned.
RELEVANCE_PERCENTILE_FLOOR = 0.75
RELEVANCE_MEDIAN_RATIO_FLOOR = 2.0
RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR = 0.10
MAJOR_WALL_PERCENTILE = 0.95  # past-only Q95 view
MIN_BASELINE_SAMPLES = 5

WALL_RELEVANCE_STATUS = "WALL_RELEVANCE_NOT_OUTCOME_CALIBRATED"
CHAIN_CALIBRATION_STATUS = "DEFENSE_CHAIN_NOT_CALIBRATED"
CHAIN_STATUS_EP1 = "CENSORED_BY_EPOCH_BOUNDARY"
TIME_BASIS = "EVENT_TIME_ONLY"
WALL_STATE = "NOT_CLASSIFIED"

FROZEN_WALL_MIGRATION_RUN = (
    "results/level_first_episode1_wall_migration_structure_v1/BTCUSDT/wms1_fa0b8dd6daf1a"
)
FROZEN_HANDOFF_PATH = (
    "results/level_first_episode1_detection_to_wall_flow_integration_v1/"
    "BTCUSDT/d2w1_eee5d0eefaffa/episode1_handoff.json"
)
FROZEN_BOOK_DIR = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a"
)
FROZEN_WALL_FLOW_TIMELINE = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/"
    "BTCUSDT/wfq1_58db1918314881b6a/feature_timeline.csv"
)
FROZEN_TRADES = (
    "results/level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/"
    "episode1_independent_derivation_inputs_v1/public_trades_zone_window.jsonl"
)

VERDICT_PROVEN = "EPISODE1_DEFENSE_CHAIN_EVENT_TIME_PROVEN"
VERDICT_MECHANICS_NO_COMPLETE = "EPISODE1_DEFENSE_CHAIN_MECHANICS_PROVEN_NO_COMPLETE_CHAIN"

ALLOW_CLICKHOUSE_WRITES = False
EPSILON = 1e-12

TRANSITION_NEXT_PRE_EXISTING = "NEXT_LAYER_PRE_EXISTING"
TRANSITION_NEXT_NEW = "NEXT_LAYER_NEWLY_APPEARED"
TRANSITION_SAME_PRICE_NEW_GEN = "SAME_PRICE_NEW_GENERATION"
TRANSITION_NO_NEXT = "NO_NEXT_RELEVANT_LAYER"
TRANSITION_CENSORED = "CENSORED_BEFORE_RESOLUTION"

RELEVANCE_CONTRACT = {
    "status": WALL_RELEVANCE_STATUS,
    "percentile_floor": RELEVANCE_PERCENTILE_FLOOR,
    "median_ratio_floor": RELEVANCE_MEDIAN_RATIO_FLOOR,
    "local_depth_share_floor": RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR,
    "major_wall_percentile_past_only": MAJOR_WALL_PERCENTILE,
    "baseline": "past_only_at_first_visible_at",
    "note": (
        "Relevance at entry uses exclusively previously observed wall initial sizes / "
        "band depth. Future peak qty must not rewrite earlier relevance. "
        "Not calibrated on Episode-1 reclaim or signal outcome."
    ),
}

__all__ = [
    "VERDICT_PROVEN",
    "VERDICT_MECHANICS_NO_COMPLETE",
    "RELEVANCE_CONTRACT",
    "CHAIN_STATUS_EP1",
]
