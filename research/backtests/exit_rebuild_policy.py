"""Backtest-only basket exit rebuild policy math (research-only)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

ExitRebuildPolicyName = Literal[
    "current",
    "non_worsening",
    "non_worsening_coverage_gate",
    "inventory_mtm",
]

POLICY_NAMES: tuple[ExitRebuildPolicyName, ...] = (
    "current",
    "non_worsening",
    "non_worsening_coverage_gate",
    "inventory_mtm",
)


@dataclass(frozen=True)
class ExitRebuildPolicyConfig:
    policy: ExitRebuildPolicyName = "current"
    coverage_tolerance_usdt: float = 0.02


@dataclass
class ExitPolicyDecision:
    policy: str
    primary_side: str
    raw_exit: float
    active_exit: float | None
    effective_exit: float
    prevented_increase: bool
    old_exit_covered: bool | None
    reason: str
    required_trade_profit: float | None = None
    pnl_at_active_exit: float | None = None
    pnl_at_effective_exit: float | None = None


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def required_trade_profit_usdt(
    *,
    primary_notional: float,
    tp_profit_target_pct: float,
    tp_buffer_pct: float,
) -> float:
    return (
        primary_notional * float(tp_profit_target_pct) / 100.0
        + primary_notional * float(tp_buffer_pct) / 100.0
    )


def expected_trade_pnl_at_exit(
    *,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    exit_price: float,
    realized_trade_pnl: float,
    fee_rate: float,
) -> float:
    long_pnl = (exit_price - long_avg) * long_qty
    short_pnl = (short_avg - exit_price) * short_qty
    entry_fees = fee_rate * (long_avg * long_qty + short_avg * short_qty)
    close_fees = fee_rate * exit_price * (long_qty + short_qty)
    return realized_trade_pnl + long_pnl + short_pnl - entry_fees - close_fees


def is_exit_covered(
    *,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    exit_price: float,
    realized_trade_pnl: float,
    fee_rate: float,
    required_profit: float,
    tolerance_usdt: float = 0.02,
) -> bool:
    pnl = expected_trade_pnl_at_exit(
        long_qty=long_qty,
        long_avg=long_avg,
        short_qty=short_qty,
        short_avg=short_avg,
        exit_price=exit_price,
        realized_trade_pnl=realized_trade_pnl,
        fee_rate=fee_rate,
    )
    return pnl + tolerance_usdt >= required_profit


def solve_fee_adjusted_long_exit(
    *,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    required_profit_usdt: float,
    fee_rate: float,
) -> float | None:
    """Lowest long-primary exit satisfying inventory MTM + fee equation.

    Same closed-form as live ``_calculate_tp_projection`` fee adjustment, using
    ``required_profit_usdt`` on the RHS (target+buffer, optionally with losses).
    """
    net_qty = long_qty - short_qty
    if abs(net_qty) <= 1e-12:
        return None
    long_notional = long_avg * long_qty
    short_notional = short_avg * short_qty
    entry_fee = fee_rate * (long_notional + short_notional)
    if fee_rate <= 0:
        # raw: (long_notional - short_notional + required) / net_qty
        return (long_notional - short_notional + required_profit_usdt) / net_qty
    denom = net_qty - fee_rate * (long_qty + short_qty)
    if abs(denom) <= 1e-12:
        return None
    return (long_notional - short_notional + required_profit_usdt + entry_fee) / denom


def round_exit_preserving_long_coverage(
    price: float,
    *,
    tick_size: float,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    realized_trade_pnl: float,
    fee_rate: float,
    required_profit: float,
    tolerance_usdt: float,
) -> float:
    if price <= 0 or tick_size <= 0:
        return max(price, 0.0)
    # Round half-up first (live normalize), then bump up ticks until covered.
    ticks = round(price / tick_size)
    rounded = ticks * tick_size
    # Prefer never rounding down below coverage for long-primary.
    candidate = rounded
    for _ in range(20):
        if is_exit_covered(
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            exit_price=candidate,
            realized_trade_pnl=realized_trade_pnl,
            fee_rate=fee_rate,
            required_profit=required_profit,
            tolerance_usdt=tolerance_usdt,
        ):
            return candidate
        candidate = (math.floor(candidate / tick_size) + 1) * tick_size
    return candidate


def apply_exit_rebuild_policy(
    *,
    policy: ExitRebuildPolicyName,
    primary_side: str,
    raw_exit: float,
    active_exit: float | None,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    realized_trade_pnl: float,
    fee_rate: float,
    tp_profit_target_pct: float,
    tp_buffer_pct: float,
    tick_size: float,
    coverage_tolerance_usdt: float = 0.02,
) -> ExitPolicyDecision:
    side = str(primary_side or "long").lower()
    primary_notional = (long_avg * long_qty) if side == "long" else (short_avg * short_qty)
    required = required_trade_profit_usdt(
        primary_notional=primary_notional,
        tp_profit_target_pct=tp_profit_target_pct,
        tp_buffer_pct=tp_buffer_pct,
    )
    old_covered: bool | None = None
    pnl_at_active: float | None = None
    if active_exit is not None and active_exit > 0:
        pnl_at_active = expected_trade_pnl_at_exit(
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            exit_price=active_exit,
            realized_trade_pnl=realized_trade_pnl,
            fee_rate=fee_rate,
        )
        old_covered = pnl_at_active + coverage_tolerance_usdt >= required

    if policy == "current":
        effective = raw_exit
        reason = "current_raw"
    elif policy == "non_worsening":
        if active_exit is None or active_exit <= 0:
            effective = raw_exit
            reason = "non_worsening_no_active"
        elif side == "long":
            effective = min(active_exit, raw_exit)
            reason = "non_worsening_min" if effective < raw_exit - 1e-12 else "non_worsening_unchanged_or_lower_raw"
        else:
            effective = max(active_exit, raw_exit)
            reason = "non_worsening_max"
    elif policy == "non_worsening_coverage_gate":
        if active_exit is not None and active_exit > 0 and old_covered:
            if side == "long":
                effective = min(active_exit, raw_exit)
            else:
                effective = max(active_exit, raw_exit)
            # If choosing raw lowers exit, still require coverage at effective.
            if not is_exit_covered(
                long_qty=long_qty,
                long_avg=long_avg,
                short_qty=short_qty,
                short_avg=short_avg,
                exit_price=effective,
                realized_trade_pnl=realized_trade_pnl,
                fee_rate=fee_rate,
                required_profit=required,
                tolerance_usdt=coverage_tolerance_usdt,
            ):
                effective = active_exit
                reason = "coverage_gate_keep_old_covered"
            else:
                reason = "coverage_gate_keep_or_improve_covered_old"
        else:
            solved = solve_fee_adjusted_long_exit(
                long_qty=long_qty,
                long_avg=long_avg,
                short_qty=short_qty,
                short_avg=short_avg,
                # Coverage gate fallback: recover to required_trade_profit using
                # realized already on the LHS → required on RHS is target+buffer only
                # relative to total trade PnL. Equivalently shift by realized:
                required_profit_usdt=required - realized_trade_pnl,
                fee_rate=fee_rate,
            )
            if solved is None or solved <= 0:
                effective = raw_exit
                reason = "coverage_gate_fallback_raw_unsolvable"
            else:
                effective = round_exit_preserving_long_coverage(
                    solved,
                    tick_size=tick_size,
                    long_qty=long_qty,
                    long_avg=long_avg,
                    short_qty=short_qty,
                    short_avg=short_avg,
                    realized_trade_pnl=realized_trade_pnl,
                    fee_rate=fee_rate,
                    required_profit=required,
                    tolerance_usdt=coverage_tolerance_usdt,
                )
                reason = "coverage_gate_min_covered_exit"
    elif policy == "inventory_mtm":
        solved = solve_fee_adjusted_long_exit(
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            required_profit_usdt=required - realized_trade_pnl,
            fee_rate=fee_rate,
        )
        if solved is None or solved <= 0:
            effective = raw_exit
            reason = "inventory_mtm_fallback_raw"
        else:
            effective = round_exit_preserving_long_coverage(
                solved,
                tick_size=tick_size,
                long_qty=long_qty,
                long_avg=long_avg,
                short_qty=short_qty,
                short_avg=short_avg,
                realized_trade_pnl=realized_trade_pnl,
                fee_rate=fee_rate,
                required_profit=required,
                tolerance_usdt=coverage_tolerance_usdt,
            )
            reason = "inventory_mtm_solved"
    else:
        raise ValueError(f"unknown exit rebuild policy: {policy}")

    prevented = False
    if (
        side == "long"
        and active_exit is not None
        and active_exit > 0
        and raw_exit > active_exit + 1e-12
        and effective <= active_exit + 1e-12
    ):
        prevented = True

    pnl_at_eff = expected_trade_pnl_at_exit(
        long_qty=long_qty,
        long_avg=long_avg,
        short_qty=short_qty,
        short_avg=short_avg,
        exit_price=effective,
        realized_trade_pnl=realized_trade_pnl,
        fee_rate=fee_rate,
    )
    return ExitPolicyDecision(
        policy=policy,
        primary_side=side,
        raw_exit=raw_exit,
        active_exit=active_exit,
        effective_exit=effective,
        prevented_increase=prevented,
        old_exit_covered=old_covered,
        reason=reason,
        required_trade_profit=required,
        pnl_at_active_exit=pnl_at_active,
        pnl_at_effective_exit=pnl_at_eff,
    )
