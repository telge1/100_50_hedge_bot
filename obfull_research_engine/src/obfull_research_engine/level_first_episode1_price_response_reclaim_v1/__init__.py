"""Episode-1 causal price response + microprice reclaim components (no WALL_STATE).

Builds on frozen packages (semantically unmodified):
  - level_first_episode1_touch_detection_independent_v1
  - level_first_episode1_wall_flow_qdh_base_v1
  - level_first_episode1_detection_to_wall_flow_integration_v1

Verdict candidate (event-time only; no reclaim thresholds tuned on Episode-1 outcome):
  EPISODE1_PRICE_RESPONSE_MICROPRICE_RECLAIM_EVENT_TIME_PROVEN

Reclaim status is always RECLAIM_NOT_CALIBRATED.
Absorption remains ABSORPTION_NOT_CALIBRATED; impact efficiency remains IMPACT_EFFICIENCY_RAW.
"""

from __future__ import annotations

AUDIT_ID = "LEVEL_FIRST_EPISODE1_PRICE_RESPONSE_RECLAIM_V1"
SCHEMA_VERSION = "level_first_episode1_price_response_reclaim_v1"
RUN_PREFIX = "prr1_"
SYMBOL = "BTCUSDT"
TICK_SIZE = 0.1
EPSILON = 1e-12
BUCKET_MS = 100

SNAPSHOT_OFFSETS_S = (1, 3, 5, 10, 15, 30, 60, 120)
ANCHORS = ("WALL_FIRST_TOUCH", "DETECTION")

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

RECLAIM_STATUS = "RECLAIM_NOT_CALIBRATED"
IMPACT_EFFICIENCY_STATUS = "IMPACT_EFFICIENCY_RAW"
ABSORPTION_STATUS = "ABSORPTION_NOT_CALIBRATED"
TIME_BASIS = "EVENT_TIME_ONLY"
WALL_STATE = "NOT_CLASSIFIED"

VERDICT_OK = "EPISODE1_PRICE_RESPONSE_MICROPRICE_RECLAIM_EVENT_TIME_PROVEN"
VERDICT_BLOCKED = "EPISODE1_PRICE_RESPONSE_RECLAIM_BLOCKED"

ALLOW_CLICKHOUSE_WRITES = False
FLOAT_ABS_TOL = 1e-9
FLOAT_REL_TOL = 1e-9

# Decimal tolerance for oracle mid/micro comparisons (tick-scale float noise).
ORACLE_PRICE_ABS_TOL = 1e-6
ORACLE_TICK_ABS_TOL = 1e-6
ORACLE_MS_ABS_TOL = 0

__all__ = [
    "AUDIT_ID",
    "VERDICT_OK",
    "VERDICT_BLOCKED",
    "RECLAIM_STATUS",
    "TIME_BASIS",
]
