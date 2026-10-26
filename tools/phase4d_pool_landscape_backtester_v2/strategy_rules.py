"""User-confirmed price rules — pool landscape strict V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PricePlan:
    rule_ids: list[str]
    entry_level: float
    sl_price: float
    tp_price: float | None
    tp_pool_bottom: float | None
    tp_pool_top: float | None
    tp_distance_pct: float | None
    unresolved: str | None = None


def sl_mult_above(pct: float) -> float:
    return 1.0 + pct / 100.0


def sl_mult_below(pct: float) -> float:
    return 1.0 - pct / 100.0


def short_sl(upper_top: float, sl_pct: float) -> float:
    return upper_top * sl_mult_above(sl_pct)


def long_sl(lower_bottom: float, sl_pct: float) -> float:
    return lower_bottom * sl_mult_below(sl_pct)


def depth2_intervals(intervals: list[dict], side: str) -> list[dict]:
    out = []
    for iv in intervals:
        if iv.get("side") != side:
            continue
        if int(iv.get("depth") or 0) != 2:
            continue
        out.append(iv)
    return out


def select_short_tp_depth2(
    entry_fill: float, intervals: list[dict], min_tp_pct: float
) -> tuple[dict | None, str | None]:
    """Nearest lower depth-2 band whose TOP is >= min_tp_pct below entry."""
    lowers = depth2_intervals(intervals, "lower")
    eligible = []
    for iv in lowers:
        top = float(iv["top_price"])
        if top >= entry_fill:
            continue
        dist = (entry_fill - top) / entry_fill * 100.0
        if dist < min_tp_pct:
            continue
        eligible.append({**iv, "top": top, "bottom": float(iv["bottom_price"]), "dist_pct": dist})
    if not eligible:
        return None, "TP_NO_DEPTH2_POOL_MEETS_MIN_DISTANCE"
    eligible.sort(key=lambda x: -x["top"])
    return eligible[0], None


def select_long_tp_depth2(
    entry_fill: float, uppers: list[dict], min_tp_pct: float
) -> tuple[dict | None, str | None]:
    """Nearest upper depth-2 band whose BOTTOM is >= min_tp_pct above entry."""
    eligible = []
    for iv in uppers:
        if int(iv.get("depth") or 0) != 2:
            continue
        bot = float(iv["bottom_price"])
        if bot <= entry_fill:
            continue
        dist = (bot - entry_fill) / entry_fill * 100.0
        if dist < min_tp_pct:
            continue
        eligible.append({**iv, "bottom": bot, "top": float(iv["top_price"]), "dist_pct": dist})
    if not eligible:
        return None, "TP_NO_DEPTH2_POOL_MEETS_MIN_DISTANCE"
    eligible.sort(key=lambda x: x["bottom"])
    return eligible[0], None


def plan_short(
    pool_lower: float,
    pool_upper: float,
    intervals: list[dict],
    entry_fill: float,
    cfg: dict[str, Any],
    *,
    quality_gate: bool,
) -> PricePlan:
    sl = short_sl(pool_upper, float(cfg["sl_above_upper_pool_pct"]))
    entry_level = pool_lower
    tp_iv, tp_reason = select_short_tp_depth2(
        entry_fill, intervals, float(cfg["min_tp_distance_pct"])
    )
    unresolved = None
    if quality_gate and tp_iv:
        unresolved = "TP_POOL_ELIGIBILITY_UNRESOLVED"
    if not tp_iv:
        unresolved = tp_reason or "TP_UNRESOLVED"
    tp_price = float(tp_iv["top"]) if tp_iv else None
    return PricePlan(
        rule_ids=["SHORT-ENTRY-LEVEL-01", "SHORT-SL-01", "SHORT-TP-01"],
        entry_level=entry_level,
        sl_price=sl,
        tp_price=tp_price,
        tp_pool_bottom=float(tp_iv["bottom"]) if tp_iv else None,
        tp_pool_top=float(tp_iv["top"]) if tp_iv else None,
        tp_distance_pct=float(tp_iv["dist_pct"]) if tp_iv else None,
        unresolved=unresolved if not tp_iv or quality_gate else None,
    )


def plan_long(
    pool_lower: float,
    pool_upper: float,
    upper_candidates: list[dict],
    entry_fill: float,
    cfg: dict[str, Any],
    *,
    quality_gate: bool,
) -> PricePlan:
    sl = long_sl(pool_lower, float(cfg["sl_below_lower_pool_pct"]))
    entry_level = pool_upper
    tp_iv, tp_reason = select_long_tp_depth2(
        entry_fill, upper_candidates, float(cfg["min_tp_distance_pct"])
    )
    unresolved = None
    if quality_gate and tp_iv:
        unresolved = "TP_POOL_ELIGIBILITY_UNRESOLVED"
    if not tp_iv:
        unresolved = tp_reason or "TP_UNRESOLVED"
    tp_price = float(tp_iv["bottom"]) if tp_iv else None
    return PricePlan(
        rule_ids=["LONG-ENTRY-LEVEL-01", "LONG-SL-01", "LONG-TP-01"],
        entry_level=entry_level,
        sl_price=sl,
        tp_price=tp_price,
        tp_pool_bottom=float(tp_iv["bottom"]) if tp_iv else None,
        tp_pool_top=float(tp_iv["top"]) if tp_iv else None,
        tp_distance_pct=float(tp_iv["dist_pct"]) if tp_iv else None,
        unresolved=unresolved if not tp_iv or quality_gate else None,
    )
