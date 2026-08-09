"""Tests for cashout + reimbursement simulation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis.simulate import (
    simulate,
)


def _df(nets, reasons=None):
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    rows = []
    for i, n in enumerate(nets, start=1):
        rows.append(
            {
                "trade_id": i,
                "symbol": "APTUSDT",
                "side": "LONG",
                "first_signal_tf": "15m",
                "exit_reason": (reasons or ["TP" if n > 0 else "SL"] * len(nets))[i - 1],
                "net_return_pct": n,
                "entry_time": t0 + pd.Timedelta(hours=i),
                "exit_time": t0 + pd.Timedelta(hours=i, minutes=30),
            }
        )
    return pd.DataFrame(rows)


def test_full_reimbursement_when_reserve_sufficient():
    # +10% on 1000 = +100, cashout 50% → active 1050, reserve 50
    # then -2% on 1050 = -21; reimburse min(21,50)=21 → active 1050, reserve 29
    df = _df([10.0, -2.0])
    res = simulate(df, cashout_rate=0.50, coverage_rate=1.0, start_active=1000.0)
    p = res["path"]
    assert abs(p.iloc[0]["reserve_after"] - 50.0) < 1e-9
    assert abs(p.iloc[1]["reimbursement_amount"] - 21.0) < 1e-9
    assert abs(p.iloc[1]["active_after"] - 1050.0) < 1e-9
    assert abs(p.iloc[1]["reserve_after"] - 29.0) < 1e-9
    assert bool(p.iloc[1]["loss_fully_covered"])


def test_partial_reimbursement():
    # win +10% cashout 10% → pnl=100, cashout=10, active=1090, reserve=10
    # loss -2% on 1090 = -21.8; reimburse 10 → active=1078.2, reserve=0
    df = _df([10.0, -2.0])
    res = simulate(df, cashout_rate=0.10, coverage_rate=1.0, start_active=1000.0)
    p = res["path"].iloc[1]
    assert abs(p["reimbursement_amount"] - 10.0) < 1e-9
    assert abs(p["reserve_after"]) < 1e-9
    assert bool(p["loss_partially_covered"])


def test_transfer_preserves_total_wealth_delta_equals_pnl():
    df = _df([10.0, -5.0, 8.0, -3.0])
    res = simulate(df, cashout_rate=0.30, coverage_rate=1.0)
    p = res["path"]
    for _, r in p.iterrows():
        assert abs((r["total_after"] - r["total_before"]) - r["raw_trade_pnl"]) < 1e-6


def test_reserve_never_negative_cashout_only_on_wins():
    df = _df([5.0, -1.0, -1.0, 5.0])
    res = simulate(df, cashout_rate=0.40, coverage_rate=1.0)
    p = res["path"]
    assert (p["reserve_after"] >= -1e-12).all()
    assert (p.loc[p["raw_trade_pnl"] <= 0, "cashout_amount"].abs() < 1e-12).all()
    assert (p.loc[p["raw_trade_pnl"] >= 0, "reimbursement_amount"].abs() < 1e-12).all()


def test_zero_cashout_baseline():
    df = _df([10.0, -5.0, 2.0])
    res = simulate(df, cashout_rate=0.0, coverage_rate=1.0, start_active=1000.0)
    # 1000*1.1=1100; *0.95=1045; *1.02=1065.9
    assert abs(res["summary"]["end_active"] - 1065.9) < 1e-6
    assert abs(res["summary"]["end_reserve"]) < 1e-12


def test_coverage_rate_half():
    # +10% cashout 50% → reserve 50, active 1050
    # -2% loss 21; coverage 50% wants 10.5 → reimburse 10.5
    df = _df([10.0, -2.0])
    res = simulate(df, cashout_rate=0.50, coverage_rate=0.50, start_active=1000.0)
    p = res["path"].iloc[1]
    assert abs(p["reimbursement_amount"] - 10.5) < 1e-9
    assert abs(p["active_after"] - (1050 - 21 + 10.5)) < 1e-9


def test_order_and_returns_unchanged():
    df = _df([1.0, -1.0, 2.0])
    res = simulate(df, cashout_rate=0.2, coverage_rate=1.0)
    assert list(res["path"]["trade_id"]) == [1, 2, 3]
    assert list(res["path"]["net_return_pct"]) == [1.0, -1.0, 2.0]


def test_sl_only_mode_skips_timeout_loss():
    df = _df([-1.0, -1.0], reasons=["TIMEOUT", "SL"])
    # first build tiny reserve
    df2 = pd.concat(
        [_df([10.0]), df],
        ignore_index=True,
    )
    df2["trade_id"] = range(1, len(df2) + 1)
    # fix times monotonic
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    df2["entry_time"] = [t0 + pd.Timedelta(hours=i) for i in range(len(df2))]
    df2["exit_time"] = [t0 + pd.Timedelta(hours=i, minutes=30) for i in range(len(df2))]
    res = simulate(df2, cashout_rate=0.5, coverage_rate=1.0, reimburse_mode="SL_ONLY")
    # trade 2 TIMEOUT should have 0 reimburse; trade 3 SL may reimburse
    assert abs(res["path"].iloc[1]["reimbursement_amount"]) < 1e-12
    assert res["path"].iloc[2]["reimbursement_amount"] >= 0
