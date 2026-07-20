"""Unit tests for Phase F0 level / leg / path metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.emergency_lock.phase_f0_speed import (
    PhaseF0Config,
    ReboundEpisodeCounter,
    build_leg_metrics,
    close_path_efficiency,
    find_level_crossings,
    tr_path_efficiency,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _c(i: int, *, h: float, l: float, c: float | None = None) -> dict:
    close = float(c if c is not None else (h + l) / 2.0)
    return {
        "timestamp": _ts(i),
        "open": close,
        "high": float(h),
        "low": float(l),
        "close": close,
        "volume": 1.0,
    }


def test_first_low_touch_and_no_double_count() -> None:
    ref = 100.0
    # Bar0 stays above -2; bar1 touches -2; bar2 reclaim; bar3 re-touch -2 (ignored)
    candles = [
        _c(0, h=100, l=99, c=99.5),
        _c(1, h=99, l=97.5, c=98),  # touches 98
        _c(2, h=99, l=98.5, c=99),
        _c(3, h=98.5, l=97.0, c=97.5),  # again below 98 — must not re-count
        _c(4, h=97, l=95.5, c=96),  # -4%
    ]
    xs = find_level_crossings(
        candles,
        reference_price=ref,
        levels_pct=(-0.02, -0.04),
        start_index=0,
        end_index=4,
        touch_mode="first_low_touch",
        event_id="e",
    )
    assert [r["level_pct"] for r in xs] == [-0.02, -0.04]
    assert xs[0]["end_bar"] == 1
    assert xs[1]["end_bar"] == 4


def test_close_cross_separate() -> None:
    ref = 100.0
    candles = [
        _c(0, h=100, l=97.5, c=99),  # low touches 98, close above
        _c(1, h=99, l=97, c=97.5),  # close below 98
    ]
    low = find_level_crossings(
        candles,
        reference_price=ref,
        levels_pct=(-0.02,),
        start_index=0,
        end_index=1,
        touch_mode="first_low_touch",
    )
    close = find_level_crossings(
        candles,
        reference_price=ref,
        levels_pct=(-0.02,),
        start_index=0,
        end_index=1,
        touch_mode="first_close_below",
    )
    assert low[0]["end_bar"] == 0
    assert close[0]["end_bar"] == 1


def test_multiple_levels_same_candle() -> None:
    ref = 100.0
    candles = [
        _c(0, h=100, l=99.5, c=100),
        _c(1, h=99, l=93.5, c=94),  # crashes through -2,-4,-6
    ]
    xs = find_level_crossings(
        candles,
        reference_price=ref,
        levels_pct=(-0.02, -0.04, -0.06),
        start_index=0,
        end_index=1,
        touch_mode="first_low_touch",
    )
    assert [r["level_pct"] for r in xs] == [-0.02, -0.04, -0.06]
    assert all(r["end_bar"] == 1 for r in xs)


def test_leg_time_and_slowdown() -> None:
    ref = 100.0
    candles = [_c(0, h=100, l=100, c=100)]
    # slow path to -2 over 6 bars, fast to -4 in 1 bar
    for i in range(1, 7):
        px = 100 - i * (2 / 6)
        candles.append(_c(i, h=px + 0.1, l=px - 0.05, c=px))
    candles.append(_c(7, h=96.5, l=95.9, c=96.0))
    origin = {
        "event_id": "e",
        "touch_mode": "first_low_touch",
        "level_index": 0,
        "level_pct": 0.0,
        "level_price": 100.0,
        "end_timestamp": candles[0]["timestamp"].isoformat(),
        "end_bar": 0,
        "minutes_needed": 0.0,
        "window_truncated_at_data_end": False,
        "reference_price": 100.0,
    }
    xs = find_level_crossings(
        candles,
        reference_price=ref,
        levels_pct=(-0.02, -0.04),
        start_index=0,
        end_index=7,
    )
    legs = build_leg_metrics([origin] + xs, candles, PhaseF0Config(), event_id="e")
    assert len(legs) == 2
    assert legs[0]["bars_for_leg"] >= 1
    assert legs[1]["previous_leg_present"] is True
    assert legs[1]["slowdown_ratio"] is not None
    assert legs[1]["slowdown_ratio"] < 1.0  # faster second leg


def test_path_efficiency_straight_vs_chop() -> None:
    straight = [_c(i, h=100 - i, l=99 - i, c=99.5 - i) for i in range(6)]
    chop = []
    px = 100.0
    for i in range(12):
        # oscillate while slowly drifting down 2%
        px = 100 - (i / 11) * 2
        wobble = 0.8 if i % 2 == 0 else -0.8
        chop.append(_c(i, h=px + abs(wobble), l=px - abs(wobble), c=px + wobble * 0.5))
    e_s = close_path_efficiency(straight, start=0, end=5)
    e_c = close_path_efficiency(chop, start=0, end=11)
    assert e_s is not None and e_c is not None
    assert e_s > e_c


def test_tr_path_no_div_zero() -> None:
    candles = [_c(0, h=100, l=100, c=100), _c(1, h=100, l=100, c=100)]
    e = tr_path_efficiency(
        candles, start=0, end=1, start_price=100.0, end_price=100.0
    )
    assert e == pytest.approx(1.0)


def test_rebound_episodes_not_double_counted() -> None:
    counter = ReboundEpisodeCounter(thresholds=(0.005,))
    counter.reset(100.0)
    # Rebound to +0.6% once
    counter.update(low=100.0, high=100.7, close=100.5)
    assert counter.counts[0.005] == 1
    # Stay elevated — no second count
    counter.update(low=100.2, high=100.8, close=100.6)
    assert counter.counts[0.005] == 1
    # New lower low then another rebound
    counter.update(low=99.0, high=99.2, close=99.1)
    counter.update(low=99.0, high=99.8, close=99.5)
    assert counter.counts[0.005] == 2


def test_missing_previous_leg_slowdown_nan_not_zero() -> None:
    candles = [_c(0, h=100, l=100, c=100), _c(1, h=99, l=97.5, c=98)]
    origin = {
        "event_id": "e",
        "touch_mode": "first_low_touch",
        "level_index": 0,
        "level_pct": 0.0,
        "level_price": 100.0,
        "end_timestamp": candles[0]["timestamp"].isoformat(),
        "end_bar": 0,
        "minutes_needed": 0.0,
        "window_truncated_at_data_end": False,
        "reference_price": 100.0,
    }
    xs = find_level_crossings(
        candles,
        reference_price=100.0,
        levels_pct=(-0.02,),
        start_index=0,
        end_index=1,
    )
    legs = build_leg_metrics([origin] + xs, candles, PhaseF0Config(), event_id="e")
    assert legs[0]["slowdown_ratio"] is None
    assert legs[0]["previous_leg_present"] is False
