"""Descriptive edge state using only prices < focus_ts."""

from __future__ import annotations

from typing import Any

from . import PROXIMITY_BINS


def classify_at_focus(
    *,
    price: float | None,
    path: list[tuple[int, float]],
    focus_unix: int,
    edge_level: float | None,
    side: str,
    price_step: float | None,
) -> dict[str, Any]:
    """``side`` is lower or upper. Path is (unix, price) with unix < focus."""
    if price is None or edge_level is None or not price_step:
        return {"state": "UNRESOLVED", "reason": "missing_price_or_edge", "edge_level": edge_level, "side": side}

    prox = PROXIMITY_BINS * float(price_step)
    dist = price - edge_level
    pre = [(u, p) for u, p in path if u < focus_unix]
    below = [p < edge_level for _, p in pre[-30:]] if pre else []
    above = [p > edge_level for _, p in pre[-30:]] if pre else []

    if side == "lower":
        if abs(dist) <= prox and dist >= 0:
            state = "LOWER_EDGE_TOUCH" if abs(dist) <= 0.5 * float(price_step) else "APPROACHING_LOWER_EDGE"
            if any(below):
                state = "LOWER_EDGE_TEST_UNRESOLVED"
        elif dist < 0:
            state = "LOWER_EDGE_EXCURSION_BELOW"
            # No 5m close confirmation at focus unless a closed 5m exists below — caller adds that.
            state = "LOWER_EDGE_TEST_UNRESOLVED"
        elif any(below) and dist > prox:
            state = "LOWER_EDGE_RECLAIMED"
        else:
            state = "INSIDE_VALUE" if dist > prox else "APPROACHING_LOWER_EDGE"
    else:
        if abs(dist) <= prox and dist <= 0:
            state = "UPPER_EDGE_TOUCH" if abs(dist) <= 0.5 * float(price_step) else "APPROACHING_UPPER_EDGE"
            if any(above):
                state = "UNRESOLVED"
        elif dist > 0:
            state = "UPPER_EDGE_EXCURSION_ABOVE"
        else:
            state = "INSIDE_VALUE"

    return {
        "state": state,
        "edge_level": edge_level,
        "side": side,
        "distance": dist,
        "near": abs(dist) <= prox,
        "confirmed_at_focus": False,
        "note": "No REJECTED/ACCEPTED from post-focus path.",
    }


def pick_relevant_lower_edge(confluence: dict[str, Any]) -> dict[str, Any] | None:
    near = confluence.get("near_lower_edges") or []
    if not near:
        return None
    # Prefer 30m then 1h then 4h previous_closed VAL/range
    rank = {"30m": 0, "1h": 1, "4h": 2, "15m": 3}
    et_rank = {"tpo_val": 0, "volume_val": 1, "range_low": 2}
    return min(
        near,
        key=lambda e: (
            0 if e.get("profile_kind") == "previous_closed" else 1,
            rank.get(e.get("timeframe"), 9),
            et_rank.get(e.get("edge_type"), 9),
            e.get("distance_to_price") or 1e18,
        ),
    )


def pick_relevant_upper_edge(confluence: dict[str, Any]) -> dict[str, Any] | None:
    near = confluence.get("near_upper_edges") or []
    if not near:
        return None
    rank = {"30m": 0, "1h": 1, "4h": 2, "15m": 3}
    et_rank = {"tpo_vah": 0, "volume_vah": 1, "range_high": 2}
    return min(
        near,
        key=lambda e: (
            0 if e.get("profile_kind") == "previous_closed" else 1,
            rank.get(e.get("timeframe"), 9),
            et_rank.get(e.get("edge_type"), 9),
            e.get("distance_to_price") or 1e18,
        ),
    )
