"""Backtest-only shim for strategy exit PnL audit helpers missing at runtime."""

from __future__ import annotations

from typing import Any


def _recompute_cycle_pnl_ledger_totals(ledger: dict[str, Any]) -> None:
    cycle_long_reduce_totals: dict[str, float] = {}
    cycle_short_tp_totals: dict[str, float] = {}
    for entry_key, entry in (ledger.get("cycle_pnl_entries") or {}).items():
        try:
            fill_type, cycle_key, _ = str(entry_key).split(":", 2)
        except ValueError:
            continue
        pnl = float((entry or {}).get("pnl") or 0.0)
        if fill_type == "cycle_long_reduce":
            cycle_long_reduce_totals[cycle_key] = (
                cycle_long_reduce_totals.get(cycle_key, 0.0) + pnl
            )
        elif fill_type == "cycle_short_tp":
            cycle_short_tp_totals[cycle_key] = (
                cycle_short_tp_totals.get(cycle_key, 0.0) + pnl
            )
    ledger["cycle_long_reduce_pnl"] = cycle_long_reduce_totals
    ledger["cycle_short_tp_pnl"] = cycle_short_tp_totals

    cycle_net = sum(cycle_long_reduce_totals.values()) + sum(cycle_short_tp_totals.values())
    final_exit_net = sum(
        float(value)
        for value in (
            ledger.get("final_long_exit_pnl"),
            ledger.get("final_short_exit_pnl"),
        )
        if value is not None
    )
    ledger["total_realized_pnl"] = cycle_net + final_exit_net


def install_exit_pnl_audit_shim(strategy: Any) -> None:
    """Attach missing audit helper used by ``_record_cycle_pnl_entry`` during backtests."""
    if getattr(strategy, "_backtest_exit_pnl_audit_shim_installed", False):
        return
    if not hasattr(strategy, "_recompute_cycle_pnl_ledger_totals"):
        strategy._recompute_cycle_pnl_ledger_totals = _recompute_cycle_pnl_ledger_totals
    strategy._backtest_exit_pnl_audit_shim_installed = True
