from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulate_psrh_midtrade import (
    FakeOrderManager,
    ScenarioCase,
    build_execution_summary,
    build_strategy,
    format_float,
    install_debug_hooks,
    resolve_last_rebuy_time,
    set_midtrade_state,
)
from strategy.execution.order_executor import OrderIntent
from strategy.state_machine import StrategyState


def _combined_pnl(strategy, price: float) -> float:
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    long_pnl = (price - long_avg) * long_size
    short_pnl = (short_avg - price) * short_size
    return long_pnl + short_pnl


def _hedge_ratio(strategy) -> float:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    if long_size <= 0:
        return 0.0
    return short_size / long_size


def _collect_triggered_conditions(tick_debug: list[str]) -> list[str]:
    triggered: list[str] = []
    markers = [
        "state_transition(",
        "ensure_hedge_integrity_result(",
        "place_long_rebuy_result(",
        "execute_take_profit_result(",
        "FORCED REBUY TRIGGERED",
        "Enforcing hedge integrity (market)",
    ]
    for line in tick_debug:
        if any(marker in line for marker in markers):
            triggered.append(line)
    return triggered


def _collect_blocked_conditions(tick_debug: list[str]) -> list[str]:
    blocked: list[str] = []
    markers = [
        "[REBUY DEBUG] skip:",
        "decision=SKIP",
        "blockade(",
        "tp_short suppressed this tick",
        "market_execution_blocked",
    ]
    for line in tick_debug:
        if any(marker in line for marker in markers):
            blocked.append(line)
    return blocked


def _suppressed_intents(strategy, tick_debug: list[str]) -> list[str]:
    suppressed: list[str] = []
    if getattr(strategy, "_last_tp_short_suppressed", False):
        suppressed.append("TP_SHORT")
    if any("tp_short suppressed this tick" in line for line in tick_debug):
        if "TP_SHORT" not in suppressed:
            suppressed.append("TP_SHORT")
    return suppressed


def _branch_reason(
    state_before: str,
    state_after: str,
    execution_summary: dict[str, Any],
    triggered: list[str],
    blocked: list[str],
    suppressed: list[str],
) -> str:
    intents = execution_summary["generated_intents"]
    if state_before != state_after:
        return f"state changed {state_before} -> {state_after}"
    if intents:
        return f"generated {', '.join(intents)}"
    if suppressed:
        return f"suppressed {', '.join(suppressed)}"
    if blocked:
        return blocked[0]
    if triggered:
        return triggered[0]
    return "no decisive branch logged"


def _scenario_recovery_rebuy() -> ScenarioCase:
    return ScenarioCase(
        name="trace_recovery_path",
        expected_branch="decision trace example",
        long_size=1000.0,
        long_avg=68014.0,
        short_size=500.0,
        short_avg=66920.0,
        state=StrategyState.NORMAL,
        last_rebuy_price=68014.0,
        dca_steps=0,
        last_price=68014.0,
        prices=[68014.0, 67800.0, 67653.0, 67300.0],
    )


def _scenario_rebuy_blocked() -> ScenarioCase:
    return ScenarioCase(
        name="trace_rebuy_blocked",
        expected_branch="identify cooldown/slippage",
        long_size=1000.0,
        long_avg=68014.0,
        short_size=500.0,
        short_avg=66920.0,
        state=StrategyState.RECOVERY,
        last_rebuy_price=67673.93,
        dca_steps=1,
        last_price=67673.93,
        prices=[67673.93, 67673.93, 67653.0],
    )


def _scenario_hedge_recover() -> ScenarioCase:
    return ScenarioCase(
        name="trace_hedge_recover",
        expected_branch="identify hedge recover",
        long_size=1000.0,
        long_avg=100.0,
        short_size=200.0,
        short_avg=98.0,
        state=StrategyState.RECOVERY,
        last_rebuy_price=100.0,
        dca_steps=1,
        last_price=98.0,
        prices=[97.0, 96.0],
    )


def _scenario_recovery_exit() -> ScenarioCase:
    return ScenarioCase(
        name="trace_recovery_exit",
        expected_branch="exit recovery",
        long_size=1000.0,
        long_avg=100.0,
        short_size=500.0,
        short_avg=99.0,
        state=StrategyState.RECOVERY,
        last_rebuy_price=101.0,
        dca_steps=1,
        last_price=100.0,
        prices=[100.0, 103.0, 105.0],
    )


