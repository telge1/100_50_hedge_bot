"""Short entry limit price from pool band + BT-D6 short_entry config."""

from __future__ import annotations

from typing import Any


def short_entry_offset_pct(short_entry: dict[str, Any]) -> float:
    raw = short_entry.get("entry_offset_pct")
    if raw is None:
        return 0.0
    return float(raw)


def compute_short_entry_level(pool_lo: float, pool_hi: float, short_entry: dict[str, Any]) -> float:
    ref = str(short_entry["entry_price_reference"])
    offset = short_entry_offset_pct(short_entry)
    if offset < 0:
        raise ValueError("entry_offset_pct must be non-negative")
    if ref == "upper_pool_lower_edge":
        return float(pool_lo) * (1.0 - offset / 100.0)
    if ref == "upper_pool_upper_edge":
        if offset != 0.0:
            raise ValueError("entry_offset_pct must be 0 for upper_pool_upper_edge")
        return float(pool_hi)
    raise ValueError(f"unsupported entry_price_reference: {ref}")
