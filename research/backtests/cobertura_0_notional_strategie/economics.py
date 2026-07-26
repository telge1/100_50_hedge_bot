"""Economic break-even solvers and full-exit gate."""

from __future__ import annotations

from dataclasses import dataclass

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from research.backtests.emergency_lock.cost_model import (
    BPS_DIVISOR,
    apply_long_open_slippage,
    apply_short_open_slippage,
    fee_usdt,
)

from .config import CoberturaConfig
from .ledger import CoberturaLedger, round_price


@dataclass(frozen=True)
class ExitEconomics:
    total_exit_economics: float
    realized_overlay_pnl: float
    core_long_open_pnl_at_exit: float
    core_short_open_pnl_at_exit: float
    overlay_long_open_pnl_at_exit: float
    overlay_short_open_pnl_at_exit: float
    cumulative_entry_fees: float
    cumulative_close_fees_paid: float
    estimated_remaining_close_fees: float
    estimated_exit_slippage: float
    cumulative_slippage_costs: float
    fee_buffer_usdt: float
    long_exit_price: float
    short_exit_price: float
    remaining_to_total_be: float
    exit_allowed: bool


def adverse_long_exit_price(reference: float, slippage_bps_close: float) -> float:
    """Sell long: worse = lower fill."""
    return apply_short_open_slippage(
        reference_price=float(reference), slippage_bps=float(slippage_bps_close)
    )


def adverse_short_exit_price(reference: float, slippage_bps_close: float) -> float:
    """Buy to close short: worse = higher fill."""
    return apply_long_open_slippage(
        reference_price=float(reference), slippage_bps=float(slippage_bps_close)
    )


def short_open_fill_price(trigger: float, slippage_bps_open: float) -> float:
    return apply_short_open_slippage(
        reference_price=float(trigger), slippage_bps=float(slippage_bps_open)
    )


def long_open_fill_price(trigger: float, slippage_bps_open: float) -> float:
    return apply_long_open_slippage(
        reference_price=float(trigger), slippage_bps=float(slippage_bps_open)
    )


def gap_aware_short_open_fill_price(
    *,
    trigger: float,
    candle_open: float,
    slippage_bps_open: float,
    enabled: bool,
) -> tuple[float, float, bool]:
    """Short open: never fill better (higher) than candle open on a down-gap.

    Returns (fill_price, raw_reference, gap_adjusted).
    """
    raw = float(trigger)
    adjusted = False
    if enabled and float(candle_open) + 1e-12 < float(trigger):
        raw = float(candle_open)
        adjusted = True
    return short_open_fill_price(raw, slippage_bps_open), raw, adjusted


def gap_aware_short_close_fill_price(
    *,
    trigger: float,
    candle_open: float,
    slippage_bps_close: float,
    enabled: bool,
) -> tuple[float, float, bool]:
    """Buy to close short: never fill better (lower) than candle open on an up-gap."""
    raw = float(trigger)
    adjusted = False
    if enabled and float(candle_open) - 1e-12 > float(trigger):
        raw = float(candle_open)
        adjusted = True
    return adverse_short_exit_price(raw, slippage_bps_close), raw, adjusted


def gap_aware_long_close_fill_price(
    *,
    trigger: float,
    candle_open: float,
    slippage_bps_close: float,
    enabled: bool,
) -> tuple[float, float, bool]:
    """Sell long: never fill better (higher) than candle open on a down-gap."""
    raw = float(trigger)
    adjusted = False
    if enabled and float(candle_open) + 1e-12 < float(trigger):
        raw = float(candle_open)
        adjusted = True
    return adverse_long_exit_price(raw, slippage_bps_close), raw, adjusted


def locked_spread_loss(ledger: CoberturaLedger) -> float:
    """Frozen core loss for long_avg > short_avg at qty-neutral hedge."""
    q = min(ledger.core_long.qty, ledger.core_short.qty)
    return q * (ledger.core_long.avg - ledger.core_short.avg)


