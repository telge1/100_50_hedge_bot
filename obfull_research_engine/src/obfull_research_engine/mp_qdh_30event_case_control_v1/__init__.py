"""Canonical 30-event (15 winner + 15 WRONG_WAY control) case-control comparison.

Reuses:
  - mp_qdh_wall_linkage_audit_v1.audit_one.audit_one_event
  - mp_qdh_wall_linkage_audit_v1.wall_track / trade_funnel / flow_series
  - level_first_episode1_wall_flow_qdh_base_v1 (attribute_intervals, mass_balance, update_qdh)
  - mp_big_move_case_control_v1 outcome classes (frozen; not retuned)

Does NOT reimplement QDH, widen ±5-tick zone, or mix legacy HIT/PULL.
"""

from __future__ import annotations

PACKAGE_NAME = "mp_qdh_30event_case_control_v1"
STUDY_ID = "MP_QDH_30EVENT_CASE_CONTROL_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "mp_qdh_30event_case_control_v1"

TICK_SIZE = 0.1
BAND_TICKS = 5
# Distance stability for WALL_FOLLOWS_PRICE: ≤ 1 tick change in |wall−mid| distance.
DISTANCE_STABLE_TICKS = 1.0
# Nearby reappearance for WALL_REAPPEARS_NEARBY: within band_ticks of prior price.
NEARBY_REAPPEAR_TICKS = BAND_TICKS

PRE_TOUCH_S = 120.0
FORENSIC_TAIL_S = 300.0
CAUSAL_SNAPSHOT_S = (5, 15, 30, 60, 120)

ALLOW_CLICKHOUSE_WRITES = False
SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL = "BTCUSDT"

OUTCOME_PARQUET_REL = (
    "obfull_research_engine/runs/mp_big_move_case_control_v1_20260916/event_outcome_classes.parquet"
)
OUTCOME_CONTRACT_REL = (
    "obfull_research_engine/runs/mp_big_move_case_control_v1_20260916/frozen_outcome_class_contract.json"
)
BATCH_RUN_REL = "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"
AUDIT_REPORT_REL = (
    "obfull_research_engine/runs/mp_qdh_wall_linkage_audit_v1_20260917/WALL_LINKAGE_AUDIT_REPORT.md"
)

WINNER_CLASSES = ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE")
CONTROL_CLASS = "WRONG_WAY"
ENTRY_MODE_FIRST_TOUCH = "ORIGINAL"
EXPECTED_N_WINNERS = 15
EXPECTED_N_CONTROLS = 15

# Phase-0 documentation (file:function — not guesses)
PHASE0_CONTRACT = {
    "winner_source": {
        "file": OUTCOME_PARQUET_REL,
        "filter": "entry_mode==ORIGINAL AND primary_outcome_class in BIG_CLEAN_MOVE|VERY_BIG_CLEAN_MOVE",
        "expected_n": 15,
        "note": "Report BIG_MOVE_COMPARISON: first_touch BIG_CLEAN=3 + VERY_BIG_CLEAN=12.",
    },
    "outcome_definitions": {
        "module": "mp_big_move_case_control_v1.outcome_classes.classify_outcome",
        "contract": OUTCOME_CONTRACT_REL,
        "BIG_CLEAN_MOVE": "TARGET_FIRST (0.41%) AND mfe_4h >= 0.6%",
        "VERY_BIG_CLEAN_MOVE": "TARGET_FIRST AND mfe_4h >= 0.75% (priority over BIG_CLEAN)",
        "WRONG_WAY": "STOP_FIRST (0.15%) AND target never reached",
        "target_pct": 0.41,
        "stop_pct": 0.15,
    },
    "touch_time": {
        "field": "events_all.csv first_touch_ts_ns",
        "loader": "mp_qdh_canonical_integration_v1.event_load.load_events_csv",
    },
    "decision_cutoff": {
        "field": "events_all.csv trigger_ts_ns (MP trigger / reference entry)",
        "note": "Same causal cutoff as wall-linkage audit TOUCH_TO_TRIGGER end.",
    },
    "wall_linkage": "mp_qdh_wall_linkage_audit_v1.wall_track.track_pre_touch_walls",
    "attribution": "level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution.attribute_intervals",
    "qdh": "level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard.update_qdh",
    "audit_runner": "mp_qdh_wall_linkage_audit_v1.audit_one.audit_one_event",
    "side_rule": "UPPER→ask / LOWER→bid via build_zone_bands; TRUE_BREAK must not flip",
    "matching": {
        "hard": ["label_price_only", "event_role", "trade_side"],
        "soft": ["confluence_class", "atr14_5m_pct bucket", "session_utc", "utc_hour proximity", "time proximity"],
        "forbidden": ["mfe/mae", "QDH/fill/pull/refill", "wall movement", "microprice response", "post-trigger"],
    },
}
