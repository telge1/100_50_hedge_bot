"""Unit tests for directional control flags / joins (no MySQL)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_directional_control.flags import (
    cci_bucket,
    inefficient_down_in_bull,
    inefficient_up_in_bear,
)
from orderbook_analyse.fractal_directional_control.load_join import (
    asof_last_completed,
    attach_next_opposite_wave,
)


def test_asof_no_future_leak() -> None:
    parent = pd.DataFrame(
        {
            "end_available_at": pd.to_datetime(
                ["2024-01-01", "2024-01-10", "2024-01-20"], utc=True
            ),
            "direction": ["UP", "DOWN", "UP"],
        }
    )
    times = pd.to_datetime(["2024-01-09", "2024-01-10", "2024-01-21"], utc=True).to_numpy(
        dtype="datetime64[ns]"
    )
    out = asof_last_completed(parent, times, ["direction"], "h4")
    assert list(out["h4_direction"]) == ["UP", "DOWN", "UP"]
    assert list(out["h4_available"]) == [True, True, True]


def test_next_opposite_wave() -> None:
    df = pd.DataFrame(
        {
            "direction": ["UP", "DOWN", "UP"],
            "end_available_at": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03"], utc=True
            ),
            "signed_price_move_pct": [1.0, 2.0, 3.0],
            "favorable_move_pct": [1.0, 2.0, 3.0],
            "adverse_move_pct": [-0.1, -0.2, -0.3],
            "directional_efficiency": [0.1, 0.2, 0.3],
            "price_move_pct": [1.0, -2.0, 3.0],
        }
    )
    out = attach_next_opposite_wave(df)
    assert out.loc[0, "next_opp_direction"] == "DOWN"
    assert out.loc[0, "next_opp_signed_price_move_pct"] == 2.0
    assert out.loc[1, "next_opp_direction"] == "UP"


def test_control_flags_and_cci_bucket() -> None:
    df = pd.DataFrame(
        {
            "direction": ["DOWN", "UP"],
            "rsi_end_gt_50": [True, False],
            "rsi_end_lt_50": [False, True],
            "ema9_vs_ema20_end": ["BULL", "BEAR"],
            "price_vs_ema20_end": ["ABOVE", "BELOW"],
            "signed_price_move_pct": [-0.5, -0.5],
            "price_move_pct": [0.5, -0.5],
            "d1_direction": ["UP", "DOWN"],
            "d1_rsi_end_gt_50": [True, False],
            "d1_ema9_vs_ema20_end": ["BULL", "BEAR"],
            "h4_direction": ["UP", "DOWN"],
            "h1_direction": ["FLAT", "FLAT"],
            "h4_ema9_vs_ema20_end": ["BULL", "BEAR"],
            "h4_price_vs_ema20_end": ["ABOVE", "BELOW"],
            "h1_ema9_vs_ema20_end": ["BULL", "BEAR"],
            "h1_price_vs_ema20_end": ["ABOVE", "BELOW"],
        }
    )
    assert bool(inefficient_down_in_bull(df).iloc[0])
    assert bool(inefficient_up_in_bear(df).iloc[1])
    b = cci_bucket(pd.Series([50.0, 120.0, 175.0, 250.0, 400.0]))
    assert list(b) == ["lt100", "100_150", "150_200", "200_300", "gt300"]
