"""R6 causal feature repair + raw OB200 archive diagnosis (v2)."""

from __future__ import annotations

AUDIT_ID = "LIQUIDITY_LOCATION_R6_CAUSAL_AND_RAW_AUDIT_V2"
VERDICT_ROOT_CAUSE = "R6_CAUSAL_FEATURES_FIXED_RAW_OB_ROOT_CAUSE_IDENTIFIED"
VERDICT_COLLECTOR_BUG = "R6_CAUSAL_FEATURES_FIXED_RAW_OB_COLLECTOR_BUG_CONFIRMED"

PHASE3_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_r6_orderflow_confirmation_v1"
)
AUDIT_V1_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_r6_phase3_audit"
)
OUT_DIR_DEFAULT = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_r6_causal_and_raw_audit_v2"
)
SHADOW_ARCHIVE = (
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)
LIVE_ARCHIVE = (
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_live/ob200_v3"
)

PHASE3_CLAIMS_STATUS = {
    "R6_near_edge_reclaim": {
        "causal_valid": False,
        "reason": "1m close used while close_known_at typically T2+60s > T3=T2+30s",
        "oos_precision_may_not_be_used_as_edge": True,
        "future_only_eval_of_leaked_flags_invalid": True,
    },
    "R6_absorption": {
        "causal_valid": False,
        "reason": "price_continuation candles extend to T2+65s > T3",
        "oos_precision_may_not_be_used_as_edge": True,
        "future_only_eval_of_leaked_flags_invalid": True,
    },
    "phase3_oos_precision_37_38pct": {
        "causal_valid": False,
        "reason": "built on leaked near_edge_reclaim / absorption flags",
        "may_not_be_used_as_confirmed_edge": True,
    },
}

DECISION_VARIANTS = ("SUBMINUTE_30S", "CLOSED_1M", "CLOSED_3M")
