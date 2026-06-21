from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REFERENCE_ROOT = Path("/home/telgenbuescher/projects/burn_reentry_simple")
if str(REFERENCE_ROOT) not in sys.path:
    sys.path.insert(0, str(REFERENCE_ROOT))

from bots.shared.burn_logic import calculate_new_position_after_burn, plan_profit_burn
from strategy.config import StrategyConfig
from strategy.psrh_strategy import PSRHStrategy

LOGGER = logging.getLogger("burn_repair_sequence")
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(logging.StreamHandler())

DEFAULT_LONG_AVG = 0.08267
DEFAULT_SHORT_AVG = 0.08157
DEFAULT_LONG_NOTIONAL = 100.0
DEFAULT_SHORT_NOTIONAL = 50.0
DEFAULT_BURN_TRIGGER_PCT = 0.006
DEFAULT_BURN_STEPS = 3
DEFAULT_BURN_PCT = 0.5
DEFAULT_BURN_PROFIT_PCT = 0.7
DEFAULT_SHORT_REENTRY_OFFSET_PCT = 0.0005
DEFAULT_REPAIR_TARGET_FACTOR = 0.5
DEFAULT_TARGET_LONG_NOTIONAL_AFTER_BURNS = 60.0


@dataclass
class HedgeState:
    long_size: float
    short_size: float
    long_avg: float
    short_avg: float
    price: float


@dataclass
class StrategyMathAdapter:
    config: StrategyConfig

    def _select_rebuy_divider(self, spread_pct: float) -> float:
        return PSRHStrategy._select_rebuy_divider(self, spread_pct)


def percent(value: float) -> str:
    return f"{value * 100:.4f}%"


def round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        return max(value, 0.0)
    rounded = math.floor(value / step) * step
    return max(rounded, 0.0)


def round_up_to_step(value: float, step: float) -> float:
    if step <= 0:
        return max(value, 0.0)
    rounded = math.ceil(value / step) * step
    return max(rounded, 0.0)


def weighted_average(size_a: float, avg_a: float, size_b: float, price_b: float) -> float:
    total_size = size_a + size_b
    if total_size <= 0:
        return 0.0
    return ((size_a * avg_a) + (size_b * price_b)) / total_size


def compute_spread(long_avg: float, short_avg: float) -> float:
    if long_avg <= 0 or short_avg <= 0:
        return 0.0
    return max((long_avg - short_avg) / long_avg, 0.0)


def state_snapshot(state: HedgeState) -> dict[str, float]:
    ratio = state.short_size / state.long_size if state.long_size > 0 else 0.0
    return {
        "price": state.price,
        "long_size": state.long_size,
        "short_size": state.short_size,
        "long_avg": state.long_avg,
        "short_avg": state.short_avg,
        "long_notional": state.long_size * state.price,
        "short_notional": state.short_size * state.price,
        "ratio": ratio,
        "spread_pct": compute_spread(state.long_avg, state.short_avg),
    }


def infer_qty_step(price: float) -> float:
    if price >= 1000:
        return 0.001
    if price >= 10:
        return 0.01
    return 0.1


def build_initial_state(
    *,
    long_avg: float,
    short_avg: float,
    long_notional: float,
    short_notional: float,
) -> HedgeState:
    reference_price = short_avg
    return HedgeState(
        long_size=long_notional / long_avg if long_avg > 0 else 0.0,
        short_size=short_notional / short_avg if short_avg > 0 else 0.0,
        long_avg=long_avg,
        short_avg=short_avg,
        price=reference_price,
    )


