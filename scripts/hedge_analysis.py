from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Callable, Sequence
import argparse


HEADER = (
    "Phase  Aktion                    Preis         "
    "LongSize LongAvg  ShortSize ShortAvg Spread Ratio"
)


@dataclass
class Position:
    long_size: float
    long_avg: float
    short_size: float
    short_avg: float

    def clone(self) -> "Position":
        return Position(self.long_size, self.long_avg, self.short_size, self.short_avg)

    def spread(self) -> float:
        return (self.long_avg - self.short_avg) / self.short_avg * 100

    def ratio(self) -> float:
        if self.short_size == 0:
            return float("inf")
        return self.long_size / self.short_size

    def add_long(self, price: float, size: float) -> None:
        self.long_avg = (
            self.long_avg * self.long_size + price * size
        ) / (self.long_size + size)
        self.long_size += size

    def add_short(self, price: float, size: float) -> None:
        self.short_avg = (
            self.short_avg * self.short_size + price * size
        ) / (self.short_size + size)
        self.short_size += size

    def close_short(self, size: float) -> None:
        if size > self.short_size:
            raise ValueError("Cannot close more shorts than exist")
        self.short_size -= size

    def close_long(self, size: float) -> None:
        if size > self.long_size:
            raise ValueError("Cannot close more longs than exist")
        self.long_size -= size


@dataclass
class StepResult:
    phase: str
    action: str
    price: str
    position: Position


@dataclass
class ScenarioStep:
    phase: str
    action: str
    short_delta: float
    long_delta: float
    price_label: str = "—"
    short_price: float | None = None
    long_price: float | None = None


def print_row(phase: str, action: str, price: str, pos: Position) -> None:
    print(
        f"{phase:6} {action:25} {price:10} "
        f"{pos.long_size:8.2f} {pos.long_avg:8.4f} "
        f"{pos.short_size:8.2f} {pos.short_avg:8.4f} "
        f"{pos.spread():6.2f}% {pos.ratio():5.2f}"
    )


def print_steps(title: str, steps: Iterable[StepResult]) -> None:
    print(f"\n{title}")
    print(HEADER)
    print("-" * len(HEADER))
    for step in steps:
        print_row(step.phase, step.action, step.price, step.position)


def capture(phase: str, action: str, price: str, pos: Position) -> StepResult:
    return StepResult(phase, action, price, pos.clone())


def run_initial_sequence(
    actions: Iterable[dict[str, str | float]], start_short_avg: float
) -> tuple[Position, list[StepResult]]:
    pos = Position(long_size=1000.0, long_avg=100.0, short_size=1000.0, short_avg=start_short_avg)
    history = [capture("Start", "Initial", "100 / 98.5", pos)]
    for step in actions:
        phase = step["phase"]
        action = step["action"]
        price = float(step["price"])
        size = float(step["size"])
        if action.startswith("Long"):
            pos.add_long(price, size)
        elif action.startswith("Short"):
            pos.add_short(price, size)
        history.append(capture(phase, action, f"{price:6.4f}", pos))
    return pos.clone(), history


SHORT_TARGETS = [
    937.50,
    703.12,
    597.66,
    508.01,
    457.21,
    411.49,
    370.34,
    351.82,
    334.23,
    317.52,
]

LONG_TARGETS_TABLE = [
    1201.42,
    1009.01,
    920.35,
    843.60,
    799.46,
    759.25,
    722.69,
    706.10,
    690.22,
    675.04,
]


def build_table_bridge_steps() -> list[ScenarioStep]:
    prev_short = 1250.0
    prev_long = 1450.0
    steps: list[ScenarioStep] = []
    for idx, (short_target, long_target) in enumerate(
        zip(SHORT_TARGETS, LONG_TARGETS_TABLE), start=1
    ):
        short_close = prev_short - short_target
        long_close = prev_long - long_target
        steps.append(
            ScenarioStep(
                phase=f"PB{idx}",
                action=f"Short-{short_close:.2f} / Long-{long_close:.2f}",
                short_delta=-short_close,
                long_delta=-long_close,
                price_label="≈98.5",
            )
        )
        prev_short = short_target
        prev_long = long_target
    return steps


