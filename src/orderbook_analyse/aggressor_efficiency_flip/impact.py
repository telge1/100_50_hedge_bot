"""Dual-impact measurements: contemporaneous + post-flow."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    opposite_move_bps,
    same_side_directional_bps,
)
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, WindowMetrics
from orderbook_analyse.aggressor_efficiency_flip.buckets import aggregate_window
from orderbook_analyse.aggressor_efficiency_flip.timeutil import bps_move, safe_finite


@dataclass
class DualImpact:
    flow: WindowMetrics
    post: WindowMetrics
    contemporaneous_return_bps: float
    contemporaneous_up_bps: float
    contemporaneous_down_bps: float
    same_side_contemporaneous_bps: float
    post_flow_return_bps: float
    post_flow_max_up_bps: float
    post_flow_max_down_bps: float
    post_same_side_followthrough_bps: float
    post_counter_move_bps: float
    post_empty: bool
    reclaim_flag: bool
    delayed_initiative_flag: bool


def _excursions(start_px: float, hi: Optional[float], lo: Optional[float]) -> tuple[float, float]:
    if start_px <= 0:
        return float("nan"), float("nan")
    up = 0.0 if hi is None else max(0.0, (hi - start_px) / start_px * 10_000.0)
    dn = 0.0 if lo is None else max(0.0, (start_px - lo) / start_px * 10_000.0)
    return up, dn


def measure_dual_impact(
    buckets: dict[datetime, SecondBucket],
    *,
    t0: datetime,
    t1: datetime,
    t2: datetime,
    side: str,
    reclaim_bps: float,
    strong_post_bps: float,
) -> DualImpact:
    """Flow [t0,t1) + post [t1,t2). Usable only when as_of >= t2 for full dual view.

    Post-flow start price is always flow_end_price (known at t1).
    """
    flow = aggregate_window(buckets, t0, t1)
    if flow.empty or flow.first_price is None or flow.last_price is None:
        raise ValueError("empty_flow_window")
    raw = bps_move(flow.first_price, flow.last_price)
    up, dn = _excursions(flow.first_price, flow.high_price, flow.low_price)
    same_c = same_side_directional_bps(side, raw)

    post = aggregate_window(buckets, t1, t2)
    post_start = flow.last_price
    if post.empty or post.last_price is None:
        post_end = post_start
        post_raw = 0.0
        post_up = 0.0
        post_dn = 0.0
        post_empty = True
    else:
        post_end = post.last_price
        post_raw = bps_move(post_start, post_end)
        # excursions vs post_start using trades in post window only
        post_up, post_dn = _excursions(post_start, post.high_price, post.low_price)
        post_empty = False

    post_same = same_side_directional_bps(side, post_raw)
    post_counter = opposite_move_bps(side, post_raw)
    # reclaim: opposing move beyond reclaim_bps
    reclaim = post_counter >= float(reclaim_bps)
    delayed = post_same >= float(strong_post_bps)

    return DualImpact(
        flow=flow,
        post=post,
        contemporaneous_return_bps=safe_finite(raw),
        contemporaneous_up_bps=safe_finite(up),
        contemporaneous_down_bps=safe_finite(dn),
        same_side_contemporaneous_bps=safe_finite(same_c),
        post_flow_return_bps=safe_finite(post_raw),
        post_flow_max_up_bps=safe_finite(post_up),
        post_flow_max_down_bps=safe_finite(post_dn),
        post_same_side_followthrough_bps=safe_finite(post_same),
        post_counter_move_bps=safe_finite(post_counter),
        post_empty=post_empty,
        reclaim_flag=reclaim,
        delayed_initiative_flag=delayed,
    )


def assert_no_post_in_flow(flow: WindowMetrics, post: WindowMetrics) -> None:
    """Integrity: post window must start at flow end."""
    if flow.end != post.start:
        raise AssertionError("post window must start exactly at flow end")
