from __future__ import annotations

from pathlib import Path
from typing import Iterable
import sys

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


SCENARIOS = [
    ("2.0", "flat", [99.5, 99.4, 99.3, 99.4, 99.3, 99.2]),
    ("2.0", "improve", [99.5, 100.2, 100.8, 100.5, 100.3, 100.1]),
    ("2.0", "worsen", [99.5, 99.0, 98.7, 98.3, 98.0, 97.7]),
    ("2.5", "flat", [99.5, 99.4, 99.3, 99.2, 99.3, 99.2]),
    ("2.5", "improve", [99.5, 100.3, 100.9, 100.6, 100.2, 100.0]),
    ("2.5", "worsen", [99.5, 99.0, 98.5, 98.0, 97.6, 97.2]),
    ("3.0", "flat", [99.5, 99.4, 99.3, 99.2, 99.1, 99.0]),
    ("3.0", "improve", [99.5, 100.4, 101.0, 100.7, 100.4, 100.1]),
    ("3.0", "worsen", [99.5, 98.9, 98.2, 97.7, 97.1, 96.8]),
    ("3.5", "flat", [99.5, 99.4, 99.3, 99.2, 99.1, 99.0]),
    ("3.5", "improve", [99.5, 100.5, 101.2, 100.9, 100.5, 100.2]),
    ("3.5", "worsen", [99.5, 98.8, 98.0, 97.3, 96.8, 96.2]),
    ("4.0", "flat", [99.5, 99.4, 99.3, 99.2, 99.1, 99.0]),
    ("4.0", "improve", [99.5, 100.6, 101.3, 101.0, 100.6, 100.2]),
    ("4.0", "worsen", [99.5, 98.7, 97.8, 97.0, 96.2, 95.7]),
]


def build_scenarios() -> list[dict[str, object]]:
    scenarios = []
    for spread_pct, scenario_type, prices in SCENARIOS:
        start_spread = float(spread_pct)
        short_avg = 100.0 - start_spread
        scenarios.append(
            {
                "name": f"{spread_pct}_{scenario_type}",
                "spread_pct": start_spread,
                "scenario_type": scenario_type,
                "prices": prices,
                "short_avg": short_avg,
            }
        )
    return scenarios


def run_scenario(scenario: dict[str, object]) -> dict[str, object]:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.config.recovery_low = 150.0
    long_size = 1000.0
    long_avg = 100.0
    short_size = 500.0
    short_avg = scenario["short_avg"]
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

    start_snapshot = strategy._get_position_snapshot()
    start_spread_val = abs(strategy.calculate_hedge_spread())

    for idx, price in enumerate(scenario["prices"], 1):
        strategy.executor._sim_current_tick = idx
        strategy.on_price_update(price)

    long_after, short_after, long_avg_after, short_avg_after = strategy._get_position_snapshot()
    end_spread_val = abs(strategy.calculate_hedge_spread())
    heal_count = sum(1 for event in order_manager.events if "RECOVERY-HEAL-PROOF" in event)
    filled_order_count = sum(1 for event in order_manager.events if "place_market_order" in event)

    return {
        "scenario_name": scenario["name"],
        "scenario_type": scenario["scenario_type"],
        "start_spread": scenario["spread_pct"],
        "start_long_size": start_snapshot[0],
        "end_long_size": long_after,
        "long_size_delta": long_after - start_snapshot[0],
        "start_long_avg": start_snapshot[2],
        "end_long_avg": long_avg_after,
        "long_avg_delta": long_avg_after - start_snapshot[2],
        "start_spread_value": start_spread_val,
        "end_spread_value": end_spread_val,
        "spread_delta": end_spread_val - start_spread_val,
        "final_state": strategy.state_machine.state.value,
        "heal_count": heal_count,
        "filled_order_count": filled_order_count,
    }


def summarize_results(results: Iterable[dict[str, object]]) -> None:
    header = (
        f"{'scenario':<20}{'type':<10}{'start_spread':>12}{'end_spread':>12}"
        f"{'spread_Δ':>12}{'heals':>8}{'longΔ':>10}{'state':>12}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row['scenario_name']:<20}"
            f"{row['scenario_type']:<10}"
            f"{row['start_spread']:>12.2f}"
            f"{row['end_spread_value']:>12.4f}"
            f"{row['spread_delta']:>12.4f}"
            f"{row['heal_count']:>8}"
            f"{row['long_size_delta']:>10.2f}"
            f"{row['final_state']:>12}"
        )
    best = sorted(results, key=lambda r: r["spread_delta"], reverse=True)[:3]
    print("\nTop scenarios by spread delta:")
    for row in best:
        print(
            f"  {row['scenario_name']}: spreadΔ={row['spread_delta']:.4f}, heals={row['heal_count']}"
        )


def main() -> None:
    scenarios = build_scenarios()
    results = [run_scenario(scenario) for scenario in scenarios]
    summarize_results(results)


if __name__ == "__main__":
    main()
