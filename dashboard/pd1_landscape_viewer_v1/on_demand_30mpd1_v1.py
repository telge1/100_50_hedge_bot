"""On-demand 30m PD1 landscape window (CH 1m → strict 30m engine candles)."""

from __future__ import annotations

from typing import Any

from .on_demand_ch1m_pd1_v1 import (
    POOL_DETAIL,
    SCOPE_30M_PD1,
    build_landscape_window_on_demand_ch1m_pd1_v1,
)

ON_DEMAND_PIPELINE_LINE_ID = SCOPE_30M_PD1.pipeline_line_id
BUILDER_VERSION_V1 = SCOPE_30M_PD1.builder_version
LIFETIME_BUILDER_VERSION = SCOPE_30M_PD1.lifetime_builder_version
POOL_TIMEFRAME = SCOPE_30M_PD1.pool_timeframe
SLOT_MINUTES = SCOPE_30M_PD1.slot_minutes


def on_demand_30mpd1_cache_identity() -> dict[str, Any]:
    return SCOPE_30M_PD1.cache_identity()


def on_demand_30mpd1_capabilities_scope() -> dict[str, Any]:
    return SCOPE_30M_PD1.capabilities_scope()


def _active_members_at(raw_pools: list[dict[str, Any]], dc) -> list[dict[str, Any]]:
    from .on_demand_ch1m_pd1_v1 import _active_members_at as _active

    return _active(SCOPE_30M_PD1, raw_pools, dc)


def build_landscape_window_on_demand_30mpd1_v1(
    from_utc,
    to_utc,
    *,
    pool_detail: int = POOL_DETAIL,
    compute_mode: str = "exact_origin_v1",
) -> dict[str, Any]:
    return build_landscape_window_on_demand_ch1m_pd1_v1(
        SCOPE_30M_PD1,
        from_utc,
        to_utc,
        pool_detail=pool_detail,
        compute_mode=compute_mode,
    )
