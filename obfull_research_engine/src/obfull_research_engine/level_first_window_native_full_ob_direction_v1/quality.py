"""Fail-closed quality and spatial coverage. No outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.local_analyzer import evaluate_full_ob
from ..drilldown.imbalance import _is_crossed
from .sides import price_in, spatial_bands


def coverage_span_for(start: datetime, end: datetime, localized: dict[str, Any] | None) -> dict[str, Any]:
    for span in (localized or {}).get("usable_spans") or []:
        a = parse_utc(span["start"])
        b = parse_utc(span["end"])
        if a <= start and end <= b:
            return {"coverage_span_id": span.get("span_id") or "", "inside": True, "span": span}
    return {"coverage_span_id": "", "inside": False, "span": None}


def quality_precheck(
    *,
    symbol: str,
    first_touch: datetime,
    detection: datetime,
    reaction_direction: str | None,
    localized_coverage: dict[str, Any] | None,
) -> dict[str, Any]:
    if reaction_direction not in {"BULLISH", "BEARISH"}:
        return {
            "pass": False,
            "replay_allowed": False,
            "reason": "REACTION_DIRECTION_UNAVAILABLE",
            "overall_if_stopped": "FULL_OB_DIRECTION_NOT_EVALUATED",
            "coverage_status": None,
        }
    fo = evaluate_full_ob(
        symbol=symbol,
        first_touch=first_touch,
        detection=detection,
        reaction_direction=reaction_direction,
        localized_coverage=localized_coverage,
    )
    start = parse_utc(fo["full_ob_window_start"])
    end = parse_utc(fo["full_ob_window_end"])
    span = coverage_span_for(start, end, localized_coverage)
    available = fo.get("full_ob_assessment_status") == "FULL_OB_AVAILABLE"
    reason = fo.get("full_ob_reason") if not available else None
    return {
        "pass": bool(available and span["inside"]),
        "replay_allowed": bool(available and span["inside"]),
        "reason": reason or ("WINDOW_NOT_INSIDE_USABLE_SPAN" if not span["inside"] else None),
        "overall_if_stopped": "FULL_OB_NOT_EVALUATED",
        "coverage_status": fo.get("full_ob_assessment_status"),
        "coverage_reason": fo.get("full_ob_reason"),
        "evidence_start": fo["full_ob_window_start"],
        "evidence_end": fo["full_ob_window_end"],
        "coverage_span_id": span["coverage_span_id"],
        "quality": fo.get("full_ob_quality") or {},
    }


def replay_quality_fail(
    *,
    replay: dict[str, Any] | None,
    states: list[dict[str, Any]],
    ordering: str | None,
    sequence_gaps: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not replay or not replay.get("ok"):
        reasons.append(str((replay or {}).get("error") or "REPLAY_NOT_OK"))
    if not replay or not replay.get("initial_bids") or not replay.get("initial_asks"):
        reasons.append("ANCHOR_OR_CHECKPOINT_MISSING")
    if int(sequence_gaps or 0) > 0:
        reasons.append("SEQUENCE_GAP_OR_TAINTED")
    # ORDERING_AMBIGUOUS is the existing trade+book equal-timestamp contract.
    # Wall/refill/100ms depth use book level_changes only and do not require that tie-break.
    crossed = 0
    for row in states:
        if _is_crossed(row) or (
            row.get("best_bid") is not None
            and row.get("best_ask") is not None
            and row["best_bid"] >= row["best_ask"]
        ):
            crossed += 1
    if crossed:
        reasons.append("CROSSED_100MS_OVERLAPS_EVIDENCE")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "crossed_book_buckets": crossed,
        "sequence_gaps": int(sequence_gaps or 0),
        "ordering_quality": ordering,
        "ordering_ambiguous_trade_book_ties_recorded": bool(ordering and "AMBIGUOUS" in str(ordering)),
        "ordering_fail_closed_for_book_families": False,
    }


def spatial_coverage(
    *,
    reaction_class: str,
    reaction_direction: str,
    zone_low: float,
    zone_high: float,
    adjacent_bps: float,
    bids: dict[float, float],
    asks: dict[float, float],
    mid_hint: float | None,
) -> dict[str, Any]:
    bands = spatial_bands(
        reaction_class=reaction_class,
        reaction_direction=reaction_direction,
        zone_low=zone_low,
        zone_high=zone_high,
        adjacent_bps=adjacent_bps,
        mid_hint=mid_hint,
    )
    prices = [px for px, qty in {**bids, **asks}.items() if qty and qty > 0]
    min_bid = min(bids) if bids else None
    max_ask = max(asks) if asks else None
    level_ok = any(price_in(bands["level_zone"], px) for px in prices)
    front_ok = any(price_in(bands["front_zone"], px) for px in prices)
    back_ok = any(price_in(bands["back_zone"], px) for px in prices)
    if level_ok and front_ok and back_ok:
        quality = "SPATIAL_COVERED"
    elif prices:
        quality = "SPATIAL_PARTIAL"
    else:
        quality = "SPATIAL_EMPTY"
    pass_ok = bool(level_ok and front_ok and back_ok)
    return {
        "min_covered_bid": min_bid,
        "max_covered_ask": max_ask,
        "level_zone_covered": level_ok,
        "front_zone_covered": front_ok,
        "back_zone_covered": back_ok,
        "spatial_coverage_quality": quality,
        "spatial_pass": pass_ok,
        **{k: bands[k] for k in ("reaction_family", "level_zone", "front_zone", "back_zone")},
    }
