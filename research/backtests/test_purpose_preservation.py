"""Purpose preservation tests for backtest order/fill logs."""

from __future__ import annotations

from datetime import datetime, timezone

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.backtest_report import build_fill_log_entry, build_order_log_entry
from research.backtests.debug_report import active_order_to_dict
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.purpose_utils import (
    is_cycle_purpose,
    is_recovery_reload_purpose,
    preserve_bot_purpose,
)
from research.backtests.simulated_execution import fill_order_at_price, process_candle_fills
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def test_purpose_helper_patterns() -> None:
    assert is_cycle_purpose("CYCLE_1_LONG_ADD") is True
    assert is_cycle_purpose("CYCLE_1_SHORT_REDUCE") is True
    assert is_cycle_purpose("LONG_TP_EXIT") is False
    assert is_recovery_reload_purpose("RECOVERY_RELOAD_LONG_ENTRY") is True
    assert is_recovery_reload_purpose("RECOVERY_RELOAD_SHORT_ENTRY") is True
    assert is_recovery_reload_purpose("CYCLE_2_LONG_ADD") is False


def test_order_purpose_preservation_on_submit() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT")
    runtime_state = RuntimeState(strategy_state={})

    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            order_type="Limit",
            price=1.0,
            reduce_only=True,
            metadata={"cycle_index": 1},
        ),
        replace=False,
    )
    book.sync_runtime_state(runtime_state)

    assert book.active_orders()[0].purpose == "CYCLE_1_LONG_ADD"
    assert book.active_orders()[0].metadata["purpose_original"] == "CYCLE_1_LONG_ADD"

    log_entry = build_order_log_entry(
        order,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        candle_index=1,
        event_type="submitted",
    )
    assert log_entry["purpose"] == "CYCLE_1_LONG_ADD"
    assert log_entry["purpose_original"] == "CYCLE_1_LONG_ADD"
    assert log_entry["cycle_index"] == 1


def test_fill_purpose_preservation_on_reduce() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.short_qty = 1.0
    book.short_avg = 1.0

    book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_1_SHORT_REDUCE",
            order_type="Market",
            trigger_price=0.95,
            reduce_only=True,
            metadata={"cycle_index": 1, "cycle_role": "short_reduce"},
        ),
        replace=False,
    )

    candle = SyntheticCandle(symbol="APTUSDT", open=1.0, high=1.0, low=0.94, close=0.98)
    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)
    assert len(fills) == 1

    fill = fills[0]
    assert fill.purpose == "CYCLE_1_SHORT_REDUCE"
    assert fill.metadata["purpose"] == "CYCLE_1_SHORT_REDUCE"
    assert fill.metadata["purpose_original"] == "CYCLE_1_SHORT_REDUCE"
    assert fill.metadata["cycle_index"] == 1
    assert fill.metadata["cycle_role"] == "short_reduce"

    fill_log_entry = build_fill_log_entry(fill, book, candle=candle, candle_index=2)
    assert fill_log_entry["purpose"] == "CYCLE_1_SHORT_REDUCE"
    assert fill_log_entry["purpose_original"] == "CYCLE_1_SHORT_REDUCE"
    assert fill_log_entry["cycle_index"] == 1
    assert fill_log_entry["cycle_role"] == "short_reduce"


def test_debug_final_active_order_preserves_original_purpose() -> None:
    result = run_historical_backtest(
        "BTCUSDT",
        "long",
        [
            SyntheticCandle(
                symbol="BTCUSDT",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for _ in range(4)
        ],
        max_candles=2,
    )
    assert result.final_active_orders
    active = result.final_active_orders[0]
    assert active["purpose"] == active["purpose_original"]
    assert active["purpose"] in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
    assert result.final_active_order_purposes[0] == active["purpose"]


def test_fill_order_at_price_keeps_purpose_original() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.long_qty = 1.0
    book.long_avg = 1.0
    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_2_LONG_ADD",
            order_type="Market",
            trigger_price=1.05,
            reduce_only=True,
        ),
        replace=False,
    )
    fill = fill_order_at_price(
        book=book,
        runtime_state=runtime_state,
        order_id=order.order_id,
        fill_price=1.05,
    )
    assert preserve_bot_purpose(fill.purpose) == "CYCLE_2_LONG_ADD"
    assert fill.metadata["purpose_original"] == "CYCLE_2_LONG_ADD"
    assert fill.metadata.get("cycle_index") == 2


def test_active_order_to_dict_exposes_purpose_fields() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT")
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="RECOVERY_RELOAD_SHORT_ENTRY",
            order_type="Limit",
            price=0.9,
        ),
        replace=False,
    )
    payload = active_order_to_dict(order)
    assert payload["purpose"] == "RECOVERY_RELOAD_SHORT_ENTRY"
    assert payload["purpose_original"] == "RECOVERY_RELOAD_SHORT_ENTRY"
    assert is_recovery_reload_purpose(payload["purpose"])
