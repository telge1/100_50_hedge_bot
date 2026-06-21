from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


EPS = 1e-6  # Tolerance to cover float drift such as 98.0 * 0.98 not being exactly 96.04
SPREAD_FORMULA = "spread_pct = abs(long_avg - short_avg) / long_avg * 100"


def price_lte(a: float, b: float) -> bool:
    return a <= b + EPS


def price_gte(a: float, b: float) -> bool:
    return a >= b - EPS


def calc_weighted_avg(old_size: float, old_avg: float, add_size: float, add_price: float) -> float:
    total = old_size + add_size
    if total == 0:
        return add_price
    return (old_avg * old_size + add_price * add_size) / total


def calc_short_close_profit(short_avg: float, close_price: float, close_qty: float) -> float:
    return (short_avg - close_price) * close_qty


def calc_spread_pct(long_avg: float, short_avg: float) -> float:
    return abs(long_avg - short_avg) / long_avg * 100 if long_avg else 0.0


def calc_ratio(long_size: float, short_size: float) -> float:
    return long_size / short_size if short_size else float("inf")


@dataclass
class State:
    long_size: float
    long_avg: float
    short_size: float
    short_avg: float
    start_price: float
    current_state: str
    last_relevant_high: float
    last_relevant_low: float
    cumulative_profit: float = 0.0
    realized_profit: float = 0.0
    reductions: int = 0
    rebuilds: int = 0
    initial_long_size: float = 0.0
    initial_long_avg: float = 0.0
    initial_short_size: float = 0.0
    initial_short_avg: float = 0.0
    down_steps_hit: int = 0
    up_steps_hit: int = 0
    down_step_qty: float = 0.0
    up_step_qty: float = 0.0
    flow_leg: int = 1


def maybe_trigger_down_step(
    state: State,
    price: float,
    down_triggers: List[float],
    close_qty: float,
) -> Optional[Tuple[str, str, str]]:
    if not (state.current_state == "START" or state.current_state.startswith("DOWN_STEP_")):
        return None
    idx = (
        int(state.current_state.split("_")[-1])
        if state.current_state.startswith("DOWN_STEP_")
        else 0
    )
    if idx >= len(down_triggers):
        return None
    trigger = down_triggers[idx]
    if price_lte(price, trigger):
        close_price = price
        state.short_size -= close_qty
        state.realized_profit = calc_short_close_profit(
            state.short_avg, close_price, close_qty
        )
        state.cumulative_profit += state.realized_profit
        state.reductions += 1
        state.down_steps_hit += 1
        state.current_state = f"DOWN_STEP_{idx + 1}"
        state.last_relevant_low = price
        reason = f"price <= trigger ({trigger:.8f})"
        formula = "price <= trigger"
        return f"short reduce down step {idx + 1}", reason, formula
    return None


def maybe_trigger_rebuild(
    state: State,
    price: float,
    rebuild_trigger: float,
    target_short: float,
) -> Optional[Tuple[str, str, str]]:
    if state.current_state != "DOWN_STEP_3":
        return None
    if price_lte(price, rebuild_trigger):
        add_qty = target_short - state.short_size
        state.short_avg = calc_weighted_avg(state.short_size, state.short_avg, add_qty, price)
        state.short_size = target_short
        state.rebuilds += 1
        state.current_state = "REBUILT"
        state.last_relevant_low = price
        reason = f"price <= rebuild_trigger ({rebuild_trigger:.8f})"
        formula = "price <= rebuild_trigger"
        return f"short rebuild qty={add_qty:.2f}", reason, formula
    return None


def maybe_trigger_heal_step(
    state: State,
    price: float,
    heal_triggers: List[float],
    add_qty: float,
) -> Optional[Tuple[str, str, str]]:
    start_state = "HEAL_START"
    prefix = "HEAL_STEP_"
    allowed = state.current_state == start_state or state.current_state.startswith(prefix)
    if not allowed:
        return None
    idx = (
        int(state.current_state.split("_")[-1])
        if state.current_state.startswith(prefix)
        else 0
    )
    if idx >= len(heal_triggers):
        return None
    trigger = heal_triggers[idx]
    if price_lte(price, trigger):
        old_long_qty = state.long_size
        old_long_avg = state.long_avg
        state.long_size += add_qty
        state.long_avg = calc_weighted_avg(old_long_qty, old_long_avg, add_qty, price)
        state.current_state = f"HEAL_STEP_{idx + 1}"
        state.last_relevant_low = price
        reason = f"price <= heal_trigger ({trigger:.8f})"
        formula = "price <= heal_trigger"
        return f"long add heal step {idx + 1}", reason, formula
    return None


