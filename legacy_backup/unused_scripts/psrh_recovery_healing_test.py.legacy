from __future__ import annotations

import csv
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SUMMARY_FIELDS = [
    "scenario_name",
    "start_spread",
    "price_path",
    "tick_count",
    "start_long_size",
    "end_long_size",
    "long_size_delta",
    "start_short_size",
    "end_short_size",
    "start_long_avg",
    "end_long_avg",
    "long_avg_delta",
    "start_short_avg",
    "end_short_avg",
    "start_spread_value",
    "end_spread_value",
    "spread_delta",
    "spread_reduced",
    "final_state",
    "total_heal_count",
    "total_heal_fill_count",
    "total_filled_order_count",
    "heal_blocked_by_cooldown_count",
    "heal_blocked_by_guard_count",
]

TICK_FIELDS = [
    "scenario_name",
    "tick",
    "price",
    "state_before",
    "state_after",
    "spread",
    "ratio",
    "long_size",
    "short_size",
    "long_avg",
    "short_avg",
    "actions",
    "filled_orders",
    "heal_triggered",
    "heal_filled",
    "heal_blocked_by_cooldown",
    "heal_blocked_by_guard",
]

SUMMARY_OUTPUT: list[dict[str, object]] = []
TICK_OUTPUT: list[dict[str, object]] = []
from scripts.simulate_psrh_midtrade import (
    FakeOrderManager,
    build_strategy,
    set_midtrade_state,
    resolve_last_rebuy_time,
)
from strategy.state_machine import StrategyState


def fill_rebuy_orders(strategy: Any):
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    intention = strategy.config.step_size_pct
    return long_size, short_size, long_avg, short_avg


