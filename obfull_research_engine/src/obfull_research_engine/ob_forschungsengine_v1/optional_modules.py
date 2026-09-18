"""Optional plan modules — gated stubs (must not change QDH_base)."""

from __future__ import annotations

from typing import Any

from .contract import (
    FLOW_TYPE_NOT_CLASSIFIED,
    LEP_DISABLED,
    M_LIQ_FIXED,
    M_OI_FIXED,
    SIGNAL_DISABLED,
    WALL_STATE_NOT_CLASSIFIED,
)


def footprint_cluster_shadow(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "ACTIVE" if enabled else "DISABLED",
        "footprint_cluster_id": None,
        "unique_trade_count": None,
        "trade_ids_hash": None,
        "note": "Deferred until same canonical trade set as QDH hits is proven",
    }


def oi_shadow(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "SHADOW" if enabled else "DISABLED",
        "M_OI": M_OI_FIXED,
        "oi_trigger_eligible": False,
        "oi_source": None,
        "oi_available_at": None,
        "oi_source_age_ms": None,
        "oi_innovation": None,
        "note": "Plan: M_OI=1 until OOS lift proven",
    }


def liquidation_shadow(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "SHADOW" if enabled else "DISABLED",
        "M_Liq": M_LIQ_FIXED,
        "forced_flow_share": None,
        "liquidation_acceleration": None,
        "LEP_state": LEP_DISABLED,
        "note": "Plan: M_Liq=1; Liq must not add to aggressive volume",
    }


def wall_state_classify(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
        "note": "Classifier off until historical scaling + ablation",
    }


def signal_v2(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "signal": SIGNAL_DISABLED,
        "side": None,
        "note": "Signal V2 after WallState calibration (plan step 15)",
    }


def flow_type_enrichment(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "flow_type": FLOW_TYPE_NOT_CLASSIFIED,
        "note": "Flow-type / LEP after OI+Liq shadow layer",
    }
