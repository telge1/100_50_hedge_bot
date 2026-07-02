"""Immediate REFILL_LONG/REFILL_SHORT market fill handling in backtest simulator."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import (
    fill_entry_intents_at_candle_close,
    is_immediate_market_fill,
    is_immediate_refill_market_fill,
)
from research.backtests.simulated_order_book import ACTIVE_ORDER_STATUSES, SyntheticCandle


def _candle(symbol: str, *, close: float) -> SyntheticCandle:
    return SyntheticCandle(
        symbol=symbol,
        open=close,
        high=close,
        low=close,
        close=close,
        timestamp=datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("REFILL_LONG", True),
        ("REFILL_SHORT", True),
        ("INITIAL_LONG_ENTRY", False),
        ("INITIAL_SHORT_ENTRY", False),
        ("CYCLE_1_LONG_ADD", False),
    ],
)
def test_is_immediate_refill_market_fill(purpose: str, expected: bool) -> None:
    intent = StrategyIntent(
        side="long" if "LONG" in purpose else "short",
        qty=1.0,
        purpose=purpose,
        order_type="Market",
        reduce_only=False,
    )
    assert is_immediate_refill_market_fill(intent) is expected


def test_is_immediate_market_fill_includes_initial_and_refill() -> None:
    initial = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="INITIAL_LONG_ENTRY",
        order_type="Market",
    )
    refill = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="REFILL_LONG",
        order_type="Market",
    )
    cycle = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="CYCLE_1_LONG_ADD",
        order_type="Limit",
        price=99.0,
    )
    assert is_immediate_market_fill(initial) is True
    assert is_immediate_market_fill(refill) is True
    assert is_immediate_market_fill(cycle) is False


def test_fill_entry_intents_still_only_fills_initial_entries() -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    try:
        intents = [
            StrategyIntent(
                side="long",
                qty=2.0,
                purpose="INITIAL_LONG_ENTRY",
                order_type="Market",
            ),
            StrategyIntent(
                side="long",
                qty=3.0,
                purpose="REFILL_LONG",
                order_type="Market",
                metadata={"trade_block_id": "tb-refill"},
            ),
        ]
        filled_pairs = fill_entry_intents_at_candle_close(
            book=sim.book,
            runtime_state=sim.runtime_state,
            intents=intents,
            candle=sim.candle,
        )
        assert len(filled_pairs) == 1
        assert filled_pairs[0][1].purpose == "INITIAL_LONG_ENTRY"
        refill_orders = sim.book.active_orders_by_purpose("REFILL_LONG")
        assert len(refill_orders) == 1
        assert refill_orders[0].status in ACTIVE_ORDER_STATUSES
    finally:
        sim.close()


@pytest.mark.parametrize("purpose", ["REFILL_LONG", "REFILL_SHORT"])
def test_submit_refill_market_intent_fills_immediately(purpose: str) -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    side = "long" if purpose == "REFILL_LONG" else "short"
    try:
        sim.book.long_qty = 50.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 25.0
        sim.book.short_avg = 100.0
        qty_before = sim.book.long_qty if side == "long" else sim.book.short_qty

        intent = StrategyIntent(
            side=side,
            qty=5.0,
            purpose=purpose,
            order_type="Market",
            reduce_only=False,
            metadata={"trade_block_id": "tb-123", "refill_batch_id": "batch-1"},
        )
        resting = sim.submit_intents_to_book([intent], event_source="test_refill")

        assert resting == []
        assert sim.book.active_orders_by_purpose(purpose) == []
        qty_after = sim.book.long_qty if side == "long" else sim.book.short_qty
        assert qty_after == pytest.approx(qty_before + 5.0)
        filled_events = [
            entry
            for entry in sim.order_log
            if entry.get("purpose") == purpose and entry.get("event_type") == "filled"
        ]
        assert len(filled_events) == 1
        filled = filled_events[0]
        assert filled.get("status") == "FILLED"
        # Trade-block export analyzers expect REFILL filled rows to carry price,
        # fill_price and position-after fields.
        for key in (
            "price",
            "fill_price",
            "long_qty_after",
            "long_avg_after",
            "short_qty_after",
            "short_avg_after",
        ):
            assert filled.get(key) not in ("", None)
    finally:
        sim.close()


def test_strategy_on_fill_called_for_both_refill_legs() -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    try:
        sim.book.long_qty = 50.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 25.0
        sim.book.short_avg = 100.0

        seen_purposes: list[str] = []
        original_on_fill = sim.strategy.on_fill

        def _capture_on_fill(fill_event, snapshot, runtime_state, context):
            seen_purposes.append(str(fill_event.purpose))
            return original_on_fill(fill_event, snapshot, runtime_state, context)

        sim.strategy.on_fill = mock.Mock(side_effect=_capture_on_fill)

        sim.submit_intents_to_book(
            [
                StrategyIntent(
                    side="long",
                    qty=2.0,
                    purpose="REFILL_LONG",
                    order_type="Market",
                ),
                StrategyIntent(
                    side="short",
                    qty=1.0,
                    purpose="REFILL_SHORT",
                    order_type="Market",
                ),
            ],
            event_source="test_refill_on_fill",
        )

        assert seen_purposes == ["REFILL_LONG", "REFILL_SHORT"]
        assert sim.strategy.on_fill.call_count == 2
    finally:
        sim.close()


def test_refill_registry_marked_submitted_when_batch_present() -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    try:
        state = sim.runtime_state.strategy_state
        state["refill_batch_id"] = "batch-abc"
        state["refill_intent_registry"] = {
            "REFILL_LONG": {
                "status": "REQUESTED",
                "qty": 4.0,
                "refill_batch_id": "batch-abc",
            }
        }
        intent = StrategyIntent(
            side="long",
            qty=4.0,
            purpose="REFILL_LONG",
            order_type="Market",
        )
        order, _ = sim.book.submit_intent(intent, replace=False)
        sim._mark_refill_registry_submitted(intent, order)

        entry = state["refill_intent_registry"]["REFILL_LONG"]
        assert entry["status"] == "SUBMITTED"
        assert entry.get("client_order_id") == order.order_id
    finally:
        sim.close()
