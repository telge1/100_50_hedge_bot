"""Causality / repaint audit helpers and known_at contract."""

from __future__ import annotations

from typing import Any

from .constants import SIDE_MAP

CAUSALITY_AUDIT: dict[str, Any] = {
    "engine_causal": True,
    "pool_geometry_repaint": False,
    "strength_uses_future": False,
    "known_at_field": "available_at",
    "known_at_meaning": (
        "Closed confirmation bar end (= available_at). Swing geometry and strength "
        "come from source candle i-1. Pool is first usable only after confirmation "
        "TF bar is fully closed."
    ),
    "display_start_timestamp": "available_at (confirmation_bar_close)",
    "display_vs_known_at": (
        "Chart ZONE start_timestamp = available_at/known_at. source_timestamp remains "
        "in overlay metadata/tooltip only. Geometry unchanged."
    ),
    "amount_prune": (
        "Dashboard overlays use amount-capped `pools`; this study uses "
        "`pools_all` to avoid survivor bias from display prune."
    ),
    "live_forming_tip": (
        "Research charts may mutate the last open candle via apply_live_forming_tip; "
        "this study uses closed aggregated candles only."
    ),
    "same_pools_as_chart": (
        "Identical TRP run_liquidity_location engine; side lower→cyan BID, "
        "upper→magenta ASK; same top/bottom/strength/created/source timestamps."
    ),
    "cluster_causality": (
        "cluster_pools(..., as_of=T) admits only pools with created_timestamp<=T "
        "and not yet invalidated at T."
    ),
}


def engine_side_to_bid_ask(side: str) -> str:
    s = str(side).strip().lower()
    if s not in SIDE_MAP:
        raise ValueError(f"unknown LLD side {side!r}")
    return SIDE_MAP[s]


def pool_row_fields(pool: Any) -> dict[str, Any]:
    """Documented field contract for one LiquidityPool (chart-parity)."""
    from orderbook_analyse.liquidity_location_causal.availability import pool_time_fields

    side_ba = engine_side_to_bid_ask(pool.side)
    top = float(pool.top_price)
    bottom = float(pool.bottom_price)
    times = pool_time_fields(pool)
    available = times["available_at"]
    return {
        "pool_id": pool.pool_id,
        "symbol": pool.symbol,
        "source_timeframe": pool.timeframe,
        "engine_side": pool.side,
        "side": side_ba,
        "lower_price": bottom,
        "upper_price": top,
        "center_price": (top + bottom) / 2.0,
        "strength": None if pool.strength is None else float(pool.strength),
        "strong_pool": bool(getattr(pool, "strong_pool", False)),
        "source_count": 1,
        "component_pools": pool.pool_id,
        "created_at": pool.created_timestamp.isoformat(),
        "source_at": pool.source_timestamp.isoformat(),
        "known_at": available.isoformat(),
        "available_at": available.isoformat(),
        "confirmation_bar_start": times["confirmation_bar_start"].isoformat(),
        "confirmation_bar_end": times["confirmation_bar_end"].isoformat(),
        "first_available_at": available.isoformat(),
        "expires_at": None,
        "invalidated_at": (
            None
            if pool.invalidated_timestamp is None
            else pool.invalidated_timestamp.isoformat()
        ),
        "active_at_run_end": bool(pool.active),
        "created_index": int(pool.created_index),
        "source_index": int(pool.source_index),
        "source_high": pool.source_high,
        "source_low": pool.source_low,
        "source_volume": pool.source_volume,
        "half_size": (pool.metadata or {}).get("half_size"),
        "display_zone_start_at": available.isoformat(),
        "display_note": "chart_start=available_at; confirmation_open=created_timestamp",
    }
