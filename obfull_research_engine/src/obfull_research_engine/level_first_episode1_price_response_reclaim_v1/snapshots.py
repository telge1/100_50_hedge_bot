"""Causal decision snapshots vs WALL_FIRST_TOUCH and DETECTION.

FEATURE uses only inputs with event_available_at <= snapshot_decision_time.
OUTCOME_ONLY looks strictly forward after decision_time and never feeds FEATURE.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import Episode1Handoff
from ..timeparse import format_utc_z
from . import SNAPSHOT_OFFSETS_S, TIME_BASIS
from .book_stream import last_book_coverage_end
from .reclaim_raw import build_reclaim_raw_bundle


def _anchor_time(handoff: Episode1Handoff, anchor: str) -> datetime:
    if anchor == "WALL_FIRST_TOUCH":
        return _as_dt(handoff.wall_first_touch_exchange_event_time)
    if anchor == "DETECTION":
        return _as_dt(handoff.detection_exchange_event_time)
    raise ValueError(anchor)


def _row_at_or_before(rows: list[dict[str, Any]], decision_time: datetime) -> dict[str, Any] | None:
    last = None
    for r in rows:
        if _as_dt(r["decision_time"]) <= decision_time:
            last = r
        else:
            break
    return last


def _outcome_only_after(
    rows: list[dict[str, Any]],
    *,
    decision_time: datetime,
    wall_price: float,
    wall_side: str,
) -> dict[str, Any]:
    """Forward-looking labels — never merged into FEATURE metrics."""
    future = [r for r in rows if _as_dt(r["decision_time"]) > decision_time and r.get("coverage_ok")]
    if not future:
        return {
            "layer": "OUTCOME_ONLY",
            "mfe_attack_ticks": None,
            "mae_defender_ticks": None,
            "later_wall_cross_observed": None,
            "later_reclaim_observed": None,
            "note": "no_future_coverage",
        }
    anchor_mid = None
    # use last feature mid at decision as reference if present
    past = _row_at_or_before(rows, decision_time)
    if past and past.get("midprice") is not None:
        anchor_mid = float(past["midprice"])
    mfe = 0.0
    mae = 0.0
    later_cross = False
    later_reclaim = False
    was_crossed = bool(past.get("wall_side_crossed")) if past else False
    for r in future:
        mid = r.get("midprice")
        if mid is None or anchor_mid is None:
            continue
        # attack ticks from decision mid
        from .microprice import attack_direction

        d = attack_direction(wall_side)
        prog = d * (float(mid) - anchor_mid) / 0.1
        mfe = max(mfe, max(prog, 0.0))
        mae = max(mae, max(-prog, 0.0))
        crossed = bool(r.get("wall_side_crossed"))
        if crossed:
            later_cross = True
        if was_crossed and r.get("price_on_defender_side"):
            later_reclaim = True
        was_crossed = crossed or was_crossed
    return {
        "layer": "OUTCOME_ONLY",
        "mfe_attack_ticks": mfe,
        "mae_defender_ticks": mae,
        "later_wall_cross_observed": later_cross,
        "later_reclaim_observed": later_reclaim,
        "note": "forward_only_not_used_in_features",
    }


def build_decision_snapshots(
    *,
    handoff: Episode1Handoff,
    rows: list[dict[str, Any]],
    book_coverage_end: datetime | None,
    offsets_s: tuple[int, ...] = SNAPSHOT_OFFSETS_S,
) -> list[dict[str, Any]]:
    snaps: list[dict[str, Any]] = []
    # Snapshot independence: each snapshot computed only from rows <= decision_time
    # (no mutation of earlier snapshots when later ones are built).
    for anchor in ("WALL_FIRST_TOUCH", "DETECTION"):
        t_anchor = _anchor_time(handoff, anchor)
        for off in offsets_s:
            decision_time = t_anchor + timedelta(seconds=int(off))
            # Coverage: need book data up to decision_time
            coverage_ok = True
            invalid_reason = None
            if book_coverage_end is not None and decision_time > book_coverage_end + timedelta(milliseconds=200):
                # allow small bucket slack; beyond persist → invalid
                coverage_ok = False
                invalid_reason = "insufficient_book_coverage"

            feature_rows = [r for r in rows if _as_dt(r["decision_time"]) <= decision_time]
            # Do not let later rows exist in this slice
            assert all(_as_dt(r["decision_time"]) <= decision_time for r in feature_rows)

            row = _row_at_or_before(feature_rows, decision_time)
            if row is None:
                coverage_ok = False
                invalid_reason = invalid_reason or "no_row_at_or_before_decision"
            elif not row.get("coverage_ok"):
                coverage_ok = False
                invalid_reason = invalid_reason or row.get("invalid_reason") or "row_invalid"
            elif row.get("replay_epoch") is not None and int(row["replay_epoch"]) != int(handoff.replay_epoch):
                coverage_ok = False
                invalid_reason = "replay_epoch_change"
            elif _as_dt(row["max_input_available_at"]) > decision_time:
                coverage_ok = False
                invalid_reason = "input_after_decision_time"

            feature = None
            if row is not None and coverage_ok:
                feature = {
                    "layer": "FEATURE",
                    "decision_time": format_utc_z(decision_time),
                    "row_decision_time": row.get("decision_time"),
                    "best_bid": row.get("best_bid"),
                    "best_ask": row.get("best_ask"),
                    "best_bid_size": row.get("best_bid_size"),
                    "best_ask_size": row.get("best_ask_size"),
                    "midprice": row.get("midprice"),
                    "microprice": row.get("microprice"),
                    "spread_ticks": row.get("spread_ticks"),
                    "spread_bps": row.get("spread_bps"),
                    "distance_to_wall_ticks": row.get("distance_to_wall_ticks"),
                    "distance_to_zone_ticks": row.get("distance_to_zone_ticks"),
                    "price_progress_attack_ticks": row.get("price_progress_attack_ticks"),
                    "price_progress_attack_bps": row.get("price_progress_attack_bps"),
                    "price_progress_defender_ticks": row.get("price_progress_defender_ticks"),
                    "microprice_distance_to_wall_ticks": row.get("microprice_distance_to_wall_ticks"),
                    "wall_side_crossed": row.get("wall_side_crossed"),
                    "microprice_side_crossed": row.get("microprice_side_crossed"),
                    "zone_boundary_crossed": row.get("zone_boundary_crossed"),
                    "price_on_defender_side": row.get("price_on_defender_side"),
                    "microprice_on_defender_side": row.get("microprice_on_defender_side"),
                    "consumed_price_levels": row.get("consumed_price_levels"),
                    "recross_count": row.get("recross_count"),
                    "time_since_last_cross_ms": row.get("time_since_last_cross_ms"),
                    "defender_side_dwell_ms": row.get("defender_side_dwell_ms"),
                    "impact_efficiency_raw": row.get("impact_efficiency_raw"),
                    "aggressor_wall_notional": row.get("aggressor_wall_notional"),
                    "max_input_available_at": row.get("max_input_available_at"),
                    "look_ahead": False,
                    "coverage_ok": True,
                    "time_basis": TIME_BASIS,
                }
                # Reclaim components causal to decision_time only
                feature["reclaim_raw_asof"] = build_reclaim_raw_bundle(
                    feature_rows,
                    wall_price=handoff.wall_price,
                    wall_side=handoff.wall_side,
                )

            outcome = _outcome_only_after(
                rows,
                decision_time=decision_time,
                wall_price=handoff.wall_price,
                wall_side=handoff.wall_side,
            )

            snaps.append(
                {
                    "anchor": anchor,
                    "offset_s": int(off),
                    "snapshot_decision_time": format_utc_z(decision_time),
                    "valid": bool(coverage_ok and feature is not None),
                    "invalid_reason": None if (coverage_ok and feature is not None) else invalid_reason,
                    "FEATURE": feature,
                    "OUTCOME_ONLY": outcome,
                }
            )
    return snaps
