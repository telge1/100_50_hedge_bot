"""Extract and validate paired long/short start schedules from continuous backtest JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .debug_report import calculate_unrealized_pnl


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimate_closing_fees(run: dict[str, Any], *, fee_rate: float = 0.00055) -> float:
    long_qty = _safe_float(run.get("final_long_qty")) or 0.0
    short_qty = _safe_float(run.get("final_short_qty")) or 0.0
    if long_qty <= 0 and short_qty <= 0:
        return 0.0
    long_avg = _safe_float(run.get("final_long_avg_price")) or 0.0
    short_avg = _safe_float(run.get("final_short_avg_price")) or 0.0
    mark = long_avg or short_avg or (_safe_float(run.get("entry_price")) or 0.0)
    if mark <= 0:
        return 0.0
    return fee_rate * (long_qty * mark + short_qty * mark)


def trade_mark_to_market(run: dict[str, Any]) -> dict[str, float]:
    realized = _safe_float(run.get("realized_pnl")) or 0.0
    unreal_long = _safe_float(run.get("unrealized_long_pnl"))
    unreal_short = _safe_float(run.get("unrealized_short_pnl"))
    unreal_total = _safe_float(run.get("unrealized_pnl"))
    if unreal_total is None and (unreal_long is not None or unreal_short is not None):
        unreal_total = (unreal_long or 0.0) + (unreal_short or 0.0)
    if unreal_long is None or unreal_short is None:
        long_qty = _safe_float(run.get("final_long_qty")) or 0.0
        short_qty = _safe_float(run.get("final_short_qty")) or 0.0
        long_avg = _safe_float(run.get("final_long_avg_price")) or 0.0
        short_avg = _safe_float(run.get("final_short_avg_price")) or 0.0
        mark = long_avg or short_avg or (_safe_float(run.get("entry_price")) or 0.0)
        calc_long, calc_short, calc_total = calculate_unrealized_pnl(
            long_qty, long_avg, short_qty, short_avg, mark
        )
        if unreal_long is None:
            unreal_long = calc_long or 0.0
        if unreal_short is None:
            unreal_short = calc_short or 0.0
        if unreal_total is None:
            unreal_total = calc_total or 0.0
    closing_fees = _estimate_closing_fees(run)
    mtm = realized + (unreal_total or 0.0) - closing_fees
    return {
        "realized_pnl": realized,
        "unrealized_long_pnl": float(unreal_long or 0.0),
        "unrealized_short_pnl": float(unreal_short or 0.0),
        "unrealized_pnl": float(unreal_total or 0.0),
        "estimated_closing_fees": closing_fees,
        "mark_to_market_pnl": mtm,
    }


def result_to_schedule_entry(run: dict[str, Any]) -> dict[str, Any]:
    mtm = trade_mark_to_market(run)
    return {
        "pair_number": int(run.get("trade_number") or 0),
        "trade_number": int(run.get("trade_number") or 0),
        "trade_block_id": run.get("trade_block_id"),
        "start_index": int(run.get("start_index") or 0),
        "start_absolute_index": int(run.get("start_index") or 0),
        "start_time": run.get("start_time"),
        "reference_entry_price": _safe_float(run.get("entry_price")),
        "end_index": int(run.get("end_index") or 0),
        "end_time": run.get("end_time"),
        "final_status": run.get("final_status"),
        "exit_reason": run.get("exit_reason"),
        "candles_processed": int(run.get("candles_processed") or 0),
        "realized_pnl": mtm["realized_pnl"],
        "unrealized_pnl": mtm["unrealized_pnl"],
        "overall_pnl": _safe_float(run.get("overall_pnl")),
        "mark_to_market_pnl": mtm["mark_to_market_pnl"],
        "recovery_activated": bool(run.get("recovery_activated")),
        "recovery_exit_timestamp": run.get("recovery_exit_timestamp"),
        "recovery_final_pnl": _safe_float(run.get("recovery_final_pnl")),
    }


def load_long_continuous_results(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "runs" not in payload:
        raise ValueError(f"invalid continuous results JSON (missing runs): {path}")
    return payload


def build_paired_start_schedule(
    long_results_path: str | Path,
    *,
    long_recovery_purpose: str,
    recovery_wait_candles: int,
) -> dict[str, Any]:
    payload = load_long_continuous_results(long_results_path)
    runs = sorted(payload.get("runs") or [], key=lambda row: int(row.get("trade_number") or 0))
    entries = [result_to_schedule_entry(run) for run in runs]
    metadata = payload.get("metadata") or {}
    return {
        "source_results_path": str(Path(long_results_path).resolve()),
        "symbol": metadata.get("symbol") or runs[0].get("symbol") if runs else None,
        "direction": "long",
        "long_recovery_purpose": long_recovery_purpose,
        "recovery_wait_candles": int(recovery_wait_candles),
        "pair_count": len(entries),
        "pairs": entries,
    }


def write_paired_start_schedule(path: str | Path, schedule: dict[str, Any]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(schedule, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj
