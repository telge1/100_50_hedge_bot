"""Shared short signal types (BT-D7/BT-D21)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ShortSignalV1:
    decision_close_utc: str
    order_active_from_utc: str
    pool_lower: float
    pool_upper: float
    pool_key: str
    entry_level: float
    gap_min_pct_used: float
    require_large_upper_gap: bool
    experimental_selection: str
    origin_ids: str | None = None


def pool_key(lo: float, hi: float) -> str:
    return f"{lo:.17g}|{hi:.17g}"
