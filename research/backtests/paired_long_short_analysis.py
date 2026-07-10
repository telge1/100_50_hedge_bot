"""Analysis helpers for paired long/short same-start backtests."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .backtest_report import BacktestResult
from .debug_report import calculate_unrealized_pnl
from .paired_start_schedule import trade_mark_to_market


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def backtest_result_to_row(result: BacktestResult) -> dict[str, Any]:
    payload = result.to_dict()
    payload["trade_number"] = result.trade_number
    payload["trade_block_id"] = result.trade_block_id
    payload["start_index"] = result.start_index
    payload["end_index"] = (
        int(result.start_index or 0) + int(result.candles_processed or 0)
        if result.start_index is not None
        else None
    )
    mtm = trade_mark_to_market(payload)
    payload.update(mtm)
    return payload


def mtm_from_backtest_result(result: BacktestResult) -> dict[str, float]:
    return trade_mark_to_market(backtest_result_to_row(result))


def summarize_direction_runs(
    runs: Iterable[dict[str, Any]],
    *,
    direction: str,
    planned_starts: int | None = None,
    skipped_starts: int = 0,
) -> dict[str, Any]:
    rows = list(runs)
    pnls = [_safe_float(row.get("realized_pnl")) or 0.0 for row in rows]
    mtm_pnls = [_safe_float(row.get("mark_to_market_pnl")) or 0.0 for row in rows]
    durations = [int(row.get("candles_processed") or 0) for row in rows]
    closed = [
        row
        for row in rows
        if str(row.get("exit_reason") or "") in {"flat_no_active_orders", "recovery_joint_exit"}
        or str(row.get("final_status") or "").startswith("closed")
    ]
    open_rows = [row for row in rows if str(row.get("final_status") or "") == "open"]
    wins = [p for p in pnls if p > 1e-9]
    losses = [p for p in pnls if p < -1e-9]
    breakeven = [p for p in pnls if abs(p) <= 1e-9]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net_realized = sum(pnls)
    unrealized = sum(_safe_float(row.get("unrealized_pnl")) or 0.0 for row in open_rows)
    total_mtm = sum(mtm_pnls)
    recovery_rows = [row for row in rows if bool(row.get("recovery_activated"))]
    recovery_closed = [
        row for row in recovery_rows if str(row.get("exit_reason") or "") == "recovery_joint_exit"
    ]
    recovery_pnls = [_safe_float(row.get("realized_pnl")) or 0.0 for row in recovery_rows]
    open_notional = 0.0
    for row in open_rows:
        long_qty = _safe_float(row.get("final_long_qty")) or 0.0
        short_qty = _safe_float(row.get("final_short_qty")) or 0.0
        mark = (
            _safe_float(row.get("final_long_avg_price"))
            or _safe_float(row.get("final_short_avg_price"))
            or _safe_float(row.get("entry_price"))
            or 0.0
        )
        open_notional += mark * (long_qty + short_qty)

    profit_factor = None
    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)

    return {
        "direction": direction,
        "planned_starts": planned_starts if planned_starts is not None else len(rows),
        "actual_started_trades": len(rows),
        "skipped_starts": skipped_starts,
        "closed_trades": len(closed),
        "open_trades_at_series_end": len(open_rows),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_realized_pnl": net_realized,
        "unrealized_pnl_at_series_end": unrealized,
        "total_mark_to_market_pnl": total_mtm,
        "profit_factor": profit_factor,
        "avg_pnl_per_trade": statistics.mean(pnls) if pnls else 0.0,
        "median_pnl": statistics.median(pnls) if pnls else 0.0,
        "best_trade_pnl": max(pnls) if pnls else 0.0,
        "worst_trade_pnl": min(pnls) if pnls else 0.0,
        "avg_trade_duration_candles": statistics.mean(durations) if durations else 0.0,
        "max_trade_duration_candles": max(durations) if durations else 0,
        "recovery_activations": len(recovery_rows),
        "recovery_closed_count": len(recovery_closed),
        "recovery_trade_pnl_sum": sum(recovery_pnls),
        "open_notional_exposure_at_series_end": open_notional,
    }


def combine_summaries(long_summary: dict[str, Any], short_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "combined_realized_pnl": (
            float(long_summary["net_realized_pnl"]) + float(short_summary["net_realized_pnl"])
        ),
        "combined_unrealized_pnl": (
            float(long_summary["unrealized_pnl_at_series_end"])
            + float(short_summary["unrealized_pnl_at_series_end"])
        ),
        "combined_mark_to_market_pnl": (
            float(long_summary["total_mark_to_market_pnl"])
            + float(short_summary["total_mark_to_market_pnl"])
        ),
        "combined_gross_profit": float(long_summary["gross_profit"]) + float(short_summary["gross_profit"]),
        "combined_gross_loss": float(long_summary["gross_loss"]) + float(short_summary["gross_loss"]),
    }


def _exit_order(long_end: datetime | None, short_end: datetime | None) -> str:
    if long_end is None and short_end is None:
        return "unknown"
    if long_end is None:
        return "short_first"
    if short_end is None:
        return "long_first"
    if long_end < short_end:
        return "long_first"
    if short_end < long_end:
        return "short_first"
    return "same_candle"


def _exit_time_diff_minutes(long_end: datetime | None, short_end: datetime | None) -> float | None:
    if long_end is None or short_end is None:
        return None
    return abs((short_end - long_end).total_seconds()) / 60.0


def build_pair_comparison_rows(
    *,
    long_runs: list[dict[str, Any]],
    short_runs_by_pair: dict[int, dict[str, Any]],
    short_mtm_at_long_exit_by_pair: dict[int, float],
    long_mtm_at_short_exit_by_pair: dict[int, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    long_by_pair = {int(run["pair_number"]): run for run in long_runs}
    for pair_number in sorted(long_by_pair):
        long_row = long_by_pair[pair_number]
        short_row = short_runs_by_pair.get(pair_number)
        if short_row is None:
            continue
        long_mtm = trade_mark_to_market(long_row)
        short_mtm = trade_mark_to_market(short_row)
        long_end = _parse_ts(long_row.get("end_time"))
        short_end = _parse_ts(short_row.get("end_time"))
        rows.append(
            {
                "pair_number": pair_number,
                "shared_start_index": long_row.get("start_index"),
                "shared_start_timestamp": long_row.get("start_time"),
                "shared_reference_price": long_row.get("reference_entry_price")
                or long_row.get("entry_price"),
                "long_trade_number": long_row.get("trade_number"),
                "short_trade_number": short_row.get("trade_number"),
                "long_end_timestamp": long_row.get("end_time"),
                "short_end_timestamp": short_row.get("end_time"),
                "long_duration_candles": long_row.get("candles_processed"),
                "short_duration_candles": short_row.get("candles_processed"),
                "long_status": long_row.get("final_status"),
                "short_status": short_row.get("final_status"),
                "long_realized_pnl": long_mtm["realized_pnl"],
                "long_unrealized_pnl": long_mtm["unrealized_pnl"],
                "long_mtm_pnl": long_mtm["mark_to_market_pnl"],
                "short_realized_pnl": short_mtm["realized_pnl"],
                "short_unrealized_pnl": short_mtm["unrealized_pnl"],
                "short_mtm_pnl": short_mtm["mark_to_market_pnl"],
                "combined_realized_pnl": long_mtm["realized_pnl"] + short_mtm["realized_pnl"],
                "combined_unrealized_pnl": long_mtm["unrealized_pnl"] + short_mtm["unrealized_pnl"],
                "combined_mtm_pnl": long_mtm["mark_to_market_pnl"] + short_mtm["mark_to_market_pnl"],
                "long_recovery_activated": bool(long_row.get("recovery_activated")),
                "short_recovery_activated": bool(short_row.get("recovery_activated")),
                "long_recovery_pnl": long_mtm["realized_pnl"] if long_row.get("recovery_activated") else 0.0,
                "short_recovery_pnl": short_mtm["realized_pnl"] if short_row.get("recovery_activated") else 0.0,
                "first_closed_leg": _exit_order(long_end, short_end),
                "exit_time_diff_minutes": _exit_time_diff_minutes(long_end, short_end),
                "short_mtm_at_long_exit": short_mtm_at_long_exit_by_pair.get(pair_number),
                "long_mtm_at_short_exit": long_mtm_at_short_exit_by_pair.get(pair_number),
                "combined_mtm_at_long_exit": (
                    long_mtm["realized_pnl"] + short_mtm_at_long_exit_by_pair[pair_number]
                    if pair_number in short_mtm_at_long_exit_by_pair
                    else None
                ),
                "combined_mtm_at_short_exit": (
                    short_mtm["realized_pnl"] + long_mtm_at_short_exit_by_pair[pair_number]
                    if pair_number in long_mtm_at_short_exit_by_pair
                    else None
                ),
            }
        )
    return rows


def classify_recovery_offset(long_recovery_pnl: float, combined_pnl: float) -> str:
    if combined_pnl >= 0:
        return "fully_offset"
    if combined_pnl > long_recovery_pnl:
        return "partially_offset"
    return "not_offset"


def build_recovery_hedge_rows(
    pair_rows: list[dict[str, Any]],
    *,
    recovery_trade_numbers: Iterable[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets = {int(value) for value in recovery_trade_numbers}
    rows: list[dict[str, Any]] = []
    for pair in pair_rows:
        pair_number = int(pair["pair_number"])
        if pair_number not in targets:
            continue
        long_recovery_pnl = float(pair.get("long_recovery_pnl") or 0.0)
        short_final_pnl = float(pair.get("short_realized_pnl") or 0.0)
        short_mtm_at_long_exit = pair.get("short_mtm_at_long_exit")
        combined_final = float(pair.get("combined_mtm_pnl") or 0.0)
        combined_at_long_exit = pair.get("combined_mtm_at_long_exit")
        if combined_at_long_exit is None and short_mtm_at_long_exit is not None:
            combined_at_long_exit = long_recovery_pnl + float(short_mtm_at_long_exit)
        offset_final = classify_recovery_offset(long_recovery_pnl, combined_final)
        offset_at_long_exit = (
            classify_recovery_offset(long_recovery_pnl, float(combined_at_long_exit))
            if combined_at_long_exit is not None
            else "unknown"
        )
        rows.append(
            {
                "pair_number": pair_number,
                "shared_start_timestamp": pair.get("shared_start_timestamp"),
                "long_recovery_pnl": long_recovery_pnl,
                "short_pnl_final": short_final_pnl,
                "short_mtm_at_long_exit": short_mtm_at_long_exit,
                "combined_pair_pnl_final": combined_final,
                "combined_pair_pnl_at_long_exit": combined_at_long_exit,
                "offset_classification_final": offset_final,
                "offset_classification_at_long_exit": offset_at_long_exit,
                "short_status_at_long_exit": (
                    "closed_before_long_exit"
                    if pair.get("first_closed_leg") == "short_first"
                    else "open_at_long_exit"
                ),
                "short_recovery_activated": pair.get("short_recovery_activated"),
            }
        )

    summary = {
        "sum_long_recovery_pnl": sum(float(row["long_recovery_pnl"]) for row in rows),
        "sum_matching_short_pnl_final": sum(float(row["short_pnl_final"]) for row in rows),
        "combined_recovery_pair_pnl_final": sum(float(row["combined_pair_pnl_final"]) for row in rows),
        "combined_recovery_pair_pnl_at_long_exit": sum(
            float(row["combined_pair_pnl_at_long_exit"])
            for row in rows
            if row.get("combined_pair_pnl_at_long_exit") is not None
        ),
        "count_fully_offset_final": sum(1 for row in rows if row["offset_classification_final"] == "fully_offset"),
        "count_partially_offset_final": sum(
            1 for row in rows if row["offset_classification_final"] == "partially_offset"
        ),
        "count_not_offset_final": sum(1 for row in rows if row["offset_classification_final"] == "not_offset"),
        "count_fully_offset_at_long_exit": sum(
            1 for row in rows if row["offset_classification_at_long_exit"] == "fully_offset"
        ),
        "count_partially_offset_at_long_exit": sum(
            1 for row in rows if row["offset_classification_at_long_exit"] == "partially_offset"
        ),
        "count_not_offset_at_long_exit": sum(
            1 for row in rows if row["offset_classification_at_long_exit"] == "not_offset"
        ),
    }
    return rows, summary


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path_obj.open("w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return path_obj
    fieldnames = list(rows[0].keys())
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return path_obj


def write_json(path: str | Path, payload: Any) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj
