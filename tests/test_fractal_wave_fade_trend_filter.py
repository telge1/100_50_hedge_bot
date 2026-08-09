"""Unit tests for trend-bucket H4 mapping."""

from __future__ import annotations

import pandas as pd

from orderbook_analyse.fractal_wave_fade_trend_filter.analysis import assign_trend_bucket


def test_h4_trend_aligned_mapping() -> None:
    df = pd.DataFrame(
        {
            "direction": ["UP", "UP", "DOWN", "DOWN", "UP"],
            "ema_context": ["EMA_BULL", "EMA_BEAR", "EMA_BEAR", "EMA_BULL", "MIXED"],
        }
    )
    b = assign_trend_bucket(df)
    assert list(b) == [
        "TREND_ALIGNED",
        "COUNTERTREND",
        "TREND_ALIGNED",
        "COUNTERTREND",
        "MIXED",
    ]
