"""Stage B helpers: stacked zones, approach side, next-zone clearance."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    NEXT_ZONE_CLEARANCE_ATR_MULT,
    NEXT_ZONE_CLEARANCE_PCT_HI,
    NEXT_ZONE_CLEARANCE_PCT_LO,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING, TICK
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    EmaZone,
    distance_to_zone,
    make_zone,
    zones_overlap,
)


def build_zones(
    *,
    ema20: float | None,
    ema59: float | None,
    ema200: float | None,
    atr: float | None,
) -> dict[str, EmaZone | None]:
    if atr is None or atr <= 0:
        return {"EMA20": None, "EMA59": None, "EMA200": None}
    return {
        "EMA20": make_zone("EMA20", ema20, atr) if ema20 is not None else None,
        "EMA59": make_zone("EMA59", ema59, atr) if ema59 is not None else None,
        "EMA200": make_zone("EMA200", ema200, atr) if ema200 is not None else None,
    }


def stacked_zone_label(zones: dict[str, EmaZone | None]) -> str | None:
    """If overlapping EMAs form one STACKED_EMA_ZONE, return label; else None."""
    present = [z for z in (zones.get("EMA20"), zones.get("EMA59"), zones.get("EMA200")) if z]
    if len(present) < 2:
        return None
    # pairwise overlap chain
    stacked_names: list[str] = []
    for i, a in enumerate(present):
        for b in present[i + 1 :]:
            if zones_overlap(a, b):
                for n in (a.name, b.name):
                    if n not in stacked_names:
                        stacked_names.append(n)
    if len(stacked_names) >= 2:
        return "STACKED_EMA_ZONE:" + "+".join(stacked_names)
    return None


def approach_side(mid_before: float | None, zone: EmaZone | None) -> str:
    if mid_before is None or zone is None:
        return "unknown"
    if mid_before > zone.high:
        return "from_above"
    if mid_before < zone.low:
        return "from_below"
    return "inside"


def next_zone_clearance(
    price: float,
    primary: EmaZone,
    candidates: list[EmaZone],
) -> dict[str, Any]:
    """Clearance to nearest stronger zone; may emit WAIT_NEXT_ZONE_CONFIRMATION."""
    others = [z for z in candidates if z.name != primary.name]
    if not others:
        return {
            "nearest_stronger_zone": MISSING,
            "clearance_pct": MISSING,
            "clearance_ticks": MISSING,
            "clearance_atr": MISSING,
            "wait_next_zone": False,
            "reason_code": "",
        }
    # "stronger" = slower EMA (higher period)
    strength = {"EMA20": 1, "EMA59": 2, "EMA200": 3}
    stronger = [z for z in others if strength.get(z.name, 0) > strength.get(primary.name, 0)]
    pool = stronger or others
    best = min(pool, key=lambda z: abs(z.center - price))
    # distance between zone edges toward the stronger center
    if best.center >= primary.center:
        gap = best.low - primary.high
    else:
        gap = primary.low - best.high
    gap = max(0.0, gap)
    mid = (primary.center + best.center) / 2.0
    pct = (gap / mid) * 100.0 if mid else 0.0
    ticks = gap / TICK if TICK else 0.0
    atr_mult = gap / primary.atr if primary.atr else 0.0
    wait = (
        NEXT_ZONE_CLEARANCE_PCT_LO <= pct <= NEXT_ZONE_CLEARANCE_PCT_HI
        or (0 < atr_mult <= NEXT_ZONE_CLEARANCE_ATR_MULT and pct <= NEXT_ZONE_CLEARANCE_PCT_HI)
    )
    return {
        "nearest_stronger_zone": best.name,
        "clearance_pct": pct,
        "clearance_ticks": ticks,
        "clearance_atr": atr_mult,
        "wait_next_zone": wait,
        "reason_code": "WAIT_NEXT_ZONE_CONFIRMATION" if wait else "",
    }


def zone_kind_label(
    *,
    primary_name: str,
    stacked: str | None,
    wall_confluence: bool,
    swing_confluence: bool,
) -> str:
    if stacked:
        return stacked  # single STACKED event — no separate multi-EMA events
    if wall_confluence and swing_confluence:
        return "EMA_WALL_CONFLUENCE+EMA_SWING_CONFLUENCE"
    if wall_confluence:
        return "EMA_WALL_CONFLUENCE"
    if swing_confluence:
        return "EMA_SWING_CONFLUENCE"
    return f"{primary_name}_ZONE"


def zone_feature_row(
    *,
    window_id: str,
    zones: dict[str, EmaZone | None],
    mid: float | None,
    mid_before: float | None,
    primary_name: str,
    wall_confluence: bool,
    swing_confluence: bool,
    zone_watch_started_at: str | None,
    zone_touch_at: str | None,
) -> dict[str, Any]:
    stacked = stacked_zone_label(zones)
    primary = zones.get(primary_name)
    kind = zone_kind_label(
        primary_name=primary_name,
        stacked=stacked,
        wall_confluence=wall_confluence,
        swing_confluence=swing_confluence,
    )
    row: dict[str, Any] = {
        "window_id": window_id,
        "zone_kind": kind,
        "primary_zone": primary_name,
        "stacked_ema_zone": stacked or "",
        "approach_side": approach_side(mid_before, primary),
        "zone_watch_started_at": zone_watch_started_at or MISSING,
        "zone_touch_at": zone_touch_at or MISSING,
        "affected_emas": stacked.split(":", 1)[-1] if stacked else primary_name,
    }
    for name, z in zones.items():
        prefix = name.lower()
        if z is None:
            row[f"{prefix}_center"] = MISSING
            row[f"{prefix}_low"] = MISSING
            row[f"{prefix}_high"] = MISSING
            row[f"{prefix}_half_width"] = MISSING
            continue
        row[f"{prefix}_center"] = z.center
        row[f"{prefix}_low"] = z.low
        row[f"{prefix}_high"] = z.high
        row[f"{prefix}_half_width"] = z.half_width
        if mid is not None:
            d = distance_to_zone(mid, z)
            row.update({f"{prefix}_{k}": v for k, v in d.items()})
    if primary and mid is not None:
        cands = [z for z in zones.values() if z is not None]
        row.update(next_zone_clearance(mid, primary, cands))
    else:
        row.update(
            {
                "nearest_stronger_zone": MISSING,
                "clearance_pct": MISSING,
                "clearance_ticks": MISSING,
                "clearance_atr": MISSING,
                "wait_next_zone": False,
                "reason_code": "",
            }
        )
    return row