def run_case(name: str, long_size: float, long_avg: float, short_size: float, short_avg: float, prices: list[float]):
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.config.recovery_heal_size_multiplier = 2.0
    strategy.config.recovery_low = 150.0
    set_midtrade_state(
        strategy,
        long_size=long_size,
        long_avg=long_avg,
        short_size=short_size,
        short_avg=short_avg,
        state=StrategyState.RECOVERY,
        last_rebuy_price=long_avg,
        dca_steps=1,
        last_price=long_avg,
        last_rebuy_time=resolve_last_rebuy_time(1.0),
    )
    order_manager.set_positions(long_size, long_avg, short_size, short_avg)
    start_spread = abs(strategy.calculate_hedge_spread())
    start_snapshot = strategy._get_position_snapshot()
    price_path = ",".join(f"{p:.2f}" for p in prices)
    start_price = prices[0]
    print(f"\n=== {name} === start spread={start_spread:.4f}")
    filled_intents = []
    heal_blocked_by_cooldown = 0
    heal_blocked_by_guard = 0
    for idx, price in enumerate(prices, 1):
        state_before = strategy.state_machine.state.value
        log_start = len(strategy._simulator_debug_logs)
        strategy.executor._sim_current_tick = idx
        strategy.on_price_update(price)
        state_after = strategy.state_machine.state.value
        long_sz, short_sz, long_avg, short_avg = strategy._get_position_snapshot()
        spread = abs(strategy.calculate_hedge_spread())
        ratio = short_sz / long_sz if long_sz else 0.0
        price_recovered = price > strategy.config.recovery_low * (1 + strategy.config.extend_trigger_pct)
        spread_ok = spread <= strategy.config.recovery_exit_spread_threshold
        ratio_ok = ratio >= strategy.config.short_ratio - strategy.config.recovery_ratio_tolerance
        actions = [event for event in order_manager.events if "place" in event][
            -len(prices) :
        ]
        new_logs = strategy._simulator_debug_logs[log_start:]
        if any("RECOVERY HEAL COOLDOWN" in log for log in new_logs):
            heal_blocked_by_cooldown += 1
        if any(
            "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in log
            for log in new_logs
        ):
            heal_blocked_by_guard += 1
        print(f"Tick {idx} | price={price:.2f}")
        print(f"  state_before={state_before} state_after={state_after}")
        print(f"  price_recovered={price_recovered} spread_ok={spread_ok} ratio_ok={ratio_ok}")
        print(f"  spread={spread:.4f} ratio={ratio:.4f}")
        print(f"  long_size={long_sz:.4f} short_size={short_sz:.4f}")
        print(f"  long_avg={long_avg:.4f} short_avg={short_avg:.4f}")
        print(f"  actions={actions or ['none']}")
        if actions:
            filled_intents.append(actions[-1])
        heal_triggered = any("RECOVERY-HEAL-PROOF" in action for action in actions)
        TICK_OUTPUT.append(
            {
                "scenario_name": name,
                "tick": idx,
                "price": price,
                "state_before": state_before,
                "state_after": state_after,
                "spread": spread,
                "ratio": ratio,
                "long_size": long_sz,
                "short_size": short_sz,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "actions": ";".join(actions) if actions else "none",
                "filled_orders": ";".join(actions) if actions else "none",
                "heal_triggered": heal_triggered,
                "heal_filled": heal_triggered,
                "heal_blocked_by_cooldown": any(
                    "RECOVERY HEAL COOLDOWN" in log for log in new_logs
                ),
                "heal_blocked_by_guard": any(
                    "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in log
                    for log in new_logs
                ),
            }
        )
        heal_triggered = any("RECOVERY-HEAL-PROOF" in action for action in actions)
        heal_blocked_by_cooldown = sum(1 for log in strategy._simulator_debug_logs if "RECOVERY HEAL COOLDOWN" in log)
        heal_blocked_by_guard = sum(
            1
            for log in strategy._simulator_debug_logs
            if "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in log
        )
        TICK_OUTPUT.append(
            {
                "scenario_name": name,
                "tick": idx,
                "price": price,
                "state_before": state_before,
                "state_after": state_after,
                "spread": spread,
                "ratio": ratio,
                "long_size": long_sz,
                "short_size": short_sz,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "actions": ";".join(actions) if actions else "none",
                "filled_orders": ";".join(actions) if actions else "none",
                "heal_triggered": heal_triggered,
                "heal_filled": heal_triggered,
                "heal_blocked_by_cooldown": bool(heal_blocked_by_cooldown),
                "heal_blocked_by_guard": bool(heal_blocked_by_guard),
            }
        )
    end_spread = abs(strategy.calculate_hedge_spread())
    long_after, short_after, long_avg_after, short_avg_after = strategy._get_position_snapshot()
    print("Final Summary")
    print(f"  start_spread={start_spread:.4f}")
    print(f"  end_spread={end_spread:.4f}")
    print(f"  spread_reduced={'yes' if end_spread < start_spread else 'no'}")
    print(f"  long: {start_snapshot[0]} -> {long_after}")
    print(f"  short: {start_snapshot[1]} -> {short_after}")
    print(f"  long_avg: {start_snapshot[2]} -> {long_avg_after}")
    print(f"  short_avg: {start_snapshot[3]} -> {short_avg_after}")
    print(f"  final_state={strategy.state_machine.state.value}")
    print(f"  filled_orders={filled_intents}")
    heal_blocked_by_cooldown_count = sum(
        1
        for log in strategy._simulator_debug_logs
        if "RECOVERY HEAL COOLDOWN" in log
    )
    heal_blocked_by_guard_count = sum(
        1
        for log in strategy._simulator_debug_logs
        if "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in log
    )

    SUMMARY_OUTPUT.append(
        {
            "scenario_name": name,
            "start_spread": abs(short_avg - long_avg),
            "price_path": price_path,
            "tick_count": len(prices),
            "start_long_size": start_snapshot[0],
            "end_long_size": long_after,
            "long_size_delta": long_after - start_snapshot[0],
            "start_short_size": start_snapshot[1],
            "end_short_size": short_after,
            "start_long_avg": start_snapshot[2],
            "end_long_avg": long_avg_after,
            "long_avg_delta": long_avg_after - start_snapshot[2],
            "start_short_avg": start_snapshot[3],
            "end_short_avg": short_avg_after,
            "start_spread_value": start_spread,
            "end_spread_value": end_spread,
            "spread_delta": end_spread - start_spread,
            "spread_reduced": "yes" if end_spread < start_spread else "no",
            "final_state": strategy.state_machine.state.value,
            "total_heal_count": sum(1 for event in order_manager.events if "RECOVERY-HEAL-PROOF" in event),
            "total_heal_fill_count": sum(1 for event in order_manager.events if "RECOVERY-HEAL-PROOF" in event),
            "total_filled_order_count": sum(1 for event in order_manager.events if "place_market_order" in event),
            "heal_blocked_by_cooldown_count": heal_blocked_by_cooldown_count,
            "heal_blocked_by_guard_count": heal_blocked_by_guard_count,
        }
    )


