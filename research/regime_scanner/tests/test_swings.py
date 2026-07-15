"""Pivot causality tests."""

from __future__ import annotations

import pandas as pd
import pytest

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.point_audit import build_point_audit
from research.regime_scanner.swings import (
    PivotVisibilityIndex,
    filter_pivots_as_of,
    find_confirmed_pivots,
)


def _frame_from_highs_lows(highs: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    n = len(highs)
    if lows is None:
        lows = [h - 1.0 for h in highs]
    start = pd.Timestamp("2026-01-13T20:00:00+00:00")
    rows = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        mid = (h + l) / 2.0
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": mid,
                "high": h,
                "low": l,
                "close": mid,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_pivot_not_confirmed_before_right_window() -> None:
    # Clear high at index 3; needs indexes 4,5,6 to confirm with pivot_right=3.
    highs = [1, 1, 1, 5, 2, 2, 2, 2]
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    partial = _frame_from_highs_lows(highs[:6])  # only through index 5 -> not confirmed
    assert find_confirmed_pivots(partial, config=cfg) == []
    full = _frame_from_highs_lows(highs[:7])  # through confirmation index 6
    pivots = find_confirmed_pivots(full, config=cfg)
    highs_p = [p for p in pivots if p.pivot_type == "high"]
    assert len(highs_p) == 1
    assert highs_p[0].pivot_index == 3
    assert highs_p[0].confirmation_index == 6


def test_equal_high_does_not_create_pivot() -> None:
    highs = [1, 1, 1, 5, 5, 2, 2, 2]  # tie on right side
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    pivots = find_confirmed_pivots(_frame_from_highs_lows(highs), config=cfg)
    assert [p for p in pivots if p.pivot_type == "high"] == []


def test_edges_do_not_crash() -> None:
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    assert find_confirmed_pivots(_frame_from_highs_lows([1, 2]), config=cfg) == []
    assert find_confirmed_pivots(pd.DataFrame(columns=["timestamp", "high", "low"]), config=cfg) == []


def test_future_candles_do_not_change_confirmed_pivots() -> None:
    highs = [1, 1, 1, 5, 2, 2, 2, 3, 4, 6, 7]
    base = _frame_from_highs_lows(highs[:7])
    decision = base["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    audit_a = build_point_audit(symbol="APTUSDT", decision_time=decision, candles=base)
    polluted = pd.concat(
        [
            base,
            pd.DataFrame(
                [
                    {
                        "timestamp": decision,
                        "open": 100.0,
                        "high": 999.0,
                        "low": 0.01,
                        "close": 500.0,
                        "volume": 1e9,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    audit_b = build_point_audit(symbol="APTUSDT", decision_time=decision, candles=polluted)
    assert audit_a["confirmed_pivots"]["last_two_highs"] == audit_b["confirmed_pivots"]["last_two_highs"]
    assert audit_a["confirmed_pivots"]["high_count"] == audit_b["confirmed_pivots"]["high_count"]


def test_filter_pivots_as_of_requires_confirmation_before_decision() -> None:
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    highs = [1, 1, 1, 5, 2, 2, 2]
    frame = _frame_from_highs_lows(highs)
    pivots = find_confirmed_pivots(frame, config=cfg)
    assert pivots
    decision = pd.Timestamp(pivots[0].confirmation_timestamp)
    assert filter_pivots_as_of(pivots, decision) == []
    after = decision + pd.Timedelta(minutes=5)
    assert len(filter_pivots_as_of(pivots, after)) == 1


def test_pivot_visibility_index_matches_filter() -> None:
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    highs = [1, 1, 1, 5, 2, 2, 2, 2, 3, 4, 6, 7]
    frame = _frame_from_highs_lows(highs)
    pivots = find_confirmed_pivots(frame, config=cfg)
    index = PivotVisibilityIndex.build(pivots)
    for i in range(len(frame)):
        decision = frame["timestamp"].iloc[i] + pd.Timedelta(minutes=5)
        assert index.as_of(decision) == filter_pivots_as_of(pivots, decision)
