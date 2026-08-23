"""Outcome labels — strictly separated from features; never used in feature math."""

from __future__ import annotations

from typing import Any

from . import constants as C

LABEL_FIELDS = (
    "exit_reason",
    "exit_at",
    "entry_price",
    "exit_price",
    "duration_minutes",
    "gross_return_pct",
    "gross_pnl_usdt",
    "net_return_pct",
    "net_pnl_usdt",
    "net_win",
    "outcome_class",
    "tp_exit",
    "sl_exit",
    "time_exit",
    "incomplete_exit",
    "mfe_pct",
    "mae_pct",
    "mfe_usdt",
    "mae_usdt",
    "mfe_mae_coverage",
    "costs_usdt",
    "timeframe",
    "mode_id",
    "group",
    "strategy_key",
    "horizon",
    "coverage_segment",
)


def _outcome_class(reason: str, net: Any) -> str:
    if reason == "INCOMPLETE_OUTCOME_HORIZON" or reason == "COVERAGE_MISSING":
        return "INCOMPLETE"
    if net is None:
        return "INCOMPLETE"
    return "WIN" if float(net) > 0 else "LOSS"


def extract_labels(trade: dict[str, Any]) -> dict[str, Any]:
    """Copy labels from checkpoint trade only (plus derived outcome_class). No PnL recompute."""
    reason = str(trade.get("exit_reason") or "")
    net = trade.get("net_pnl_usdt")
    gross = trade.get("gross_pnl_usdt")
    if gross is None and trade.get("gross_return_pct") is not None:
        try:
            gross = float(trade["gross_return_pct"]) / 100.0 * float(trade.get("notional_usdt") or C.REF_NOTIONAL)
        except (TypeError, ValueError):
            gross = None
    net_win = None if net is None else (1 if float(net) > 0 else 0)
    return {
        f"{C.LABEL_PREFIX}exit_reason": trade.get("exit_reason"),
        f"{C.LABEL_PREFIX}exit_at": trade.get("exit_at"),
        f"{C.LABEL_PREFIX}entry_price": trade.get("entry_price"),
        f"{C.LABEL_PREFIX}exit_price": trade.get("exit_price"),
        f"{C.LABEL_PREFIX}duration_minutes": trade.get("duration_minutes"),
        f"{C.LABEL_PREFIX}gross_return_pct": trade.get("gross_return_pct"),
        f"{C.LABEL_PREFIX}gross_pnl_usdt": gross,
        f"{C.LABEL_PREFIX}net_return_pct": trade.get("net_return_pct"),
        f"{C.LABEL_PREFIX}net_pnl_usdt": net,
        f"{C.LABEL_PREFIX}net_win": net_win,
        f"{C.LABEL_PREFIX}outcome_class": _outcome_class(reason, net),
        f"{C.LABEL_PREFIX}tp_exit": 1 if reason == "TP_EXIT" else 0,
        f"{C.LABEL_PREFIX}sl_exit": 1 if reason == "SL_EXIT" else 0,
        f"{C.LABEL_PREFIX}time_exit": 1 if reason in ("TIME_EXIT", "HORIZON_EXIT") else 0,
        f"{C.LABEL_PREFIX}incomplete_exit": 1 if reason == "INCOMPLETE_OUTCOME_HORIZON" else 0,
        f"{C.LABEL_PREFIX}costs_usdt": trade.get("costs_usdt"),
        f"{C.LABEL_PREFIX}timeframe": trade.get("timeframe"),
        f"{C.LABEL_PREFIX}mode_id": trade.get("mode_id"),
        f"{C.LABEL_PREFIX}group": trade.get("group"),
        f"{C.LABEL_PREFIX}strategy_key": trade.get("strategy_key"),
        f"{C.LABEL_PREFIX}horizon": trade.get("horizon"),
        f"{C.LABEL_PREFIX}coverage_segment": trade.get("coverage_segment")
        or trade.get("coverage_class"),
    }


def label_parity_fields(trade: dict[str, Any]) -> dict[str, Any]:
    """Fields compared for unchanged label parity."""
    return {
        "exit_reason": trade.get("exit_reason"),
        "exit_at": trade.get("exit_at"),
        "duration_minutes": trade.get("duration_minutes"),
        "gross_return_pct": trade.get("gross_return_pct"),
        "net_return_pct": trade.get("net_return_pct"),
        "net_pnl_usdt": trade.get("net_pnl_usdt"),
    }
