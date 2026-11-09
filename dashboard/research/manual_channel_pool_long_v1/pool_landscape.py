"""
Upper ACTIVE pool landscape metrics for L1 V3 (5m PD1 SSOT — same source as existing gate).

Note: ACTIVE member rows expose micro band geometry (lower/upper_bound, strength) but not
canonical unique-origin D2 counts. Origin diversity is approximated by distinct pool_id tails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import (
    MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT,
    MIN_BANDS_PER_SUBSTANTIAL_ZONE,
    MIN_POOL_DIST_PCT,
    MIN_SUBSTANTIAL_UPPER_ZONES,
    MIN_UPPER_BAND_WIDTH_PCT,
    ZONE_MERGE_GAP_PCT,
)


@dataclass
class PoolLandscapeMetrics:
    snapshot_upper_active: int
    gate_qualified_bands: int
    merged_zones: int
    substantial_zones: int
    thin_zones: int
    nearest_substantial_dist_pct: float | None
    second_substantial_dist_pct: float | None
    third_substantial_dist_pct: float | None
    nearest_zone_band_count: int
    nearest_zone_max_width_pct: float | None
    max_gap_between_substantial_pct: float | None
    unique_origin_slots: int
    substantial_in_channel: int
    filter_pass: bool
    filter_reason: str | None
    summary: str


def _band_width_pct(m: dict) -> float:
    lo = float(m["lower_bound"])
    hi = float(m["upper_bound"])
    if lo <= 0:
        return 0.0
    return (hi - lo) / lo * 100.0


def _origin_slot(pool_id: str) -> str:
    return str(pool_id).rsplit(":", 1)[-1]


def analyze_upper_landscape(
    members: list[dict] | None,
    entry: float,
    *,
    channel_upper: float | None,
) -> PoolLandscapeMetrics:
    if members is None:
        return PoolLandscapeMetrics(
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            0,
            None,
            None,
            0,
            0,
            False,
            "UNRESOLVED_POOL_SNAPSHOT",
            "no SSOT snapshot",
        )

    uppers = [m for m in members if str(m.get("side", "")).lower() == "upper"]
    qualified = []
    for m in uppers:
        bottom = float(m.get("lower_bound", 0))
        if bottom <= entry:
            continue
        dist = (bottom - entry) / entry * 100.0
        if dist < MIN_POOL_DIST_PCT - 1e-12:
            continue
        qualified.append(m)
    qualified.sort(key=lambda m: float(m["lower_bound"]))

    zones: list[dict[str, Any]] = []
    for m in qualified:
        w = _band_width_pct(m)
        lo = float(m["lower_bound"])
        hi = float(m["upper_bound"])
        if not zones:
            zones.append({"lo": lo, "hi": hi, "bands": 1, "width_max": w, "members": [m]})
            continue
        z = zones[-1]
        gap_pct = (lo - z["hi"]) / entry * 100.0
        if gap_pct <= ZONE_MERGE_GAP_PCT:
            z["hi"] = max(z["hi"], hi)
            z["bands"] += 1
            z["width_max"] = max(z["width_max"], w)
            z["members"].append(m)
        else:
            zones.append({"lo": lo, "hi": hi, "bands": 1, "width_max": w, "members": [m]})

    substantial: list[dict[str, Any]] = []
    thin: list[dict[str, Any]] = []
    for z in zones:
        if z["width_max"] >= MIN_UPPER_BAND_WIDTH_PCT or z["bands"] >= MIN_BANDS_PER_SUBSTANTIAL_ZONE:
            substantial.append(z)
        else:
            thin.append(z)

    def zone_dist(z: dict) -> float:
        return (float(z["lo"]) - entry) / entry * 100.0

    substantial.sort(key=lambda z: z["lo"])
    gaps: list[float] = []
    for i in range(1, len(substantial)):
        gaps.append((substantial[i]["lo"] - substantial[i - 1]["hi"]) / entry * 100.0)

    origin_slots = {_origin_slot(str(m.get("pool_id", ""))) for m in qualified}

    substantial_in_channel = 0
    if channel_upper is not None and channel_upper > entry:
        for z in substantial:
            if float(z["lo"]) <= channel_upper:
                substantial_in_channel += 1

    n0 = substantial[0] if substantial else None
    reasons: list[str] = []
    if not substantial:
        reasons.append("NO_SUBSTANTIAL_UPPER_ZONE")
    else:
        d0 = zone_dist(n0)
        if d0 > MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT:
            reasons.append("FIRST_SUBSTANTIAL_TOO_FAR")
        if n0["bands"] < MIN_BANDS_PER_SUBSTANTIAL_ZONE:
            reasons.append("NEAREST_ZONE_LOW_BAND_DENSITY")
        if len(substantial) < MIN_SUBSTANTIAL_UPPER_ZONES:
            reasons.append("INSUFFICIENT_SUBSTANTIAL_ZONE_COUNT")

    filter_pass = not reasons
    filter_reason = ";".join(reasons) if reasons else None
    summary = (
        f"qual={len(qualified)} zones={len(zones)} subst={len(substantial)} "
        f"near_dist={zone_dist(n0):.4f}% near_bands={n0['bands'] if n0 else 0}"
        if n0
        else f"qual={len(qualified)} zones={len(zones)} subst=0"
    )

    return PoolLandscapeMetrics(
        snapshot_upper_active=len(uppers),
        gate_qualified_bands=len(qualified),
        merged_zones=len(zones),
        substantial_zones=len(substantial),
        thin_zones=len(thin),
        nearest_substantial_dist_pct=zone_dist(n0) if n0 else None,
        second_substantial_dist_pct=zone_dist(substantial[1]) if len(substantial) > 1 else None,
        third_substantial_dist_pct=zone_dist(substantial[2]) if len(substantial) > 2 else None,
        nearest_zone_band_count=int(n0["bands"]) if n0 else 0,
        nearest_zone_max_width_pct=float(n0["width_max"]) if n0 else None,
        max_gap_between_substantial_pct=max(gaps) if gaps else None,
        unique_origin_slots=len(origin_slots),
        substantial_in_channel=substantial_in_channel,
        filter_pass=filter_pass,
        filter_reason=filter_reason,
        summary=summary,
    )


def metrics_to_row_fields(m: PoolLandscapeMetrics, decision_close: str) -> dict[str, str]:
    def fnum(v: float | None, places: int = 4) -> str:
        return f"{v:.{places}f}" if v is not None else ""

    return {
        "pool_snapshot_close_utc": decision_close,
        "pool_active_upper_count": str(m.snapshot_upper_active),
        "pool_qualified_bands": str(m.gate_qualified_bands),
        "pool_merged_zones": str(m.merged_zones),
        "pool_substantial_zones": str(m.substantial_zones),
        "pool_thin_zones": str(m.thin_zones),
        "pool_nearest_substantial_dist_pct": fnum(m.nearest_substantial_dist_pct),
        "pool_nearest_zone_band_count": str(m.nearest_zone_band_count),
        "pool_max_gap_substantial_pct": fnum(m.max_gap_between_substantial_pct),
        "pool_unique_origin_slots": str(m.unique_origin_slots),
        "pool_substantial_in_channel": str(m.substantial_in_channel),
        "pool_landscape_pass": "yes" if m.filter_pass else "no",
        "pool_landscape_reason": m.filter_reason or "",
    }
