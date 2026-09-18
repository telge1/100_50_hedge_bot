"""mp_qdh_first_touch_study_v1 — tradeable first-touch QDH study with % MFE/MAE."""

from __future__ import annotations

PACKAGE_NAME = "mp_qdh_first_touch_study_v1"
STUDY_ID = "MP_QDH_FIRST_TOUCH_STUDY_V1"
SCHEMA_VERSION = "mp_qdh_first_touch_study_v1"
CONTRACT_VERSION = "1.0.0"

ALLOW_CLICKHOUSE_WRITES = False
SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL = "BTCUSDT"

BATCH_RUN_REL = "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"
PRICE_PATH_RUN_REL = "obfull_research_engine/runs/mp_price_path_4h_v1_20260916"

TARGET_REACH_PCT = 0.41
REACH_THRESHOLDS_PCT = (0.10, 0.25, 0.41, 0.50, 0.75, 1.00)
HORIZONS_MIN = (5, 15, 30, 60, 120, 240)
TP_SL_HORIZONS_MIN = (30, 60, 120, 240)
SL_STOPS_PCT = (0.10, 0.15, 0.20, 0.25, 0.30, 0.41)
COST_ROUNDTRIP_PCT = (0.08, 0.12)

ABSORPTION_RATIO_STATUS = "NOT_CALIBRATED"
VACUUM_SCORE_STATUS = "NOT_CALIBRATED"

# Prior V2 contract version string for stale rejection tests
LEGACY_V2_CONTRACT_VERSION = "2.0.0"
