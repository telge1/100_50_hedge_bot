"""Depth-2 entry geometry vs price (does not alter user decision labels)."""

from __future__ import annotations

from typing import Any

# Documented S1 measurement; not an active signal rule.
SUGGESTED_TOLERANCE_RESEARCH_PCT = 0.05


def _f(x: Any) -> float:
    return float(x)


def _pct_distance(price: float, edge: float, base: float) -> float:
    if base == 0:
        return 0.0
    return abs(price - edge) / base * 100.0


def classify_entry_vs_depth2(
    intervals: list[dict[str, Any]],
    price: float | None,
    side_focus: str = "upper",
) -> dict[str, Any]:
    """
    Returns geometry status separate from SHORT/NO_SHORT label.

    Status values: POOL_AT_ENTRY | NEARBY_DEPTH2_POOL | NOT_VERIFIED
    """
    if price is None:
        return {
            "geometry_status": "NOT_VERIFIED",
            "reason": "no_manual_entry_price",
            "side_focus": side_focus,
            "suggested_tolerance_research_pct": SUGGESTED_TOLERANCE_RESEARCH_PCT,
            "note": "Tolerance is research-only; not applied to classification.",
        }

    p = _f(price)
    bands = [
        iv
        for iv in intervals
        if iv.get("side") == side_focus
        and _f(iv["bottom_price"]) <= p <= _f(iv["top_price"])
    ]
    if bands:
        b = bands[0]
        return {
            "geometry_status": "POOL_AT_ENTRY",
            "side_focus": side_focus,
            "price": p,
            "pool_bottom": _f(b["bottom_price"]),
            "pool_top": _f(b["top_price"]),
            "depth": b.get("depth"),
            "origins": b.get("origins"),
            "distance_to_pool_bottom_pct": 0.0,
            "distance_to_pool_top_pct": 0.0,
            "suggested_tolerance_research_pct": SUGGESTED_TOLERANCE_RESEARCH_PCT,
            "within_suggested_tolerance": True,
        }

    same_side = [iv for iv in intervals if iv.get("side") == side_focus]
    below = sorted(
        [u for u in same_side if _f(u["top_price"]) < p],
        key=lambda u: _f(u["top_price"]),
        reverse=True,
    )
    above = sorted(
        [u for u in same_side if _f(u["bottom_price"]) > p],
        key=lambda u: _f(u["bottom_price"]),
    )

    candidates: list[tuple[float, dict, str]] = []
    if below:
        u = below[0]
        d = _pct_distance(p, _f(u["top_price"]), p)
        candidates.append((d, u, "below_ref_nearest_top"))
    if above:
        u = above[0]
        d = _pct_distance(p, _f(u["bottom_price"]), p)
        candidates.append((d, u, "above_ref_nearest_bottom"))

    if not candidates:
        return {
            "geometry_status": "NOT_VERIFIED",
            "reason": "no_depth2_bands_on_focus_side",
            "side_focus": side_focus,
            "price": p,
            "suggested_tolerance_research_pct": SUGGESTED_TOLERANCE_RESEARCH_PCT,
        }

    candidates.sort(key=lambda x: x[0])
    dist_pct, band, relation = candidates[0]
    return {
        "geometry_status": "NEARBY_DEPTH2_POOL",
        "side_focus": side_focus,
        "price": p,
        "distance_to_nearest_pct": dist_pct,
        "distance_relation": relation,
        "nearest_pool_bottom": _f(band["bottom_price"]),
        "nearest_pool_top": _f(band["top_price"]),
        "nearest_depth": band.get("depth"),
        "nearest_origins": band.get("origins"),
        "suggested_tolerance_research_pct": SUGGESTED_TOLERANCE_RESEARCH_PCT,
        "within_suggested_tolerance": dist_pct <= SUGGESTED_TOLERANCE_RESEARCH_PCT,
        "note": (
            "S1 reference ~0.024% is a prior measurement; "
            "tolerance 0.05% is not an active rule."
        ),
    }