def estimate_close_fees(
    ledger: CoberturaLedger,
    *,
    long_exit_price: float,
    short_exit_price: float,
    fee_rate_close: float,
) -> float:
    fee = 0.0
    if ledger.core_long.qty > 0:
        fee += fee_usdt(
            fill_price=long_exit_price,
            qty=ledger.core_long.qty,
            fee_rate=fee_rate_close,
        )
    if ledger.overlay_long.qty > 0:
        fee += fee_usdt(
            fill_price=long_exit_price,
            qty=ledger.overlay_long.qty,
            fee_rate=fee_rate_close,
        )
    if ledger.core_short.qty > 0:
        fee += fee_usdt(
            fill_price=short_exit_price,
            qty=ledger.core_short.qty,
            fee_rate=fee_rate_close,
        )
    if ledger.overlay_short.qty > 0:
        fee += fee_usdt(
            fill_price=short_exit_price,
            qty=ledger.overlay_short.qty,
            fee_rate=fee_rate_close,
        )
    return fee


def compute_total_exit_economics(
    ledger: CoberturaLedger,
    cfg: CoberturaConfig,
    *,
    reference_exit_price: float,
) -> ExitEconomics:
    """Full-trade economics at a hypothetical complete exit.

    Slippage is applied inside exit prices (not subtracted again).
    Already-paid close fees are subtracted so multi-round overlays stay consistent.
    """
    ref = float(reference_exit_price)
    long_exit = adverse_long_exit_price(ref, cfg.slippage_bps_close)
    short_exit = adverse_short_exit_price(ref, cfg.slippage_bps_close)

    core_long_pnl = (
        calculate_pnl(ledger.core_long.avg, long_exit, ledger.core_long.qty, "long")
        if ledger.core_long.qty > 0
        else 0.0
    )
    core_short_pnl = (
        calculate_pnl(ledger.core_short.avg, short_exit, ledger.core_short.qty, "short")
        if ledger.core_short.qty > 0
        else 0.0
    )
    ov_long_pnl = (
        calculate_pnl(
            ledger.overlay_long.avg, long_exit, ledger.overlay_long.qty, "long"
        )
        if ledger.overlay_long.qty > 0
        else 0.0
    )
    ov_short_pnl = (
        calculate_pnl(
            ledger.overlay_short.avg, short_exit, ledger.overlay_short.qty, "short"
        )
        if ledger.overlay_short.qty > 0
        else 0.0
    )

    est_close = estimate_close_fees(
        ledger,
        long_exit_price=long_exit,
        short_exit_price=short_exit,
        fee_rate_close=cfg.fee_rate_close,
    )
    est_exit_slip = (
        max(ref - long_exit, 0.0) * (ledger.core_long.qty + ledger.overlay_long.qty)
        + max(short_exit - ref, 0.0) * (ledger.core_short.qty + ledger.overlay_short.qty)
    )

    total = (
        float(ledger.realized_overlay_pnl)
        + core_long_pnl
        + core_short_pnl
        + ov_long_pnl
        + ov_short_pnl
        - float(ledger.cumulative_entry_fees)
        - float(ledger.cumulative_close_fees)
        - est_close
        - float(cfg.fee_buffer_usdt)
    )
    # Note: cumulative_slippage_costs / estimated_exit_slippage are informational
    # (already embedded in fill / exit prices) and are NOT subtracted again.

    if str(cfg.full_exit_target_mode) == "net_be":
        target = float(cfg.full_exit_target_usdt) + float(
            cfg.full_exit_safety_buffer_usdt
        )
    else:
        target = float(cfg.target_total_pnl_usdt) + float(cfg.target_profit_buffer_usdt)
    # Exit slippage is already embedded in adverse fill prices used for MTM / fees.
    # Safety buffer (net_be) sits on the threshold; fee_buffer_usdt is already in total.
    exit_allowed = total >= (target - float(cfg.pnl_tolerance_usdt))
    remaining = target - total

    return ExitEconomics(
        total_exit_economics=total,
        realized_overlay_pnl=float(ledger.realized_overlay_pnl),
        core_long_open_pnl_at_exit=core_long_pnl,
        core_short_open_pnl_at_exit=core_short_pnl,
        overlay_long_open_pnl_at_exit=ov_long_pnl,
        overlay_short_open_pnl_at_exit=ov_short_pnl,
        cumulative_entry_fees=float(ledger.cumulative_entry_fees),
        cumulative_close_fees_paid=float(ledger.cumulative_close_fees),
        estimated_remaining_close_fees=est_close,
        estimated_exit_slippage=est_exit_slip,
        cumulative_slippage_costs=float(ledger.cumulative_slippage_costs),
        fee_buffer_usdt=float(cfg.fee_buffer_usdt),
        long_exit_price=long_exit,
        short_exit_price=short_exit,
        remaining_to_total_be=remaining,
        exit_allowed=exit_allowed,
    )