def trace_scenario(scenario: ScenarioCase) -> None:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    intent_events: list[str] = []
    debug_events: list[str] = []
    original_execute_intent = strategy.executor.execute_intent

    def tracked_execute_intent(
        intent: OrderIntent,
        enqueue_follow_ups=None,
        allow_tp_short: bool = True,
    ) -> bool:
        intent_events.append(
            f"execute_intent(purpose={intent.purpose}, side={intent.side}, qty={intent.qty}, price={intent.price})"
        )
        return original_execute_intent(
            intent,
            enqueue_follow_ups=enqueue_follow_ups,
            allow_tp_short=allow_tp_short,
        )

    strategy.executor.execute_intent = tracked_execute_intent
    install_debug_hooks(strategy, debug_events)

    set_midtrade_state(
        strategy,
        long_size=scenario.long_size,
        long_avg=scenario.long_avg,
        short_size=scenario.short_size,
        short_avg=scenario.short_avg,
        state=scenario.state,
        last_rebuy_price=scenario.last_rebuy_price,
        dca_steps=scenario.dca_steps,
        last_price=scenario.last_price,
        last_rebuy_time=resolve_last_rebuy_time(scenario.last_rebuy_age_seconds),
    )
    order_manager.set_positions(
        scenario.long_size,
        scenario.long_avg,
        scenario.short_size,
        scenario.short_avg,
    )

    print(f"=== TRACE {scenario.name} ===")
    print(f"Scenario: {asdict(scenario)}")
    print()

    for idx, price in enumerate(scenario.prices, start=1):
        strategy.executor._sim_current_tick = idx
        last_rebuy_before = strategy.last_rebuy_price
        state_before = strategy.state_machine.state.value
        order_event_offset = len(order_manager.events)
        intent_event_offset = len(intent_events)
        debug_event_offset = len(debug_events)
        log_offset = len(strategy._simulator_debug_logs)

        if scenario.bridge_cooldown and strategy.last_rebuy_time:
            strategy.last_rebuy_time -= timedelta(
                seconds=strategy.config.min_rebuy_interval + 0.01
            )

        strategy.on_price_update(price)

        state_after = strategy.state_machine.state.value
        tick_debug = (
            debug_events[debug_event_offset:]
            + strategy._simulator_debug_logs[log_offset:]
        )
        actions = (
            intent_events[intent_event_offset:]
            + order_manager.events[order_event_offset:]
        )
        execution_summary = build_execution_summary(
            strategy._simulator_debug_logs[log_offset:],
            actions,
        )

        long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
        spread = abs(strategy.calculate_hedge_spread())
        ratio = _hedge_ratio(strategy)
        target_ratio = strategy.config.short_ratio
        combined_pnl = _combined_pnl(strategy, price)
        triggered = _collect_triggered_conditions(tick_debug)
        blocked = _collect_blocked_conditions(tick_debug)
        suppressed = _suppressed_intents(strategy, tick_debug)
        executed = [
            action for action in actions if action.startswith("execute_intent(")
        ]
        branch_reason = _branch_reason(
            state_before,
            state_after,
            execution_summary,
            triggered,
            blocked,
            suppressed,
        )
        long_size_interp = "base asset qty (contracts)"
        short_size_interp = "base asset qty (contracts)"
        pnl_formula = "(price - long_avg)*long_qty + (short_avg - price)*short_qty"
        intent_price = "none"
        for intent in execution_summary["generated_intents"]:
            if "LONG_REBUY" in intent:
                intent_price = intent.split("price=")[-1].rstrip(")")
                break
        rebuy_trigger_level = last_rebuy_before
        last_rebuy_after = strategy.last_rebuy_price

        print(f"Tick {idx}")
        print(f"  price: {price:.4f}")
        print(f"  state_before: {state_before}")
        print(f"  state_after: {state_after}")
        print(f"  long_size / long_avg: {format_float(long_size)} / {format_float(long_avg)}")
        print(f"  short_size / short_avg: {format_float(short_size)} / {format_float(short_avg)}")
        print(f"  combined_pnl: {combined_pnl:.6f}")
        print(f"  spread: {spread:.6f}")
        print(f"  hedge_ratio: {ratio:.6f}")
        print(f"  target_hedge_ratio: {target_ratio:.6f}")
        print(f"  dca_steps: {strategy.dca_steps}")
        print(f"  last_rebuy_price: {format_float(strategy.last_rebuy_price)}")
        print(f"  tick_price: {price:.6f}")
        print(f"  rebuy_trigger_level: {format_float(rebuy_trigger_level)}")
        print(f"  intent_price: {intent_price}")
        print(f"  last_rebuy_price_after_tick: {format_float(last_rebuy_after)}")
        print(f"  triggered_conditions: {triggered or ['none']}")
        print(f"  blocked_conditions: {blocked or ['none']}")
        print(f"  generated_intents: {execution_summary['generated_intents'] or ['none']}")
        print(f"  suppressed_intents: {suppressed or ['none']}")
        print(f"  executed_intents: {executed or ['none']}")
        print(f"  size_interpretation_long: {long_size_interp}")
        print(f"  size_interpretation_short: {short_size_interp}")
        print(f"  pnl_formula_used: {pnl_formula}")
        print(f"  branch_reason: {branch_reason}")
        print()


def main() -> None:
    scenarios = [
        _scenario_recovery_rebuy(),
        _scenario_rebuy_blocked(),
        _scenario_hedge_recover(),
        _scenario_recovery_exit(),
    ]
    for scenario in scenarios:
        trace_scenario(scenario)
        print("=" * 80)


if __name__ == "__main__":
    main()
