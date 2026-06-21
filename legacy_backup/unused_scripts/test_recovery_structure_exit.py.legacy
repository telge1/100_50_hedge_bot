from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import List

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


def _spread(long_avg: float, short_avg: float) -> float:
    mid = (long_avg + short_avg) / 2 if (long_avg + short_avg) > 0 else 0.0
    if mid <= 0:
        return 0.0
    return (short_avg - long_avg) / mid


def _run_case(name: str, long_size: float, long_avg: float, short_size: float, short_avg: float, recovery_low: float, ratio_tolerance: float, spread_threshold: float, prices: List[float]) -> None:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    strategy.config.recovery_low = recovery_low
    strategy.config.recovery_exit_spread_threshold = spread_threshold
    strategy.config.recovery_ratio_tolerance = ratio_tolerance
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
    print(f"\n=== {name} ===")
    for price in prices:
        state_before = strategy.state_machine.state.value
        strategy.on_price_update(price)
        state_after = strategy.state_machine.state.value
        spread = _spread(long_avg, short_avg)
        ratio = short_size / long_size if long_size > 0 else 0.0
        price_recovered = price > strategy.config.recovery_low * (1 + strategy.config.extend_trigger_pct)
        print(f"price={price:.2f} | price_recovered={price_recovered:.3f} spread={spread:.4f} ratio={ratio:.4f} state_before={state_before} state_after={state_after}")


def main() -> None:
    cases = [
        ("spread_ok_ratio_ok", 1000.0, 100.0, 500.0, 101.0, 99.5, 0.05, 0.012, [100.0]),
        ("spread_ok_ratio_bad", 1000.0, 100.0, 430.0, 101.0, 99.5, 0.05, 0.012, [100.0]),
        ("spread_bad_ratio_ok", 1000.0, 100.0, 500.0, 103.5, 99.5, 0.05, 0.012, [100.0]),
        ("spread_bad_ratio_bad", 1000.0, 100.0, 430.0, 103.5, 99.5, 0.05, 0.012, [100.0]),
    ]
    for name, lsz, lavg, ssz, savg, low, ratio_tol, spread_thr, prices in cases:
        _run_case(name, lsz, lavg, ssz, savg, low, ratio_tol, spread_thr, prices)


if __name__ == "__main__":
    main()
