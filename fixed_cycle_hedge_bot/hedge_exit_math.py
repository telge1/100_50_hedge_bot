from dataclasses import dataclass


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


def calculate_hedge_exit_price(
    long_avg: float,
    long_qty: float,
    short_avg: float,
    short_qty: float,
    tp_profit_target_pct: float,
    tp_buffer_pct: float,
    realized_cycle_net: float,
    pending_cycle_loss_usdt: float = 0.0,
) -> HedgeExitComponents:
    profit_basis_usdt = long_avg * long_qty
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
    return HedgeExitComponents(
        profit_basis_usdt=profit_basis_usdt,
        target_profit_usdt=target_profit_usdt,
        buffer_usdt=buffer_usdt,
        realized_cycle_net=realized_cycle_net,
        pending_cycle_loss_usdt=pending_loss,
        required_profit_usdt=required_profit_usdt,
        net_qty=net_qty,
        exit_price=exit_price,
    )
