"""Unit tests for EMA / Wilder indicators."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.indicators import (
    atr_wilder,
    compute_indicator_frame,
    directional_moves,
    ema,
    start_end_slope_pct,
    true_range,
    wilder_rma,
)


def _manual_ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    prev: float | None = None
    for value in values:
        if prev is None:
            prev = float(value)
        else:
            prev = alpha * float(value) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def test_ema_matches_manual_adjust_false() -> None:
    closes = [10.0, 11.0, 12.0, 11.5, 13.0, 12.5, 14.0]
    series = pd.Series(closes)
    actual = ema(series, 3)
    expected = _manual_ema(closes, 3)
    np.testing.assert_allclose(actual.to_numpy(), expected, rtol=0, atol=1e-12)


def test_ema_warmup_is_defined_from_first_bar_with_adjust_false() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    values = ema(series, 3)
    assert values.notna().all()
    assert values.iloc[0] == pytest.approx(1.0)


def test_true_range_and_directional_moves_reference() -> None:
    high = pd.Series([10.0, 12.0, 11.0, 13.0])
    low = pd.Series([9.0, 10.0, 9.5, 11.0])
    close = pd.Series([9.5, 11.0, 10.0, 12.5])

    tr = true_range(high, low, close)
    # first TR uses only high-low because prev close is NaN -> max ignores NaN pairs? 
    # pandas max of [1.0, NaN, NaN] = 1.0
    assert tr.iloc[0] == pytest.approx(1.0)
    assert tr.iloc[1] == pytest.approx(max(2.0, abs(12.0 - 9.5), abs(10.0 - 9.5)))
    assert tr.iloc[2] == pytest.approx(max(1.5, abs(11.0 - 11.0), abs(9.5 - 11.0)))

    plus_dm, minus_dm = directional_moves(high, low)
    # bar1: up=2, down=-0.5? down_move = -(10-9)= -1? Wait: down_move = -low.diff() = -(10-9) = -1
    # Actually down_move = -low.diff(); low.diff()[1]=1 => down_move=-1. up_move=2.
    # plus when up>down and up>0: 2 > -1 and 2>0 -> plus=2, minus=0
    assert plus_dm.iloc[1] == pytest.approx(2.0)
    assert minus_dm.iloc[1] == pytest.approx(0.0)
    # bar2: up=-1, down=-(9.5-10)=0.5; down>up and down>0 -> minus=0.5, plus=0
    assert plus_dm.iloc[2] == pytest.approx(0.0)
    assert minus_dm.iloc[2] == pytest.approx(0.5)


def test_wilder_rma_manual_steps() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    actual = wilder_rma(values, 3)
    # alpha=1/3; seed first value
    expected = []
    prev = None
    alpha = 1.0 / 3.0
    for value in values:
        if prev is None:
            prev = float(value)
        else:
            prev = alpha * float(value) + (1.0 - alpha) * prev
        expected.append(prev)
    np.testing.assert_allclose(actual.to_numpy(), expected, rtol=0, atol=1e-12)


def test_compute_indicator_frame_no_infinities_and_nan_on_zero_div() -> None:
    cfg = RegimeScannerConfig(
        ema_periods=(3, 5),
        slope_windows=(2, 3),
        atr_period=3,
        adx_period=3,
    )
    # Constant prices -> TR eventually 0 after first bar path; slopes may be 0/const
    n = 20
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "open": np.full(n, 100.0),
            "high": np.full(n, 100.0),
            "low": np.full(n, 100.0),
            "close": np.full(n, 100.0),
            "volume": np.full(n, 1.0),
        }
    )
    out = compute_indicator_frame(df, config=cfg)
    numeric = out.select_dtypes(include=[np.number])
    assert np.isfinite(numeric.to_numpy(dtype=float)).sum() >= 0  # no crash
    assert not np.isinf(numeric.to_numpy(dtype=float)).any()

    # Explicit zero-division path for slopes
    slopes = start_end_slope_pct(pd.Series([0.0, 0.0, 1.0, 2.0]), 1)
    assert math.isnan(float(slopes.iloc[1]))
    assert not np.isinf(slopes.to_numpy(dtype=float)).any()


def test_atr_optional_talib_comparison() -> None:
    talib = pytest.importorskip("talib", reason="TA-Lib optional reference only")
    rng = np.random.default_rng(42)
    n = 250
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    high = close + rng.uniform(0.1, 0.8, n)
    low = close - rng.uniform(0.1, 0.8, n)
    our = atr_wilder(pd.Series(high), pd.Series(low), pd.Series(close), 14)
    ref = talib.ATR(high.astype(float), low.astype(float), close.astype(float), timeperiod=14)
    # After warmup, Wilder ewm(alpha=1/14) tracks TA-Lib closely; early seed may differ.
    mask = np.isfinite(ref) & our.notna().to_numpy()
    # Compare the last 100 bars with a modest absolute tolerance.
    np.testing.assert_allclose(
        our.to_numpy()[mask][-100:],
        ref[mask][-100:],
        rtol=1e-6,
        atol=1e-6,
    )


def test_adx_optional_talib_comparison_with_seed_tolerance() -> None:
    talib = pytest.importorskip("talib", reason="TA-Lib optional reference only")
    cfg = RegimeScannerConfig(ema_periods=(9,), slope_windows=(3,), atr_period=14, adx_period=14)
    rng = np.random.default_rng(7)
    n = 400
    close = 50 + np.cumsum(rng.normal(0, 0.2, n))
    high = close + rng.uniform(0.05, 0.5, n)
    low = close - rng.uniform(0.05, 0.5, n)
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 10.0),
        }
    )
    out = compute_indicator_frame(df, config=cfg)
    ref = talib.ADX(high.astype(float), low.astype(float), close.astype(float), timeperiod=14)
    # Seed differences can remain visible; require the late-series correlation/closeness.
    ours = out["adx"].to_numpy(dtype=float)
    valid = np.isfinite(ours) & np.isfinite(ref)
    late = valid.copy()
    late[:200] = False
    abs_err = np.abs(ours[late] - ref[late])
    assert float(np.nanmedian(abs_err)) < 1.0
    assert float(np.nanmax(abs_err)) < 5.0
