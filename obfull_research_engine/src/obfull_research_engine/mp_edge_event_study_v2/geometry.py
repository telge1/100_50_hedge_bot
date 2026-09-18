"""Geometry helpers (copied semantics from V1; V2-local to avoid coupling)."""

from __future__ import annotations

from obfull_research_engine.mp_edge_event_study_v1.util import (
    bps_distance,
    bps_signed,
    price_offset_bps,
)


def distance_to_zone_bps(mid: float, lo: float, hi: float) -> float:
    if lo <= mid <= hi:
        return 0.0
    if mid < lo:
        return bps_distance(mid, lo)
    return bps_distance(mid, hi)


def in_touch_band(mid: float, lo: float, hi: float, touch_tol_bps: float) -> bool:
    return distance_to_zone_bps(mid, lo, hi) <= float(touch_tol_bps)


def is_beyond(mid: float, role: str, lo: float, hi: float) -> bool:
    if role == "UPPER":
        return mid > hi
    return mid < lo


def is_fully_reclaimed(mid: float, role: str, lo: float, hi: float, reclaim_tol_bps: float) -> bool:
    if role == "UPPER":
        limit = lo + price_offset_bps(lo, reclaim_tol_bps)
        return mid < limit if reclaim_tol_bps > 0 else mid < lo
    limit = hi - price_offset_bps(hi, reclaim_tol_bps)
    return mid > limit if reclaim_tol_bps > 0 else mid > hi


def penetration_bps(mid: float, role: str, lo: float, hi: float) -> float:
    if role == "UPPER":
        if mid <= hi:
            return 0.0
        return bps_signed(mid - hi, hi)
    if mid >= lo:
        return 0.0
    return bps_signed(lo - mid, lo)


def fade_distance_bps(mid: float, role: str, lo: float, hi: float) -> float:
    if role == "UPPER":
        if mid >= lo:
            return 0.0
        return bps_signed(lo - mid, lo)
    if mid <= hi:
        return 0.0
    return bps_signed(mid - hi, hi)


def approach_origin_ok(mid: float, role: str, lo: float, hi: float, origin_bps: float) -> bool:
    """Price on the correct approach side by at least origin_bps."""
    if role == "UPPER":
        # below zone
        if mid >= lo:
            return False
        return bps_signed(lo - mid, lo) >= float(origin_bps)
    if mid <= hi:
        return False
    return bps_signed(mid - hi, hi) >= float(origin_bps)


def on_break_side(mid: float, role: str, lo: float, hi: float) -> bool:
    return is_beyond(mid, role, lo, hi)


def approach_side_label(role: str) -> str:
    return "FROM_BELOW" if role == "UPPER" else "FROM_ABOVE"
