from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from typing import Any

import pytest

from scripts.simulate_psrh_midtrade import ScenarioCase, build_scenarios, run_scenario, FakeOrderManager, build_strategy
from strategy.psrh_strategy import PSRHStrategy
from strategy.state_machine import StrategyState


def _setup_recovery_strategy(
    *,
    long_size: float,
    long_avg: float,
    short_size: float,
    short_avg: float,
    recovery_low: float,
    spread_threshold: float,
    ratio_tolerance: float,
) -> PSRHStrategy:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.position_manager.sync_positions(long_size, long_avg, short_size, short_avg)
    strategy.config.recovery_low = recovery_low
    strategy.config.recovery_exit_spread_threshold = spread_threshold
    strategy.config.recovery_ratio_tolerance = ratio_tolerance
    strategy.config.extend_trigger_pct = 0.01
    strategy.state_machine.transition(StrategyState.RECOVERY)
    return strategy


def _run_scenario(name: str) -> dict[str, Any]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        scenario = next(s for s in build_scenarios() if s.name == name)
        return run_scenario(scenario)


def _run_custom_scenario(scenario: ScenarioCase) -> dict[str, Any]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        return run_scenario(scenario)


def test_recovery_tick_without_rebuy_intent_suppresses_tp_short() -> None:
    summary = _run_scenario("scenario_8_real_numbers_rebuy_drift")
    history = summary["tick_priority_history"]
    suppressed_ticks = [tick for tick in history if tick["tp_short_suppressed"]]
    assert suppressed_ticks, f"No suppressed ticks in {summary['scenario_name']}"
    for tick in suppressed_ticks:
        assert not any(
            "TP_SHORT" in intent for intent in tick["generated_intents"]
        ), f"TP_SHORT should be suppressed in tick {tick['tick']}"
        assert tick["state_after"] == "recovery", "State should remain recovery when suppressed"


def test_rebuy_tick_with_intent_still_suppresses_tp_short() -> None:
    summary = _run_scenario("scenario_7_real_numbers_tp_wait_for_hedge")
    history = summary["tick_priority_history"]
    rebuy_ticks = [tick for tick in history if tick["rebuy_attempted"]]
    assert rebuy_ticks, f"No rebuy attempt logged in {summary['scenario_name']}"
    for tick in rebuy_ticks:
        assert tick["tp_short_suppressed"], f"Tick {tick['tick']} should suppress TP_SHORT"
        assert not any(
            "TP_SHORT" in intent for intent in tick["generated_intents"]
        ), "TP_SHORT should be suppressed while a rebuy intent runs"


def test_net_tp_requires_combined_profit() -> None:
    scenario = ScenarioCase(
        name="scenario_net_tp_exit",
        expected_branch="net tp exit",
        long_size=1000.0,
        long_avg=100.0,
        short_size=500.0,
        short_avg=99.0,
        state=StrategyState.RECOVERY,
        last_rebuy_price=100.0,
        dca_steps=0,
        last_price=100.0,
        prices=[102.0, 104.0],
    )
    summary = _run_custom_scenario(scenario)
    history = summary["tick_priority_history"]
    tick1_intents = history[0]["generated_intents"]
    tick2_intents = history[1]["generated_intents"]
    assert not any("TP" in intent for intent in tick1_intents), "TP should not trigger at zero net profit"
    assert any("TP_SHORT" in intent for intent in tick2_intents), "TP_SHORT should trigger once net pnl target reached"
    assert any("TP_LONG" in intent for intent in tick2_intents), "TP_LONG should accompany the net TP exit"


def test_recovery_exit_structure_success() -> None:
    strategy = _setup_recovery_strategy(
        long_size=1000.0,
        long_avg=100.0,
        short_size=500.0,
        short_avg=100.5,
        recovery_low=100.0,
        spread_threshold=0.012,
        ratio_tolerance=0.05,
    )
    price = 100.5
    spread = abs(strategy.calculate_hedge_spread())
    strategy.update_state(price, spread)
    assert strategy.state_machine.state == StrategyState.NORMAL


def test_recovery_exit_ratio_blocked() -> None:
    strategy = _setup_recovery_strategy(
        long_size=1000.0,
        long_avg=100.0,
        short_size=430.0,
        short_avg=101.0,
        recovery_low=100.0,
        spread_threshold=0.012,
        ratio_tolerance=0.05,
    )
    price = 100.5
    spread = abs(strategy.calculate_hedge_spread())
    strategy.update_state(price, spread)
    assert strategy.state_machine.state == StrategyState.RECOVERY


def test_recovery_exit_spread_blocked() -> None:
    strategy = _setup_recovery_strategy(
        long_size=1000.0,
        long_avg=100.0,
        short_size=500.0,
        short_avg=103.5,
        recovery_low=100.0,
        spread_threshold=0.012,
        ratio_tolerance=0.05,
    )
    price = 100.5
    spread = abs(strategy.calculate_hedge_spread())
    strategy.update_state(price, spread)
    assert strategy.state_machine.state == StrategyState.RECOVERY


def test_recovery_exit_reference_price() -> None:
    strategy = _setup_recovery_strategy(
        long_size=1000.0,
        long_avg=100.0,
        short_size=500.0,
        short_avg=100.5,
        recovery_low=108.0,
        spread_threshold=0.012,
        ratio_tolerance=0.05,
    )
    price = 109.0
    spread = abs(strategy.calculate_hedge_spread())
    strategy.update_state(price, spread)
    assert strategy.state_machine.state == StrategyState.NORMAL


def test_recovery_exit_boundary_inclusive() -> None:
    strategy = _setup_recovery_strategy(
        long_size=1000.0,
        long_avg=100.0,
        short_size=450.0,
        short_avg=101.20724346076459,
        recovery_low=105.0,
        spread_threshold=0.012,
        ratio_tolerance=0.05,
    )
    price = 105.05
    current_spread = abs(strategy.calculate_hedge_spread())
    assert current_spread == pytest.approx(strategy.config.recovery_exit_spread_threshold)
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    current_ratio = short_size / long_size
    assert current_ratio == pytest.approx(
        strategy.config.short_ratio - strategy.config.recovery_ratio_tolerance
    )
    assert price < strategy.config.recovery_low * (1 + strategy.config.extend_trigger_pct)
    strategy.update_state(price, current_spread)
    assert strategy.state_machine.state == StrategyState.NORMAL
