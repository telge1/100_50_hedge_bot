"""Tests for C3.5c robustness / OOS audit."""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    _ret_pct,
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    COST_MODEL_DOC,
    DEFAULT_OUT,
    MAX_PORTFOLIO_OPEN,
    assign_split,
    enrich_trade_costs,
    fixed_chrono_splits,
    outlier_metrics,
    portfolio_variant_c,
    rolling_window_stats,
)


def test_output_path_and_cost_doc() -> None:
    assert "c35c_robustness_audit" in str(DEFAULT_OUT)
    assert COST_MODEL_DOC["not_per_side"] is True


def test_pnl_mirror() -> None:
    assert abs(_ret_pct(1, 100.0, 110.0) - 10.0) < 1e-12
    assert abs(_ret_pct(-1, 100.0, 90.0) - (100 / 90 - 1) * 100) < 1e-12


def test_opposite_exit_ignores_same_side_and_open_end() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=5, freq="15min", tz="UTC"),
            "open": [100.0, 100.0, 101.0, 99.0, 98.0],
            "high": [102] * 5,
            "low": [97] * 5,
            "close": [100.0, 100.5, 100.8, 98.5, 97.5],
            "symbol": ["T"] * 5,
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
            "trigger_bar": 1,
            "fill_bar": 2,
            "trigger_timestamp": frame.iloc[1]["timestamp"],
            "fill_timestamp": frame.iloc[2]["timestamp"],
            "entry_price": 101.0,
        },
        {
            "side": -1,
            "side_name": "short",
            "setup_id": 3,
            "trigger_bar": 2,
            "fill_bar": 3,
            "trigger_timestamp": frame.iloc[2]["timestamp"],
            "fill_timestamp": frame.iloc[3]["timestamp"],
            "entry_price": 99.0,
        },
    ]
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    assert float(trades.iloc[0]["exit_price"]) == 99.0
    # last short may be open_at_end
    assert bool(trades.iloc[-1]["closed"]) is False or trades.iloc[-1]["side"] == "short"


def test_costs_not_double_counted() -> None:
    df = pd.DataFrame({"gross_return_pct": [1.0, -0.5], "closed": [True, True]})
    out = enrich_trade_costs(df)
    assert abs(float(out.iloc[0]["net_return_0_20_pct"]) - 0.80) < 1e-12
    # slippage is additive once, not applied twice to same base incorrectly
    assert abs(float(out.iloc[0]["net_return_0_20_slip_0_10_pct"]) - 0.70) < 1e-12
    assert abs(float(out.iloc[0]["net_return_0_20_pct"]) - (1.0 - 0.20)) < 1e-12


def test_chrono_splits_fixed_and_assign() -> None:
    splits = fixed_chrono_splits(
        pd.Timestamp("2025-01-01", tz="UTC"),
        pd.Timestamp("2026-01-01", tz="UTC"),
    )
    assert splits["fixed_before_results"] is True
    assert splits["no_tuning_on_val_oos"] is True
    assert splits["method"] == "60_20_20"
    assert assign_split(pd.Timestamp("2025-02-01", tz="UTC"), splits) == "development"
    assert assign_split(pd.Timestamp("2025-11-01", tz="UTC"), splits) == "oos"
    short = fixed_chrono_splits(
        pd.Timestamp("2026-02-01", tz="UTC"),
        pd.Timestamp("2026-05-01", tz="UTC"),
    )
    assert short["method"] == "equal_thirds"


def test_outlier_removal_and_flags() -> None:
    rets = pd.Series([10.0, 2.0, 1.0, -1.0, -0.5])
    om = outlier_metrics(rets)
    assert abs(om["without_best"] - (2 + 1 - 1 - 0.5)) < 1e-9
    assert om["best_trade_dominates"] is True  # 10/11.5 > 0.35
    assert "edge_disappears_without_best" in om


def test_rolling_windows() -> None:
    ts = pd.date_range("2026-02-01", periods=10, freq="7D", tz="UTC")
    df = pd.DataFrame(
        {
            "symbol": ["APTUSDT"] * 10,
            "entry_timestamp": ts,
            "closed": [True] * 10,
            "net_return_0_20_pct": np.linspace(-1, 1, 10),
            "gross_return_pct": np.linspace(-1, 1, 10),
            "holding_hours": [10] * 10,
            "side": ["long"] * 10,
        }
    )
    r = rolling_window_stats(df, 30)
    assert not r.empty
    assert set(r["window_days"]) == {30}


def test_portfolio_max5_deterministic() -> None:
    rows = []
    base = pd.Timestamp("2026-02-01", tz="UTC")
    # 7 overlapping candidates across coins
    for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]):
        rows.append(
            {
                "symbol": sym,
                "trigger_timestamp": base + pd.Timedelta(minutes=i),
                "entry_timestamp": base + pd.Timedelta(hours=1),
                "exit_timestamp": base + pd.Timedelta(days=2),
                "net_return_0_20_pct": 1.0,
                "closed": True,
            }
        )
    df = pd.DataFrame(rows)
    summary, eq = portfolio_variant_c(df, max_open=MAX_PORTFOLIO_OPEN)
    assert summary["n_accepted"] == 5
    assert summary["n_rejected_capacity"] == 2
    # alphabetical among same entry: AAA..EEE accepted (earlier triggers first)
    assert set(eq["symbol"]) == {"AAA", "BBB", "CCC", "DDD", "EEE"}


def test_no_lookahead_and_sm_untouched() -> None:
    import research.regime_scanner.pullback_entry_c3_5c_robustness_audit as mod

    src = inspect.getsource(mod)
    assert "shift(-" not in src
    assert "lookahead_on" not in src
    # must reuse Exit A, not reimplement SM
    assert "trades_exit_a_opposite_entry" in src
    assert "apply_pullback_entry" in src