def simulate_burn_phase(
    *,
    state: HedgeState,
    burn_steps: int,
    burn_trigger_pct: float,
    burn_pct: float,
    burn_profit_pct: float,
    short_ratio: float,
    qty_step: float,
    short_reentry_offset_pct: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for index in range(1, burn_steps + 1):
        burn_price = state.short_avg * (1.0 - burn_trigger_pct)
        realized_profit = max((state.short_avg - burn_price) * state.short_size, 0.0)
        burn_plan = plan_profit_burn(
            realized_profit=realized_profit,
            burn_pct=burn_pct,
            loss_price=burn_price,
            position_avg=state.long_avg,
            position_size=state.long_size,
            qty_step=qty_step,
            min_qty=qty_step,
            burn_profit_pct=burn_profit_pct,
        )
        if not burn_plan:
            break

        burn_size = float(burn_plan["burn_coins_clamped"])
        burn_loss_usdt = float(burn_plan["burn_usdt_target"])
        new_long_size, new_long_avg, _, _ = calculate_new_position_after_burn(
            state.long_size,
            state.long_avg,
            burn_size,
            burn_price,
        )
        reentry_short_avg = burn_price * (1.0 - short_reentry_offset_pct)
        reentry_short_size = round_down_to_step(new_long_size * short_ratio, qty_step)

        state.long_size = new_long_size
        state.long_avg = new_long_avg
        state.short_size = reentry_short_size
        state.short_avg = reentry_short_avg
        state.price = burn_price

        snapshot = state_snapshot(state)
        snapshot.update(
            {
                "phase": "burn",
                "step": index,
                "burn_price": burn_price,
                "burn_size": burn_size,
                "realized_profit": realized_profit,
                "burn_loss_usdt": burn_loss_usdt,
                "net_burn_pnl": realized_profit - burn_loss_usdt,
            }
        )
        results.append(snapshot)

    return results


def simulate_target_burn_phase(
    *,
    state: HedgeState,
    burn_steps: int,
    burn_trigger_pct: float,
    short_ratio: float,
    qty_step: float,
    short_reentry_offset_pct: float,
    target_long_notional_after_burns: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if burn_steps <= 0:
        return results

    target_long_size = (
        target_long_notional_after_burns / state.long_avg if state.long_avg > 0 else 0.0
    )
    target_long_size = max(target_long_size, 0.0)

    for index in range(1, burn_steps + 1):
        remaining_steps = burn_steps - index + 1
        if state.long_size <= target_long_size:
            break

        burn_price = state.short_avg * (1.0 - burn_trigger_pct)
        remaining_reduction = max(state.long_size - target_long_size, 0.0)
        burn_size = round_down_to_step(remaining_reduction / remaining_steps, qty_step)
        if burn_size <= 0:
            burn_size = min(round_down_to_step(remaining_reduction, qty_step), state.long_size)
        if burn_size <= 0:
            break

        realized_profit = max((state.short_avg - burn_price) * state.short_size, 0.0)
        burn_loss_usdt = burn_size * abs(state.long_avg - burn_price)
        new_long_size, new_long_avg, _, _ = calculate_new_position_after_burn(
            state.long_size,
            state.long_avg,
            burn_size,
            burn_price,
        )
        reentry_short_avg = burn_price * (1.0 - short_reentry_offset_pct)
        reentry_short_size = round_down_to_step(new_long_size * short_ratio, qty_step)

        state.long_size = new_long_size
        state.long_avg = new_long_avg
        state.short_size = reentry_short_size
        state.short_avg = reentry_short_avg
        state.price = burn_price

        snapshot = state_snapshot(state)
        snapshot.update(
            {
                "phase": "burn_target",
                "step": index,
                "burn_price": burn_price,
                "burn_size": burn_size,
                "realized_profit": realized_profit,
                "burn_loss_usdt": burn_loss_usdt,
                "net_burn_pnl": realized_profit - burn_loss_usdt,
                "target_long_notional_after_burns": target_long_notional_after_burns,
            }
        )
        results.append(snapshot)

    return results


def calc_rebuy_plan(
    *,
    state: HedgeState,
    config: StrategyConfig,
    qty_step: float,
    dca_steps: int,
    math_adapter: StrategyMathAdapter,
) -> dict[str, Any] | None:
    spread_pct = compute_spread(state.long_avg, state.short_avg)
    rebuy_distance_pct, distance_info = PSRHStrategy._calc_rebuy_distance_pct(
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
        max_rebuy_notional = current_long_notional * 0.25
    elif spread_pct < 0.04:
        max_rebuy_notional = current_long_notional * 0.35
    else:
        max_rebuy_notional = current_long_notional * 0.5

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
        "distance_info": distance_info,
        "size_multiplier": size_multiplier,
        "rebuy_distance_pct": rebuy_distance_pct,
        "next_rebuy_level": next_rebuy_level,
        "fill_quantity": fill_quantity,
        "fill_notional": fill_notional,
        "purpose": "LONG_REBUY_HEDGE" if adjust else "LONG_REBUY",
    }


def apply_repair_cycle(
    *,
    state: HedgeState,
    config: StrategyConfig,
    qty_step: float,
    dca_steps: int,
    math_adapter: StrategyMathAdapter,
) -> dict[str, Any] | None:
    plan = calc_rebuy_plan(
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
    spread_after_long_fill = compute_spread(state.long_avg, state.short_avg)

    target_short_size = state.long_size * config.short_ratio
    short_add_qty = round_down_to_step(max(target_short_size - state.short_size, 0.0), qty_step)
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

    snapshot = state_snapshot(state)
    snapshot.update(
        {
            "phase": "repair",
            "step": dca_steps + 1,
            "rebuy_price": rebuy_price,
            "rebuy_qty": rebuy_qty,
            "rebuy_notional": rebuy_qty * rebuy_price,
            "short_add_qty": short_add_qty,
            "short_add_notional": short_add_notional,
            "spread_before_repair_pct": float(plan["spread_pct"]),
            "spread_after_long_fill_pct": spread_after_long_fill,
            "spread_after_short_add_pct": snapshot["spread_pct"],
            "size_multiplier": float(plan["size_multiplier"]),
            "rebuy_distance_pct": float(plan["rebuy_distance_pct"]),
            "purpose": plan["purpose"],
        }
    )
    return snapshot


def run_simulation(
    *,
    long_avg: float = DEFAULT_LONG_AVG,
    short_avg: float = DEFAULT_SHORT_AVG,
    long_notional: float = DEFAULT_LONG_NOTIONAL,
    short_notional: float = DEFAULT_SHORT_NOTIONAL,
    burn_steps: int = DEFAULT_BURN_STEPS,
    burn_trigger_pct: float = DEFAULT_BURN_TRIGGER_PCT,
    burn_pct: float = DEFAULT_BURN_PCT,
    burn_profit_pct: float = DEFAULT_BURN_PROFIT_PCT,
    repair_target_factor: float = DEFAULT_REPAIR_TARGET_FACTOR,
) -> list[dict[str, Any]]:
    config = StrategyConfig(api_key="", secret_key="")
    qty_step = infer_qty_step(short_avg)
    state = build_initial_state(
        long_avg=long_avg,
        short_avg=short_avg,
        long_notional=long_notional,
        short_notional=short_notional,
    )
    math_adapter = StrategyMathAdapter(config=config)

    results: list[dict[str, Any]] = []
    start_snapshot = state_snapshot(state)
    start_snapshot.update({"phase": "start", "step": 0})
    results.append(start_snapshot)

    results.extend(
        simulate_burn_phase(
            state=state,
            burn_steps=burn_steps,
            burn_trigger_pct=burn_trigger_pct,
            burn_pct=burn_pct,
            burn_profit_pct=burn_profit_pct,
            short_ratio=config.short_ratio,
            qty_step=qty_step,
            short_reentry_offset_pct=DEFAULT_SHORT_REENTRY_OFFSET_PCT,
        )
    )

    spread_after_burns = compute_spread(state.long_avg, state.short_avg)
    target_spread_pct = spread_after_burns * repair_target_factor
    dca_steps = 0
    while compute_spread(state.long_avg, state.short_avg) > target_spread_pct and dca_steps < config.max_rebuy_loops:
        repair_snapshot = apply_repair_cycle(
            state=state,
            config=config,
            qty_step=qty_step,
            dca_steps=dca_steps,
            math_adapter=math_adapter,
        )
        if repair_snapshot is None:
            break
        results.append(repair_snapshot)
        dca_steps += 1

    final_snapshot = state_snapshot(state)
    final_snapshot.update(
        {
            "phase": "final",
            "step": dca_steps,
            "target_spread_pct": target_spread_pct,
            "repair_cycles": dca_steps,
            "qty_step": qty_step,
        }
    )
    results.append(final_snapshot)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate burn then repair using live bot math.")
    parser.add_argument("--long-avg", type=float, default=DEFAULT_LONG_AVG)
    parser.add_argument("--short-avg", type=float, default=DEFAULT_SHORT_AVG)
    parser.add_argument("--long-notional", type=float, default=DEFAULT_LONG_NOTIONAL)
    parser.add_argument("--short-notional", type=float, default=DEFAULT_SHORT_NOTIONAL)
    parser.add_argument("--burn-steps", type=int, default=DEFAULT_BURN_STEPS)
    parser.add_argument("--burn-trigger-pct", type=float, default=DEFAULT_BURN_TRIGGER_PCT)
    parser.add_argument("--burn-pct", type=float, default=DEFAULT_BURN_PCT)
    parser.add_argument("--burn-profit-pct", type=float, default=DEFAULT_BURN_PROFIT_PCT)
    parser.add_argument("--repair-target-factor", type=float, default=DEFAULT_REPAIR_TARGET_FACTOR)
    parser.add_argument(
        "--target-long-notional-after-burns",
        type=float,
        default=DEFAULT_TARGET_LONG_NOTIONAL_AFTER_BURNS,
        help="Target long notional used by the stronger target-burn comparison mode.",
    )
    parser.add_argument(
        "--table-only",
        action="store_true",
        help="Print only the compact summary table.",
    )
    parser.add_argument(
        "--compare-burn-modes",
        action="store_true",
        help="Compare multiple burn variants side by side before repair.",
    )
    return parser.parse_args()


def _table_value(row: dict[str, Any], phase: str) -> str:
    if phase == "BURN":
        return f"burn={row['burn_size']:.1f}"
    if phase == "BURN_TARGET":
        return f"burn={row['burn_size']:.1f} target={row['target_long_notional_after_burns']:.2f}"
    if phase == "REPAIR":
        return (
            f"rebuy={row['rebuy_notional']:.2f}"
            f" short_add={row['short_add_notional']:.2f}"
            f" mult={row['size_multiplier']:.3f}"
        )
    if phase == "FINAL":
        return f"target={percent(row['target_spread_pct'])}"
    return "-"


def log_results_table(results: list[dict[str, Any]]) -> None:
    headers = (
        "phase",
        "step",
        "price",
        "long_$",
        "short_$",
        "ratio",
        "spread",
        "long_avg",
        "short_avg",
        "details",
    )
    rows: list[tuple[str, ...]] = []
    for row in results:
        phase = str(row["phase"]).upper()
        rows.append(
            (
                phase,
                str(row["step"]),
                f"{row['price']:.8f}",
                f"{row['long_notional']:.2f}",
                f"{row['short_notional']:.2f}",
                f"{row['ratio']:.4f}",
                percent(row["spread_pct"]),
                f"{row['long_avg']:.8f}",
                f"{row['short_avg']:.8f}",
                _table_value(row, phase),
            )
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def format_row(values: tuple[str, ...]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    LOGGER.info("Compact summary table")
    LOGGER.info(format_row(headers))
    LOGGER.info(separator)
    for row in rows:
        LOGGER.info(format_row(row))


def log_results(results: list[dict[str, Any]]) -> None:
    for row in results:
        phase = str(row["phase"]).upper()
        LOGGER.info(
            (
                "%-6s step=%s price=%.8f long=$%.2f short=$%.2f "
                "ratio=%.4f spread=%s long_avg=%.8f short_avg=%.8f"
            ),
            phase,
            row["step"],
            row["price"],
            row["long_notional"],
            row["short_notional"],
            row["ratio"],
            percent(row["spread_pct"]),
            row["long_avg"],
            row["short_avg"],
        )
        if phase == "BURN":
            LOGGER.info(
                "       burn_price=%.8f burn_qty=%.4f net_burn_pnl=%.4f",
                row["burn_price"],
                row["burn_size"],
                row["net_burn_pnl"],
            )
        if phase == "BURN_TARGET":
            LOGGER.info(
                "       burn_price=%.8f burn_qty=%.4f target_long_after_burns=%.2f net_burn_pnl=%.4f",
                row["burn_price"],
                row["burn_size"],
                row["target_long_notional_after_burns"],
                row["net_burn_pnl"],
            )
        if phase == "REPAIR":
            LOGGER.info(
                (
                    "       rebuy_price=%.8f rebuy_qty=%.4f short_add_qty=%.4f "
                    "multiplier=%.4f spread_before=%s spread_after_long=%s spread_after_short=%s purpose=%s"
                ),
                row["rebuy_price"],
                row["rebuy_qty"],
                row["short_add_qty"],
                row["size_multiplier"],
                percent(row["spread_before_repair_pct"]),
                percent(row["spread_after_long_fill_pct"]),
                percent(row["spread_after_short_add_pct"]),
                row["purpose"],
            )
        if phase == "FINAL":
            LOGGER.info(
                "       repair_cycles=%s target_spread=%s qty_step=%s",
                row["repair_cycles"],
                percent(row["target_spread_pct"]),
                row["qty_step"],
            )
    log_results_table(results)


def summarize_burn_only_run(
    *,
    label: str,
    start_state: HedgeState,
    burn_rows: list[dict[str, Any]],
) -> dict[str, str]:
    final_row = burn_rows[-1] if burn_rows else state_snapshot(start_state)
    start_long_notional = start_state.long_size * start_state.price
    final_long_notional = float(final_row["long_notional"])
    final_short_notional = float(final_row["short_notional"])
    long_reduction_pct = (
        ((start_long_notional - final_long_notional) / start_long_notional)
        if start_long_notional > 0
        else 0.0
    )
    total_burn_qty = sum(float(row.get("burn_size", 0.0)) for row in burn_rows)
    return {
        "mode": label,
        "burn_steps": str(len(burn_rows)),
        "final_long_$": f"{final_long_notional:.2f}",
        "final_short_$": f"{final_short_notional:.2f}",
        "long_reduction": percent(long_reduction_pct),
        "final_spread": percent(float(final_row["spread_pct"])),
        "total_burn_qty": f"{total_burn_qty:.1f}",
    }


def log_burn_mode_comparison(rows: list[dict[str, str]]) -> None:
    headers = (
        "mode",
        "burn_steps",
        "final_long_$",
        "final_short_$",
        "long_reduction",
        "final_spread",
        "total_burn_qty",
    )
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, header in enumerate(headers):
            widths[idx] = max(widths[idx], len(row[header]))

    def format_row(values: tuple[str, ...]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    LOGGER.info("Burn mode comparison")
    LOGGER.info(format_row(headers))
    LOGGER.info(separator)
    for row in rows:
        LOGGER.info(format_row(tuple(row[header] for header in headers)))


def run_burn_mode_comparison(
    *,
    long_avg: float,
    short_avg: float,
    long_notional: float,
    short_notional: float,
    burn_steps: int,
    burn_trigger_pct: float,
    burn_pct: float,
    burn_profit_pct: float,
    target_long_notional_after_burns: float,
) -> None:
    qty_step = infer_qty_step(short_avg)
    config = StrategyConfig(api_key="", secret_key="")

    def make_state() -> HedgeState:
        return build_initial_state(
            long_avg=long_avg,
            short_avg=short_avg,
            long_notional=long_notional,
            short_notional=short_notional,
        )

    current_state = make_state()
    current_rows = simulate_burn_phase(
        state=current_state,
        burn_steps=burn_steps,
        burn_trigger_pct=burn_trigger_pct,
        burn_pct=burn_pct,
        burn_profit_pct=burn_profit_pct,
        short_ratio=config.short_ratio,
        qty_step=qty_step,
        short_reentry_offset_pct=DEFAULT_SHORT_REENTRY_OFFSET_PCT,
    )

    unlimited_state = make_state()
    unlimited_rows = simulate_burn_phase(
        state=unlimited_state,
        burn_steps=burn_steps,
        burn_trigger_pct=burn_trigger_pct,
        burn_pct=burn_pct,
        burn_profit_pct=None,
        short_ratio=config.short_ratio,
        qty_step=qty_step,
        short_reentry_offset_pct=DEFAULT_SHORT_REENTRY_OFFSET_PCT,
    )

    target_state = make_state()
    target_rows = simulate_target_burn_phase(
        state=target_state,
        burn_steps=burn_steps,
        burn_trigger_pct=burn_trigger_pct,
        short_ratio=config.short_ratio,
        qty_step=qty_step,
        short_reentry_offset_pct=DEFAULT_SHORT_REENTRY_OFFSET_PCT,
        target_long_notional_after_burns=target_long_notional_after_burns,
    )

    comparison_rows = [
        summarize_burn_only_run(
            label=f"current_budget_{burn_profit_pct:.2f}",
            start_state=make_state(),
            burn_rows=current_rows,
        ),
        summarize_burn_only_run(
            label="no_budget_limit",
            start_state=make_state(),
            burn_rows=unlimited_rows,
        ),
        summarize_burn_only_run(
            label=f"target_long_{target_long_notional_after_burns:.0f}",
            start_state=make_state(),
            burn_rows=target_rows,
        ),
    ]
    log_burn_mode_comparison(comparison_rows)


def main() -> None:
    args = parse_args()
    if args.compare_burn_modes:
        run_burn_mode_comparison(
            long_avg=args.long_avg,
            short_avg=args.short_avg,
            long_notional=args.long_notional,
            short_notional=args.short_notional,
            burn_steps=args.burn_steps,
            burn_trigger_pct=args.burn_trigger_pct,
            burn_pct=args.burn_pct,
            burn_profit_pct=args.burn_profit_pct,
            target_long_notional_after_burns=args.target_long_notional_after_burns,
        )
        return

    results = run_simulation(
        long_avg=args.long_avg,
        short_avg=args.short_avg,
        long_notional=args.long_notional,
        short_notional=args.short_notional,
        burn_steps=args.burn_steps,
        burn_trigger_pct=args.burn_trigger_pct,
        burn_pct=args.burn_pct,
        burn_profit_pct=args.burn_profit_pct,
        repair_target_factor=args.repair_target_factor,
    )
    if args.table_only:
        log_results_table(results)
    else:
        log_results(results)


if __name__ == "__main__":
    main()
