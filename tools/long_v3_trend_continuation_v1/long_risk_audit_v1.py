"""Structural long SL risk audit (no R_max enforcement)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts_v1 import SL_BELOW_LOWER_POOL_PCT


def structural_long_sl(lower_edge: float) -> float:
    return lower_edge * (1.0 - SL_BELOW_LOWER_POOL_PCT / 100.0)


def risk_pct(actual_entry: float, sl: float) -> float | None:
    if actual_entry <= 0:
        return None
    return (actual_entry - sl) / actual_entry * 100.0


@dataclass(frozen=True)
class SlOverlapFindingV1:
    overlapping: bool
    conflicts: list[dict[str, Any]]


def sl_active_interval_overlaps(snapshot: dict[str, Any], sl: float) -> SlOverlapFindingV1:
    conflicts: list[dict[str, Any]] = []
    for key in ("lower_bands", "upper_bands"):
        for b in snapshot.get(key) or []:
            if str(b.get("pool_status") or "").upper() != "ACTIVE":
                continue
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            if lo <= sl <= hi:
                conflicts.append(
                    {
                        "side": b.get("side"),
                        "lower": lo,
                        "upper": hi,
                        "unique_origin_count": int(b.get("unique_origin_count") or 0),
                    }
                )
    return SlOverlapFindingV1(overlapping=bool(conflicts), conflicts=conflicts)
