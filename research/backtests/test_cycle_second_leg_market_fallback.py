"""Immediate MARKET fallback fills for cycle second-leg reduces."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import (
    is_immediate_cycle_second_leg_market_fill,
    is_immediate_market_fill,
)
from research.backtests.simulated_order_book import SyntheticCandle


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
    "purpose",
    [
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_3_LONG_REDUCE",
    ],
)
def test_cycle_second_leg_market_reduce_without_price_is_immediate_fill(purpose: str) -> None:
    intent = StrategyIntent(
        side="short" if "SHORT" in purpose else "long",
        qty=1.0,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
    )
    assert is_immediate_cycle_second_leg_market_fill(intent) is True
    assert is_immediate_market_fill(intent) is True


@pytest.mark.parametrize(
    "purpose",
    [
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_3_LONG_REDUCE",
    ],
)
def test_cycle_second_leg_with_price_or_not_reduce_only_is_not_immediate(purpose: str) -> None:
    # Mit Limit-/Triggerpreis oder ohne reduce_only darf der Helper nicht greifen.
    base = StrategyIntent(
        side="short" if "SHORT" in purpose else "long",
        qty=1.0,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
    )

    with_price = StrategyIntent(**{**base.__dict__, "price": 99.0})
    with_trigger = StrategyIntent(**{**base.__dict__, "trigger_price": 99.0})
    not_reduce_only = StrategyIntent(**{**base.__dict__, "reduce_only": False})

    for intent in (with_price, with_trigger, not_reduce_only):
        assert is_immediate_cycle_second_leg_market_fill(intent) is False


def test_cycle_long_add_market_not_treated_as_immediate() -> None:
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="CYCLE_3_LONG_ADD",
        order_type="Market",
        reduce_only=False,
    )
    assert is_immediate_cycle_second_leg_market_fill(intent) is False
    # is_immediate_market_fill kann für andere Pfade noch True sein; hier erwarten wir False.
    assert is_immediate_market_fill(intent) is False


def test_e2e_cycle_second_leg_market_fallback_fills_immediately() -> None:
    """E2E: Market-Fallback für CYCLE_3_SHORT_REDUCE wird im Simulator sofort gefüllt."""
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    try:
        # Ausgangsposition
        sim.book.long_qty = 100.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 50.0
        sim.book.short_avg = 100.0

        runtime_state: RuntimeState = sim.runtime_state
        state = runtime_state.strategy_state
        state.update(
            {
                "trade_block_id": "tb-cycle-3",
                "cycle_waiting_for_short_tp": True,
                "short_tp_pending_cycle": 3,
                "pending_short_cycle_index": 3,
                "current_long_cycle_index": 3,
                "current_short_cycle_index": 0,
                "processed_cycle_purposes": ["CYCLE_3_LONG_ADD"],
                "initial_entry_confirmed": True,
            }
        )

        intent = StrategyIntent(
            side="short",
            qty=5.0,
            purpose="CYCLE_3_SHORT_REDUCE",
            order_type="Market",
            reduce_only=True,
            metadata={"cycle_index": 3, "cycle_role": "short_reduce"},
        )

        qty_before_short = sim.book.short_qty

        resting = sim.submit_intents_to_book(
            [intent],
            event_source="test_cycle_second_leg_market_fallback",
        )

        # Intent darf nicht als NEW im Orderbuch liegen bleiben.
        assert resting == []
        assert sim.book.active_orders_by_purpose("CYCLE_3_SHORT_REDUCE") == []

        # Short-Position muss reduziert worden sein.
        qty_after_short = sim.book.short_qty
        assert qty_after_short == pytest.approx(qty_before_short - 5.0)

        # Es muss ein entsprechender Fill im order_log erscheinen.
        fills = [
            entry
            for entry in sim.order_log
            if entry.get("purpose") == "CYCLE_3_SHORT_REDUCE" and entry.get("event_type") == "filled"
        ]
        assert len(fills) == 1
        filled = fills[0]
        assert filled.get("status") == "FILLED"
        assert filled.get("price") == pytest.approx(100.0)
        assert filled.get("fill_price") == pytest.approx(100.0)
        # Keine doppelte Ausführung im gleichen Candle-Lauf.
        assert filled.get("same_candle_fill_count", 1) == 1
    finally:
        sim.close()

