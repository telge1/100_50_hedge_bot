"""Deferred MARKET fallback fills for cycle second-leg reduces (next candle)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import (
    is_immediate_cycle_second_leg_market_fill,
    is_immediate_market_fill,
)
from research.backtests.simulated_order_book import SyntheticCandle


def _candle(symbol: str, *, close: float, ts: datetime | None = None) -> SyntheticCandle:
    return SyntheticCandle(
        symbol=symbol,
        open=close,
        high=close,
        low=close,
        close=close,
        timestamp=ts or datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "purpose",
    [
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_3_LONG_REDUCE",
    ],
)
def test_cycle_second_leg_market_reduce_without_price_is_detected_but_not_immediate(
    purpose: str,
) -> None:
    intent = StrategyIntent(
        side="short" if "SHORT" in purpose else "long",
        qty=1.0,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
    )
    assert is_immediate_cycle_second_leg_market_fill(intent) is True
    # Causal rule: not filled in the creation candle via immediate path.
    assert is_immediate_market_fill(intent) is False


@pytest.mark.parametrize(
    "purpose",
    [
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_3_LONG_REDUCE",
    ],
)
def test_cycle_second_leg_with_price_or_not_reduce_only_is_not_immediate(purpose: str) -> None:
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
    assert is_immediate_market_fill(intent) is False


def test_e2e_cycle_second_leg_market_fallback_fills_next_candle() -> None:
    """E2E: Market-Fallback rests on creation candle and fills on the next candle."""
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    sim.candle = _candle("BTCUSDT", close=100.0)
    try:
        sim.book.long_qty = 100.0
        sim.book.long_avg = 100.0
        sim.book.short_qty = 50.0
        sim.book.short_avg = 100.0
        sim.candle_index = 3

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
            event_source="after_fill",
        )

        assert len(resting) == 1
        assert resting[0].eligible_from_candle_index == 4
        assert sim.book.active_orders_by_purpose("CYCLE_3_SHORT_REDUCE")
        assert sim.book.short_qty == pytest.approx(qty_before_short)

        # Creation candle must not fill it via process_candle.
        result_same = sim.process_candle(sim.candle, fill_model="conservative")
        assert not any(f.purpose == "CYCLE_3_SHORT_REDUCE" for f in result_same.candle_fills)
        assert sim.book.short_qty == pytest.approx(qty_before_short)

        # Next candle fills deferred market at close.
        sim.candle_index = 4
        next_candle = _candle(
            "BTCUSDT",
            close=99.0,
            ts=datetime(2026, 6, 24, 12, 5, tzinfo=timezone.utc),
        )
        result_next = sim.process_candle(next_candle, fill_model="conservative")
        assert any(f.purpose == "CYCLE_3_SHORT_REDUCE" for f in result_next.candle_fills)
        assert sim.book.short_qty == pytest.approx(qty_before_short - 5.0)
        assert sim.book.active_orders_by_purpose("CYCLE_3_SHORT_REDUCE") == []
    finally:
        sim.close()
