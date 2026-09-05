"""Phase-3 audit: decision/label separation, future-only OOS, raw OB inventory."""

from __future__ import annotations

AUDIT_ID = "LIQUIDITY_LOCATION_R6_PHASE3_AUDIT"
VERDICT_COMPLETE = "LIQUIDITY_LOCATION_R6_PHASE3_AUDIT_COMPLETE"
VERDICT_LEAKAGE = "LIQUIDITY_LOCATION_R6_PHASE3_LEAKAGE_FOUND"
VERDICT_LOADER_BROKEN = "LIQUIDITY_LOCATION_R6_RAW_OB_LOADER_PATH_BROKEN"

PHASE3_DIR_DEFAULT = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_r6_orderflow_confirmation_v1"
)
OUT_DIR_DEFAULT = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_r6_phase3_audit"
)
SHADOW_ARCHIVE = (
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)
LIVE_ARCHIVE_DEFAULT = (
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_live/ob200_v3"
)
PRIMARY_T3_SEC = 30
