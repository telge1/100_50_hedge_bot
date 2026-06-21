from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass
class Hedge:
    long_size: float
    long_avg: float
    short_size: float
    short_avg: float

    def ratio(self) -> float:
        if self.short_size == 0:
            return float("inf")
        return self.long_size / self.short_size

    def spread(self) -> float:
        if self.short_avg == 0:
            return 0.0
        return (self.long_avg - self.short_avg) / self.short_avg * 100

    def add_short(self, amount: float, price: float) -> None:
        if amount <= 0:
            return
        self.short_avg = (self.short_avg * self.short_size + price * amount) / (
            self.short_size + amount
        )
        self.short_size += amount

    def close_short(self, amount: float) -> None:
        if amount <= 0:
            return
        amount = min(amount, self.short_size)
        self.short_size -= amount

    def add_long(self, amount: float, price: float) -> None:
        if amount <= 0:
            return
        self.long_avg = (self.long_avg * self.long_size + price * amount) / (
            self.long_size + amount
        )
        self.long_size += amount

    def close_long(self, amount: float) -> None:
        if amount <= 0:
            return
        amount = min(amount, self.long_size)
        self.long_size -= amount


def print_row(step: int, price: float, action: str, hedge: Hedge) -> None:
    print(
        f"{step:>3} {price:7.4f} {action:25} "
        f"Long={hedge.long_size:7.2f}@{hedge.long_avg:7.4f} "
        f"Short={hedge.short_size:7.2f}@{hedge.short_avg:7.4f} "
        f"Spread={hedge.spread():6.2f}% Ratio={hedge.ratio():5.2f}"
    )


def simulate(
    price_path: Sequence[float],
    drop_trigger: float = 0.03,
    rebound_trigger: float = 0.01,
    reset_trigger: float = 0.03,
    short_add_pct: float = 0.05,
    short_close_pct: float = 0.1,
    base_short_size: float = 1250,
    min_short_frac: float = 0.03,
    reset_size: float = 100,
) -> None:
    hedge = Hedge(1450.0, 98.5038, 1250.0, 97.5801)
    last_high = price_path[0]
    last_low = price_path[0]
    drop_triggered = False
    step = 0
    print_row(step, price_path[0], "start", hedge)

    for price in price_path[1:]:
        step += 1
        action = "hold"
        if price > last_high:
            last_high = price
            drop_triggered = False
        drop_level = last_high * (1 - drop_trigger)
        reset_level = last_high * (1 - reset_trigger)

        if price <= reset_level:
            hedge.long_size = reset_size
            hedge.short_size = reset_size
            hedge.long_avg = price
            hedge.short_avg = price
            last_high = price
            last_low = price
            drop_triggered = False
            action = "reset 100:100"
        elif not drop_triggered and price <= drop_level:
            target_short = hedge.long_size * min_short_frac
            amount = hedge.short_size - target_short
            if amount > 0:
                hedge.close_short(amount)
                last_low = price
                drop_triggered = True
                action = f"short reduce {amount:.2f}"
            else:
                action = "min short reached"
        elif drop_triggered and price >= last_low * (1 + rebound_trigger):
            target_short = hedge.long_size * 0.5
            amount = target_short - hedge.short_size
            if amount > 0:
                hedge.add_short(amount, price)
                action = f"short rebuild {amount:.2f}"
            else:
                action = "rebuild hold"
            last_high = price
            drop_triggered = False

        print_row(step, price, action, hedge)


def parse_price_string(value: str) -> list[float]:
    return [float(token) for token in value.split(",") if token.strip()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Aggressive 100:100 flow simulator")
    parser.add_argument(
        "--prices",
        type=parse_price_string,
        required=True,
        help="Comma-separated price list representing the scenario",
    )
    parser.add_argument("--drop", type=float, default=0.03, help="Down trigger")
    parser.add_argument("--rebound", type=float, default=0.01, help="Rebound trigger")
    parser.add_argument("--reset", type=float, default=0.05, help="Reset threshold")
    parser.add_argument(
        "--short-add", type=float, default=0.05, help="Short add as pct of base"
    )
    parser.add_argument(
        "--short-close", type=float, default=0.10, help="Short close percentage"
    )

    args = parser.parse_args()
    simulate(
        args.prices,
        drop_trigger=args.drop,
        rebound_trigger=args.rebound,
        reset_trigger=args.reset,
        short_add_pct=args.short_add,
        short_close_pct=args.short_close,
    )


if __name__ == "__main__":
    main()
