"""Bridge: MP/Silver L2 + Public Trades → Episode-1 wall-flow / QDH (reuse).

Combines the new ClickHouse Silver v1.3 path with the proven
``level_first_episode1_wall_flow_qdh_base_v1`` attribution / QDH / aggressor
modules. Read-only. No CH writes. No Signal V2 thresholds.
"""

from __future__ import annotations

AUDIT_ID = "MP_WALL_FLOW_QDH_SILVER_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "mp_wall_flow_qdh_silver_v1"
RUN_PREFIX = "mwqs1_"

SYMBOL = "BTCUSDT"
SILVER_DATABASE = "research_full_ob_silver_v1_3"
ALLOW_CLICKHOUSE_WRITES = False

# Frozen Episode-1 contract (same IDs as wall_flow_qdh_base_v1).
EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"
ZONE_ID = "pc_7775a856ab22f006"
LEVEL_ID = "lvl:CLOSED:30m:TPO_VAL:1788723000"
WALL_ID = "w_781b6ed696e777e1"
WALL_SIDE = "ask"
WALL_PRICE = 79780.0
TICK_SIZE = 0.1
BAND_TICKS_DEFAULT = 5

ZONE_AVAILABLE_UTC = "2026-09-06T20:00:00Z"
ZONE_TOUCH_UTC = "2026-09-06T20:19:02.229Z"
WALL_TOUCH_UTC = "2026-09-06T20:19:02.232Z"
DETECTION_UTC = "2026-09-06T20:21:00Z"
# Slight warm-up so first LC old_size seeds the queue before zone available.
COVERAGE_START_UTC = "2026-09-06T19:55:00Z"
ANALYSIS_END_UTC = DETECTION_UTC

BTC_CHAIN_VERSION = (
    "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
)

VERDICT_EVENT_TIME = "MP_SILVER_EPISODE1_WALL_FLOW_QDH_BASE_EVENT_TIME_PROVEN"
VERDICT_BLOCKED = "STOP_MP_SILVER_QDH_BRIDGE"

REFERENCE_WFQ1_SUMMARY = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/"
    "wfq1_58db1918314881b6a/wall_flow_summary.json"
)

__all__ = [
    "AUDIT_ID",
    "VERDICT_EVENT_TIME",
    "ALLOW_CLICKHOUSE_WRITES",
    "SILVER_DATABASE",
]
