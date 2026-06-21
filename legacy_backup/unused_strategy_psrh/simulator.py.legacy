from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


@dataclass
class SimulatorConfig:
    rebuy_distance_pct: float = 0.015
    rebuy_multiplier: float = 1.1
    burn_levels: List[float] | None = None
    burn_pct: float = 0.2
    max_rebuys: int = 6
    step_size_pct: float = 0.001
    debug: bool = False


def weighted_avg(old_avg: float, old_size: float, new_price: float, new_size: float) -> float:
    total = old_avg * old_size + new_price * new_size
    size = old_size + new_size
    return total / size if size else 0.0


def apply_rebuy(
    long_size: float,
    long_avg: float,
    price: float,
    rebuy_factor: float,
) -> tuple[float, float]:
    add_size = max(long_size * 0.25 * rebuy_factor, 0.0001)
    new_avg = weighted_avg(long_avg, long_size, price, add_size)
    return long_size + add_size, new_avg


def apply_burn(
    short_size: float, short_avg: float, price: float, burn_pct: float
) -> tuple[float, float]:
    close_share = min(burn_pct, 1.0)
    close_size = short_size * close_share
    remaining = short_size - close_size
    if remaining <= 0:
        return 0.0, 0.0
    avg = weighted_avg(short_avg, short_size, price, -close_size)
    return remaining, avg


def simulate(
    *,
    long_entry_price: float,
    long_size: float,
    short_entry_price: float,
    short_size: float,
    target_price: float,
    config: SimulatorConfig | None = None,
) -> dict:
    cfg = config or SimulatorConfig()
    current_price = long_entry_price
    direction = 1 if target_price > long_entry_price else -1
    step = abs(cfg.step_size_pct * long_entry_price)
    events: List[str] = []
    rebuy_count = 0
    long_avg = long_entry_price
    short_avg = short_entry_price

    burn_levels = cfg.burn_levels or []

    while (
        (direction == 1 and current_price <= target_price)
        or (direction == -1 and current_price >= target_price)
    ):
        target_threshold = long_avg * (1 - cfg.rebuy_distance_pct * cfg.rebuy_multiplier ** rebuy_count)
        if rebuy_count < cfg.max_rebuys and current_price <= target_threshold:
            long_size, long_avg = apply_rebuy(long_size, long_avg, current_price, 1 + rebuy_count * 0.1)
            events.append(f"LONG REBUY @ {current_price:.4f}")
            rebuy_count += 1
            if cfg.debug:
                print(events[-1])

        for level in burn_levels:
            level_hit = (direction == 1 and current_price >= level) or (
                direction == -1 and current_price <= level
            )
            if level_hit and short_size > 0:
                short_size, short_avg = apply_burn(short_size, short_avg, current_price, cfg.burn_pct)
                events.append(f"SHORT BURN @ {current_price:.4f}")
                if cfg.debug:
                    print(events[-1])
                break

        tp_long = long_avg * 1.004
        tp_short = short_avg * 1.003
        if current_price >= tp_short:
            events.append(f"SHORT TP @ {current_price:.4f}")
            short_size = 0.0
            short_avg = 0.0
            break
        if current_price >= tp_long and short_size == 0:
            events.append(f"LONG TP @ {current_price:.4f}")
            long_size = 0.0
            long_avg = 0.0
            break

        current_price += direction * step
        if cfg.debug:
            print(f"Price step: {current_price:.4f}")

    mid_price = (long_avg + short_avg) / 2 if (long_avg + short_avg) else current_price
    spread_pct = abs(long_avg - short_avg) / mid_price * 100 if mid_price else 0.0

    return {
        "long": {"size": round(long_size, 6), "avg_price": round(long_avg, 6)},
        "short": {"size": round(short_size, 6), "avg_price": round(short_avg, 6)},
        "spread_pct": round(spread_pct, 4),
        "events": events,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PSRH strategy simulator")
    parser.add_argument("--long-entry", type=float, required=True)
    parser.add_argument("--long-size", type=float, required=True)
    parser.add_argument("--short-entry", type=float, required=True)
    parser.add_argument("--short-size", type=float, required=True)
    parser.add_argument("--target-price", type=float, required=True)
    parser.add_argument("--step-pct", type=float, default=0.001)
    parser.add_argument("--burn-levels", type=float, nargs="*", default=[])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    config = SimulatorConfig(
        step_size_pct=args.step_pct,
        burn_levels=args.burn_levels if args.burn_levels else None,
        debug=args.debug,
    )
    result = simulate(
        long_entry_price=args.long_entry,
        long_size=args.long_size,
        short_entry_price=args.short_entry,
        short_size=args.short_size,
        target_price=args.target_price,
        config=config,
    )
    print("Simulation result:")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
