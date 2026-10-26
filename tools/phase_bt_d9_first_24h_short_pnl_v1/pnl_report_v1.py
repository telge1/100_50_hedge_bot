"""Normalize BT-D6 PnL dict to BT-D9 report field names."""

from __future__ import annotations

from typing import Any


def format_pnl_summary(pnl: dict[str, Any] | None) -> dict[str, Any]:
    if not pnl:
        return {"error": "NO_PNL"}
    return {
        "initial_equity_usdt": pnl.get("initial_equity_usdt"),
        "ending_equity_usdt": pnl.get("ending_equity_usdt"),
        "total_net_pnl_usdt": pnl.get("net_pnl_usdt"),
        "total_return_pct": pnl.get("total_return_pct"),
        "gross_pnl_usdt": pnl.get("gross_pnl_usdt"),
        "trading_fees_usdt": pnl.get("trading_fees_usdt"),
        "slippage_cost_usdt": pnl.get("slippage_cost_usdt"),
        "funding_fees_usdt": pnl.get("funding_fees_usdt"),
        "total_trades": pnl.get("total_trades"),
        "winning_trades": pnl.get("winning_trades"),
        "losing_trades": pnl.get("losing_trades"),
        "win_rate_pct": pnl.get("win_rate"),
        "profit_factor": pnl.get("profit_factor"),
        "max_drawdown_pct": pnl.get("max_drawdown_pct"),
        "equity_curve": pnl.get("equity_curve"),
        "trade_ledger": pnl.get("trade_ledger"),
    }
