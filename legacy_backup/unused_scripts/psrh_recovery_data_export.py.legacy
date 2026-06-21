from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulate_psrh_midtrade import (
    FakeOrderManager,
    build_strategy,
    set_midtrade_state,
    resolve_last_rebuy_time,
)
from strategy.state_machine import StrategyState

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

SCENARIOS = [
    {
        "name": "recovery_spread_healing_case",
        "long_size": 1000.0,
        "long_avg": 100.0,
        "short_size": 500.0,
        "short_avg": 97.5,
        "prices": [99.5, 99.0, 98.7, 98.4, 98.1, 97.9],
    },
    {
        "name": "recovery_spread_healing_case_2",
        "long_size": 1400.0,
        "long_avg": 97.9,
        "short_size": 700.0,
        "short_avg": 95.5,
        "prices": [96.5, 96.1, 95.9, 95.6, 95.4, 95.2],
    },
    {
        "name": "deep_trace_spread_3_0",
        "long_size": 1000.0,
        "long_avg": 100.0,
        "short_size": 500.0,
        "short_avg": 97.0,
        "prices": [99, 98, 97, 96, 95, 93, 92, 93, 94, 95, 96, 97],
    },
    {
        "name": "deep_trace_spread_3_5",
        "long_size": 1000.0,
        "long_avg": 100.0,
        "short_size": 500.0,
        "short_avg": 96.5,
        "prices": [99, 98, 97, 96, 95, 93, 92, 93, 94, 95, 96, 97],
    },
]


def run_scenario(scenario: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.config.recovery_low = 150.0
    set_midtrade_state(
        strategy,
        long_size=scenario["long_size"],
        long_avg=scenario["long_avg"],
        short_size=scenario["short_size"],
        short_avg=scenario["short_avg"],
        state=StrategyState.RECOVERY,
        last_rebuy_price=scenario["long_avg"],
        dca_steps=1,
        last_price=scenario["long_avg"],
        last_rebuy_time=resolve_last_rebuy_time(1.0),
    )
    order_manager.set_positions(
        scenario["long_size"],
        scenario["long_avg"],
        scenario["short_size"],
        scenario["short_avg"],
    )

    summary_start_snapshot = strategy._get_position_snapshot()
    start_spread_value = abs(strategy.calculate_hedge_spread())
    tick_rows: list[dict[str, object]] = []

    heal_blocked_by_cooldown = 0
    heal_blocked_by_guard = 0

    for idx, price in enumerate(scenario["prices"], 1):
        state_before = strategy.state_machine.state.value
        log_start = len(strategy._simulator_debug_logs)
        event_start = len(order_manager.events)
        strategy.executor._sim_current_tick = idx
        strategy.on_price_update(price)
        state_after = strategy.state_machine.state.value
        long_sz, short_sz, long_avg, short_avg = strategy._get_position_snapshot()
        spread = abs(strategy.calculate_hedge_spread())
        ratio = short_sz / long_sz if long_sz else 0.0
        new_logs = strategy._simulator_debug_logs[log_start:]
        actions = [
            event
            for event in order_manager.events[event_start:]
            if "place" in event
        ]
        filled_orders = [
            event
            for event in order_manager.events[event_start:]
            if "place" in event
        ]
        heal_triggered = any("RECOVERY-HEAL-PROOF" in action for action in filled_orders)
        cooldown_hit = any("RECOVERY HEAL COOLDOWN" in log for log in new_logs)
        guard_hit = any(
            "RECOVERY HEAL BLOCKED BY SPREAD/PRICE IMPROVEMENT" in log
            for log in new_logs
        )
        if cooldown_hit:
            heal_blocked_by_cooldown += 1
        if guard_hit:
            heal_blocked_by_guard += 1

        tick_rows.append(
            {
                "scenario_name": scenario["name"],
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
                "filled_orders": ";".join(filled_orders) if filled_orders else "none",
                "heal_triggered": heal_triggered,
                "heal_filled": heal_triggered,
                "heal_blocked_by_cooldown": cooldown_hit,
                "heal_blocked_by_guard": guard_hit,
            }
        )

    end_snapshot = strategy._get_position_snapshot()
    end_spread_value = abs(strategy.calculate_hedge_spread())
    heal_events = [event for event in order_manager.events if "RECOVERY-HEAL-PROOF" in event]
    filled_events = [event for event in order_manager.events if "place_market_order" in event]

    summary = {
        "scenario_name": scenario["name"],
        "start_spread": abs(scenario["short_avg"] - scenario["long_avg"]),
        "price_path": ",".join(f"{p:.2f}" for p in scenario["prices"]),
        "tick_count": len(scenario["prices"]),
        "start_long_size": summary_start_snapshot[0],
        "end_long_size": end_snapshot[0],
        "long_size_delta": end_snapshot[0] - summary_start_snapshot[0],
        "start_short_size": summary_start_snapshot[1],
        "end_short_size": end_snapshot[1],
        "start_long_avg": summary_start_snapshot[2],
        "end_long_avg": end_snapshot[2],
        "long_avg_delta": end_snapshot[2] - summary_start_snapshot[2],
        "start_short_avg": summary_start_snapshot[3],
        "end_short_avg": end_snapshot[3],
        "start_spread_value": start_spread_value,
        "end_spread_value": end_spread_value,
        "spread_delta": end_spread_value - start_spread_value,
        "spread_reduced": "yes" if end_spread_value < start_spread_value else "no",
        "final_state": strategy.state_machine.state.value,
        "total_heal_count": len(heal_events),
        "total_heal_fill_count": len(heal_events),
        "total_filled_order_count": len(filled_events),
        "heal_blocked_by_cooldown_count": heal_blocked_by_cooldown,
        "heal_blocked_by_guard_count": heal_blocked_by_guard,
    }
    return summary, tick_rows


def write_summary_csv(rows: Iterable[dict[str, object]]) -> None:
    path = PROJECT_ROOT / "psrh_recovery_summary.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_tick_csv(rows: Iterable[dict[str, object]]) -> None:
    path = PROJECT_ROOT / "psrh_recovery_ticks.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TICK_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    summaries: list[dict[str, object]] = []
    ticks: list[dict[str, object]] = []
    for scenario in SCENARIOS:
        summary, tick_rows = run_scenario(scenario)
        summaries.append(summary)
        ticks.extend(tick_rows)
    write_summary_csv(summaries)
    write_tick_csv(ticks)
    print(f"Saved summary to {PROJECT_ROOT / 'psrh_recovery_summary.csv'}")
    print(f"Saved ticks to {PROJECT_ROOT / 'psrh_recovery_ticks.csv'}")


if __name__ == "__main__":
    main()
