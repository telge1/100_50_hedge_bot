"""Tests for liquidation event horizon / TP-SL backtest helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_backtest import (
    BacktestConfig,
    apply_cost,
    assign_sample,
    evaluate_horizon_trade,
    evaluate_tp_sl_trade,
    in_sample_cut,
    long_return_pct,
    path_mfe_mae_long,
    path_mfe_mae_short,
    run_control_comparison,
    short_return_pct,
    build_horizon_trades,
)
from research.liquidation_level.liquidation_features import SignalEvent


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _ohlcv(n: int = 20) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        o = px
        h = px + 1.0
        l = px - 1.0
        c = px + 0.2
        rows.append({"timestamp": _ts(i), "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
        px = c
    return pd.DataFrame(rows)


def test_long_short_returns() -> None:
    assert long_return_pct(100, 101) == pytest.approx(1.0)
    assert short_return_pct(100, 99) == pytest.approx((100 / 99 - 1.0) * 100.0)


def test_mfe_mae_long_short() -> None:
    highs = np.array([101.0, 103.0])
    lows = np.array([99.0, 98.0])
    mfe, mae, _, _ = path_mfe_mae_long(100.0, highs, lows)
    assert mfe == pytest.approx(3.0)
    assert mae == pytest.approx(-2.0)
    mfe_s, mae_s, _, _ = path_mfe_mae_short(100.0, highs, lows)
    assert mfe_s == pytest.approx((100 / 98 - 1) * 100)
    assert mae_s == pytest.approx((100 / 103 - 1) * 100)


def test_cost_model() -> None:
    assert apply_cost(1.0, 0.12) == pytest.approx(0.88)


def test_horizon_entry_open_exit_close() -> None:
    df = _ohlcv(10)
    opens = df.open.to_numpy()
    highs = df.high.to_numpy()
    lows = df.low.to_numpy()
    closes = df.close.to_numpy()
    ev = evaluate_horizon_trade(
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_index=2,
        direction="long",
        horizon=3,
        roundtrip_cost_pct=0.12,
    )
    assert ev is not None
    assert ev["entry_price"] == pytest.approx(float(opens[2]))
    assert ev["exit_close"] == pytest.approx(float(closes[4]))
    assert ev["bars_held"] == 3
    assert ev["net_return_pct"] == pytest.approx(ev["gross_return_pct"] - 0.12)


def test_no_lookahead_horizon_requires_future_bars() -> None:
    df = _ohlcv(5)
    ev = evaluate_horizon_trade(
        opens=df.open.to_numpy(),
        highs=df.high.to_numpy(),
        lows=df.low.to_numpy(),
        closes=df.close.to_numpy(),
        entry_index=3,
        direction="long",
        horizon=5,
        roundtrip_cost_pct=0.12,
    )
    assert ev is None


def test_tp_sl_conservative_sl_first() -> None:
    # one bar hits both TP and SL
    opens = np.array([100.0, 100.0])
    highs = np.array([100.0, 102.0])
    lows = np.array([100.0, 98.0])
    closes = np.array([100.0, 101.0])
    ev = evaluate_tp_sl_trade(
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_index=1,
        direction="long",
        tp_pct=1.0,
        sl_pct=1.0,
        max_hold=3,
        roundtrip_cost_pct=0.12,
    )
    assert ev is not None
    assert ev["exit_reason"] == "sl"
    assert ev["exit_price"] == pytest.approx(99.0)


def test_timeout_and_end_of_data() -> None:
    opens = np.array([100.0, 100.0, 100.0])
    highs = np.array([100.2, 100.2, 100.2])
    lows = np.array([99.8, 99.8, 99.8])
    closes = np.array([100.1, 100.1, 100.05])
    to = evaluate_tp_sl_trade(
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_index=0,
        direction="long",
        tp_pct=5.0,
        sl_pct=5.0,
        max_hold=2,
        roundtrip_cost_pct=0.12,
    )
    assert to is not None
    assert to["exit_reason"] == "timeout"
    assert to["bars_held"] == 2

    eod = evaluate_tp_sl_trade(
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_index=1,
        direction="long",
        tp_pct=5.0,
        sl_pct=5.0,
        max_hold=5,
        roundtrip_cost_pct=0.12,
    )
    assert eod is not None
    assert eod["exit_reason"] == "end_of_data"


def test_split_70_30() -> None:
    n = 100
    cut = in_sample_cut(n, 0.70)
    assert cut == 70
    assert assign_sample(69, n) == "in_sample"
    assert assign_sample(70, n) == "out_of_sample"


def test_controls_deterministic() -> None:
    from research.liquidation_level.liquidation_backtest import HorizonTrade

    df = _ohlcv(50)
    trades = []
    for i, entry in enumerate([5, 10, 15, 20]):
        trades.append(
            HorizonTrade(
                trade_id=f"t{i}",
                signal_id=f"s{i}",
                variant="L1",
                direction="long",
                sample=assign_sample(entry - 1, len(df)),
                signal_index=entry - 1,
                entry_index=entry,
                horizon=3,
                entry_price=float(df.iloc[entry]["open"]),
                exit_close=float(df.iloc[entry + 2]["close"]),
                gross_return_pct=1.0,
                net_return_pct=0.88,
                maximum_favorable_excursion_pct=2.0,
                maximum_adverse_excursion_pct=-1.0,
                maximum_high=1.0,
                minimum_low=1.0,
                bars_held=3,
                complete_horizon=True,
            )
        )
    cfg = BacktestConfig(control_runs=20, random_seed=42, horizons=(3,))
    a = run_control_comparison(trades, df, cfg)
    b = run_control_comparison(trades, df, cfg)
    assert a == b


def test_horizon_trades_from_signals_no_entry_on_signal_bar() -> None:
    df = _ohlcv(30)
    sig = SignalEvent(
        signal_id="L1_1",
        variant="L1",
        direction="long",
        signal_index=5,
        signal_timestamp=pd.Timestamp(_ts(5)),
        entry_index=6,
        entry_timestamp=pd.Timestamp(_ts(6)),
        source_event_id="c",
        side="lower",
        close_location_value=0.7,
        sweep_body_pct=20.0,
        upper_wick_pct=10.0,
        lower_wick_pct=30.0,
        swept_level_count=1,
        swept_total_strength=1,
    )
    trades = build_horizon_trades([sig], df, BacktestConfig(horizons=(1, 3), skip_tp_sl=True))
    assert trades
    assert all(t.entry_index == 6 for t in trades)
    assert all(t.signal_index == 5 for t in trades)


def test_deterministic_replay_features_backtest_smoke() -> None:
    from research.liquidation_level.liquidation_features import build_feature_bundle
    from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels
    from research.liquidation_level.liquidation_backtest import run_backtest

    rows = []
    for i in range(40):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.6,
                "low": 99.4,
                "close": 100.1,
                "volume": 100.0 if i < 12 else (400.0 if i in {12, 25} else 80.0),
            }
        )
    rows[13]["high"] = 110.0
    rows[13]["low"] = 90.0
    rows[26]["high"] = 110.0
    rows[26]["low"] = 90.0
    df = pd.DataFrame(rows)
    r1 = replay_liquidation_levels(df, LiquidationLevelConfig())
    r2 = replay_liquidation_levels(df, LiquidationLevelConfig())
    assert [x.level_id for x in r1.all_levels] == [x.level_id for x in r2.all_levels]
    f1 = build_feature_bundle(r1, df)
    f2 = build_feature_bundle(r2, df)
    assert [s.signal_id for s in f1.signals] == [s.signal_id for s in f2.signals]
    b1 = run_backtest(f1, BacktestConfig(skip_tp_sl=True, control_runs=5, horizons=(1, 3, 6)))
    b2 = run_backtest(f2, BacktestConfig(skip_tp_sl=True, control_runs=5, horizons=(1, 3, 6)))
    assert [t.gross_return_pct for t in b1.horizon_trades] == [t.gross_return_pct for t in b2.horizon_trades]
