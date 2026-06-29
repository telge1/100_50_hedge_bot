"""Phase-1 smoke tests: original hedge strategies without Bybit."""

from __future__ import annotations

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator


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
