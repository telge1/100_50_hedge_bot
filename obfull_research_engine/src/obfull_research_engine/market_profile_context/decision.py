"""Research decision context — no orders."""

from __future__ import annotations

from typing import Any


def combine(
    *,
    at_focus_state: dict[str, Any],
    lower_edge: dict[str, Any] | None,
    upper_edge: dict[str, Any] | None,
    pressure: str,
    footprint_delta_60s: float | None,
    oi_quadrant: str | None,
    visual_hindsight: bool,
) -> dict[str, Any]:
    state = str(at_focus_state.get("state") or "UNRESOLVED")
    near_lower = bool(lower_edge) or "LOWER" in state
    near_upper = bool(upper_edge) or "UPPER" in state
    sell_pressure = pressure in {"SELL", "BEARISH"} or (
        footprint_delta_60s is not None and footprint_delta_60s < 0
    )
    buy_pressure = pressure in {"BUY", "BULLISH"} or (
        footprint_delta_60s is not None and footprint_delta_60s > 0
    )

    tag = "NO_EDGE_CONTEXT"
    action = "NO_EDGE_WAIT"
    if sell_pressure and near_lower and "UNRESOLVED" in state.replace("TEST_UNRESOLVED", "UNRESOLVED"):
        tag = "SELL_PRESSURE_NEAR_LOWER_EDGE"
        action = "WAIT_FOR_EDGE_RESOLUTION"
    elif sell_pressure and near_lower and state in {
        "LOWER_EDGE_TOUCH",
        "APPROACHING_LOWER_EDGE",
        "LOWER_EDGE_EXCURSION_BELOW",
        "LOWER_EDGE_TEST_UNRESOLVED",
    }:
        tag = "SELL_PRESSURE_NEAR_LOWER_EDGE"
        action = "WAIT_FOR_EDGE_RESOLUTION"
    elif buy_pressure and near_upper and state in {
        "UPPER_EDGE_TOUCH",
        "APPROACHING_UPPER_EDGE",
        "UPPER_EDGE_EXCURSION_ABOVE",
        "UNRESOLVED",
    }:
        tag = "BUY_PRESSURE_NEAR_UPPER_EDGE"
        action = "WAIT_FOR_EDGE_RESOLUTION"
    elif not near_lower and not near_upper:
        tag = "NOT_NEAR_RELEVANT_EDGE"
        action = "NO_EDGE_WAIT"

    if visual_hindsight:
        note = "VISUAL_HINDSIGHT_PROFILE_NOT_AVAILABLE_AT_FOCUS"
    else:
        note = "At-focus uses previous-closed + developing only."

    return {
        "status": "RESEARCH_CONTEXT_ONLY",
        "pressure": pressure,
        "profile_location_state": state,
        "resolution": "UNRESOLVED",
        "research_tag": tag,
        "short_action": action if tag == "SELL_PRESSURE_NEAR_LOWER_EDGE" else "NOT_A_SHORT_SIGNAL",
        "long_action": action if tag == "BUY_PRESSURE_NEAR_UPPER_EDGE" else "NOT_A_LONG_SIGNAL",
        "wait_for_edge_resolution": action == "WAIT_FOR_EDGE_RESOLUTION",
        "oi_quadrant": oi_quadrant,
        "footprint_delta_60s": footprint_delta_60s,
        "visual_hindsight_flag": note if visual_hindsight else None,
        "no_order": True,
    }
