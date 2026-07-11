"""Slope / band / overextension analysis tests."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.indicators import compute_indicator_frame, ema
from research.regime_scanner.trend_analysis import (
    analyze_ema_bands,
    analyze_overextension,
    classify_slope_change,
    slope_comparison_for_series,
)


def test_slope_weakening_while_still_positive() -> None:
    assert classify_slope_change(0.8, 2.0, epsilon=0.05) == "weakening"


def test_slope_strengthening() -> None:
    assert classify_slope_change(2.0, 0.8, epsilon=0.05) == "strengthening"


def test_slope_more_negative_is_weakening() -> None:
    # Algebraic change: -3 - (-1) = -2 => weakening (becoming more negative).
    assert classify_slope_change(-3.0, -1.0, epsilon=0.05) == "weakening"


def test_slope_deadband_stable() -> None:
    assert classify_slope_change(1.02, 1.00, epsilon=0.05) == "stable"


def test_slope_unavailable_history() -> None:
    series = pd.Series([1.0, 1.1, 1.2])
    result = slope_comparison_for_series(series, window=3, epsilon=0.05)
    assert result["status"] == "unavailable"
    assert result["previous_slope"] is None


def test_bullish_expanding_and_contracting_bands() -> None:
    cfg = RegimeScannerConfig(
        ema_periods=(9, 20, 59, 200),
        slope_windows=(3,),
        band_windows=(3,),
        band_change_epsilon=0.01,
    )
    n = 250
    # Expanding then later we only need endpoint; craft wide vs narrow via EMA levels directly.
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.linspace(100, 120, n)
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1.0,
            "ema_9": close + 4,
            "ema_20": close + 1,
            "ema_59": close - 1,
            "ema_200": close - 5,
        }
    )
    # Make abs gap 3 bars ago smaller than now => expanding.
    df.loc[n - 1 - 3, ["ema_9", "ema_20"]] = [close[n - 1 - 3] + 2, close[n - 1 - 3] + 1]
    bands = analyze_ema_bands(df, config=cfg)
    pair = bands["ema_9_vs_ema_20"]
    assert pair["orientation"] == "bullish"
    assert pair["windows"]["3"]["status"] == "expanding"

    # Contracting: shrink current gap.
    df2 = df.copy()
    df2.loc[n - 1, ["ema_9", "ema_20"]] = [close[-1] + 1.1, close[-1] + 1.0]
    df2.loc[n - 1 - 3, ["ema_9", "ema_20"]] = [close[n - 1 - 3] + 4, close[n - 1 - 3] + 1]
    bands2 = analyze_ema_bands(df2, config=cfg)
    assert bands2["ema_9_vs_ema_20"]["orientation"] == "bullish"
    assert bands2["ema_9_vs_ema_20"]["windows"]["3"]["status"] == "contracting"


def test_bearish_band_and_flat_epsilon() -> None:
    cfg = RegimeScannerConfig(band_windows=(3,), band_orientation_epsilon=1e-6)
    n = 30
    close = np.full(n, 100.0)
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
            "ema_9": close - 2,
            "ema_20": close + 1,
            "ema_59": close + 2,
            "ema_200": close + 3,
        }
    )
    bands = analyze_ema_bands(df, config=cfg)
    assert bands["ema_9_vs_ema_20"]["orientation"] == "bearish"

    df_flat = df.copy()
    df_flat["ema_9"] = close
    df_flat["ema_20"] = close
    assert analyze_ema_bands(df_flat, config=cfg)["ema_9_vs_ema_20"]["orientation"] == "flat"


def test_band_no_infinities_on_zero_close() -> None:
    cfg = RegimeScannerConfig(band_windows=(3,))
    n = 20
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
            "volume": 1.0,
            "ema_9": 1.0,
            "ema_20": 0.5,
            "ema_59": 0.2,
            "ema_200": 0.1,
        }
    )
    bands = analyze_ema_bands(df, config=cfg)
    for pair in bands.values():
        assert pair["current_signed_pct"] is None or math.isfinite(pair["current_signed_pct"])
        for win in pair["windows"].values():
            for key in ("current_abs_pct", "previous_abs_pct", "abs_change_pp", "rel_change_pct"):
                val = win[key]
                assert val is None or math.isfinite(val)


def test_overextension_atr_units() -> None:
    cfg = RegimeScannerConfig(atr_pct_mean_windows=(12,))
    n = 30
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "close": np.full(n, 100.0),
            "ema_9": np.full(n, 98.0),
            "ema_20": np.full(n, 97.0),
            "ema_59": np.full(n, 95.0),
            "ema_200": np.full(n, 90.0),
            "atr": np.full(n, 2.0),
            "atr_pct": np.concatenate([np.full(n - 1, 1.0), np.array([2.0])]),
        }
    )
    out = analyze_overextension(df, config=cfg)
    assert out["close_vs_ema_atr_units"]["ema_9"] == pytest.approx(1.0)
    assert out["atr_pct_vs_means"]["12"]["label"] == "above_recent_volatility"
