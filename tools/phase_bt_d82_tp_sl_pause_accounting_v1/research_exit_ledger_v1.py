"""Opt-in D46 exit accounting layered on frozen isolated executable fills."""

from __future__ import annotations

from typing import Any

from exit_semantics_audit_v1 import research_exit_from_fill


def build_research_ledger(
    *,
    config: dict[str, Any],
    replay: dict[str, Any],
    prices: dict[str, dict[str, float]],
    horizon_end_utc: str,
) -> dict[str, Any]:
    """Executable baseline events + D46 simulate_short_exit per fill (does not mutate runner)."""
    modeled = list(replay.get("modeled_positions") or [])
    fills = [m for m in modeled if m.get("event") == "SIMULATED_FILL"]
    sl_events = {m.get("order_id"): m for m in modeled if m.get("event") == "SIMULATED_SL"}

    rows: list[dict[str, Any]] = []
    for fill in fills:
        oid = fill.get("order_id")
        baseline_sl = sl_events.get(oid)
        d46 = research_exit_from_fill(
            config,
            entry_bar_utc=str(fill["decision_close_utc"]),
            entry_price=float(fill["entry_price"]),
            sl_price=float(fill["sl_price"]),
            tp_price=float(fill["tp_price"]),
            prices=prices,
            end_utc=horizon_end_utc,
        )
        rows.append(
            {
                "order_id": oid,
                "signal_close_utc": fill.get("signal_close_utc"),
                "executable_fill_utc": fill.get("decision_close_utc"),
                "entry_price": fill.get("entry_price"),
                "sl_price": fill.get("sl_price"),
                "tp_price": fill.get("tp_price"),
                "pool_key": fill.get("pool_key"),
                "baseline_sl_exit_utc": baseline_sl.get("decision_close_utc") if baseline_sl else None,
                "baseline_records_tp_exit": False,
                "d46_research_exit": d46,
            }
        )

    open_at_horizon = bool(replay.get("position_open_at_end"))
    unresolved = [
        r
        for r in rows
        if r["d46_research_exit"]["exit_reason"] == "OPEN_AT_END" and not r["baseline_sl_exit_utc"]
    ]
    return {
        "fill_count_executable": len(fills),
        "research_ledger_rows": rows,
        "open_at_horizon_executable": open_at_horizon,
        "d46_unresolved_at_horizon": unresolved,
        "pause_state_final": replay.get("pause_state_final"),
        "pause_events": replay.get("pause_events"),
    }
