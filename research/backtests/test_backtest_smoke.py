"""Phase-1/2 smoke tests: original hedge strategies without Bybit."""

from __future__ import annotations

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.hedge_bot_original_simulator import (
    HedgeBotOriginalSimulator,
    KNOWN_STRUCTURE_PURPOSE_PREFIXES,
    KNOWN_STRUCTURE_PURPOSE_SUFFIXES,
)
from research.backtests.simulated_order_book import SimulatedOrderBook


@pytest.fixture
def long_simulator() -> HedgeBotOriginalSimulator:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    yield sim
    sim.close()


@pytest.fixture
def short_simulator() -> HedgeBotOriginalSimulator:
    sim = HedgeBotOriginalSimulator(signal="short", symbol="BTCUSDT", candle_close=100.0)
    yield sim
    sim.close()


def _assert_entry_intents(result) -> None:
    assert result.entry_intents, "on_start must return initial entry intents on flat snapshot"
    purposes = {intent.purpose for intent in result.entry_intents}
    assert "INITIAL_LONG_ENTRY" in purposes
    assert "INITIAL_SHORT_ENTRY" in purposes
    assert all(intent.qty > 0 for intent in result.entry_intents)


def _assert_entry_fills(result) -> None:
    assert len(result.entry_fills) == 2
    fill_purposes = {fill.purpose for fill in result.entry_fills}
    assert fill_purposes == {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}
    for fill in result.entry_fills:
        assert fill.status == "FILLED"
        assert fill.exec_qty > 0
        assert fill.exec_price > 0
        assert fill.metadata.get("confirmed_closed_pnl") is not None


def _assert_post_entry_state(result) -> None:
    state = result.strategy_state
    snapshot = result.final_snapshot
    assert snapshot is not None
    assert snapshot.long_qty > 0
    assert snapshot.short_qty > 0
    assert state.get("initial_long_entry_reconciled") is True
    assert state.get("initial_short_entry_reconciled") is True
    assert state.get("entry_reference_price", 0) > 0

    post_purposes = {intent.purpose for intent in result.post_fill_intents}
    has_structure_intents = bool(result.post_fill_intents)
    has_structure_state = any(
        [
            bool(state.get("initial_structure_built")),
            bool(state.get("next_required_purpose")),
            bool(state.get("initial_entry_confirmed")),
            int(state.get("active_cycle_index") or 0) > 0,
        ]
    )
    assert has_structure_intents or has_structure_state, (
        "expected initial structure/cycle/exit intents or updated cycle state after entry fills"
    )
    if has_structure_intents:
        assert any(
            purpose.startswith("CYCLE_") or purpose.endswith("_EXIT")
            for purpose in post_purposes
        )


def _is_structure_purpose(purpose: str) -> bool:
    return purpose.startswith(KNOWN_STRUCTURE_PURPOSE_PREFIXES) or purpose.endswith(
        KNOWN_STRUCTURE_PURPOSE_SUFFIXES
    )


def _assert_active_virtual_orders(result, sim: HedgeBotOriginalSimulator) -> None:
    runtime_state = result.runtime_state
    snapshot = result.final_snapshot
    assert runtime_state is not None
    assert snapshot is not None

    book_orders = sim.book.active_orders()
    assert book_orders, "expected at least one active virtual order after entry smoke"
    assert runtime_state.active_orders, "runtime_state.active_orders must be synced"
    assert snapshot.active_orders, "snapshot must expose active orders via snapshot_from_mapping"

    runtime_purposes = {order.purpose for order in runtime_state.active_orders.values()}
    snapshot_purposes = {order.purpose for order in snapshot.active_orders}
    book_purposes = {order.purpose for order in book_orders}

    assert runtime_purposes == book_purposes
    assert snapshot_purposes == book_purposes
    assert any(_is_structure_purpose(purpose) for purpose in book_purposes)


def test_long_signal_starts_fixed_cycle_strategy_and_builds_structure(long_simulator) -> None:
    assert isinstance(long_simulator.strategy, FixedCycleHedgeStrategy)
    assert not isinstance(long_simulator.strategy, ShortFixedCycleHedgeStrategy)

    result = long_simulator.run_entry_smoke()
    assert result.strategy_name == "FixedCycleHedgeStrategy"
    _assert_entry_intents(result)
    _assert_entry_fills(result)
    _assert_post_entry_state(result)


def test_short_signal_starts_short_fixed_cycle_strategy_and_builds_structure(short_simulator) -> None:
    assert isinstance(short_simulator.strategy, ShortFixedCycleHedgeStrategy)

    result = short_simulator.run_entry_smoke()
    assert result.strategy_name == "ShortFixedCycleHedgeStrategy"
    _assert_entry_intents(result)
    _assert_entry_fills(result)
    _assert_post_entry_state(result)

    first_leg_purpose = short_simulator.strategy._get_first_leg_purpose(1)
    assert first_leg_purpose == "CYCLE_1_SHORT_REDUCE"
    post_purposes = {intent.purpose for intent in result.post_fill_intents}
    if post_purposes:
        assert first_leg_purpose in post_purposes or any(
            p.endswith("_EXIT") for p in post_purposes
        )


def test_long_phase2_active_virtual_orders_in_runtime_and_snapshot(long_simulator) -> None:
    result = long_simulator.run_entry_smoke()
    _assert_active_virtual_orders(result, long_simulator)


def test_short_phase2_active_virtual_orders_in_runtime_and_snapshot(short_simulator) -> None:
    result = short_simulator.run_entry_smoke()
    _assert_active_virtual_orders(result, short_simulator)


def test_cancel_by_purpose_removes_only_matching_order() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})

    intent_a = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=101.0,
        reduce_only=True,
    )
    intent_b = StrategyIntent(
        side="short",
        qty=0.5,
        purpose="SHORT_SL_EXIT",
        order_type="Limit",
        price=99.0,
        reduce_only=True,
    )
    order_a = book.submit_intent(intent_a, replace=False)
    order_b = book.submit_intent(intent_b, replace=False)
    book.sync_runtime_state(runtime_state)

    canceled = book.cancel_by_purpose("LONG_TP_EXIT")
    book.sync_runtime_state(runtime_state)

    assert canceled == [order_a.order_id]
    active_purposes = {order.purpose for order in book.active_orders()}
    assert active_purposes == {"SHORT_SL_EXIT"}
    assert order_b.order_id in runtime_state.active_orders
    assert order_a.order_id not in runtime_state.active_orders


def test_submit_same_purpose_replaces_existing_order() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})

    first = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=101.0,
        reduce_only=True,
    )
    second = StrategyIntent(
        side="long",
        qty=2.5,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=102.0,
        reduce_only=True,
    )

    old_order = book.submit_intent(first, replace=True)
    new_order = book.submit_intent(second, replace=True)
    book.sync_runtime_state(runtime_state)

    active = book.active_orders_by_purpose("LONG_TP_EXIT")
    assert len(active) == 1
    assert active[0].order_id == new_order.order_id
    assert active[0].order_id != old_order.order_id
    assert active[0].qty == 2.5
    assert active[0].price == 102.0
    assert book.get_order(old_order.order_id) is not None
    assert book.get_order(old_order.order_id).status == "CANCELED"
    assert len(runtime_state.active_orders) == 1
    assert new_order.order_id in runtime_state.active_orders
