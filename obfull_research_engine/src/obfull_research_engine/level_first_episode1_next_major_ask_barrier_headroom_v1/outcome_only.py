"""OUTCOME_ONLY post-hoc path — never feeds barrier selection."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import EPOCH4_COVERAGE_END, OUTCOME_CENSORED, TICK_SIZE


def outcome_only_barrier_touch(
    *,
    barrier_price: float,
    decision_time: str,
    mid_path: list[tuple[str, float]],
    coverage_end: str = EPOCH4_COVERAGE_END,
) -> dict[str, Any]:
    """Whether price later reached barrier — strictly OUTCOME_ONLY / may be censored."""
    bp = float(barrier_price)
    dt = _as_dt(decision_time)
    cov = _as_dt(coverage_end)
    after = [(t, m) for t, m in mid_path if _as_dt(t) > dt]
    if not after:
        return {
            "layer": "OUTCOME_ONLY",
            "status": OUTCOME_CENSORED,
            "reason": "NO_FORWARD_PATH",
            "influences_barrier_selection": False,
        }
    first_touch = None
    max_before = None
    for t, m in after:
        if _as_dt(t) > cov:
            if first_touch is None:
                return {
                    "layer": "OUTCOME_ONLY",
                    "status": OUTCOME_CENSORED,
                    "reason": "EPOCH_BOUNDARY",
                    "max_move_before_touch_ticks": max_before,
                    "influences_barrier_selection": False,
                }
            break
        if max_before is None:
            max_before = 0.0
        # excursion from first mid after decision
        max_before = max(max_before, (m - after[0][1]) / TICK_SIZE)
        if m + 1e-12 >= bp:
            first_touch = t
            break
    if first_touch is None:
        # path ended without touch within coverage
        last_t = after[-1][0]
        if _as_dt(last_t) >= cov or _as_dt(coverage_end) <= cov:
            return {
                "layer": "OUTCOME_ONLY",
                "status": OUTCOME_CENSORED if _as_dt(last_t) >= cov else "NO_TOUCH_WITHIN_COVERAGE",
                "reason": "EPOCH_BOUNDARY_OR_NO_TOUCH",
                "reached_barrier": False,
                "max_move_before_touch_ticks": max_before,
                "influences_barrier_selection": False,
            }
    # post-touch move within coverage
    post = None
    if first_touch:
        post_pts = [(t, m) for t, m in after if _as_dt(t) >= _as_dt(first_touch) and _as_dt(t) <= cov]
        if len(post_pts) >= 2:
            post = (post_pts[-1][1] - post_pts[0][1]) / TICK_SIZE
    return {
        "layer": "OUTCOME_ONLY",
        "status": "OBSERVED" if first_touch else "NO_TOUCH_WITHIN_COVERAGE",
        "reached_barrier": bool(first_touch),
        "first_touch_time": format_utc_z(_as_dt(first_touch)) if first_touch else None,
        "max_move_before_touch_ticks": max_before,
        "move_after_touch_ticks": post,
        "influences_barrier_selection": False,
    }
