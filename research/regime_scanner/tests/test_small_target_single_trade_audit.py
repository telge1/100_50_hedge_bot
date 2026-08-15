"""Tests for small-target single-trade audit."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.small_target_single_trade.config import EXIT_COMBOS, TP_VALUES, SL_VALUES, matrix_rows
from research.regime_scanner.small_target_single_trade.outcomes import (
    evaluate_outcome_params,
    short_tp_sl_prices,
)
from research.regime_scanner.small_target_single_trade.sequential import apply_sequential


def test_exit_matrix_exactly_12():
    assert len(TP_VALUES) == 4
    assert len(SL_VALUES) == 3
    assert len(EXIT_COMBOS) == 12
    assert len(matrix_rows()) == 12


def test_short_tp_sl_prices():
    tp, sl = short_tp_sl_prices(100.0, 0.50, 1.00)
    assert abs(tp - 99.5) < 1e-12
    assert abs(sl - 101.0) < 1e-12


def test_same_bar_conservative_sl():
    # one bar touches both +0.5% fav (low) and -0.5% adv (high) for short
    entry = 100.0
    highs = np.array([100.6])  # adverse for short (+0.6%)
    lows = np.array([99.4])  # favorable for short (+0.6%)
    closes = np.array([100.0])
    ts = [pd.Timestamp("2026-01-01", tz="UTC")]
    out = evaluate_outcome_params(
        side=-1,
        entry=entry,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        fill_i=0,
        n_bars=1,
        tp_pct=0.50,
        sl_pct=-0.50,
        horizon_bars=1,
        cost_pct=0.20,
    )
    assert out["same_bar_ambiguous"] is True
    assert out["exit_reason"] == "same_bar_conservative_sl"
    assert abs(out["gross_pnl_pct"] - (-0.50)) < 1e-12
    assert abs(out["net_pnl_pct"] - (-0.70)) < 1e-12


def test_tp_hit_short():
    entry = 100.0
    highs = np.array([100.1, 100.2])
    lows = np.array([99.9, 99.4])  # bar1 hits +0.6% fav
    closes = np.array([100.0, 99.5])
    ts = [pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01 00:15", tz="UTC")]
    out = evaluate_outcome_params(
        side=-1,
        entry=entry,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        fill_i=0,
        n_bars=2,
        tp_pct=0.50,
        sl_pct=-1.00,
        horizon_bars=8,
        cost_pct=0.20,
    )
    assert out["exit_reason"] == "TP"
    assert out["bars_to_tp"] == 1
    assert abs(out["net_pnl_pct"] - 0.30) < 1e-12


def test_sequential_skips_while_open():
    rows = []
    for i, held in enumerate([4, 1, 2]):
        rows.append(
            {
                "strategy_source": "a6_short",
                "symbol": "APTUSDT",
                "tp_pct": 0.5,
                "sl_pct": -0.75,
                "horizon_bars": 192,
                "effective_cost_pct": 0.20,
                "fill_timestamp": pd.Timestamp("2026-03-01", tz="UTC") + pd.Timedelta(minutes=15 * i * 2),
                "bars_held": held,
                "net_pnl_pct": 0.1,
            }
        )
    # second fill at +30m while first held 4 bars (60m) → should skip
    df = pd.DataFrame(rows)
    # adjust: fill0 t0 held4; fill1 at t0+30m; fill2 at t0+90m after exit
    df.loc[0, "fill_timestamp"] = pd.Timestamp("2026-03-01T00:00:00+00:00")
    df.loc[0, "bars_held"] = 4
    df.loc[1, "fill_timestamp"] = pd.Timestamp("2026-03-01T00:30:00+00:00")
    df.loc[1, "bars_held"] = 1
    df.loc[2, "fill_timestamp"] = pd.Timestamp("2026-03-01T01:30:00+00:00")
    df.loc[2, "bars_held"] = 1
    out = apply_sequential(df)
    assert bool(out.iloc[0]["taken_sequential"]) is True
    assert bool(out.iloc[1]["taken_sequential"]) is False
    assert bool(out.iloc[2]["taken_sequential"]) is True


def test_no_extra_combos():
    allowed = {(0.25, 0.50), (0.25, 0.75), (0.25, 1.00), (0.50, 0.50), (0.50, 0.75), (0.50, 1.00),
               (0.75, 0.50), (0.75, 0.75), (0.75, 1.00), (1.00, 0.50), (1.00, 0.75), (1.00, 1.00)}
    assert set(EXIT_COMBOS) == allowed