def build_ratio_bridge_steps(long_ratio: float) -> list[ScenarioStep]:
    prev_short = 1250.0
    prev_long = 1450.0
    steps: list[ScenarioStep] = []
    for idx, short_target in enumerate(SHORT_TARGETS, start=1):
        short_close = prev_short - short_target
        long_close = min(prev_long, short_close * long_ratio)
        steps.append(
            ScenarioStep(
                phase=f"PB{idx}",
                action=f"Short-{short_close:.2f} / Long-{long_close:.2f}",
                short_delta=-short_close,
                long_delta=-long_close,
                price_label="≈98.5",
            )
        )
        prev_short = short_target
        prev_long -= long_close
    return steps


def apply_step(pos: Position, step: ScenarioStep) -> None:
    if step.short_delta < 0:
        pos.close_short(-step.short_delta)
    elif step.short_delta > 0:
        if step.short_price is None:
            raise ValueError("short_price required for add")
        pos.add_short(step.short_price, step.short_delta)
    if step.long_delta < 0:
        pos.close_long(-step.long_delta)
    elif step.long_delta > 0:
        if step.long_price is None:
            raise ValueError("long_price required for add")
        pos.add_long(step.long_price, step.long_delta)


def run_bridge_scenario(name: str, start_pos: Position, steps: list[ScenarioStep]) -> list[StepResult]:
    pos = start_pos.clone()
    history: list[StepResult] = [capture("PB0", "Reference", "—", pos)]
    for step in steps:
        apply_step(pos, step)
        history.append(capture(step.phase, step.action, step.price_label, pos))
    print_steps(f"Bridge: {name}", history)
    final = history[-1].position
    print(
        f" → final long {final.long_size:7.2f}, short {final.short_size:7.2f}, "
        f"spread {final.spread():.2f}%, ratio {final.ratio():.2f}"
    )
    return history


SCENARIOS: dict[str, Sequence[float]] = {
    "noise": [99.5, 98.8, 99.6, 98.2, 99.3],
    "extreme": [101.455, 96.382],
}


def run_scenario_pair(name: str, base_pos: Position, start_price: float, price_path: Sequence[float]) -> None:
    run_rebound_scenario(f"Rebound {name}", base_pos, start_price, price_path)
    run_clockwork_scenario(f"Clockwork {name}", base_pos, start_price, price_path)


def run_rebound_scenario(
    name: str,
    start_pos: Position,
    start_price: float,
    price_path: Sequence[float],
    close_pct: float = 0.10,
    add_pct: float = 0.05,
    rise_threshold: float = 0.01,
    fall_threshold: float = 0.01,
) -> list[StepResult]:
    pos = start_pos.clone()
    history: list[StepResult] = [capture("REB0", "Reference", f"{start_price:.2f}", pos)]
    last_high = start_price
    waiting_for_drop = False
    baseline_short = start_pos.short_size
    for idx, price in enumerate(price_path, start=1):
        action = "Hold"
        price_label = f"{price:.2f}"
        if price >= last_high * (1 + rise_threshold):
            close_amount = pos.short_size * close_pct
            pos.close_short(close_amount)
            waiting_for_drop = True
            last_high = price
            action = f"ShortClose {close_amount:.2f}"
        else:
            if price > last_high:
                last_high = price
            if waiting_for_drop and price <= last_high * (1 - fall_threshold):
                add_amount = baseline_short * add_pct
                pos.add_short(price, add_amount)
                waiting_for_drop = False
                action = f"ShortAdd {add_amount:.2f}"
        history.append(capture(f"REB{idx}", action, price_label, pos))
    print_steps(f"Rebound: {name}", history)
    final = history[-1].position
    print(
        f" → final long {final.long_size:7.2f}, short {final.short_size:7.2f}, "
        f"spread {final.spread():.2f}%, ratio {final.ratio():.2f}"
    )
    return history


