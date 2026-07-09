from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PrimarySide = Literal["long", "short"]


@dataclass
class HedgeExitComponents:
    profit_basis_usdt: float
    target_profit_usdt: float
    buffer_usdt: float
    realized_cycle_net: float
    pending_cycle_loss_usdt: float
    required_profit_usdt: float
    net_qty: float
    exit_price: float
    primary_side: str = ""


def _resolve_profit_basis_usdt(
    *,
    primary_side: PrimarySide,
    long_avg: float,
    long_qty: float,
    short_avg: float,
    short_qty: float,
) -> float:
    normalized = str(primary_side or "").strip().lower()
    if normalized == "long":
        return long_avg * long_qty
    if normalized == "short":
        return short_avg * short_qty
    raise ValueError(f"primary_side must be 'long' or 'short', got {primary_side!r}")


def calculate_hedge_exit_price(
    long_avg: float,
    long_qty: float,
    short_avg: float,
    short_qty: float,
    tp_profit_target_pct: float,
    tp_buffer_pct: float,
    realized_cycle_net: float,
    pending_cycle_loss_usdt: float = 0.0,
    *,
    primary_side: PrimarySide,
) -> HedgeExitComponents:
    profit_basis_usdt = _resolve_profit_basis_usdt(
        primary_side=primary_side,
        long_avg=long_avg,
        long_qty=long_qty,
        short_avg=short_avg,
        short_qty=short_qty,
    )
    target_profit_usdt = profit_basis_usdt * tp_profit_target_pct / 100.0
    buffer_usdt = profit_basis_usdt * tp_buffer_pct / 100.0
    pending_loss = max(float(pending_cycle_loss_usdt or 0.0), 0.0)
    realized_profit_credit = max(float(realized_cycle_net or 0.0), 0.0)
    realized_loss_usdt = max(-float(realized_cycle_net or 0.0), 0.0)
    loss_recovery_usdt = max(pending_loss, realized_loss_usdt if pending_loss <= 0.0 else 0.0)
    required_profit_usdt = (
        target_profit_usdt + buffer_usdt + loss_recovery_usdt - realized_profit_credit
    )
    net_qty = long_qty - short_qty
    base_diff = (long_avg * long_qty) - (short_avg * short_qty)
    exit_price = base_diff + required_profit_usdt
    exit_price = exit_price / net_qty if abs(net_qty) > 1e-12 else 0.0
    normalized_side = str(primary_side).strip().lower()
    return HedgeExitComponents(
        profit_basis_usdt=profit_basis_usdt,
        target_profit_usdt=target_profit_usdt,
        buffer_usdt=buffer_usdt,
        realized_cycle_net=realized_cycle_net,
        pending_cycle_loss_usdt=pending_loss,
        required_profit_usdt=required_profit_usdt,
        net_qty=net_qty,
        exit_price=exit_price,
        primary_side=normalized_side,
    )