def run_deep_trace_case(
    name: str,
    long_size: float,
    long_avg: float,
    short_size: float,
    short_avg: float,
    prices: list[float],
) -> None:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.config.recovery_heal_size_multiplier = 2.0
    strategy.config.recovery_low = 150.0
    set_midtrade_state(
        strategy,
        long_size=long_size,
        long_avg=long_avg,
        short_size=short_size,
        short_avg=short_avg,
        state=StrategyState.RECOVERY,
        last_rebuy_price=long_avg,
        dca_steps=1,
        last_price=long_avg,
        last_rebuy_time=resolve_last_rebuy_time(1.0),
    )
    order_manager.set_positions(long_size, long_avg, short_size, short_avg)
    start_spread = abs(strategy.calculate_hedge_spread())
    start_snapshot = strategy._get_position_snapshot()

    print(f"\n=== {name} ===")
    for idx, price in enumerate(prices, 1):
        state_before = strategy.state_machine.state.value
        log_start = len(strategy._simulator_debug_logs)
        event_start = len(order_manager.events)

        strategy.executor._sim_current_tick = idx
        strategy.on_price_update(price)

        state_after = strategy.state_machine.state.value
        long_sz, short_sz, long_avg_now, short_avg_now = strategy._get_position_snapshot()
        spread = abs(strategy.calculate_hedge_spread())
        ratio = short_sz / long_sz if long_sz else 0.0
        actions = [event for event in order_manager.events if "place" in event][event_start:]
        filled_orders = order_manager.events[event_start:]
        new_logs = strategy._simulator_debug_logs[log_start:]
        heal_triggered = any("RECOVERY-HEAL-PROOF" in event for event in filled_orders)
        heal_blocked_by_cooldown = any("RECOVERY HEAL COOLDOWN" in entry for entry in new_logs)
        heal_blocked_by_guard = any(
            "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in entry for entry in new_logs
        )

        print(f"Tick {idx} | price={price:.2f}")
        print(f"  state_before={state_before} state_after={state_after}")
        print(f"  spread={spread:.4f} ratio={ratio:.4f}")
        print(f"  long_size={long_sz:.4f} short_size={short_sz:.4f}")
        print(f"  long_avg={long_avg_now:.4f} short_avg={short_avg_now:.4f}")
        print(f"  actions={actions or ['none']}")
        print(f"  filled_orders={filled_orders or ['none']}")
        print(f"  heal_triggered={heal_triggered}")
        print(f"  heal_blocked_by_cooldown={heal_blocked_by_cooldown}")
        print(f"  heal_blocked_by_guard={heal_blocked_by_guard}")

    end_spread = abs(strategy.calculate_hedge_spread())
    end_snapshot = strategy._get_position_snapshot()
    healed = sum(1 for event in order_manager.events if "RECOVERY-HEAL-PROOF" in event)
    filled = sum(1 for event in order_manager.events if "place_market_order" in event)

    print("\nFinal Summary")
    print(f"  start_spread={start_spread:.4f}")
    print(f"  end_spread={end_spread:.4f}")
    print(f"  spread_reduced={'yes' if end_spread < start_spread else 'no'}")
    print(f"  long: {start_snapshot[0]} -> {end_snapshot[0]}")
    print(f"  long_avg: {start_snapshot[2]} -> {end_snapshot[2]}")
    print(f"  final_state={strategy.state_machine.state.value}")
    print(f"  total_heal_count={healed}")
    print(f"  total_filled_order_count={filled}")



def main() -> None:
    run_case(
        "recovery_spread_healing_case",
        1000.0,
        100.0,
        500.0,
        97.5,
        [99.5, 99.0, 98.7, 98.4, 98.1, 97.9],
    )
    run_case(
        "recovery_spread_healing_case_2",
        1400.0,
        97.9,
        700.0,
        95.5,
        [96.5, 96.1, 95.9, 95.6, 95.4, 95.2],
    )
    run_deep_trace_case(
        "deep_trace_spread_3_0",
        1000.0,
        100.0,
        500.0,
        97.0,
        [99, 98, 97, 96, 95, 93, 92, 93, 94, 95, 96, 97],
    )
    run_deep_trace_case(
        "deep_trace_spread_3_5",
        1000.0,
        100.0,
        500.0,
        96.5,
        [99, 98, 97, 96, 95, 93, 92, 93, 94, 95, 96, 97],
    )


if __name__ == "__main__":
    main()
