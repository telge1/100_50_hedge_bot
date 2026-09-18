"""Canonical MP → QDH integration (reuse exact wall-flow / QDH engine).

Does NOT reimplement QDH. Enrichment HIT/PULL remains LEGACY_ZONE_PROXY only.
"""

from __future__ import annotations

PACKAGE_NAME = "mp_qdh_canonical_integration_v1"
AUDIT_ID = "MP_QDH_CANONICAL_INTEGRATION_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "mp_qdh_canonical_integration_v1"
RUN_PREFIX = "mqci1_"

TICK_SIZE = 0.1
BAND_TICKS = 5  # canonical QDH band (level_first_episode1 defaults)
BASELINE_S = 120.0
FORENSIC_TAIL_S = 300.0
WARMUP_S = 300.0

# Engine epsilon for QUEUE_EXHAUSTED (from QDH package EPSILON)
QUEUE_EXHAUSTED_EPS = 1e-12
# Uncalibrated warning only — not a trading threshold
NEAR_ZERO_QUEUE_WARN_QTY = 0.01

LEGACY_PROXY_TAG = "LEGACY_ZONE_PROXY"
CANONICAL_PREFIX = "canonical_qdh_"

SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL = "BTCUSDT"
ALLOW_CLICKHOUSE_WRITES = False

BATCH_RUN_REL = "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"
ENRICHMENT_FEATURES_PARQUET = (
    "obfull_research_engine/runs/mp_ob_feature_enrichment_v1_20260916/event_features.parquet"
)
CHART_PAIRS_CSV = (
    "obfull_research_engine/runs/mp_big_move_case_control_v1_20260916/selected_chart_pairs.csv"
)

QDH_SOURCE_MODULES = {
    "attribution": "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution",
    "mass_balance": "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance",
    "qdh": "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard",
    "aggressor": "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow",
    "timeline": "obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.timeline_100ms",
    "engine": "obfull_research_engine.ob_forschungsengine_v1.qdh_engine",
}
