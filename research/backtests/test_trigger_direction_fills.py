"""Phase-8.5 trigger_direction fill semantics tests."""

from __future__ import annotations

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.simulated_execution import (
    evaluate_order_touch,
    process_candle_fills,
    should_fill_order_on_candle,
)
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def _candle(*, high: float, low: float) -> SyntheticCandle:
    return SyntheticCandle(symbol="BTCUSDT", open=100.0, high=high, low=low, close=100.0)


def _submit(book: SimulatedOrderBook, intent: StrategyIntent) -> None:
    book.submit_intent(intent, replace=False)


def test_rise_trigger_buy_does_not_fill_on_low_only() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=100.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    assert not should_fill_order_on_candle(order, _candle(high=90.0, low=50.0))
    assert should_fill_order_on_candle(order, _candle(high=101.0, low=50.0))


def test_rise_trigger_sell_fills_on_high() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=100.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    assert should_fill_order_on_candle(order, _candle(high=101.0, low=90.0))
    touch = evaluate_order_touch(order, _candle(high=101.0, low=90.0))
    assert touch.trigger_touch_rule == "high>=trigger"


def test_fall_trigger_buy_fills_on_low() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_1_SHORT_REDUCE",
            order_type="Market",
            trigger_price=100.0,
            trigger_direction=2,
            reduce_only=True,
        ),
        replace=False,
    )
    assert should_fill_order_on_candle(order, _candle(high=120.0, low=99.0))
    touch = evaluate_order_touch(order, _candle(high=120.0, low=99.0))
    assert touch.trigger_touch_rule == "low<=trigger"


def test_fall_trigger_sell_does_not_fill_on_high_only() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_SL_EXIT",
            order_type="Market",
            trigger_price=100.0,
            trigger_direction=-1,
            reduce_only=True,
        ),
        replace=False,
    )
    assert not should_fill_order_on_candle(order, _candle(high=120.0, low=101.0))
    assert should_fill_order_on_candle(order, _candle(high=120.0, low=99.0))


def test_limit_buy_and_sell_without_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    buy_order, _ = book.submit_intent(
        StrategyIntent(side="long", qty=1.0, purpose="TEST_BUY", order_type="Limit", price=100.0),
        replace=False,
    )
    sell_order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Limit",
            price=100.0,
            reduce_only=True,
        ),
        replace=False,
    )
    assert should_fill_order_on_candle(buy_order, _candle(high=110.0, low=99.0))
    assert not should_fill_order_on_candle(buy_order, _candle(high=110.0, low=101.0))
    assert should_fill_order_on_candle(sell_order, _candle(high=101.0, low=90.0))
    assert not should_fill_order_on_candle(sell_order, _candle(high=99.0, low=90.0))


def test_missing_trigger_direction_does_not_fill_and_warns() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=100.0,
            reduce_only=True,
        ),
        replace=False,
    )
    candle = _candle(high=101.0, low=50.0)
    touch = evaluate_order_touch(order, candle)
    assert touch.touched is False
    assert touch.trigger_warning == "missing_trigger_direction"
    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)
    assert fills == []


def test_apt_regression_no_premature_short_exit_fill() -> None:
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name

    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    candles = load_candles_for_symbol("APTUSDT", limit=1000)
    result = run_historical_backtest("APTUSDT", "long", candles, max_candles=999)

    exit_fills = [
        fill
        for fill in result.fill_log
        if fill.get("purpose") in {"SHORT_SL_EXIT", "SHORT_TP_EXIT", "LONG_SL_EXIT", "LONG_TP_EXIT"}
    ]
    assert not exit_fills, f"unexpected premature exit fills: {exit_fills}"
    assert result.fills_count == 2
    assert result.active_orders_count == 2
    purposes = set(result.final_active_order_purposes)
    assert purposes == {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
