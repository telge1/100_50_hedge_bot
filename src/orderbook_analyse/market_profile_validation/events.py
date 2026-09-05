"""Touch detection and barrier races on 1m candles.

Pure functions over price arrays so the logic is testable without ClickHouse.

Two deliberate choices about what counts as evidence:

The race starts on the bar *after* the touch. The touch bar's own high/low
already reached the level, and its extremes may straddle a barrier, so using
it would let the outcome leak into its own trigger.

A bar whose high clears the upper barrier while its low clears the lower one
cannot be ordered from OHLC alone. Such races return ``AMBIGUOUS`` rather
than a guess. They are counted and reported, and the report also shows the
worst case in which all of them resolve adversely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from . import (
    APPROACH_ABOVE,
    APPROACH_BELOW,
    H1_BROKE,
    H1_REJECTED,
    H2_CONTINUED,
    H2_REVERSED,
    OUTCOME_AMBIGUOUS,
    OUTCOME_TIMEOUT,
)
from .contracts import RevisitEvent, TouchEvent

RACE_UP = "UP"
RACE_DOWN = "DOWN"


def race_barriers(
    highs: Sequence[float],
    lows: Sequence[float],
    start_idx: int,
    up_barrier: float,
    down_barrier: float,
    end_idx: int,
) -> tuple[str, int | None]:
    """First barrier reached in ``[start_idx, end_idx]``.

    Returns ``(RACE_UP | RACE_DOWN | OUTCOME_TIMEOUT | OUTCOME_AMBIGUOUS, idx)``.
    """
    if up_barrier <= down_barrier:
        raise ValueError("up_barrier must sit above down_barrier")
    last = min(int(end_idx), len(highs) - 1)
    for i in range(max(0, int(start_idx)), last + 1):
        hit_up = highs[i] >= up_barrier
        hit_down = lows[i] <= down_barrier
        if hit_up and hit_down:
            return OUTCOME_AMBIGUOUS, i
        if hit_up:
            return RACE_UP, i
        if hit_down:
            return RACE_DOWN, i
    return OUTCOME_TIMEOUT, None


def excursion(
    highs: Sequence[float],
    lows: Sequence[float],
    start_idx: int,
    end_idx: int,
    reference_price: float,
    favorable_sign: int,
) -> tuple[float, float]:
    """Max favorable and max adverse move from `reference_price`, in price units.

    Both are returned as non-negative magnitudes.
    """
    last = min(int(end_idx), len(highs) - 1)
    first = max(0, int(start_idx))
    if first > last:
        return 0.0, 0.0
    hi = max(highs[first : last + 1])
    lo = min(lows[first : last + 1])
    up = max(0.0, hi - reference_price)
    down = max(0.0, reference_price - lo)
    if favorable_sign >= 0:
        return up, down
    return down, up


def _first_index(pred, n: int) -> int | None:
    for i in range(n):
        if pred(i):
            return i
    return None


def _resolve_horizon(touch_idx: int, n: int, max_horizon_bars: int) -> int:
    if max_horizon_bars <= 0:
        return n - 1
    return min(n - 1, touch_idx + max_horizon_bars)


def build_pair_events(
    *,
    symbol: str,
    profile,
    test_window_id: str,
    times: Sequence[datetime],
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    edge_margin_fracs: Sequence[float],
    poc_unit_fracs: Sequence[float],
    max_horizon_bars: int,
) -> tuple[list[TouchEvent], RevisitEvent | None]:
    """Events for one (reference profile -> test window) pair.

    `times/opens/highs/lows` must contain only bars of the test window, i.e.
    bars that open at or after the reference window closed.

    A touch is located once and then raced against every barrier width in the
    grids. One arbitrary stop distance would otherwise decide the verdict: a
    tight stop makes any level look like it fails, a wide one makes it look
    like it holds while the reward/risk quietly collapses.
    """
    n = len(times)
    if n == 0:
        return [], None

    rng = float(profile.price_range)
    if rng <= 0:
        return [], None

    poc = float(profile.value_area.poc)
    vah = float(profile.value_area.vah)
    val = float(profile.value_area.val)
    direction = 1.0 if profile.close_price > profile.open_price else (
        -1.0 if profile.close_price < profile.open_price else 0.0
    )

    common = dict(
        symbol=symbol,
        ref_window_id=profile.window.window_id,
        ref_label=profile.window.label,
        ref_shape_kind=profile.shape.kind,
        ref_shape_letter=profile.shape.letter,
        ref_range=rng,
        ref_direction=direction,
        ref_va_range_share=float(profile.shape.va_range_share),
        ref_directional_share=float(profile.shape.directional_share),
        ref_poc_position=float(profile.shape.poc_position),
        test_window_id=test_window_id,
        poc_price=poc,
    )

    events: list[TouchEvent] = []

    def add_race(
        *,
        hypothesis: str,
        variant: str,
        level_kind: str,
        level_price: float,
        approach: str,
        touch_idx: int,
        up_barrier: float,
        down_barrier: float,
        favorable_sign: int,
        up_outcome: str,
        down_outcome: str,
        target_price: float,
        stop_price: float,
    ) -> None:
        start = touch_idx + 1
        end = _resolve_horizon(touch_idx, n, max_horizon_bars)
        if start > end:
            return
        race, idx = race_barriers(highs, lows, start, up_barrier, down_barrier, end)
        if race == RACE_UP:
            outcome = up_outcome
        elif race == RACE_DOWN:
            outcome = down_outcome
        else:
            outcome = race
        mfe, mae = excursion(highs, lows, start, end, level_price, favorable_sign)
        risk = abs(stop_price - level_price)
        reward = abs(target_price - level_price)
        events.append(
            TouchEvent(
                hypothesis=hypothesis,
                variant=variant,
                level_kind=level_kind,
                level_price=level_price,
                approach=approach,
                favorable_sign=favorable_sign,
                touch_ts=times[touch_idx],
                touch_price=level_price,
                target_price=target_price,
                stop_price=stop_price,
                outcome=outcome,
                resolution_ts=times[idx] if idx is not None else None,
                bars_to_resolution=(idx - touch_idx) if idx is not None else None,
                mfe_frac=mfe / rng,
                mae_frac=mae / rng,
                reward_risk=(reward / risk) if risk > 0 else 0.0,
                **common,
            )
        )

    # --- H1: value-area edges ------------------------------------------------
    # Only counted when the test window opened on the inside of the edge, so
    # a gap straight through it is not scored as a rejection setup.
    if opens[0] < vah and vah > poc:
        idx = _first_index(lambda i: highs[i] >= vah, n)
        if idx is not None:
            for frac in edge_margin_fracs:
                margin = frac * rng
                if margin <= 0:
                    continue
                add_race(
                    hypothesis="H1",
                    variant=f"margin_{frac:.2f}",
                    level_kind="VAH",
                    level_price=vah,
                    approach=APPROACH_BELOW,
                    touch_idx=idx,
                    up_barrier=vah + margin,
                    down_barrier=poc,
                    favorable_sign=-1,
                    up_outcome=H1_BROKE,
                    down_outcome=H1_REJECTED,
                    target_price=poc,
                    stop_price=vah + margin,
                )

    if opens[0] > val and val < poc:
        idx = _first_index(lambda i: lows[i] <= val, n)
        if idx is not None:
            for frac in edge_margin_fracs:
                margin = frac * rng
                if margin <= 0:
                    continue
                add_race(
                    hypothesis="H1",
                    variant=f"margin_{frac:.2f}",
                    level_kind="VAL",
                    level_price=val,
                    approach=APPROACH_ABOVE,
                    touch_idx=idx,
                    up_barrier=poc,
                    down_barrier=val - margin,
                    favorable_sign=+1,
                    up_outcome=H1_REJECTED,
                    down_outcome=H1_BROKE,
                    target_price=poc,
                    stop_price=val - margin,
                )

    # --- H2: POC as a way station -------------------------------------------
    # Needs a reference direction to have something to continue.
    if direction != 0.0:
        idx = _first_index(lambda i: lows[i] <= poc <= highs[i], n)
        if idx is not None:
            approach = APPROACH_BELOW if opens[0] < poc else APPROACH_ABOVE
            for frac in poc_unit_fracs:
                unit = frac * rng
                if unit <= 0:
                    continue
                up_is_continuation = direction > 0
                add_race(
                    hypothesis="H2",
                    variant=f"unit_{frac:.2f}",
                    level_kind="POC",
                    level_price=poc,
                    approach=approach,
                    touch_idx=idx,
                    up_barrier=poc + unit,
                    down_barrier=poc - unit,
                    favorable_sign=1 if up_is_continuation else -1,
                    up_outcome=H2_CONTINUED if up_is_continuation else H2_REVERSED,
                    down_outcome=H2_REVERSED if up_is_continuation else H2_CONTINUED,
                    target_price=poc + unit if up_is_continuation else poc - unit,
                    stop_price=poc - unit if up_is_continuation else poc + unit,
                )

    # --- H3: POC as a magnet -------------------------------------------------
    idx = _first_index(lambda i: lows[i] <= poc <= highs[i], n)
    revisit = RevisitEvent(
        symbol=symbol,
        ref_window_id=profile.window.window_id,
        ref_label=profile.window.label,
        ref_shape_kind=profile.shape.kind,
        ref_shape_letter=profile.shape.letter,
        ref_range=rng,
        test_window_id=test_window_id,
        poc_price=poc,
        poc_distance_frac=(poc - opens[0]) / rng,
        revisited=idx is not None,
        revisit_ts=times[idx] if idx is not None else None,
        minutes_to_revisit=idx if idx is not None else None,
        revisited_60m=idx is not None and idx < 60,
        revisited_240m=idx is not None and idx < 240,
    )
    return events, revisit
