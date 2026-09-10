"""Raw reclaim components — no calibrated thresholds.

Output status is always RECLAIM_NOT_CALIBRATED. Components are descriptive only
and must never be turned into HELD/BROKEN labels in this package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import RECLAIM_STATUS
from .microprice import on_defender_side, side_crossed


def _ms(a: datetime, b: datetime) -> int:
    return int(round((b - a).total_seconds() * 1000.0))


def accumulate_cross_dwell(
    rows: list[dict[str, Any]],
    *,
    wall_price: float,
    wall_side: str,
    price_key: str = "midprice",
) -> dict[str, Any]:
    """Walk causal rows and compute cross / dwell / recross stats for one price series."""
    first_cross_at: datetime | None = None
    last_cross_at: datetime | None = None
    recross_count = 0
    prev_crossed: bool | None = None
    on_def = True
    dwell_start: datetime | None = None
    current_dwell_ms = 0
    longest_dwell_ms = 0
    uninterrupted_after_first_reclaim_ms = 0
    post_cross_price_distance_ticks: float | None = None
    post_cross_spread_ticks: float | None = None
    during_cross_spread_ticks: float | None = None

    for row in rows:
        if not row.get("coverage_ok"):
            continue
        t = _as_dt(row["decision_time"])
        px = row.get(price_key)
        crossed = side_crossed(px, wall_price=wall_price, wall_side=wall_side)
        defender = on_defender_side(px, wall_price=wall_price, wall_side=wall_side)
        if crossed is None or defender is None:
            continue

        if prev_crossed is False and crossed is True:
            if first_cross_at is None:
                first_cross_at = t
                during_cross_spread_ticks = row.get("spread_ticks")
            else:
                recross_count += 1
            last_cross_at = t
        if prev_crossed is True and crossed is False:
            # Returned to defender side after being crossed.
            if first_cross_at is not None and uninterrupted_after_first_reclaim_ms == 0:
                # start measuring first reclaim dwell below
                pass

        # Defender dwell tracking
        if defender:
            if dwell_start is None:
                dwell_start = t
            current_dwell_ms = _ms(dwell_start, t) if dwell_start else 0
            longest_dwell_ms = max(longest_dwell_ms, current_dwell_ms)
            if first_cross_at is not None and prev_crossed is True and crossed is False:
                # just reclaimed — dwell_start reset above
                uninterrupted_after_first_reclaim_ms = current_dwell_ms
            elif first_cross_at is not None and defender and prev_crossed is not True:
                uninterrupted_after_first_reclaim_ms = max(
                    uninterrupted_after_first_reclaim_ms, current_dwell_ms
                )
        else:
            dwell_start = None
            current_dwell_ms = 0

        if first_cross_at is not None and t >= first_cross_at:
            post_cross_price_distance_ticks = row.get("distance_to_wall_ticks")
            post_cross_spread_ticks = row.get("spread_ticks")

        prev_crossed = crossed
        on_def = defender

    # Finalize uninterrupted dwell if still on defender after first cross reclaim
    if first_cross_at is not None and on_def and dwell_start is not None and rows:
        last_t = _as_dt(rows[-1]["decision_time"])
        uninterrupted_after_first_reclaim_ms = max(
            uninterrupted_after_first_reclaim_ms, _ms(dwell_start, last_t)
        )

    time_since_last_cross_ms = None
    if last_cross_at is not None and rows:
        time_since_last_cross_ms = _ms(last_cross_at, _as_dt(rows[-1]["decision_time"]))

    return {
        "first_cross_at": format_utc_z(first_cross_at) if first_cross_at else None,
        "last_cross_at": format_utc_z(last_cross_at) if last_cross_at else None,
        "recross_count": int(recross_count),
        "uninterrupted_defender_dwell_ms": int(uninterrupted_after_first_reclaim_ms),
        "longest_defender_dwell_ms": int(longest_dwell_ms),
        "current_defender_dwell_ms": int(current_dwell_ms),
        "time_since_last_cross_ms": time_since_last_cross_ms,
        "price_distance_after_cross_ticks": post_cross_price_distance_ticks,
        "spread_during_cross_ticks": during_cross_spread_ticks,
        "spread_after_cross_ticks": post_cross_spread_ticks,
        "currently_on_defender_side": on_def,
    }


def build_reclaim_raw_bundle(
    rows: list[dict[str, Any]],
    *,
    wall_price: float,
    wall_side: str,
) -> dict[str, Any]:
    price = accumulate_cross_dwell(rows, wall_price=wall_price, wall_side=wall_side, price_key="midprice")
    micro = accumulate_cross_dwell(rows, wall_price=wall_price, wall_side=wall_side, price_key="microprice")

    # Joint cross: first time BOTH mid and micro are crossed.
    first_joint = None
    for row in rows:
        if not row.get("coverage_ok"):
            continue
        if row.get("wall_side_crossed") and row.get("microprice_side_crossed"):
            first_joint = row["decision_time"]
            break

    # Micro distance after first micro cross
    micro_dist_after = None
    if micro.get("first_cross_at"):
        fc = _as_dt(micro["first_cross_at"])
        for row in rows:
            if not row.get("coverage_ok"):
                continue
            if _as_dt(row["decision_time"]) >= fc:
                micro_dist_after = row.get("microprice_distance_to_wall_ticks")

    return {
        "reclaim_status": RECLAIM_STATUS,
        "first_price_cross_at": price.get("first_cross_at"),
        "first_microprice_cross_at": micro.get("first_cross_at"),
        "first_joint_cross_at": first_joint,
        "uninterrupted_defender_dwell_ms": price.get("uninterrupted_defender_dwell_ms"),
        "longest_defender_dwell_ms": price.get("longest_defender_dwell_ms"),
        "recross_count": price.get("recross_count"),
        "micro_recross_count": micro.get("recross_count"),
        "price_distance_after_cross_ticks": price.get("price_distance_after_cross_ticks"),
        "microprice_distance_after_cross_ticks": micro_dist_after,
        "spread_during_cross_ticks": price.get("spread_during_cross_ticks"),
        "spread_after_cross_ticks": price.get("spread_after_cross_ticks"),
        "price_cross_detail": price,
        "microprice_cross_detail": micro,
        "note": (
            "Raw reclaim components only. No HELD/BROKEN/FAKE_BREAK thresholds. "
            "RECLAIM_NOT_CALIBRATED — thresholds must not be chosen from Episode-1 outcome."
        ),
    }


def attach_running_cross_fields(
    rows: list[dict[str, Any]],
    *,
    wall_price: float,
    wall_side: str,
) -> list[dict[str, Any]]:
    """Add running recross / dwell / time_since_last_cross to each causal row (FEATURE)."""
    prev_crossed: bool | None = None
    first_cross_at: datetime | None = None
    last_cross_at: datetime | None = None
    recross = 0
    dwell_start: datetime | None = None
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        if not r.get("coverage_ok"):
            r.update(
                {
                    "recross_count": None,
                    "time_since_last_cross_ms": None,
                    "defender_side_dwell_ms": None,
                }
            )
            out.append(r)
            continue
        t = _as_dt(r["decision_time"])
        crossed = bool(r.get("wall_side_crossed"))
        defender = bool(r.get("price_on_defender_side"))
        if prev_crossed is False and crossed:
            if first_cross_at is None:
                first_cross_at = t
            else:
                recross += 1
            last_cross_at = t
        if defender:
            if dwell_start is None:
                dwell_start = t
            dwell_ms = _ms(dwell_start, t)
        else:
            dwell_start = None
            dwell_ms = 0
        r["recross_count"] = recross
        r["time_since_last_cross_ms"] = _ms(last_cross_at, t) if last_cross_at else None
        r["defender_side_dwell_ms"] = dwell_ms if defender else 0
        prev_crossed = crossed
        out.append(r)
    return out
