"""Tests for pull vs consumption helpers."""

from __future__ import annotations

from research.orderbook.historical_break_pull_consumption.classify import classify_mechanism
from research.orderbook.historical_break_pull_consumption.trades import (
    Trade,
    aggressor_side_for_direction,
    filter_trades_causal,
    trade_ts_seconds_to_ms,
    wall_book_side,
)
from research.orderbook.historical_break_pull_consumption.walls import (
    WallSnapshot,
    build_actions_from_snaps,
    classify_action,
    match_aggressive_trades,
)


def test_side_mapping_bearish_bullish() -> None:
    assert aggressor_side_for_direction("bearish") == "Sell"
    assert aggressor_side_for_direction("bullish") == "Buy"
    assert wall_book_side("bearish") == "bid"
    assert wall_book_side("bullish") == "ask"


def test_seconds_to_ms() -> None:
    ms = trade_ts_seconds_to_ms("1767657602.5801")
    assert abs(ms - 1767657602580) <= 1


def test_causal_trade_cutoff() -> None:
    trades = [
        Trade(1000, "Sell", 1.0, 1.0, "a"),
        Trade(2000, "Sell", 1.0, 1.0, "b"),
        Trade(3000, "Buy", 1.0, 1.0, "c"),
    ]
    kept = filter_trades_causal(trades, asof_ms=2000)
    assert [t.trade_id for t in kept] == ["a", "b"]


def test_wall_decrease_and_delete() -> None:
    a = WallSnapshot(0, 1.0, 10.0, 10.0, 1.0, 1.1, 1.05, 0.0)
    b = WallSnapshot(100, 1.0, 4.0, 4.0, 1.0, 1.1, 1.05, 0.0)
    c = WallSnapshot(200, None, 0.0, 0.0, 0.99, 1.1, 1.045, -10.0)
    assert classify_action(prev=a, cur=b) == "DECREASE"
    assert classify_action(prev=b, cur=c) == "DELETE"


def test_refill_detection() -> None:
    a = WallSnapshot(0, 1.0, 2.0, 2.0, 1.0, 1.1, 1.05, 0.0)
    b = WallSnapshot(100, 1.0, 8.0, 8.0, 1.0, 1.1, 1.05, 0.0)
    assert classify_action(prev=a, cur=b) == "INCREASE"
    z = WallSnapshot(0, None, 0.0, 0.0, 1.0, 1.1, 1.05, 0.0)
    r = WallSnapshot(50, 1.0, 5.0, 5.0, 1.0, 1.1, 1.05, 0.0)
    assert classify_action(prev=z, cur=r) == "REAPPEAR"


def test_trade_to_wall_price_and_time_match() -> None:
    trades = [
        Trade(1000, "Sell", 1.000, 5.0, "1"),
        Trade(1100, "Sell", 1.050, 5.0, "2"),  # far price
        Trade(5000, "Sell", 1.000, 5.0, "3"),  # far time
        Trade(1200, "Buy", 1.000, 5.0, "4"),  # wrong side
    ]
    qty, n, matched = match_aggressive_trades(
        trades,
        action_ts_ms=1000,
        aggressor_side="Sell",
        ref_price=1.0,
        match_time_ms=500,
        match_price_bps=10.0,
    )
    assert n == 1 and qty == 5.0 and matched[0].trade_id == "1"


def test_zero_trade_pull_case() -> None:
    snaps = [
        WallSnapshot(0, 1.0, 100.0, 100.0, 1.0, 1.01, 1.005, 0.0),
        WallSnapshot(1000, 1.0, 10.0, 10.0, 1.0, 1.01, 1.005, 0.0),
    ]
    actions = build_actions_from_snaps(
        "e", snaps, level=1.0, trades=[], aggressor_side="Sell"
    )
    assert actions[0].action == "DECREASE"
    assert actions[0].matched_aggressive_qty == 0.0
    assert actions[0].mechanism_hint == "PULLISH"


def test_heavy_trade_consumption_case() -> None:
    snaps = [
        WallSnapshot(0, 1.0, 100.0, 100.0, 1.0, 1.01, 1.005, 0.0),
        WallSnapshot(1000, 1.0, 20.0, 20.0, 1.0, 1.01, 1.005, 0.0),
    ]
    trades = [Trade(1000, "Sell", 1.0, 80.0, "x")]
    actions = build_actions_from_snaps(
        "e", snaps, level=1.0, trades=trades, aggressor_side="Sell"
    )
    assert actions[0].consumption_ratio is not None
    assert actions[0].consumption_ratio >= 0.7
    assert actions[0].mechanism_hint == "CONSUMPTIONISH"


def test_refill_absorption_classification() -> None:
    snaps = [
        WallSnapshot(t, 1.0, 50.0, 50.0, 1.0, 1.01, 1.005, 0.0)
        for t in range(0, 60_000, 1000)
    ]
    # slight dip then refill
    snaps[50] = WallSnapshot(50_000, 1.0, 20.0, 20.0, 1.0, 1.01, 1.005, 0.0)
    snaps[55] = WallSnapshot(55_000, 1.0, 55.0, 55.0, 1.0, 1.01, 1.005, 0.0)
    actions = build_actions_from_snaps(
        "e",
        snaps,
        level=1.0,
        trades=[Trade(50_000, "Sell", 1.0, 40.0, "a")],
        aggressor_side="Sell",
    )
    mech = classify_mechanism(
        actions=actions,
        snaps=snaps,
        break_ms=60_000,
        aggressive_qty_pre_break=40.0,
        peak_wall_qty=55.0,
        beyond_at_break=False,
        beyond_at_60s=False,
        prior_ob_class="REFILL_THEN_RECLAIM",
    )
    assert mech["mechanism_class"] in {"REFILL_ABSORPTION", "MIXED_PULL_CONSUMPTION", "CONSUMPTION_DOMINANT"}


def test_no_future_trade_in_match_window_beyond_tol() -> None:
    trades = [Trade(10_000, "Sell", 1.0, 9.0, "future")]
    qty, n, _ = match_aggressive_trades(
        trades,
        action_ts_ms=1000,
        aggressor_side="Sell",
        ref_price=1.0,
        match_time_ms=500,
    )
    assert n == 0 and qty == 0.0