def maybe_trigger_up_step(
    state: State,
    price: float,
    up_triggers: List[float],
    close_qty: float,
) -> Optional[Tuple[str, str, str]]:
    if not state.current_state.startswith("REBUILT") and not state.current_state.startswith("UP_STEP_"):
        return None
    idx = (
        int(state.current_state.split("_")[-1])
        if state.current_state.startswith("UP_STEP_")
        else 0
    )
    if idx >= len(up_triggers):
        return None
    trigger = up_triggers[idx]
    if price_gte(price, trigger):
        close_price = price
        state.short_size -= close_qty
        state.realized_profit = calc_short_close_profit(
            state.short_avg, close_price, close_qty
        )
        state.cumulative_profit += state.realized_profit
        state.reductions += 1
        state.up_steps_hit += 1
        state.current_state = f"UP_STEP_{idx + 1}"
        state.last_relevant_high = price
        reason = f"price >= trigger ({trigger:.8f})"
        formula = "price >= trigger"
        return f"short reduce up step {idx + 1}", reason, formula
    return None


def maybe_trigger_down_step_two_leg(
    state: State,
    price: float,
    down_triggers: List[float],
    close_qty: float,
) -> Optional[Tuple[str, str, str, bool]]:
    leg = state.flow_leg
    start_state = f"LEG{leg}_START"
    prefix = f"LEG{leg}_DOWN_STEP_"
    allowed = state.current_state == start_state or state.current_state.startswith(prefix)
    if not allowed:
        return None
    idx = (
        int(state.current_state.split("_")[-1])
        if state.current_state.startswith(prefix)
        else 0
    )
    if idx >= len(down_triggers):
        return None
    trigger = down_triggers[idx]
    if price_lte(price, trigger):
        close_price = price
        state.short_size -= close_qty
        state.realized_profit = calc_short_close_profit(
            state.short_avg, close_price, close_qty
        )
        state.cumulative_profit += state.realized_profit
        state.reductions += 1
        state.down_steps_hit += 1
        state.current_state = f"LEG{leg}_DOWN_STEP_{idx + 1}"
        state.last_relevant_low = price
        reason = f"price <= trigger ({trigger:.8f})"
        formula = "price <= trigger"
        is_final = idx + 1 == len(down_triggers)
        return f"short reduce down step {idx + 1}", reason, formula, is_final
    return None


def maybe_trigger_rebuild_two_leg(
    state: State,
    price: float,
    rebuild_trigger: float,
    target_short: float,
    final_step_count: int,
) -> Optional[Tuple[str, str, str]]:
    leg = state.flow_leg
    expected_state = f"LEG{leg}_DOWN_STEP_{final_step_count}"
    if state.current_state != expected_state:
        return None
    if price_lte(price, rebuild_trigger):
        add_qty = target_short - state.short_size
        state.short_avg = calc_weighted_avg(state.short_size, state.short_avg, add_qty, price)
        state.short_size = target_short
        state.rebuilds += 1
        state.current_state = f"LEG{leg}_REBUILT"
        state.last_relevant_low = price
        reason = f"price <= rebuild_trigger ({rebuild_trigger:.8f})"
        formula = "price <= rebuild_trigger"
        return f"short rebuild qty={add_qty:.2f}", reason, formula
    return None


def print_explanation_line(
    idx: int,
    action: str,
    action_type: str,
    reason: str,
    formula: str,
    profit: float,
    old_long_avg: float,
    new_long_avg: float,
    old_short_avg: float,
    new_short_avg: float,
    old_spread: float,
    new_spread: float,
) -> None:
    print(
        f"  → explain idx={idx} action={action} type={action_type} reason={reason}; "
        f"formula={formula}; profit={profit:+.2f} USD; "
        f"long_avg={old_long_avg:.4f}->{new_long_avg:.4f}; "
        f"short_avg={old_short_avg:.4f}->{new_short_avg:.4f}; "
        f"spread={old_spread:.2f}%->{new_spread:.2f}%; {SPREAD_FORMULA}"
    )


