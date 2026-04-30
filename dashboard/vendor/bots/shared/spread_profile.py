"""
Shared spread profile helpers for hedge bots.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_rebuy_profile(
    profile: Any,
    base_hedge_ratio: float,
    cycle_index: int,
    spread_pct: Optional[float] = None,
    zones: Optional[Dict[str, Any]] = None,
) -> tuple[float, float, Optional[str], Optional[int]]:
    """
    Resolve effective rebuy_factor / hedge_ratio from cycle profile plus spread zone.

    Strategy:
    - Select the cycle-based profile entry first.
    - If a spread zone is active, enforce a minimum defensive profile:
      low  -> first profile bucket
      mid  -> middle/defensive bucket
      high -> last profile bucket
    - The more defensive of (cycle bucket, zone bucket) wins.
    """
    try:
        default_hr = float(base_hedge_ratio or 0.5)
    except Exception:
        default_hr = 0.5
    if default_hr <= 0 or default_hr > 1.0:
        default_hr = 0.5

    entries: list[dict[str, float | int]] = []
    if isinstance(profile, list):
        for raw in profile:
            if not isinstance(raw, dict):
                continue
            try:
                max_cycle = int(raw.get("max_cycle", 0) or 0)
            except Exception:
                continue
            if max_cycle <= 0:
                continue
            try:
                rebuy_factor = float(raw.get("rebuy_factor", 1.0) or 1.0)
            except Exception:
                rebuy_factor = 1.0
            try:
                hedge_ratio = float(raw.get("hedge_ratio", default_hr) or default_hr)
            except Exception:
                hedge_ratio = default_hr
            if rebuy_factor <= 0:
                rebuy_factor = 1.0
            if hedge_ratio <= 0 or hedge_ratio > 1.0:
                hedge_ratio = default_hr
            entries.append(
                {
                    "max_cycle": max_cycle,
                    "rebuy_factor": rebuy_factor,
                    "hedge_ratio": hedge_ratio,
                }
            )

    if not entries:
        return 1.0, default_hr, None, None

    cycle_idx = len(entries) - 1
    for idx, entry in enumerate(entries):
        cycle_idx = idx
        if cycle_index <= int(entry["max_cycle"]):
            break

    zone = None
    zone_idx = None
    if spread_pct is not None and isinstance(zones, dict) and zones:
        low = zones.get("low") or {}
        mid = zones.get("mid") or {}
        high = zones.get("high") or {}
        try:
            if low and spread_pct < float(low.get("max_spread_pct", 0.0) or 0.0):
                zone = "low"
                zone_idx = 0
            elif mid and float(mid.get("min_spread_pct", 0.0) or 0.0) <= spread_pct <= float(mid.get("max_spread_pct", 0.0) or 0.0):
                zone = "mid"
                zone_idx = min(len(entries) - 1, max(1, len(entries) // 2))
            elif high and spread_pct >= float(high.get("min_spread_pct", 0.0) or 0.0):
                zone = "high"
                zone_idx = len(entries) - 1
        except Exception:
            zone = None
            zone_idx = None

    selected_idx = cycle_idx if zone_idx is None else max(cycle_idx, zone_idx)
    selected = entries[selected_idx]
    return (
        float(selected["rebuy_factor"]),
        float(selected["hedge_ratio"]),
        zone,
        selected_idx,
    )
