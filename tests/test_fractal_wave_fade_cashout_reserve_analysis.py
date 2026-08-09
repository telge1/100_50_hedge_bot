"""Unit tests for cashout/reserve analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.simulate import simulate_cashout
from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.streaks import (
    find_streaks,
    losing_predicate,
    sl_predicate,
)


def _toy_trades():
    # +10%, -5%, +10%  (net_return_pct)
    rows = []
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    nets = [10.0, -5.0, 10.0]
    reasons = ["TP", "SL", "TP"]
    for i, (n, r) in enumerate(zip(nets, reasons), start=1):
        rows.append(
            {
                "trade_id": i,
                "symbol": "APTUSDT",
                "side": "LONG",
                "first_signal_tf": "15m",
                "exit_reason": r,
                "net_return_pct": n,
                "entry_time": t0 + pd.Timedelta(hours=i),
                "exit_time": t0 + pd.Timedelta(hours=i, minutes=30),
            }
        )
    return pd.DataFrame(rows)


def test_reserve_never_decreases_and_cashout_only_on_profit():
    df = _toy_trades()
    res = simulate_cashout(df, 0.30, start_active=1000.0)
    path = res["path"]
    # reserves non-decreasing
    assert (path["reserve_after"] >= path["reserve_before"] - 1e-12).all()
    # loss trade cashout == 0
    loss = path[path["net_return_pct"] < 0].iloc[0]
    assert abs(loss["cashout"]) < 1e-12
    assert abs(loss["reserve_after"] - loss["reserve_before"]) < 1e-12
    # win cashout
    win = path[path["net_return_pct"] > 0].iloc[0]
    assert win["cashout"] > 0
    assert abs(win["cashout"] - win["trade_pnl"] * 0.30) < 1e-9


def test_zero_cashout_full_compound():
    df = _toy_trades()
    res = simulate_cashout(df, 0.0, start_active=1000.0)
    # 1000 * 1.10 = 1100; *0.95 = 1045; *1.10 = 1149.5
    assert abs(res["summary"]["end_active"] - 1149.5) < 1e-6
    assert abs(res["summary"]["end_reserve"]) < 1e-12
    assert abs(res["summary"]["end_total_wealth"] - 1149.5) < 1e-6


def test_total_wealth_equals_active_plus_reserve():
    df = _toy_trades()
    res = simulate_cashout(df, 0.20)
    p = res["path"]
    assert np.allclose(p["total_after"], p["active_after"] + p["reserve_after"])


def test_sl_streak_vs_losing_streak():
    # SL, SL, TIMEOUT(loss), SL  → max SL streak 2; losing may be longer
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    rows = []
    for i, (reason, net) in enumerate(
        [("SL", -1.0), ("SL", -1.0), ("TIMEOUT", -0.5), ("SL", -1.0)], start=1
    ):
        rows.append(
            {
                "trade_id": i,
                "symbol": "DOGEUSDT",
                "side": "SHORT",
                "first_signal_tf": "15m",
                "exit_reason": reason,
                "net_return_pct": net,
                "entry_time": t0 + pd.Timedelta(hours=i),
                "exit_time": t0 + pd.Timedelta(hours=i, minutes=10),
            }
        )
    df = pd.DataFrame(rows)
    sl = find_streaks(df, predicate=sl_predicate, label="SL")
    lose = find_streaks(df, predicate=losing_predicate, label="LOSING")
    assert sl["max_length"] == 2
    assert lose["max_length"] == 4


def test_trade_order_preserved():
    df = _toy_trades()
    res = simulate_cashout(df, 0.1)
    assert list(res["path"]["trade_id"]) == [1, 2, 3]
    assert list(res["path"]["net_return_pct"]) == [10.0, -5.0, 10.0]
