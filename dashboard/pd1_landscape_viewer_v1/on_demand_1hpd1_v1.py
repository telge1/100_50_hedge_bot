"""On-demand 1h PD1 landscape window (CH 1m → 1h engine, no frozen pool publications)."""

from __future__ import annotations

from typing import Any

from .on_demand_ch1m_pd1_v1 import (
    POOL_DETAIL,
    SCOPE_1H_PD1,
    OnDemandCh1mPd1Scope,
    build_landscape_window_on_demand_ch1m_pd1_v1,
    on_demand_ch1m_scope_for_timeframe,
)

ON_DEMAND_PIPELINE_LINE_ID = SCOPE_1H_PD1.pipeline_line_id
BUILDER_VERSION_V1 = SCOPE_1H_PD1.builder_version
LIFETIME_BUILDER_VERSION = SCOPE_1H_PD1.lifetime_builder_version
POOL_TIMEFRAME = SCOPE_1H_PD1.pool_timeframe
SLOT_MINUTES = SCOPE_1H_PD1.slot_minutes


def on_demand_1hpd1_cache_identity() -> dict[str, Any]:
    return SCOPE_1H_PD1.cache_identity()


def on_demand_1hpd1_capabilities_scope() -> dict[str, Any]:
    return SCOPE_1H_PD1.capabilities_scope()


def _active_members_at(raw_pools: list[dict[str, Any]], dc) -> list[dict[str, Any]]:
    from .on_demand_ch1m_pd1_v1 import _active_members_at as _active

    return _active(SCOPE_1H_PD1, raw_pools, dc)


def _load_engine_candles(vis_from, vis_to):
    from .on_demand_ch1m_pd1_v1 import _load_engine_candles as _load

    return _load(SCOPE_1H_PD1, vis_from, vis_to)


def _build_lifetimes_on_demand(grid, raw_pools, slot_minutes: int):
    from .on_demand_ch1m_pd1_v1 import _build_lifetimes_on_demand as _build

    assert int(slot_minutes) == SCOPE_1H_PD1.slot_minutes
    return _build(SCOPE_1H_PD1, grid, raw_pools)


def build_landscape_window_on_demand_1hpd1_v1(
    from_utc,
    to_utc,
    *,
    pool_detail: int = POOL_DETAIL,
    compute_mode: str = "exact_origin_v1",
) -> dict[str, Any]:
    return build_landscape_window_on_demand_ch1m_pd1_v1(
        SCOPE_1H_PD1,
        from_utc,
        to_utc,
        pool_detail=pool_detail,
        compute_mode=compute_mode,
    )


__all__ = [
    "ON_DEMAND_PIPELINE_LINE_ID",
    "BUILDER_VERSION_V1",
    "LIFETIME_BUILDER_VERSION",
    "POOL_DETAIL",
    "POOL_TIMEFRAME",
    "SLOT_MINUTES",
    "OnDemandCh1mPd1Scope",
    "SCOPE_1H_PD1",
    "on_demand_ch1m_scope_for_timeframe",
    "on_demand_1hpd1_cache_identity",
    "on_demand_1hpd1_capabilities_scope",
    "_active_members_at",
    "_load_engine_candles",
    "_build_lifetimes_on_demand",
    "build_landscape_window_on_demand_1hpd1_v1",
]