def run_clockwork_scenario(
    name: str,
    start_pos: Position,
    start_price: float,
    price_path: Sequence[float],
    short_close_pct: float = 0.10,
    short_add_pct: float = 0.05,
    mini_long_pct: float = 0.02,
    rise_threshold: float = 0.01,
    drop_thresholds: Sequence[float] = (0.01, 0.02),
) -> list[StepResult]:
    pos = start_pos.clone()
    history: list[StepResult] = [capture("CLK0", "Reference", f"{start_price:.2f}", pos)]
    last_high = start_price
    drop_idx = 0
    base_short = start_pos.short_size
    for idx, price in enumerate(price_path, start=1):
        action = "Hold"
        price_label = f"{price:.2f}"
        if price >= last_high * (1 + rise_threshold):
            close_amount = pos.short_size * short_close_pct
            pos.close_short(close_amount)
            action = f"ShortClose {close_amount:.2f}"
            if pos.ratio() < 1.2:
                mini_long = pos.long_size * mini_long_pct
                pos.add_long(price, mini_long)
                action += f" +MiniLong {mini_long:.2f}"
            last_high = price
            drop_idx = 0
        elif drop_idx < len(drop_thresholds) and price <= last_high * (
            1 - drop_thresholds[drop_idx]
        ):
            add_amount = base_short * short_add_pct
            pos.add_short(price, add_amount)
            action = f"ShortAdd {add_amount:.2f}"
            drop_idx += 1
        history.append(capture(f"CLK{idx}", action, price_label, pos))
    print_steps(f"Clockwork: {name}", history)
    final = history[-1].position
    print(
        f" → final long {final.long_size:7.2f}, short {final.short_size:7.2f}, "
        f"spread {final.spread():.2f}%, ratio {final.ratio():.2f}"
    )
    return history


def _parse_price_string(value: str) -> list[float]:
    return [float(item) for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> None:
    actions = [
        {"phase": "R1", "action": "Long +100", "price": 97.0200, "size": 100.0},
        {"phase": "R2", "action": "Long +100", "price": 96.0498, "size": 100.0},
        {"phase": "R3", "action": "Long +150", "price": 94.8492, "size": 150.0},
        {"phase": "S1", "action": "Short +100", "price": 93.9007, "size": 100.0},
        {"phase": "S2", "action": "Short +150", "price": 93.9007, "size": 150.0},
        {"phase": "L4", "action": "Long +100", "price": 92.9617, "size": 100.0},
    ]
    parser = argparse.ArgumentParser(description="Compare hedge scenarios")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="noise",
        help="Select predefined noise scenario",
    )
    parser.add_argument(
        "--prices",
        type=str,
        help="Comma-separated price path (overrides --scenario)",
    )
    parser.add_argument(
        "--start-price",
        type=float,
        default=98.0,
        help="Start price for the rebound/clockwork scenarios",
    )
    parser.add_argument(
        "--start-spread",
        type=float,
        default=2.0,
        help="Spread (%) between Long/Short at the start",
    )
    args = parser.parse_args(argv)

    start_short_avg = 100.0 / (1 + args.start_spread / 100)
    base_pos, base_steps = run_initial_sequence(actions, start_short_avg)
    print_steps(f"Build Sequence ({args.start_spread:.2f}% Spread)", base_steps)
    scenario_builders: list[tuple[str, Callable[[], list[ScenarioStep]]]] = [
        ("Table Bridge (Long Burn)", build_table_bridge_steps),
        ("Balanced Reduction (ratio 0.85)", lambda: build_ratio_bridge_steps(0.85)),
        ("Even Deleveraging (ratio 1.0)", lambda: build_ratio_bridge_steps(1.0)),
        ("Short-only (ratio 0.0)", lambda: build_ratio_bridge_steps(0.0)),
    ]
    for name, builder in scenario_builders:
        run_bridge_scenario(name, base_pos, builder())

    if args.prices:
        price_path = _parse_price_string(args.prices)
    else:
        price_path = SCENARIOS.get(args.scenario, [])

    if not price_path:
        raise ValueError("Price path must contain at least one value")
    run_scenario_pair(args.scenario, base_pos, args.start_price, price_path)


if __name__ == "__main__":
    main()