def overlay_short_be_trigger_price(
    ledger: CoberturaLedger,
    cfg: CoberturaConfig,
) -> float | None:
    """Highest short-close trigger such that overlay economics >= target.

    Short close fill = trigger * (1 + slip_close). Solve for fill then back out trigger.
    """
    qty = float(ledger.overlay_short.qty)
    if qty <= 0.0:
        return None
    if ledger.overlay_add_count_round < int(cfg.overlay_be_min_fill_count):
        return None

    avg = float(ledger.overlay_short.avg)
    entry_fees = float(ledger.overlay_entry_fees)
    target = float(cfg.overlay_be_target_usdt)
    buffer = float(cfg.fee_buffer_usdt)
    fee_close = float(cfg.fee_rate_close)
    slip = float(cfg.slippage_bps_close) / BPS_DIVISOR

    # qty*(avg - fill) - entry_fees - fee_close*|fill|*qty - buffer >= target
    # qty*avg - fill*qty - fee_close*fill*qty >= target + entry_fees + buffer
    # fill * qty * (1 + fee_close) <= qty*avg - target - entry_fees - buffer
    rhs = qty * avg - target - entry_fees - buffer
    denom = qty * (1.0 + fee_close)
    if denom <= 0.0:
        return None
    fill = rhs / denom
    if fill <= 0.0:
        return None
    trigger = fill / (1.0 + slip) if slip > -1.0 else fill
    if trigger <= 0.0:
        return None
    # Floor to tick so rounding cannot push economics below target.
    tick = float(cfg.tick_size)
    trigger = max(tick, (int(trigger / tick)) * tick)
    # Walk down ticks if residual numerical / fee effects remain.
    for _ in range(20):
        if overlay_short_exit_economics_at(
            ledger, cfg, trigger_price=trigger
        ) >= target - 1e-9:
            return trigger
        trigger = round_price(trigger - tick, cfg.tick_size)
        if trigger <= 0.0:
            return None
    return None


def overlay_short_exit_economics_at(
    ledger: CoberturaLedger,
    cfg: CoberturaConfig,
    *,
    trigger_price: float,
) -> float:
    qty = float(ledger.overlay_short.qty)
    if qty <= 0.0:
        return 0.0
    fill = adverse_short_exit_price(trigger_price, cfg.slippage_bps_close)
    gross = calculate_pnl(ledger.overlay_short.avg, fill, qty, "short")
    close_fee = fee_usdt(fill_price=fill, qty=qty, fee_rate=cfg.fee_rate_close)
    return (
        gross
        - float(ledger.overlay_entry_fees)
        - close_fee
        - float(cfg.fee_buffer_usdt)
    )


def overlay_open_profit_usdt(ledger: CoberturaLedger, mark: float) -> float:
    pnls = ledger.open_pnl_at(mark)
    return float(pnls["overlay_long_open_pnl"] + pnls["overlay_short_open_pnl"])


def distance_pct(price: float, avg: float) -> float | None:
    if avg <= 0.0:
        return None
    return (float(price) - float(avg)) / float(avg)
