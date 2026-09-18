"""Analysis contract + feature gates aligned with OB-Forschungsengine.md."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PACKAGE_NAME = "ob_forschungsengine_v1"
AUDIT_ID = "OB_FORSCHUNGSENGINE_V1"
ANALYSIS_CONTRACT_VERSION = "1.0.0"
RUN_PREFIX = "obfe1_"
SCHEMA_VERSION = "ob_forschungsengine_v1"

SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL_DEFAULT = "BTCUSDT"
ALLOW_CLICKHOUSE_WRITES = False

# Plan: first prove L2 + public trades alone.
M_OI_FIXED = 1.0
M_LIQ_FIXED = 1.0

WALL_STATE_NOT_CLASSIFIED = "NOT_CLASSIFIED"
SIGNAL_DISABLED = "SIGNAL_V2_DISABLED"
FLOW_TYPE_NOT_CLASSIFIED = "NOT_CLASSIFIED"
LEP_DISABLED = "LEP_DISABLED"

VERDICT_EVENT_OK = "OB_FORSCHUNGSENGINE_EVENT_QDH_BASE_OK"
VERDICT_BLOCKED = "STOP_OB_FORSCHUNGSENGINE"


@dataclass(frozen=True)
class FeatureGates:
    """Which plan layers are active. Defaults match Episode-1 QDH-base proof."""

    # Proven / required for base analysis
    event_timeline: bool = True
    wall_flow_attribution: bool = True
    qdh_base: bool = True
    aggressor_persistence: bool = True
    price_response: bool = True
    mp_hit_pull_enrichment: bool = True

    # Plan step 5 — skeleton only until wired without double-counting
    footprint_cluster: bool = False

    # Plan steps 10–11 — shadow only; must not change base
    oi_shadow: bool = False
    liquidation_shadow: bool = False
    flow_type_lep: bool = False

    # Plan steps 14–15 — after historical scaling + ablation
    wall_state_classifier: bool = False
    signal_v2: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def active_modules(self) -> list[str]:
        return [k for k, v in self.to_dict().items() if v]

    def deferred_modules(self) -> list[str]:
        return [k for k, v in self.to_dict().items() if not v]


def default_gates() -> FeatureGates:
    """Gates for the current research stage (post Episode-1 QDH-base proof)."""
    return FeatureGates()


# Map plan module names → implementation location (reuse, not rewrite).
MODULE_REGISTRY: dict[str, dict[str, str]] = {
    "wall_flow_attribution": {
        "status": "ACTIVE",
        "impl": "level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution",
    },
    "aggressor_flow": {
        "status": "ACTIVE",
        "impl": "level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow",
    },
    "queue_depletion_hazard": {
        "status": "ACTIVE",
        "impl": "level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard",
    },
    "price_response": {
        "status": "ACTIVE",
        "impl": "level_first_episode1_wall_flow_qdh_base_v1.price_response",
    },
    "timeline_100ms": {
        "status": "ACTIVE",
        "impl": "level_first_episode1_wall_flow_qdh_base_v1.timeline_100ms",
    },
    "silver_loaders": {
        "status": "ACTIVE",
        "impl": "mp_ob_feature_enrichment_v1.loaders",
    },
    "mp_hit_pull_enrichment": {
        "status": "ACTIVE",
        "impl": "mp_ob_feature_enrichment_v1.features",
    },
    "footprint_cluster": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.footprint_cluster",
    },
    "oi_context": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.oi_context",
    },
    "liquidation_flow": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.liquidation_flow",
    },
    "wall_state_classifier": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.wall_state",
    },
    "signal_v2": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.signal_v2",
    },
    "flow_type_enrichment": {
        "status": "STUB",
        "impl": "ob_forschungsengine_v1.flow_type",
    },
}
