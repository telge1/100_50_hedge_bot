"""
Isolated portfolio PnL aggregator (synthetic trades only in BT-D6).

Does not run strategy or market backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_validate_v1 import ConfigValidationError, validate_config_dict


@dataclass
class SyntheticTrade:
    trade_id: str
    symbol: str
    direction: str  # SHORT | LONG
    entry_price: float
    exit_price: float
    entry_utc: str
    exit_utc: str
    exit_reason: str
    bars_held: int
    ambiguous_intrabar: bool = False


class PnlAggregatorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _notional_usdt(config: dict[str, Any], equity: float) -> float:
    pnl = config["pnl"]
    if pnl["sizing_mode"] == "fixed_notional_usdt":
        return float(pnl["position_notional_usdt"]) * float(pnl["leverage"])
    frac = float(pnl["risk_fraction_pct"]) / 100.0
    return equity * frac * float(pnl["leverage"])


def _fee_usdt(notional: float, config: dict[str, Any]) -> float:
    pct = float(config["pnl"]["trading_fee_model"]["roundtrip_pct"]) / 100.0
    return notional * pct


def _slippage_usdt(notional: float, config: dict[str, Any], *, entry: bool, exit_: bool) -> float:
    sm = config["pnl"]["slippage_model"]
    if sm["model"] == "none":
        return 0.0
    total = 0.0
    if entry:
        total += notional * float(sm["entry_slippage_pct"]) / 100.0
    if exit_:
        total += notional * float(sm["exit_slippage_pct"]) / 100.0
    return total


def _funding_usdt(notional: float, bars_held: int, config: dict[str, Any]) -> float:
    fm = config["pnl"]["funding_model"]
    if fm["model"] == "none":
        return 0.0
    # 5m bars -> 8h periods for constant bps model (explicit, not hidden)
    periods_8h = bars_held * (5.0 / 60.0) / 8.0
    bps = float(fm["funding_rate_bps_per_8h"]) / 10000.0
    return notional * bps * periods_8h


def _gross_pnl_usdt(direction: str, entry: float, exit_p: float, notional: float) -> float:
    if entry <= 0 or exit_p <= 0:
        raise PnlAggregatorError("INVALID_PRICES", "entry/exit must be positive")
    qty = notional / entry
    if direction == "SHORT":
        return (entry - exit_p) * qty
    return (exit_p - entry) * qty


def aggregate_pnl(
    config: dict[str, Any],
    trades: list[SyntheticTrade],
) -> dict[str, Any]:
    validate_config_dict(config)
    initial = float(config["pnl"]["initial_equity_usdt"])
    ambiguity = config["pnl"]["intrabar_ambiguity_policy"]
    eot_policy = config["pnl"]["end_of_test_open_position_policy"]

    ledger: list[dict[str, Any]] = []
    equity = initial
    equity_curve: list[dict[str, Any]] = [{"utc": "start", "equity_usdt": equity}]
    realized_net = 0.0
    gross_sum = 0.0
    fees_sum = 0.0
    slip_sum = 0.0
    funding_sum = 0.0
    wins = 0
    losses = 0
    closed = 0
    gross_profit = 0.0
    gross_loss = 0.0

    for t in trades:
        if t.ambiguous_intrabar and ambiguity == "no_trade_ambiguous":
            ledger.append(
                {
                    **t.__dict__,
                    "status": "SKIPPED_AMBIGUOUS_INTRABAR",
                    "gross_pnl_usdt": None,
                    "net_pnl_usdt": None,
                }
            )
            continue
        if t.exit_reason == "OPEN_AT_END" and eot_policy == "exclude_from_realized_pnl":
            ledger.append({**t.__dict__, "status": "EXCLUDED_EOT_OPEN", "net_pnl_usdt": None})
            continue

        notional = _notional_usdt(config, equity)
        gross = _gross_pnl_usdt(t.direction, t.entry_price, t.exit_price, notional)
        fees = _fee_usdt(notional, config)
        slip = _slippage_usdt(notional, config, entry=True, exit_=True)
        funding = _funding_usdt(notional, t.bars_held, config)
        net = gross - fees - slip - funding

        gross_sum += gross
        fees_sum += fees
        slip_sum += slip
        funding_sum += funding
        realized_net += net
        equity += net
        closed += 1
        if net > 0:
            wins += 1
            gross_profit += net
        elif net < 0:
            losses += 1
            gross_loss += abs(net)

        ledger.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_utc": t.entry_utc,
                "exit_utc": t.exit_utc,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "notional_usdt": notional,
                "gross_pnl_usdt": gross,
                "trading_fees_usdt": fees,
                "slippage_cost_usdt": slip,
                "funding_fees_usdt": funding,
                "net_pnl_usdt": net,
                "equity_after_usdt": equity,
            }
        )
        equity_curve.append({"utc": t.exit_utc, "equity_usdt": equity})

    peak = initial
    max_dd = 0.0
    for point in equity_curve:
        e = float(point["equity_usdt"])
        peak = max(peak, e)
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            max_dd = max(max_dd, dd)

    win_rate = (wins / closed * 100.0) if closed else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    return {
        "total_trades": len(trades),
        "closed_trades": closed,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": win_rate,
        "gross_pnl_usdt": gross_sum,
        "trading_fees_usdt": fees_sum,
        "slippage_cost_usdt": slip_sum,
        "funding_fees_usdt": funding_sum,
        "net_pnl_usdt": realized_net,
        "total_return_pct": (equity - initial) / initial * 100.0,
        "max_drawdown_pct": max_dd,
        "profit_factor": profit_factor,
        "ending_equity_usdt": equity,
        "initial_equity_usdt": initial,
        "equity_curve": equity_curve,
        "trade_ledger": ledger,
        "synthetic_only": True,
    }
