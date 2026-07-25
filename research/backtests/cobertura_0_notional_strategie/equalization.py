"""Dynamic long-equalization trigger math (fee- and slippage-aware)."""

from __future__ import annotations

from dataclasses import dataclass

from research.backtests.emergency_lock.cost_model import BPS_DIVISOR, fee_usdt

from .config import CoberturaConfig
from .economics import long_open_fill_price
from .ledger import CoberturaLedger, round_price, round_qty


@dataclass(frozen=True)
class EqualizationPlan:
    add_qty: float
    current_long_qty: float
    current_long_avg: float
    current_short_qty: float
    current_short_avg: float
    target_long_avg: float
    max_long_add_fill_price_raw: float
    max_long_add_fill_price: float  # fee/slippage-aware trigger / limit
    locked_spread_pct_target: float


def equalization_add_qty(ledger: CoberturaLedger) -> float:
    """Missing long qty so total long matches total short."""
    return float(ledger.total_short_qty()) - float(ledger.total_long_qty())


def raw_max_long_add_fill_price(
    *,
    current_long_qty: float,
    current_long_avg: float,
    add_qty: float,
    target_long_avg: float,
) -> float:
    """Solve VWAP constraint: new_long_avg <= target_long_avg."""
    lq = float(current_long_qty)
    la = float(current_long_avg)
    aq = float(add_qty)
    t = float(target_long_avg)
    if aq <= 0.0:
        raise ValueError("add_qty must be positive")
    return (t * (lq + aq) - la * lq) / aq


def projected_long_avg_after_add(
    *,
    current_long_qty: float,
    current_long_avg: float,
    add_qty: float,
    fill_price: float,
) -> float:
    lq = float(current_long_qty)
    aq = float(add_qty)
    return (lq * float(current_long_avg) + aq * float(fill_price)) / (lq + aq)


def compute_equalization_plan(
    ledger: CoberturaLedger,
    cfg: CoberturaConfig,
) -> EqualizationPlan | None:
    add_raw = equalization_add_qty(ledger)
    if add_raw <= 1e-12:
        return None
    add_qty = round_qty(add_raw, cfg.qty_step)
    if add_qty <= 0.0:
        return None
    # Do not overshoot neutrality due to rounding.
    if add_qty > add_raw + 1e-12:
        add_qty = round_qty(add_raw - float(cfg.qty_step), cfg.qty_step)
        if add_qty <= 0.0:
            return None

    long_qty = float(ledger.total_long_qty())
    long_avg = float(ledger.total_long_avg())
    short_qty = float(ledger.total_short_qty())
    short_avg = float(ledger.total_short_avg())
    if short_avg <= 0.0 or long_qty <= 0.0:
        return None

    spread = float(cfg.max_locked_spread_pct)
    target_long_avg = short_avg * (1.0 + spread)
    raw = raw_max_long_add_fill_price(
        current_long_qty=long_qty,
        current_long_avg=long_avg,
        add_qty=add_qty,
        target_long_avg=target_long_avg,
    )
    if raw <= 0.0:
        return None

    # Fee buffer: shave price room so post-cost economics stay inside spread.
    buffer = float(cfg.long_equalization_fee_buffer_usdt)
    shaved = raw - (buffer / add_qty if add_qty > 0 else 0.0)
    # Reserve open-fee notional impact as a small price haircut.
    fee_haircut = float(cfg.fee_rate_open) * shaved
    shaved = shaved - fee_haircut
    if shaved <= 0.0:
        return None

    # Slippage worsens long fill (higher). Trigger must be low enough that
    # trigger*(1+slip) still projects avg <= target.
    slip = float(cfg.slippage_bps_open) / BPS_DIVISOR
    trigger = shaved / (1.0 + slip) if slip > -1.0 else shaved
    tick = float(cfg.tick_size)
    trigger = max(tick, (int(trigger / tick)) * tick)

    # Walk down until projected fill meets constraint.
    for _ in range(50):
        fill = long_open_fill_price(trigger, cfg.slippage_bps_open)
        new_avg = projected_long_avg_after_add(
            current_long_qty=long_qty,
            current_long_avg=long_avg,
            add_qty=add_qty,
            fill_price=fill,
        )
        if new_avg <= target_long_avg + 1e-12:
            return EqualizationPlan(
                add_qty=add_qty,
                current_long_qty=long_qty,
                current_long_avg=long_avg,
                current_short_qty=short_qty,
                current_short_avg=short_avg,
                target_long_avg=target_long_avg,
                max_long_add_fill_price_raw=raw,
                max_long_add_fill_price=trigger,
                locked_spread_pct_target=spread,
            )
        trigger = round_price(trigger - tick, cfg.tick_size)
        if trigger <= 0.0:
            break
    return None


def locked_spread_pct(long_avg: float, short_avg: float) -> float | None:
    if short_avg <= 0.0:
        return None
    return (float(long_avg) - float(short_avg)) / float(short_avg)


def estimate_equalization_open_fee(
    *, fill_price: float, qty: float, fee_rate_open: float
) -> float:
    return fee_usdt(fill_price=fill_price, qty=qty, fee_rate=fee_rate_open)
