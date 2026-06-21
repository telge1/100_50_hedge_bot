from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Sequence


@dataclass
class Position:
    long_size: float
    long_avg: float
    short_size: float
    short_avg: float

    def spread(self) -> float:
        return (self.long_avg - self.short_avg) / self.short_avg * 100

    def ratio(self) -> float:
        if self.short_size == 0:
            return float("inf")
        return self.long_size / self.short_size

    def close_short(self, amount: float) -> None:
        if amount > self.short_size:
            amount = self.short_size
        self.short_size -= amount

    def add_short(self, amount: float, price: float) -> None:
        self.short_avg = (
            self.short_avg * self.short_size + price * amount
        ) / (self.short_size + amount)
        self.short_size += amount


def print_header() -> None:
    print("\nPhase  Price    Action          Long  LongAvg  Short  ShortAvg  Spread  Ratio")
    print("-" * 80)


def print_row(step: int, price: float, action: str, pos: Position) -> None:
    print(
        f"{step:>3}    {price:7.2f}  {action:14}  "
        f"{pos.long_size:5.2f}  {pos.long_avg:7.4f}  "
        f"{pos.short_size:5.2f}  {pos.short_avg:8.4f}  "
        f"{pos.spread():6.2f}%  {pos.ratio():5.2f}"
    )


def run_scenario(price_path: Sequence[float], start_price: float = 98.0) -> None:
    pos = Position(long_size=100.0, long_avg=100.0, short_size=100.0, short_avg=98.0)
    print_header()
    print_row(0, start_price, "start", pos)
    last_high = start_price
    event_idx = 1
    for price in price_path:
        action = "hold"
        if price > last_high:
            last_high = price
        if price >= last_high * 1.01:
            close_amount = pos.short_size * 0.1
            pos.close_short(close_amount)
            action = f"close short {close_amount:.2f}"
            last_high = price
        elif price <= last_high * 0.99:
            add_amount = pos.short_size * 0.05
            pos.add_short(add_amount, price)
            action = f"add short {add_amount:.2f}"
        print_row(event_idx, price, action, pos)
        event_idx += 1

    print("\nFinal Spread:", f"{pos.spread():.2f}%", "Ratio:", f"{pos.ratio():.2f}", "Short Avg:", f"{pos.short_avg:.4f}")


def parse_prices(value: str) -> list[float]:
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge test for 100/100 @ 2% spread")
    parser.add_argument(
        "--prices",
        type=parse_prices,
        required=True,
        help="Comma-separated price sequence to replay",
    )
    parser.add_argument(
        "--start-price",
        type=float,
        default=98.0,
        help="Starting short price (default 98.0 for 2% spread)",
    )
    args = parser.parse_args()
    run_scenario(args.prices, args.start_price)


if __name__ == "__main__":
    main()
