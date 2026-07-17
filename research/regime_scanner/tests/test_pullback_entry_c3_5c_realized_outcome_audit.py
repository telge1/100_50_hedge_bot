"""Tests for C3.5c realized outcome audit."""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    DEFAULT_OUT,
    EXIT_B_DOC,
    _mfe_mae,
    _ret_pct,
    trades_exit_a_opposite_entry,
    trades_exit_c_horizon,
)


def test_output_and_exit_b_docs() -> None:
    assert "c35c_realized_outcome_audit" in str(DEFAULT_OUT)
    assert "reset_after_entry" in EXIT_B_DOC["sm_post_entry"]


def test_pnl_long_short_mirror() -> None:
    assert abs(_ret_pct(1, 100.0, 101.0) - 1.0) < 1e-12
    assert abs(_ret_pct(-1, 100.0, 99.0) - (100 / 99 - 1) * 100) < 1e-12


def test_opposite_entry_ignores_same_side() -> None:
    # bars 0..5
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=6, freq="15min", tz="UTC"),
            "open": [100.0, 100.0, 101.0, 102.0, 99.0, 98.0],
            "high": [101] * 6,
            "low": [99] * 6,
            "close": [100.0, 100.5, 101.5, 101.0, 98.5, 97.5],
            "symbol": ["T"] * 6,
        }
    )
    filled = [
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 1,
            "trigger_bar": 0,
            "fill_bar": 1,
            "trigger_timestamp": frame.iloc[0]["timestamp"],
            "fill_timestamp": frame.iloc[1]["timestamp"],
            "entry_price": 100.0,
        },
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 2,
            "trigger_bar": 2,
            "fill_bar": 3,
            "trigger_timestamp": frame.iloc[2]["timestamp"],
            "fill_timestamp": frame.iloc[3]["timestamp"],
            "entry_price": 102.0,
        },
        {
            "side": -1,
            "side_name": "short",
            "setup_id": 3,
            "trigger_bar": 3,
            "fill_bar": 4,
            "trigger_timestamp": frame.iloc[3]["timestamp"],
            "fill_timestamp": frame.iloc[4]["timestamp"],
            "entry_price": 99.0,
        },
    ]
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    # First long exits at short fill 99; same-side long#2 skipped as exit
    assert len(trades) >= 1
    t0 = trades.iloc[0]
    assert t0["side"] == "long"
    assert t0["exit_price"] == 99.0
    assert bool(t0["closed"]) is True


def test_horizon_uses_close_not_best() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=10, freq="15min", tz="UTC"),
            "open": [100.0] * 10,
            "high": [110.0] * 10,
            "low": [90.0] * 10,
            "close": [100 + i for i in range(10)],
            "symbol": ["T"] * 10,
        }
    )
    filled = [
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 1,
            "trigger_bar": 0,
            "fill_bar": 0,
            "trigger_timestamp": frame.iloc[0]["timestamp"],
            "fill_timestamp": frame.iloc[0]["timestamp"],
            "entry_price": 100.0,
        }
    ]
    # monkey: horizon_bars_for_tf(15m, 1h)=4 bars → end index fill+3
    from research.regime_scanner import pullback_entry_c3_5c_realized_outcome_audit as mod

    old_h, old_l = mod.HORIZON_HOURS, mod.HORIZON_LABELS
    mod.HORIZON_HOURS = (1.0,)
    mod.HORIZON_LABELS = ("1h",)
    try:
        trades = trades_exit_c_horizon(frame, filled, timeframe="15m", variant="A6")
    finally:
        mod.HORIZON_HOURS, mod.HORIZON_LABELS = old_h, old_l
    assert len(trades) == 1
    # 4 bars from fill 0 → indices 0..3, close at 3 = 103
    assert trades.iloc[0]["exit_price"] == 103.0
    assert trades.iloc[0]["exit_reason"] == "horizon_close_1h"


def test_mfe_mae_long() -> None:
    highs = np.array([101.0, 103.0, 102.0])
    lows = np.array([99.0, 98.0, 100.0])
    mfe, mae = _mfe_mae(1, 100.0, highs, lows, 0, 2)
    assert abs(mfe - 3.0) < 1e-9
    assert abs(mae - (-2.0)) < 1e-9


def test_no_lookahead() -> None:
    import research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit as mod

    src = inspect.getsource(mod)
    assert "lookahead_on" not in src
    assert "shift(-" not in src


def test_open_at_end_excluded_from_closed() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=4, freq="15min", tz="UTC"),
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [105] * 4,
            "low": [95] * 4,
            "close": [100.5, 101.5, 102.5, 103.5],
            "symbol": ["T"] * 4,
        }
    )
    filled = [
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 1,
            "trigger_bar": 0,
            "fill_bar": 1,
            "trigger_timestamp": frame.iloc[0]["timestamp"],
            "fill_timestamp": frame.iloc[1]["timestamp"],
            "entry_price": 101.0,
        }
    ]
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    assert len(trades) == 1
    assert bool(trades.iloc[0]["open_at_end"]) is True
    assert bool(trades.iloc[0]["closed"]) is False
    assert trades.iloc[0]["exit_price"] == 103.5


def test_costs_subtracted() -> None:
    assert abs(_ret_pct(1, 100.0, 101.0) - 1.0) < 1e-12
    # net columns tested via exit A builder
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=3, freq="15min", tz="UTC"),
            "open": [100.0, 100.0, 99.0],
            "high": [101] * 3,
            "low": [98] * 3,
            "close": [100.0, 100.0, 99.0],
            "symbol": ["T"] * 3,
        }
    )
    filled = [
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 1,
            "trigger_bar": 0,
            "fill_bar": 0,
            "trigger_timestamp": frame.iloc[0]["timestamp"],
            "fill_timestamp": frame.iloc[0]["timestamp"],
            "entry_price": 100.0,
        },
        {
            "side": -1,
            "side_name": "short",
            "setup_id": 2,
            "trigger_bar": 1,
            "fill_bar": 2,
            "trigger_timestamp": frame.iloc[1]["timestamp"],
            "fill_timestamp": frame.iloc[2]["timestamp"],
            "entry_price": 99.0,
        },
    ]
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    g = float(trades.iloc[0]["gross_return_pct"])
    assert abs(float(trades.iloc[0]["net_return_0_10_pct"]) - (g - 0.10)) < 1e-12
    assert abs(float(trades.iloc[0]["net_return_0_20_pct"]) - (g - 0.20)) < 1e-12