def compute_leg_triggers_from_price(price: float) -> List[float]:
    return [
        round(price * 0.99, 8),
        round(price * 0.98, 8),
        round(price * 0.97, 8),
    ]


def run_simulation(
    prices: Iterable[float],
    long_size: float,
    long_avg: float,
    short_size: float,
    short_avg: float,
    start_price: Optional[float],
    size_mode: str,
    input_long_size: float,
    input_short_size: float,
    flow_mode: str,
    explain_run: bool,
) -> None:
    price_list = list(prices)
    if not price_list:
        raise ValueError("Price list must not be empty")
    start = start_price if start_price is not None else price_list[0]
    state = State(
        long_size=long_size,
        long_avg=long_avg,
        short_size=short_size,
        short_avg=short_avg,
        start_price=start,
        current_state="LEG1_START" if flow_mode == "two_leg_down" else "START",
        last_relevant_high=start,
        last_relevant_low=start,
        initial_long_size=long_size,
        initial_long_avg=long_avg,
        initial_short_size=short_size,
        initial_short_avg=short_avg,
    )
    target_half_short = state.long_size * 0.5
    total_close_down = state.short_size - target_half_short
    close_per_down_step = total_close_down / 3
    target_short_after_rebuild = state.long_size
    total_close_up = target_short_after_rebuild - target_half_short
    close_per_up_step = total_close_up / 3

    down_triggers = compute_leg_triggers_from_price(start)
    rebuild_trigger = round(down_triggers[-1] * 0.99, 8)
    up_reference = rebuild_trigger
    up_triggers = [
        round(up_reference * 1.01, 8),
        round(up_reference * 1.02, 8),
        round(up_reference * 1.03, 8),
    ]

    state.down_step_qty = close_per_down_step
    state.up_step_qty = close_per_up_step

    print_header()
    print(f"Spread formula: {SPREAD_FORMULA}")
    initial_spread = calc_spread_pct(state.long_avg, state.short_avg)
    print_state_line(0, price_list[0], "start", 0.0, 0.0, state)
    if explain_run:
        print_explanation_line(
            0,
            "start",
            "initialization",
            "initialization",
            "none",
            0.0,
            state.long_avg,
            state.long_avg,
            state.short_avg,
            state.short_avg,
            initial_spread,
            initial_spread,
        )
    prev_spread = initial_spread

    def log_action(
        idx: int,
        price: float,
        action_label: str,
        qty: float,
        profit: float,
        reason: str,
        formula: str,
        action_type: str,
        old_long_avg: float,
        new_long_avg: float,
        old_short_avg: float,
        new_short_avg: float,
    ) -> None:
        nonlocal prev_spread
        print_state_line(idx, price, action_label, qty, profit, state)
        new_spread = calc_spread_pct(new_long_avg, new_short_avg)
        if explain_run:
            print_explanation_line(
                idx,
                action_label,
                action_type,
                reason,
                formula,
                profit,
                old_long_avg,
                new_long_avg,
                old_short_avg,
                new_short_avg,
                prev_spread,
                new_spread,
            )
        prev_spread = new_spread

    active_down_triggers = down_triggers
    second_leg_triggers: Optional[List[float]] = None
    second_leg_trigger_count: Optional[int] = None
    heal_triggers: Optional[List[float]] = None
    heal_trigger_count = 0
    spread_at_rebuild: Optional[float] = None
    idx = 0
    for price in price_list[1:]:
        idx += 1
        action_label = "hold"
        profit = 0.0
        reason = "no trigger"
        formula = "none"
        qty = 0.0

        if flow_mode == "two_leg_down":
            down_result = maybe_trigger_down_step_two_leg(
                state, price, active_down_triggers, close_per_down_step
            )
            if down_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula, _ = down_result
                profit = state.realized_profit
                qty = close_per_down_step
                log_action(
                    idx,
                    price,
                    action_label,
                    qty,
                    profit,
                    reason,
                    formula,
                    "profit-taking",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                continue
            if state.flow_leg == 1:
                rebuild_result = maybe_trigger_rebuild_two_leg(
                    state,
                    price,
                    rebuild_trigger,
                    target_short_after_rebuild,
                    len(active_down_triggers),
                )
                if rebuild_result:
                    old_long_avg = state.long_avg
                    old_short_avg = state.short_avg
                    action_label, reason, formula = rebuild_result
                    log_action(
                        idx,
                        price,
                        action_label,
                        0.0,
                        0.0,
                        reason,
                        formula,
                        "rebuild",
                        old_long_avg,
                        state.long_avg,
                        old_short_avg,
                        state.short_avg,
                    )
                    second_leg_triggers = compute_leg_triggers_from_price(price)
                    active_down_triggers = second_leg_triggers
                    state.flow_leg = 2
                    state.current_state = "LEG2_START"
                    state.last_relevant_low = price
                    second_leg_trigger_count = len(second_leg_triggers)
                    continue
        elif flow_mode == "rebuild_then_heal":
            down_result = maybe_trigger_down_step(state, price, down_triggers, close_per_down_step)
            if down_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula = down_result
                profit = state.realized_profit
                qty = close_per_down_step
                log_action(
                    idx,
                    price,
                    action_label,
                    qty,
                    profit,
                    reason,
                    formula,
                    "profit-taking",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                continue
            rebuild_result = maybe_trigger_rebuild(
                state, price, rebuild_trigger, target_short_after_rebuild
            )
            if rebuild_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula = rebuild_result
                log_action(
                    idx,
                    price,
                    action_label,
                    0.0,
                    0.0,
                    reason,
                    formula,
                    "rebuild",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                spread_at_rebuild = calc_spread_pct(state.long_avg, state.short_avg)
                heal_triggers = compute_leg_triggers_from_price(price)
                state.current_state = "HEAL_START"
                state.last_relevant_low = price
                heal_trigger_count = 0
                continue
            if heal_triggers:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                heal_result = maybe_trigger_heal_step(
                    state, price, heal_triggers, close_per_down_step
                )
                if heal_result:
                    action_label, reason, formula = heal_result
                    qty = 0.0
                    log_action(
                        idx,
                        price,
                        action_label,
                        qty,
                        0.0,
                        reason,
                        formula,
                        "spread-healing",
                        old_long_avg,
                        state.long_avg,
                        old_short_avg,
                        state.short_avg,
                    )
                    heal_trigger_count += 1
                    continue
        else:
            down_result = maybe_trigger_down_step(state, price, down_triggers, close_per_down_step)
            if down_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula = down_result
                profit = state.realized_profit
                qty = close_per_down_step
                log_action(
                    idx,
                    price,
                    action_label,
                    qty,
                    profit,
                    reason,
                    formula,
                    "profit-taking",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                continue
            rebuild_result = maybe_trigger_rebuild(
                state, price, rebuild_trigger, target_short_after_rebuild
            )
            if rebuild_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula = rebuild_result
                log_action(
                    idx,
                    price,
                    action_label,
                    0.0,
                    0.0,
                    reason,
                    formula,
                    "rebuild",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                continue
            up_result = maybe_trigger_up_step(state, price, up_triggers, close_per_up_step)
            if up_result:
                old_long_avg = state.long_avg
                old_short_avg = state.short_avg
                action_label, reason, formula = up_result
                profit = state.realized_profit
                qty = close_per_up_step
                log_action(
                    idx,
                    price,
                    action_label,
                    qty,
                    profit,
                    reason,
                    formula,
                    "profit-taking",
                    old_long_avg,
                    state.long_avg,
                    old_short_avg,
                    state.short_avg,
                )
                continue

        log_action(
            idx,
            price,
            action_label,
            qty,
            profit,
            reason,
            formula,
            "hold",
            state.long_avg,
            state.long_avg,
            state.short_avg,
            state.short_avg,
        )

    if flow_mode == "rebuild_then_heal":
        second_leg_trigger_count = heal_trigger_count
    print_summary(
        state,
        size_mode,
        input_long_size,
        input_short_size,
        flow_mode,
        second_leg_trigger_count,
        spread_at_rebuild,
    )


def parse_prices(value: str) -> List[float]:
    return [float(val) for val in value.split(",") if val.strip()]


def print_header() -> None:
    print(
        "idx price action qty delta_short realized_profit cum_profit "
        "long_qty long_avg short_qty short_avg spread ratio state"
    )


def print_state_line(
    idx: int,
    price: float,
    action: str,
    qty: float,
    profit: float,
    state: State,
) -> None:
    state_line = (
        f"{idx:>3} {price:7.4f} "
        f"{action:30} {qty:7.2f} {profit:13.2f} {state.cumulative_profit:11.2f} "
        f"{state.long_size:9.4f} {state.long_avg:9.4f} "
        f"{state.short_size:9.4f} {state.short_avg:9.4f} "
        f"{calc_spread_pct(state.long_avg, state.short_avg):6.2f}% "
        f"{calc_ratio(state.long_size, state.short_size):5.2f} State={state.current_state}"
    )
    print(state_line)


def print_summary(
    state: State,
    size_mode: str,
    input_long_size: float,
    input_short_size: float,
    flow_mode: str,
    second_leg_trigger_count: Optional[int],
    spread_at_rebuild: Optional[float],
) -> None:
    print("\nSummary:")
    print(f"spread_formula: {SPREAD_FORMULA}")
    print(f"input_long_size_usd: {input_long_size:.2f}")
    print(f"input_short_size_usd: {input_short_size:.2f}")
    print(f"initial_long_qty: {state.initial_long_size:.4f}")
    print(f"initial_short_qty: {state.initial_short_size:.4f}")
    print(f"current_long_qty: {state.long_size:.4f}")
    print(f"current_short_qty: {state.short_size:.4f}")
    print(f"final_long_avg: {state.long_avg:.4f}")
    print(f"final_short_avg: {state.short_avg:.4f}")
    print(f"total_realized_short_profit_usd: {state.cumulative_profit:.2f}")
    print(f"number_of_short_reduction_events: {state.reductions}")
    print(f"number_of_short_rebuild_events: {state.rebuilds}")
    print(f"down_step_close_qty: {state.down_step_qty:.2f}")
    print(f"up_step_close_qty: {state.up_step_qty:.2f}")
    final_spread = calc_spread_pct(state.long_avg, state.short_avg)
    print(f"final_spread: {final_spread:.2f}%")
    print(
        f"triggers_hit: DOWN={state.down_steps_hit}, REBUILD={state.rebuilds}, UP={state.up_steps_hit}"
    )
    print(f"size_mode_used: {size_mode}")
    print(f"flow_mode_used: {flow_mode}")
    print(f"second_leg_trigger_count: {second_leg_trigger_count or 0}")
    if flow_mode == "rebuild_then_heal" and spread_at_rebuild is not None:
        healed = final_spread < spread_at_rebuild
        print(f"spread_at_rebuild: {spread_at_rebuild:.2f}%")
        print(f"spread_healing_success: {healed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage-based short rebalance simulator")
    parser.add_argument("--prices", type=parse_prices, required=True, help="Comma-separated price list")
    parser.add_argument("--long-size", type=float, default=1450.0)
    parser.add_argument("--long-avg", type=float, default=98.5038)
    parser.add_argument("--short-size", type=float, default=1450.0)
    parser.add_argument("--short-avg", type=float, default=97.5801)
    parser.add_argument("--start-price", type=float, default=None)
    parser.add_argument("--size-mode", choices=["qty", "notional"], default="notional")
    parser.add_argument("--flow-mode", choices=["default", "two_leg_down", "rebuild_then_heal"], default="default")
    parser.add_argument("--explain-run", action="store_true")

    args = parser.parse_args()
    if args.size_mode == "notional":
        long_qty = args.long_size / args.long_avg
        short_qty = args.short_size / args.short_avg
    else:
        long_qty = args.long_size
        short_qty = args.short_size

    run_simulation(
        args.prices,
        long_qty,
        args.long_avg,
        short_qty,
        args.short_avg,
        args.start_price,
        args.size_mode,
        args.long_size,
        args.short_size,
        args.flow_mode,
        args.explain_run,
    )


if __name__ == "__main__":
    main()
