"""Confirmed divergence tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.divergence import (
    detect_bearish_divergence,
    detect_bullish_divergence,
    detect_confirmed_divergence_for_indicator,
)
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots


def _indicator_frame_with_manual_pivots() -> tuple[pd.DataFrame, list[ConfirmedPivot]]:
    n = 40
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.linspace(10, 20, n)
    high = close + 1
    low = close - 1
    # Force two clear highs at 10 and 25 with rising prices and falling ADX.
    high[10] = 30
    high[25] = 35
    adx = np.full(n, 40.0)
    adx[10] = 55.0
    adx[25] = 35.0
    plus_di = adx.copy()
    di_spread = adx.copy()
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1.0),
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": np.full(n, 10.0),
            "di_spread": di_spread,
        }
    )
    pivots = [
        ConfirmedPivot(10, ts[10].isoformat(), 13, ts[13].isoformat(), 30.0, "high"),
        ConfirmedPivot(25, ts[25].isoformat(), 28, ts[28].isoformat(), 35.0, "high"),
    ]
    return df, pivots


def test_confirmed_bearish_adx_divergence() -> None:
    df, pivots = _indicator_frame_with_manual_pivots()
    cfg = RegimeScannerConfig(divergence_min_swing_separation=5, divergence_indicator_epsilon=0.25)
    result = detect_bearish_divergence(df, pivots, indicator="adx", config=cfg)
    assert result.status == "confirmed_bearish_divergence"
    assert result.first_indicator_value == 55.0
    assert result.second_indicator_value == 35.0


def test_confirmed_bullish_adx_divergence() -> None:
    n = 40
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.linspace(20, 10, n)
    low = close - 1
    low[10] = 5.0
    low[25] = 3.0
    adx = np.full(n, 40.0)
    adx[10] = 50.0
    adx[25] = 30.0
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close + 1,
            "low": low,
            "close": close,
            "volume": 1.0,
            "adx": adx,
            "minus_di": adx,
            "di_spread": -adx,
            "plus_di": np.full(n, 5.0),
        }
    )
    pivots = [
        ConfirmedPivot(10, ts[10].isoformat(), 13, ts[13].isoformat(), 5.0, "low"),
        ConfirmedPivot(25, ts[25].isoformat(), 28, ts[28].isoformat(), 3.0, "low"),
    ]
    cfg = RegimeScannerConfig(divergence_min_swing_separation=5)
    result = detect_bullish_divergence(df, pivots, indicator="adx", config=cfg)
    assert result.status == "confirmed_bullish_divergence"


def test_no_divergence_when_second_high_is_lower() -> None:
    df, pivots = _indicator_frame_with_manual_pivots()
    pivots = [
        pivots[0],
        ConfirmedPivot(
            pivots[1].pivot_index,
            pivots[1].pivot_timestamp,
            pivots[1].confirmation_index,
            pivots[1].confirmation_timestamp,
            price=25.0,
            pivot_type="high",
        ),
    ]
    result = detect_bearish_divergence(df, pivots, indicator="adx")
    assert result.status == "no_confirmed_divergence"


def test_insufficient_swings() -> None:
    df, pivots = _indicator_frame_with_manual_pivots()
    result = detect_bearish_divergence(df, pivots[:1], indicator="adx")
    assert result.status == "insufficient_confirmed_swings"


def test_indicator_epsilon_blocks_small_drop() -> None:
    df, pivots = _indicator_frame_with_manual_pivots()
    df.loc[25, "adx"] = 54.9  # drop of 0.1 only
    cfg = RegimeScannerConfig(divergence_indicator_epsilon=0.5)
    result = detect_bearish_divergence(df, pivots, indicator="adx", config=cfg)
    assert result.status == "no_confirmed_divergence"


def test_min_separation_blocks_close_swings() -> None:
    df, pivots = _indicator_frame_with_manual_pivots()
    close_pivots = [
        pivots[0],
        ConfirmedPivot(12, pivots[0].pivot_timestamp, 15, pivots[0].confirmation_timestamp, 36.0, "high"),
    ]
    cfg = RegimeScannerConfig(divergence_min_swing_separation=10)
    result = detect_bearish_divergence(df, close_pivots, indicator="adx", config=cfg)
    assert result.status == "insufficient_confirmed_swings"


def test_unconfirmed_second_pivot_not_used() -> None:
    # Build real pivots; truncate before second confirmation.
    highs = [1] * 3 + [5] + [2] * 3 + [1] * 3 + [8] + [3] * 2  # second high lacks right=3
    start = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i, h in enumerate(highs):
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": h - 0.5,
                "high": float(h),
                "low": float(h) - 1.0,
                "close": h - 0.5,
                "volume": 1.0,
                "adx": 50.0 - i,
                "plus_di": 50.0 - i,
                "minus_di": 5.0,
                "di_spread": 40.0 - i,
            }
        )
    df = pd.DataFrame(rows)
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3, divergence_min_swing_separation=5)
    pivots = find_confirmed_pivots(df, config=cfg)
    highs_p = [p for p in pivots if p.pivot_type == "high"]
    assert len(highs_p) == 1  # second not confirmed
    result = detect_confirmed_divergence_for_indicator(df, pivots, indicator="adx", config=cfg)
    assert result.status == "insufficient_confirmed_swings"
