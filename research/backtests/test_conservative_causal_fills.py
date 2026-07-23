"""Causal OHLC eligibility for conservative fills (no same-candle follow fills)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import (
    is_deferred_cycle_second_leg_market_order,
    is_immediate_cycle_second_leg_market_fill,
    is_immediate_market_fill,
    is_immediate_refill_market_fill,
    order_is_fill_eligible_on_candle,
    process_candle_fills,
    stamp_order_causal_eligibility,
)
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def _candle(
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    ts: datetime | None = None,
) -> SyntheticCandle:
    return SyntheticCandle(
        symbol="BTCUSDT",
        open=open_,
        high=high,
        low=low,
        close=close,
        timestamp=ts or datetime(2026, 1, 14, 5, 25, tzinfo=timezone.utc),
    )


def test_new_follow_order_waits_until_next_candle() -> None:
    """Long fill in candle X; newly created short reduce fills only from X+1."""
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    try:
        sim.book.long_qty = 50.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 25.0
        sim.book.short_avg = 100.0
        sim.candle_index = 0
        sim.candle = _candle(open_=100.0, high=100.0, low=95.0, close=96.0)

        long_add, _ = sim.book.submit_intent(
            StrategyIntent(
                side="long",
                qty=10.0,
                purpose="CYCLE_3_LONG_ADD",
                order_type="Market",
                trigger_price=98.0,
                trigger_direction=2,
                reduce_only=True,
            ),
            replace=False,
        )
        stamp_order_causal_eligibility(long_add, created_candle_index=0, eligible_from_candle_index=0)
        sim.book.sync_runtime_state(sim.runtime_state)

        result_x = sim.process_candle(sim.candle, fill_model="conservative", max_fills_per_candle=1)
        assert any(f.purpose == "CYCLE_3_LONG_ADD" for f in result_x.candle_fills)

        # Follow-up short reduce created in candle X (as strategy would).
        resting = sim.submit_intents_to_book(
            [
                StrategyIntent(
                    side="short",
                    qty=5.0,
                    purpose="CYCLE_3_SHORT_REDUCE",
                    order_type="Market",
                    reduce_only=True,
                )
            ],
            event_source="after_fill",
        )
        assert len(resting) == 1
        assert resting[0].eligible_from_candle_index == 1
        assert is_deferred_cycle_second_leg_market_order(resting[0])
        assert sim.book.short_qty == pytest.approx(25.0)

        # Candle X again would still be ineligible (eligible_from=1).
        assert not order_is_fill_eligible_on_candle(resting[0], 0)

        sim.candle_index = 1
        candle_x1 = _candle(
            open_=96.0,
            high=97.0,
            low=94.0,
            close=95.0,
            ts=datetime(2026, 1, 14, 5, 30, tzinfo=timezone.utc),
        )
        result_x1 = sim.process_candle(candle_x1, fill_model="conservative")
        assert any(f.purpose == "CYCLE_3_SHORT_REDUCE" for f in result_x1.candle_fills)
        assert sim.book.active_orders_by_purpose("CYCLE_3_SHORT_REDUCE") == []
    finally:
        sim.close()


def test_pre_existing_orders_can_both_fill_same_candle_when_model_allows() -> None:
    """Two orders open before candle X may both fill (no blanket one-fill rule)."""
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.long_qty = 1.0
    book.long_avg = 100.0
    book.short_qty = 0.5
    book.short_avg = 100.0

    long_tp, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=101.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    short_sl, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=99.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    stamp_order_causal_eligibility(long_tp, created_candle_index=0, eligible_from_candle_index=1)
    stamp_order_causal_eligibility(short_sl, created_candle_index=0, eligible_from_candle_index=1)
    book.current_candle_index = 1
    eligible = list(book.active_orders())
    candle = _candle(open_=100.0, high=102.0, low=98.0, close=100.0)

    fills, stats = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="conservative",
        max_fills_per_candle=1,
        candle_index=1,
    )
    assert len(fills) == 2
    assert stats["paired_exit_fills_count"] == 2
    assert {f.purpose for f in fills} == {"LONG_TP_EXIT", "SHORT_SL_EXIT"}


def test_cancel_replace_new_trigger_not_eligible_same_candle() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.long_qty = 10.0
    book.long_avg = 100.0

    old, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=10.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=105.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    stamp_order_causal_eligibility(old, created_candle_index=0, eligible_from_candle_index=0)
    book.current_candle_index = 5

    # Replace in candle 5 with a trigger that the same candle would touch.
    new, replaced = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=10.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=99.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=True,
    )
    assert replaced
    stamp_order_causal_eligibility(new, created_candle_index=5)
    assert new.eligible_from_candle_index == 6
    assert not order_is_fill_eligible_on_candle(new, 5)

    candle = _candle(open_=100.0, high=101.0, low=98.0, close=100.0)
    # Snapshot still listing old id should not fill the replaced order.
    fills, _ = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=[old, new],
        fill_model="conservative",
        max_fills_per_candle=2,
        candle_index=5,
    )
    assert fills == []
    assert book.active_orders_by_purpose("LONG_TP_EXIT")
    assert book.active_orders_by_purpose("LONG_TP_EXIT")[0].order_id == new.order_id


def test_refill_market_remains_immediate_same_timestamp() -> None:
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="REFILL_LONG",
        order_type="Market",
    )
    assert is_immediate_refill_market_fill(intent) is True
    assert is_immediate_market_fill(intent) is True

    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle(open_=100.0, high=100.0, low=100.0, close=100.0)
    try:
        sim.book.long_qty = 50.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 25.0
        sim.book.short_avg = 100.0
        resting = sim.submit_intents_to_book(
            [intent],
            event_source="after_fill",
        )
        assert resting == []
        assert sim.book.long_qty == pytest.approx(51.0)
    finally:
        sim.close()


def test_cycle_second_leg_market_is_deferred_not_immediate() -> None:
    intent = StrategyIntent(
        side="short",
        qty=1.0,
        purpose="CYCLE_3_SHORT_REDUCE",
        order_type="Market",
        reduce_only=True,
    )
    assert is_immediate_cycle_second_leg_market_fill(intent) is True
    assert is_immediate_market_fill(intent) is False


def test_apt_trade12_cycle3_and_cycle5_no_same_candle_follow_fill() -> None:
    """Regression fixture around A12 Cycle 3 / Cycle 5 timestamps."""
    sim = HedgeBotOriginalSimulator(signal="long", symbol="APTUSDT", candle_close=1.96)
    try:
        sim.book.long_qty = 50.308
        sim.book.long_avg = 1.9660444879542023
        sim.book.short_qty = 25.153
        sim.book.short_avg = 1.966072162366318

        # Pre-existing CYCLE_3_LONG_ADD eligible on candle 0.
        long_add, _ = sim.book.submit_intent(
            StrategyIntent(
                side="long",
                qty=12.577,
                purpose="CYCLE_3_LONG_ADD",
                order_type="Market",
                trigger_price=1.9581,
                trigger_direction=2,
                reduce_only=True,
            ),
            replace=False,
        )
        stamp_order_causal_eligibility(long_add, created_candle_index=0, eligible_from_candle_index=0)
        sim.book.sync_runtime_state(sim.runtime_state)

        candle_c3 = _candle(
            open_=1.96,
            high=1.96,
            low=1.9386,
            close=1.9386,
            ts=datetime(2026, 1, 14, 5, 25, tzinfo=timezone.utc),
        )
        sim.candle_index = 0
        result = sim.process_candle(candle_c3, fill_model="conservative")
        purposes = [f.purpose for f in result.candle_fills]
        assert "CYCLE_3_LONG_ADD" in purposes
        assert "CYCLE_3_SHORT_REDUCE" not in purposes

        # Explicitly submit the market second leg as the strategy would after the long fill.
        sim.submit_intents_to_book(
            [
                StrategyIntent(
                    side="short",
                    qty=6.288,
                    purpose="CYCLE_3_SHORT_REDUCE",
                    order_type="Market",
                    reduce_only=True,
                )
            ],
            event_source="after_fill",
        )
        short = sim.book.active_orders_by_purpose("CYCLE_3_SHORT_REDUCE")[0]
        assert short.eligible_from_candle_index == 1
        assert sim.book.short_qty == pytest.approx(25.153)

        # Cycle 5 style: long fill then short must not share candle.
        sim.candle_index = 10
        long5, _ = sim.book.submit_intent(
            StrategyIntent(
                side="long",
                qty=12.577,
                purpose="CYCLE_5_LONG_ADD",
                order_type="Market",
                trigger_price=1.9386,
                trigger_direction=2,
                reduce_only=True,
            ),
            replace=False,
        )
        stamp_order_causal_eligibility(long5, created_candle_index=9, eligible_from_candle_index=10)
        sim.book.sync_runtime_state(sim.runtime_state)
        candle_c5 = _candle(
            open_=1.94,
            high=1.94,
            low=1.9221,
            close=1.9221,
            ts=datetime(2026, 1, 14, 13, 10, tzinfo=timezone.utc),
        )
        result5 = sim.process_candle(candle_c5, fill_model="conservative")
        purposes5 = [f.purpose for f in result5.candle_fills]
        # Deferred C3 short may fill here (eligible), but C5 short created after C5 long must not.
        assert "CYCLE_5_LONG_ADD" in purposes5 or "CYCLE_3_SHORT_REDUCE" in purposes5
        sim.submit_intents_to_book(
            [
                StrategyIntent(
                    side="short",
                    qty=6.0,
                    purpose="CYCLE_5_SHORT_REDUCE",
                    order_type="Market",
                    reduce_only=True,
                )
            ],
            event_source="after_fill",
        )
        assert "CYCLE_5_SHORT_REDUCE" not in [
            f.purpose for f in result5.candle_fills
        ]
        c5_short = sim.book.active_orders_by_purpose("CYCLE_5_SHORT_REDUCE")
        assert c5_short
        assert c5_short[0].eligible_from_candle_index == 11
    finally:
        sim.close()
