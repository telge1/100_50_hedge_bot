"""Fixed 1000 USDT notional PnL (Phase 4P)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POSITION_NOTIONAL_USDT = 1000.0
FEE_RATE_PER_SIDE = 0.00055  # 0.055% — half of roundtrip_fees_pct=0.11
ROUNDTRIP_FEE_PCT_LEGACY = 0.11  # documented in strategy_config; USDT fees use split model below


@dataclass
class TradePnL:
    quantity_doge: float
    entry_fee_usdt: float
    exit_fee_usdt: float | None
    gross_pnl_usdt: float | None
    net_pnl_usdt: float | None
    gross_return_pct: float | None
    net_return_pct: float | None
    risk_usdt: float
    reward_usdt: float
    realized_R_multiple: float | None
    unrealized_pnl_usdt: float | None = None


def quantity_doge(entry_price: float) -> float:
    return POSITION_NOTIONAL_USDT / entry_price


def entry_fee_usdt() -> float:
    return POSITION_NOTIONAL_USDT * FEE_RATE_PER_SIDE


def exit_fee_usdt(exit_price: float, quantity: float) -> float:
    return exit_price * quantity * FEE_RATE_PER_SIDE


def gross_pnl_usdt(direction: str, entry: float, exit_p: float, quantity: float) -> float:
    if direction == "SHORT":
        return (entry - exit_p) * quantity
    return (exit_p - entry) * quantity


def risk_reward_usdt(direction: str, entry: float, sl: float, tp: float, quantity: float) -> tuple[float, float]:
    if direction == "SHORT":
        risk = max(0.0, (sl - entry) * quantity)
        reward = max(0.0, (entry - tp) * quantity)
    else:
        risk = max(0.0, (entry - sl) * quantity)
        reward = max(0.0, (tp - entry) * quantity)
    return risk, reward


def compute_trade_pnl(
    direction: str,
    entry_price: float,
    exit_price: float | None,
    sl_price: float,
    tp_price: float,
    *,
    ambiguous: bool,
    open_at_end: bool,
    mark_close: float | None = None,
) -> TradePnL:
    qty = quantity_doge(entry_price)
    efee = entry_fee_usdt()
    risk, reward = risk_reward_usdt(direction, entry_price, sl_price, tp_price, qty)

    if ambiguous:
        return TradePnL(
            quantity_doge=qty,
            entry_fee_usdt=efee,
            exit_fee_usdt=None,
            gross_pnl_usdt=None,
            net_pnl_usdt=None,
            gross_return_pct=None,
            net_return_pct=None,
            risk_usdt=risk,
            reward_usdt=reward,
            realized_R_multiple=None,
        )

    if open_at_end and exit_price is None and mark_close is not None:
        gross = gross_pnl_usdt(direction, entry_price, mark_close, qty)
        xfee = exit_fee_usdt(mark_close, qty)
        net = gross - efee - xfee
        return TradePnL(
            quantity_doge=qty,
            entry_fee_usdt=efee,
            exit_fee_usdt=xfee,
            gross_pnl_usdt=gross,
            net_pnl_usdt=net,
            gross_return_pct=gross / POSITION_NOTIONAL_USDT * 100.0,
            net_return_pct=net / POSITION_NOTIONAL_USDT * 100.0,
            risk_usdt=risk,
            reward_usdt=reward,
            realized_R_multiple=None,
            unrealized_pnl_usdt=net,
        )

    if exit_price is None:
        return TradePnL(
            quantity_doge=qty,
            entry_fee_usdt=efee,
            exit_fee_usdt=None,
            gross_pnl_usdt=None,
            net_pnl_usdt=None,
            gross_return_pct=None,
            net_return_pct=None,
            risk_usdt=risk,
            reward_usdt=reward,
            realized_R_multiple=None,
        )

    gross = gross_pnl_usdt(direction, entry_price, exit_price, qty)
    xfee = exit_fee_usdt(exit_price, qty)
    net = gross - efee - xfee
    r_mult = net / risk if risk > 0 else None
    return TradePnL(
        quantity_doge=qty,
        entry_fee_usdt=efee,
        exit_fee_usdt=xfee,
        gross_pnl_usdt=gross,
        net_pnl_usdt=net,
        gross_return_pct=gross / POSITION_NOTIONAL_USDT * 100.0,
        net_return_pct=net / POSITION_NOTIONAL_USDT * 100.0,
        risk_usdt=risk,
        reward_usdt=reward,
        realized_R_multiple=r_mult,
    )


def ambiguous_scenarios(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
) -> dict[str, Any]:
    qty = quantity_doge(entry_price)
    efee = entry_fee_usdt()
    sl_gross = gross_pnl_usdt(direction, entry_price, sl_price, qty)
    tp_gross = gross_pnl_usdt(direction, entry_price, tp_price, qty)
    sl_net = sl_gross - efee - exit_fee_usdt(sl_price, qty)
    tp_net = tp_gross - efee - exit_fee_usdt(tp_price, qty)
    return {
        "conservative_sl_first_gross_usdt": sl_gross,
        "conservative_sl_first_net_usdt": sl_net,
        "optimistic_tp_first_gross_usdt": tp_gross,
        "optimistic_tp_first_net_usdt": tp_net,
    }


def equity_curve_from_ledger(rows: list[dict]) -> list[dict]:
    realized = [
        r
        for r in rows
        if r.get("exit_reason") in ("SL", "TP")
        and r.get("execution_ambiguity") == "NONE"
        and r.get("net_pnl_usdt") not in ("", None)
    ]
    realized.sort(key=lambda x: (x.get("exit_utc") or "", x.get("trade_id") or ""))
    equity = 0.0
    peak = 0.0
    curve: list[dict] = []
    for r in realized:
        net = float(r["net_pnl_usdt"])
        equity += net
        peak = max(peak, equity)
        dd_usdt = equity - peak
        if peak > 0:
            dd_pct = dd_usdt / peak * 100.0
        else:
            dd_pct = dd_usdt / POSITION_NOTIONAL_USDT * 100.0
        curve.append(
            {
                "trade_id": r["trade_id"],
                "exit_utc": r["exit_utc"],
                "net_pnl_usdt": net,
                "cumulative_net_pnl_usdt": equity,
                "drawdown_usdt": dd_usdt,
                "drawdown_pct": dd_pct,
            }
        )
    return curve


def max_drawdown(curve: list[dict]) -> tuple[float, float]:
    if not curve:
        return 0.0, 0.0
    return min(float(c["drawdown_usdt"]) for c in curve), min(float(c["drawdown_pct"]) for c in curve)
