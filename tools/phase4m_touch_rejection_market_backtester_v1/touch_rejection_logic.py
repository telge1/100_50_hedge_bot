"""Touch → rejection confirmation rules (Phase 4M, user-confirmed)."""

from __future__ import annotations

from typing import Any


def short_pool_touch(bar: dict[str, float], pool_lower: float) -> bool:
    return bar["h"] >= pool_lower


def short_rejection_confirmed(bar: dict[str, float], pool_lower: float) -> bool:
    """Close strictly below upper-pool lower edge."""
    return bar["c"] < pool_lower


def long_pool_touch(bar: dict[str, float], pool_upper: float) -> bool:
    return bar["l"] <= pool_upper


def long_rejection_confirmed(bar: dict[str, float], pool_upper: float) -> bool:
    return bar["c"] > pool_upper


def invalid_short_entry_geometry(open_price: float, sl_price: float, tp_price: float | None) -> str | None:
    if open_price >= sl_price:
        return "INVALID_ENTRY_GEOMETRY_OPEN_AT_OR_ABOVE_SL"
    if tp_price is None:
        return "INVALID_ENTRY_GEOMETRY_TP_UNRESOLVED"
    return None


def invalid_long_entry_geometry(open_price: float, sl_price: float, tp_price: float | None) -> str | None:
    if open_price <= sl_price:
        return "INVALID_ENTRY_GEOMETRY_OPEN_AT_OR_BELOW_SL"
    if tp_price is None:
        return "INVALID_ENTRY_GEOMETRY_TP_UNRESOLVED"
    return None
