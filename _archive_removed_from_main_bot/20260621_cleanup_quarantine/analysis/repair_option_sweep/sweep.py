from __future__ import annotations

import argparse
import itertools
import logging
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.burn_repair_sequence.simulate import (
    StrategyMathAdapter,
    build_initial_state,
    compute_spread,
    infer_qty_step,
    round_down_to_step,
    round_up_to_step,
    weighted_average,
)
from strategy.config import RecoveryRebuyBand, StrategyConfig
from strategy.psrh_strategy import PSRHStrategy

LOGGER = logging.getLogger("repair_option_sweep")
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(logging.StreamHandler())


def parse_float_list(raw: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        values.append(float(value))
    return values


def parse_str_list(raw: str) -> list[str]:
    values: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        values.append(value)
    return values


def percent(value: float) -> str:
    return f"{value * 100:.4f}%"


def build_config(
    *,
    divider: float,
    base_multiplier: float,
    increment: float,
    span: float,
    recovery_base_step_pct: float,
    long_entry_size: float,
    short_ratio: float,
    max_rebuy_usdt: float,
    small_cap_frac: float,
    mid_cap_frac: float,
    large_cap_frac: float,
    short_add_gap_fill_frac: float,
    short_add_mode: str,
) -> StrategyConfig:
    config = StrategyConfig(api_key="", secret_key="")
    config.long_entry_size = long_entry_size
    config.short_ratio = short_ratio
    config.recovery_base_step_pct = recovery_base_step_pct
    config.step_size_pct = recovery_base_step_pct
    config.recovery_rebuy_bands = (
        RecoveryRebuyBand(0.0, 1.0, divider),
    )
    config.user.rebuy_size_multiplier_base = base_multiplier
    config.user.rebuy_size_multiplier_increment = increment
    config.user.rebuy_size_multiplier_span = span
    config.max_rebuy_usdt = max_rebuy_usdt
    config.sweep_small_cap_frac = small_cap_frac
    config.sweep_mid_cap_frac = mid_cap_frac
    config.sweep_large_cap_frac = large_cap_frac
    config.sweep_short_add_gap_fill_frac = short_add_gap_fill_frac
    config.sweep_short_add_mode = short_add_mode
    return config


def calc_rebuy_plan_with_caps(
    *,
    state,
    config: StrategyConfig,
    qty_step: float,
    dca_steps: int,
    math_adapter: StrategyMathAdapter,
) -> dict[str, float | str] | None:
    spread_pct = compute_spread(state.long_avg, state.short_avg)
    rebuy_distance_pct, _ = PSRHStrategy._calc_rebuy_distance_pct(
        math_adapter,
        spread_pct,
    )
    next_rebuy_level = state.short_avg * (1.0 - rebuy_distance_pct)
    if next_rebuy_level <= 0:
        return None

    size_multiplier = PSRHStrategy._calc_rebuy_size_multiplier(math_adapter, spread_pct)
    raw_rebuy_notional = config.long_entry_size * size_multiplier * (1 + dca_steps * 0.5)
    raw_fill_quantity = raw_rebuy_notional / next_rebuy_level
    min_qty_for_notional = round_up_to_step(config.min_order_value / next_rebuy_level, qty_step)
    raw_fill_quantity = max(raw_fill_quantity, min_qty_for_notional)

    max_rebuy_usdt = getattr(config, "max_rebuy_usdt", 50.0)
    if raw_fill_quantity * next_rebuy_level > max_rebuy_usdt:
        raw_fill_quantity = max_rebuy_usdt / next_rebuy_level

    current_long_notional = state.long_size * state.long_avg
    if spread_pct < 0.03:
        max_rebuy_notional = current_long_notional * getattr(config, "sweep_small_cap_frac", 0.25)
    elif spread_pct < 0.04:
        max_rebuy_notional = current_long_notional * getattr(config, "sweep_mid_cap_frac", 0.35)
    else:
        max_rebuy_notional = current_long_notional * getattr(config, "sweep_large_cap_frac", 0.5)

    rebuy_notional = raw_fill_quantity * next_rebuy_level
    if rebuy_notional > max_rebuy_notional and next_rebuy_level > 0:
        raw_fill_quantity = max_rebuy_notional / next_rebuy_level

    fill_quantity = round_down_to_step(raw_fill_quantity, qty_step)
    fill_notional = fill_quantity * next_rebuy_level
    if fill_quantity <= 0 or fill_notional < config.min_order_value:
        return None

    next_step = dca_steps + 1
    if spread_pct < 0.03:
        adjust = True
    elif spread_pct < 0.05:
        adjust = next_step % 2 == 0
    else:
        adjust = next_step % 3 == 0

    return {
        "spread_pct": spread_pct,
        "size_multiplier": size_multiplier,
        "rebuy_distance_pct": rebuy_distance_pct,
        "next_rebuy_level": next_rebuy_level,
        "fill_quantity": fill_quantity,
        "fill_notional": fill_notional,
        "purpose": "LONG_REBUY_HEDGE" if adjust else "LONG_REBUY",
    }


def apply_repair_cycle_with_caps(
    *,
    state,
    config: StrategyConfig,
    qty_step: float,
    dca_steps: int,
    math_adapter: StrategyMathAdapter,
) -> dict[str, float | str] | None:
    plan = calc_rebuy_plan_with_caps(
        state=state,
        config=config,
        qty_step=qty_step,
        dca_steps=dca_steps,
        math_adapter=math_adapter,
    )
    if plan is None:
        return None

    rebuy_price = float(plan["next_rebuy_level"])
    rebuy_qty = float(plan["fill_quantity"])

    state.long_avg = weighted_average(state.long_size, state.long_avg, rebuy_qty, rebuy_price)
    state.long_size += rebuy_qty
    spread_after_long_fill_pct = compute_spread(state.long_avg, state.short_avg)

    short_add_mode = getattr(config, "sweep_short_add_mode", "immediate")
    short_add_gap_fill_frac = getattr(config, "sweep_short_add_gap_fill_frac", 1.0)
    target_short_size = state.long_size * config.short_ratio
    short_gap = max(target_short_size - state.short_size, 0.0)
    if short_add_mode == "final_only":
        short_add_qty = 0.0
    else:
        short_add_qty = round_down_to_step(short_gap * short_add_gap_fill_frac, qty_step)
    short_add_notional = short_add_qty * rebuy_price
    if short_add_qty > 0 and short_add_notional >= config.min_order_value:
        state.short_avg = weighted_average(
            state.short_size,
            state.short_avg,
            short_add_qty,
            rebuy_price,
        )
        state.short_size += short_add_qty
    else:
        short_add_qty = 0.0
        short_add_notional = 0.0

    state.price = rebuy_price
    spread_after_short_add_pct = (
        compute_spread(state.long_avg, state.short_avg)
        if short_add_qty > 0
        else None
    )
    return {
        "rebuy_notional": rebuy_qty * rebuy_price,
        "short_add_notional": short_add_notional,
        "spread_after_long_fill_pct": spread_after_long_fill_pct,
        "spread_after_short_add_pct": spread_after_short_add_pct,
        "size_multiplier": float(plan["size_multiplier"]),
        "purpose": str(plan["purpose"]),
        "short_add_mode": short_add_mode,
        "short_add_gap_fill_frac": float(short_add_gap_fill_frac),
    }


def apply_final_short_rebalance(
    *,
    state,
    config: StrategyConfig,
    qty_step: float,
) -> float:
    target_short_size = state.long_size * config.short_ratio
    short_gap = max(target_short_size - state.short_size, 0.0)
    short_add_qty = round_down_to_step(short_gap, qty_step)
    short_add_notional = short_add_qty * state.price
    if short_add_qty > 0 and short_add_notional >= config.min_order_value:
        state.short_avg = weighted_average(
            state.short_size,
            state.short_avg,
            short_add_qty,
            state.price,
        )
        state.short_size += short_add_qty
        return short_add_notional
    return 0.0


def run_single_repair_scenario(
    *,
    divider: float,
    base_multiplier: float,
    increment: float,
    span: float,
    recovery_base_step_pct: float,
    long_notional: float,
    short_notional: float,
    long_avg: float,
    short_avg: float,
    target_spread_pct: float,
    long_entry_size: float,
    short_ratio: float,
    max_rebuy_usdt: float,
    small_cap_frac: float,
    mid_cap_frac: float,
    large_cap_frac: float,
    short_add_gap_fill_frac: float,
    short_add_mode: str,
) -> dict[str, float | int | bool]:
    config = build_config(
        divider=divider,
        base_multiplier=base_multiplier,
        increment=increment,
        span=span,
        recovery_base_step_pct=recovery_base_step_pct,
        long_entry_size=long_entry_size,
        short_ratio=short_ratio,
        max_rebuy_usdt=max_rebuy_usdt,
        small_cap_frac=small_cap_frac,
        mid_cap_frac=mid_cap_frac,
        large_cap_frac=large_cap_frac,
        short_add_gap_fill_frac=short_add_gap_fill_frac,
        short_add_mode=short_add_mode,
    )
    math_adapter = StrategyMathAdapter(config=config)
    state = build_initial_state(
        long_avg=long_avg,
        short_avg=short_avg,
        long_notional=long_notional,
        short_notional=short_notional,
    )
    qty_step = infer_qty_step(short_avg)

    start_spread_pct = compute_spread(state.long_avg, state.short_avg)
    cycle_rows: list[dict[str, float]] = []
    dca_steps = 0
    while (
        compute_spread(state.long_avg, state.short_avg) > target_spread_pct
        and dca_steps < config.max_rebuy_loops
    ):
        row = apply_repair_cycle_with_caps(
            state=state,
            config=config,
            qty_step=qty_step,
            dca_steps=dca_steps,
            math_adapter=math_adapter,
        )
        if row is None:
            break
        cycle_rows.append(row)
        dca_steps += 1

    final_short_rebalance_notional = 0.0
    if getattr(config, "sweep_short_add_mode", "immediate") == "final_only":
        final_short_rebalance_notional = apply_final_short_rebalance(
            state=state,
            config=config,
            qty_step=qty_step,
        )

    final_spread_pct = compute_spread(state.long_avg, state.short_avg)
    final_ratio = state.short_size / state.long_size if state.long_size > 0 else 0.0
    success = final_spread_pct <= target_spread_pct + 1e-9
    first_cycle = cycle_rows[0] if cycle_rows else {}
    last_cycle = cycle_rows[-1] if cycle_rows else {}
    return {
        "divider": divider,
        "base_multiplier": base_multiplier,
        "increment": increment,
        "span": span,
        "recovery_base_step_pct": recovery_base_step_pct,
        "max_rebuy_usdt": max_rebuy_usdt,
        "small_cap_frac": small_cap_frac,
        "mid_cap_frac": mid_cap_frac,
        "large_cap_frac": large_cap_frac,
        "short_add_gap_fill_frac": short_add_gap_fill_frac,
        "short_add_mode": short_add_mode,
        "repair_cycles": dca_steps,
        "start_spread_pct": start_spread_pct,
        "target_spread_pct": target_spread_pct,
        "final_spread_pct": final_spread_pct,
        "final_ratio": final_ratio,
        "final_long_notional": state.long_size * state.price,
        "final_short_notional": state.short_size * state.price,
        "success": success,
        "first_rebuy_notional": float(first_cycle.get("rebuy_notional", 0.0)),
        "last_rebuy_notional": float(last_cycle.get("rebuy_notional", 0.0)),
        "final_short_rebalance_notional": final_short_rebalance_notional,
    }


def rank_results(results: Iterable[dict[str, float | int | bool]]) -> list[dict[str, float | int | bool]]:
    return sorted(
        results,
        key=lambda row: (
            0 if bool(row["success"]) else 1,
            float(row["final_long_notional"]),
            float(row["final_spread_pct"]),
            -float(row["final_ratio"]),
            int(row["repair_cycles"]),
            0 if str(row["short_add_mode"]) == "immediate" else 1,
            float(row["recovery_base_step_pct"]),
            float(row["max_rebuy_usdt"]),
        ),
    )


def log_results_table(
    *,
    title: str,
    rows: list[dict[str, float | int | bool]],
    limit: int,
) -> None:
    headers = (
        "divider",
        "base",
        "inc",
        "span",
        "base_step",
        "max_$",
        "caps",
        "short_add",
        "cycles",
        "final_long_$",
        "final_short_$",
        "final_ratio",
        "final_spread",
        "target_hit",
        "first_rebuy_$",
        "last_rebuy_$",
    )
    display_rows: list[tuple[str, ...]] = []
    for row in rows[:limit]:
        display_rows.append(
            (
                f"{float(row['divider']):.2f}",
                f"{float(row['base_multiplier']):.3f}",
                f"{float(row['increment']):.3f}",
                f"{float(row['span']):.4f}",
                f"{float(row['recovery_base_step_pct']):.4f}",
                f"{float(row['max_rebuy_usdt']):.0f}",
                (
                    f"{float(row['small_cap_frac']):.2f}/"
                    f"{float(row['mid_cap_frac']):.2f}/"
                    f"{float(row['large_cap_frac']):.2f}"
                ),
                (
                    f"{str(row['short_add_mode'])}:"
                    f"{float(row['short_add_gap_fill_frac']):.2f}"
                ),
                str(int(row["repair_cycles"])),
                f"{float(row['final_long_notional']):.2f}",
                f"{float(row['final_short_notional']):.2f}",
                f"{float(row['final_ratio']):.4f}",
                percent(float(row["final_spread_pct"])),
                "yes" if bool(row["success"]) else "no",
                f"{float(row['first_rebuy_notional']):.2f}",
                f"{float(row['last_rebuy_notional']):.2f}",
            )
        )

    widths = [len(header) for header in headers]
    for row in display_rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def format_row(values: tuple[str, ...]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    LOGGER.info(title)
    LOGGER.info(format_row(headers))
    LOGGER.info(separator)
    for row in display_rows:
        LOGGER.info(format_row(row))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sweep repair divider and multiplier options for a fixed burn outcome.",
    )
    parser.add_argument("--long-notional", type=float, default=60.0)
    parser.add_argument("--short-notional", type=float, default=30.0)
    parser.add_argument("--long-avg", type=float, default=1.0)
    parser.add_argument("--short-avg", type=float, default=0.98)
    parser.add_argument("--target-spread-pct", type=float, default=0.01)
    parser.add_argument("--long-entry-size", type=float, default=60.0)
    parser.add_argument("--short-ratio", type=float, default=0.5)
    parser.add_argument("--dividers", type=str, default="3,4,5")
    parser.add_argument("--base-multipliers", type=str, default="0.20,0.25,0.30,0.35,0.40,0.50")
    parser.add_argument("--increments", type=str, default="0.00,0.025,0.05")
    parser.add_argument("--spans", type=str, default="0.005,0.01")
    parser.add_argument("--recovery-base-steps", type=str, default="0.0025,0.005,0.0075,0.01")
    parser.add_argument("--max-rebuy-usdts", type=str, default="20,30,50,75")
    parser.add_argument("--small-cap-fracs", type=str, default="0.15,0.20,0.25")
    parser.add_argument("--mid-cap-fracs", type=str, default="0.25,0.30,0.35")
    parser.add_argument("--large-cap-fracs", type=str, default="0.35,0.50")
    parser.add_argument("--short-add-modes", type=str, default="immediate,final_only")
    parser.add_argument("--short-add-gap-fill-fracs", type=str, default="1.0,0.75,0.5,0.25")
    parser.add_argument("--min-final-ratio", type=float, default=0.0)
    parser.add_argument("--top", type=int, default=15)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dividers = parse_float_list(args.dividers)
    base_multipliers = parse_float_list(args.base_multipliers)
    increments = parse_float_list(args.increments)
    spans = parse_float_list(args.spans)
    recovery_base_steps = parse_float_list(args.recovery_base_steps)
    max_rebuy_usdts = parse_float_list(args.max_rebuy_usdts)
    small_cap_fracs = parse_float_list(args.small_cap_fracs)
    mid_cap_fracs = parse_float_list(args.mid_cap_fracs)
    large_cap_fracs = parse_float_list(args.large_cap_fracs)
    short_add_modes = parse_str_list(args.short_add_modes)
    short_add_gap_fill_fracs = parse_float_list(args.short_add_gap_fill_fracs)

    results: list[dict[str, float | int | bool]] = []
    for (
        divider,
        base_multiplier,
        increment,
        span,
        recovery_base_step_pct,
        max_rebuy_usdt,
        small_cap_frac,
        mid_cap_frac,
        large_cap_frac,
        short_add_mode,
        short_add_gap_fill_frac,
    ) in itertools.product(
        dividers,
        base_multipliers,
        increments,
        spans,
        recovery_base_steps,
        max_rebuy_usdts,
        small_cap_fracs,
        mid_cap_fracs,
        large_cap_fracs,
        short_add_modes,
        short_add_gap_fill_fracs,
    ):
        if not (small_cap_frac <= mid_cap_frac <= large_cap_frac):
            continue
        if short_add_mode == "final_only" and short_add_gap_fill_frac != short_add_gap_fill_fracs[0]:
            continue
        result = run_single_repair_scenario(
            divider=divider,
            base_multiplier=base_multiplier,
            increment=increment,
            span=span,
            recovery_base_step_pct=recovery_base_step_pct,
            max_rebuy_usdt=max_rebuy_usdt,
            small_cap_frac=small_cap_frac,
            mid_cap_frac=mid_cap_frac,
            large_cap_frac=large_cap_frac,
            short_add_gap_fill_frac=short_add_gap_fill_frac,
            short_add_mode=short_add_mode,
            long_notional=args.long_notional,
            short_notional=args.short_notional,
            long_avg=args.long_avg,
            short_avg=args.short_avg,
            target_spread_pct=args.target_spread_pct,
            long_entry_size=args.long_entry_size,
            short_ratio=args.short_ratio,
        )
        results.append(result)

    ranked = rank_results(results)
    successful = [row for row in ranked if bool(row["success"])]
    ratio_qualified = [
        row for row in successful if float(row["final_ratio"]) >= args.min_final_ratio
    ]
    unsuccessful = [row for row in ranked if not bool(row["success"])]

    LOGGER.info(
        "Sweep finished: %s combinations, %s reached target %s, %s met min final ratio %.4f",
        len(results),
        len(successful),
        percent(args.target_spread_pct),
        len(ratio_qualified),
        args.min_final_ratio,
    )
    if ratio_qualified:
        log_results_table(
            title="Best successful combinations after ratio filter",
            rows=ratio_qualified,
            limit=args.top,
        )
    elif successful:
        LOGGER.info("No successful combinations met the ratio filter.")
        log_results_table(
            title="Best successful combinations before ratio filter",
            rows=successful,
            limit=args.top,
        )
    else:
        LOGGER.info("No successful combinations reached the target spread.")

    if unsuccessful:
        log_results_table(
            title="Closest unsuccessful combinations",
            rows=unsuccessful,
            limit=min(args.top, 10),
        )


if __name__ == "__main__":
    main()
