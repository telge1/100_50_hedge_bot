"""Thin protection pool + planner SL buffer. Thickness from BigBeluga strength/cluster size."""

from __future__ import annotations

from typing import Any

from .config import planner_root


def _ensure_planner() -> None:
    import sys

    root = str(planner_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _height_pct(cluster: dict[str, Any], entry: float) -> float:
    return (float(cluster["top"]) - float(cluster["bottom"])) / entry * 100.0


def select_thin_protection(
    clusters: list[dict[str, Any]],
    *,
    entry: float,
    executed_direction: str,
) -> dict[str, Any] | None:
    """Nearest isolated non-micro cluster on the protection side."""
    _ensure_planner()
    from research.liquidity.order_planner import MICRO_HEIGHT_PCT

    direction = executed_direction.upper()
    cands = []
    for c in clusters:
        if int(c.get("pool_count") or 0) != 1:
            continue
        if _height_pct(c, entry) < MICRO_HEIGHT_PCT:
            continue
        if direction == "LONG":
            if float(c["top"]) < entry:
                cands.append(c)
        else:
            if float(c["bottom"]) > entry:
                cands.append(c)
    if not cands:
        return None
    if direction == "LONG":
        cands.sort(key=lambda c: entry - float(c["top"]))
    else:
        cands.sort(key=lambda c: float(c["bottom"]) - entry)
    return cands[0]


def sl_from_cluster(cluster: dict[str, Any], *, executed_direction: str, entry: float) -> dict[str, Any]:
    _ensure_planner()
    from research.liquidity.order_planner import SL_BUFFER, SL_MAX_ABS_PCT

    if executed_direction.upper() == "LONG":
        sl = float(cluster["bottom"]) * (1.0 - SL_BUFFER)
    else:
        sl = float(cluster["top"]) * (1.0 + SL_BUFFER)
    dist = (sl - entry) / entry * 100.0
    return {
        "sl_price": sl,
        "sl_distance_pct": dist,
        "sl_too_wide": abs(dist) > SL_MAX_ABS_PCT,
        "sl_cluster": cluster,
        "sl_buffer": SL_BUFFER,
    }
