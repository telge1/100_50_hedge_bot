"""Event-level mechanism classification (descriptive thresholds)."""

from __future__ import annotations

from typing import Any

from research.orderbook.historical_break_pull_consumption import (
    AGGRESSIVE_FLOW_MIN_FRAC,
    CONSUMPTION_RATIO_MIN,
    PULL_RATIO_MAX,
    REFILL_RATIO_MIN,
)
from research.orderbook.historical_break_pull_consumption.walls import WallAction, WallSnapshot


def classify_mechanism(
    *,
    actions: list[WallAction],
    snaps: list[WallSnapshot],
    break_ms: int,
    aggressive_qty_pre_break: float,
    peak_wall_qty: float,
    beyond_at_break: bool,
    beyond_at_60s: bool | None,
    prior_ob_class: str | None,
) -> dict[str, Any]:
    """Classify using transparent thresholds; prefer LOW_CONFIDENCE over overclaiming."""

    pre = [a for a in actions if a.ts_ms <= break_ms and a.ts_ms >= break_ms - 60_000]
    decreases = [a for a in pre if a.action in {"DECREASE", "DELETE"}]
    increases = [a for a in pre if a.action in {"INCREASE", "REAPPEAR", "ADD"}]

    gross_removal = sum(max(0.0, -a.delta_qty) for a in decreases)
    gross_matched = sum(a.matched_aggressive_qty for a in decreases)
    gross_refill = sum(max(0.0, a.delta_qty) for a in increases)
    unmatched = max(0.0, gross_removal - gross_matched)
    ratio = (gross_matched / gross_removal) if gross_removal > 1e-12 else None

    pull_start = None
    cons_start = None
    refill_start = None
    for a in sorted(pre, key=lambda x: x.ts_ms):
        if a.action in {"DECREASE", "DELETE"} and a.mechanism_hint == "PULLISH" and pull_start is None:
            pull_start = a.ts_ms
        if a.action in {"DECREASE", "DELETE"} and a.mechanism_hint == "CONSUMPTIONISH" and cons_start is None:
            cons_start = a.ts_ms
        if a.action in {"INCREASE", "REAPPEAR"} and refill_start is None:
            refill_start = a.ts_ms
        # also catch early shrink without hint yet
        if a.action in {"DECREASE", "DELETE"} and pull_start is None and (a.consumption_ratio or 0) < 0.3:
            pull_start = a.ts_ms
        if a.action in {"DECREASE", "DELETE"} and cons_start is None and (a.consumption_ratio or 0) > 0.7:
            cons_start = a.ts_ms

    def sec_before(ts: int | None) -> float | None:
        if ts is None:
            return None
        return (break_ms - ts) / 1000.0

    # Snapshots at markers
    def qty_at(offset_s: int) -> float | None:
        target = break_ms + offset_s * 1000
        cand = [s for s in snaps if s.ts_ms <= target]
        return cand[-1].zone_qty if cand else None

    wall_60 = qty_at(-60)
    wall_10 = qty_at(-10)
    wall_0 = qty_at(0)

    significant_flow = aggressive_qty_pre_break >= max(
        1e-9, AGGRESSIVE_FLOW_MIN_FRAC * max(peak_wall_qty, 1e-9)
    )
    refill_frac = (gross_refill / gross_removal) if gross_removal > 1e-12 else (
        1.0 if gross_refill > 0 else 0.0
    )

    confidence = "MEDIUM"
    notes: list[str] = []

    # Absorption / refill: significant aggressor flow, wall not net-depleted, no sustained beyond
    net_depletion = None
    if wall_60 is not None and wall_0 is not None:
        net_depletion = max(0.0, wall_60 - wall_0)

    mechanism = "NO_CLEAR_MECHANISM"
    if gross_removal <= 1e-9 and not significant_flow:
        mechanism = "NO_CLEAR_MECHANISM"
        confidence = "LOW"
        notes.append("little wall activity in last 60s")
    elif (
        significant_flow
        and refill_frac >= REFILL_RATIO_MIN
        and (not beyond_at_break or beyond_at_60s is False)
        and (prior_ob_class in {"REFILL_THEN_RECLAIM", "WALL_HELD_OR_RECLAIM"} or beyond_at_60s is False)
    ):
        mechanism = "REFILL_ABSORPTION"
        confidence = "MEDIUM"
        notes.append("aggressive flow with refill / failed sustained acceptance")
    elif ratio is not None and ratio <= PULL_RATIO_MAX and gross_removal > 0:
        mechanism = "PULL_DOMINANT"
        notes.append(f"matched/removed={ratio:.2f} <= {PULL_RATIO_MAX}")
    elif ratio is not None and ratio >= CONSUMPTION_RATIO_MIN and gross_removal > 0:
        mechanism = "CONSUMPTION_DOMINANT"
        notes.append(f"matched/removed={ratio:.2f} >= {CONSUMPTION_RATIO_MIN}")
    elif ratio is not None and gross_removal > 0:
        mechanism = "MIXED_PULL_CONSUMPTION"
        notes.append(f"matched/removed={ratio:.2f} in mixed band")
    elif significant_flow and (net_depletion or 0) < peak_wall_qty * 0.2:
        mechanism = "REFILL_ABSORPTION"
        confidence = "LOW"
        notes.append("flow without clear net depletion")
    else:
        confidence = "LOW"
        notes.append("insufficient coherent wall-trade linkage")

    # Feed uncertainty: if matched near thresholds, lower confidence
    if ratio is not None and abs(ratio - 0.5) < 0.15:
        confidence = "LOW"
        notes.append("ratio near mixed center; feed sync uncertainty")

    return {
        "mechanism_class": mechanism,
        "confidence": confidence,
        "gross_removal_qty": gross_removal,
        "gross_matching_aggressive_qty": gross_matched,
        "unmatched_removal_qty": unmatched,
        "gross_refill_qty": gross_refill,
        "consumption_ratio": ratio,
        "refill_ratio": refill_frac if gross_removal > 0 else None,
        "aggressive_qty_pre_break": aggressive_qty_pre_break,
        "peak_wall_qty": peak_wall_qty,
        "wall_qty_60s_before": wall_60,
        "wall_qty_10s_before": wall_10,
        "wall_qty_at_break": wall_0,
        "net_wall_reduction_60s": net_depletion,
        "pull_start_ts_ms": pull_start,
        "consumption_start_ts_ms": cons_start,
        "refill_start_ts_ms": refill_start,
        "pull_start_seconds_before_break": sec_before(pull_start),
        "consumption_start_seconds_before_break": sec_before(cons_start),
        "refill_start_seconds_before_break": sec_before(refill_start),
        "notes": "; ".join(notes),
        "thresholds": {
            "PULL_RATIO_MAX": PULL_RATIO_MAX,
            "CONSUMPTION_RATIO_MIN": CONSUMPTION_RATIO_MIN,
            "REFILL_RATIO_MIN": REFILL_RATIO_MIN,
            "MATCH_TIME_MS": 750,
            "ZONE_BPS": 8.0,
        },
    }
