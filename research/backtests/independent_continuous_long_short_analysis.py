"""Analysis for independent continuous long/short bots with shared initial start only."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from .backtest_report import BacktestResult
from .paired_start_schedule import trade_mark_to_market


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def backtest_result_to_row(result: BacktestResult) -> dict[str, Any]:
    payload = result.to_dict()
    payload["trade_number"] = result.trade_number
    payload["trade_block_id"] = result.trade_block_id
    payload["direction"] = result.direction
    payload["start_index"] = result.start_index
    payload["end_index"] = (
        int(result.start_index or 0) + int(result.candles_processed or 0)
        if result.start_index is not None
        else None
    )
    mtm = trade_mark_to_market(payload)
    payload.update(mtm)
    payload["overall_mtm_pnl"] = mtm["mark_to_market_pnl"]
    payload["recovery_closed"] = (
        bool(payload.get("recovery_activated"))
        and str(payload.get("exit_reason") or "") == "recovery_joint_exit"
    )
    return payload


def run_dict_to_row(run: dict[str, Any]) -> dict[str, Any]:
    row = dict(run)
    mtm = trade_mark_to_market(row)
    row.update(mtm)
    row["overall_mtm_pnl"] = mtm["mark_to_market_pnl"]
    row["recovery_closed"] = (
        bool(row.get("recovery_activated"))
        and str(row.get("exit_reason") or "") == "recovery_joint_exit"
    )
    return row


def summarize_direction_runs(
    runs: Iterable[dict[str, Any]],
    *,
    direction: str,
) -> dict[str, Any]:
    rows = [run_dict_to_row(dict(run)) for run in runs]
    pnls = [float(row.get("realized_pnl") or 0.0) for row in rows]
    mtm_pnls = [float(row.get("mark_to_market_pnl") or 0.0) for row in rows]
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
    recovery_rows = [row for row in rows if bool(row.get("recovery_activated"))]
    recovery_closed = [row for row in recovery_rows if bool(row.get("recovery_closed"))]
    recovery_pnls = [float(row.get("realized_pnl") or 0.0) for row in recovery_rows]
    open_long_qty = sum(_safe_float(row.get("final_long_qty")) or 0.0 for row in open_rows)
    open_short_qty = sum(_safe_float(row.get("final_short_qty")) or 0.0 for row in open_rows)
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
        "trades_started": len(rows),
        "closed_trades": len(closed),
        "open_trades_at_series_end": len(open_rows),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_realized_pnl": sum(pnls),
        "unrealized_pnl_at_series_end": sum(_safe_float(row.get("unrealized_pnl")) or 0.0 for row in open_rows),
        "total_mark_to_market_pnl": sum(mtm_pnls),
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
        "open_long_qty_at_series_end": open_long_qty,
        "open_short_qty_at_series_end": open_short_qty,
        "open_notional_exposure_at_series_end": open_notional,
    }


def combine_independent_summaries(
    long_summary: dict[str, Any],
    short_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "combined_realized_pnl": (
            float(long_summary["net_realized_pnl"]) + float(short_summary["net_realized_pnl"])
        ),
        "combined_unrealized_pnl": (
            float(long_summary["unrealized_pnl_at_series_end"])
            + float(short_summary["unrealized_pnl_at_series_end"])
        ),
        "combined_mtm_pnl": (
            float(long_summary["total_mark_to_market_pnl"])
            + float(short_summary["total_mark_to_market_pnl"])
        ),
        "combined_gross_profit": float(long_summary["gross_profit"]) + float(short_summary["gross_profit"]),
        "combined_gross_loss": float(long_summary["gross_loss"]) + float(short_summary["gross_loss"]),
        "combined_win_trades": int(long_summary["win_trades"]) + int(short_summary["win_trades"]),
        "combined_loss_trades": int(long_summary["loss_trades"]) + int(short_summary["loss_trades"]),
        "combined_recovery_closed_count": (
            int(long_summary["recovery_closed_count"]) + int(short_summary["recovery_closed_count"])
        ),
        "long_recovery_trade_pnl_sum": float(long_summary["recovery_trade_pnl_sum"]),
        "short_recovery_trade_pnl_sum": float(short_summary["recovery_trade_pnl_sum"]),
        "combined_recovery_trade_pnl_sum": (
            float(long_summary["recovery_trade_pnl_sum"]) + float(short_summary["recovery_trade_pnl_sum"])
        ),
        "combined_open_long_qty_at_series_end": (
            float(long_summary["open_long_qty_at_series_end"])
            + float(short_summary["open_long_qty_at_series_end"])
        ),
        "combined_open_short_qty_at_series_end": (
            float(long_summary["open_short_qty_at_series_end"])
            + float(short_summary["open_short_qty_at_series_end"])
        ),
        "combined_open_notional_exposure_at_series_end": (
            float(long_summary["open_notional_exposure_at_series_end"])
            + float(short_summary["open_notional_exposure_at_series_end"])
        ),
    }


def build_timeline_rows(runs: Iterable[dict[str, Any]], *, direction: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        row = run_dict_to_row(run)
        rows.append(
            {
                "bot_direction": direction,
                "trade_number": row.get("trade_number"),
                "start_index": row.get("start_index"),
                "start_timestamp": row.get("start_time"),
                "end_index": row.get("end_index"),
                "end_timestamp": row.get("end_time"),
                "duration_candles": row.get("candles_processed"),
                "final_status": row.get("final_status"),
                "realized_pnl": row.get("realized_pnl"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "overall_mtm_pnl": row.get("overall_mtm_pnl"),
                "recovery_activated": row.get("recovery_activated"),
                "recovery_closed": row.get("recovery_closed"),
            }
        )
    return rows


def merge_timeline(long_runs: list[dict[str, Any]], short_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = build_timeline_rows(long_runs, direction="long")
    rows.extend(build_timeline_rows(short_runs, direction="short"))
    rows.sort(key=lambda row: (int(row.get("start_index") or 0), str(row.get("bot_direction"))))
    return rows


def _intervals_from_runs(runs: list[dict[str, Any]]) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for run in runs:
        start = int(run.get("start_index") or 0)
        end = int(run.get("end_index") or start)
        intervals.append((start, end))
    return intervals


def validate_no_within_direction_overlap(runs: list[dict[str, Any]], *, direction: str) -> dict[str, Any]:
    overlaps = 0
    for left, right in zip(runs, runs[1:]):
        left_end = int(left.get("end_index") or 0)
        right_start = int(right.get("start_index") or 0)
        if left_end >= right_start:
            overlaps += 1
    reentry_ok = True
    for left, right in zip(runs, runs[1:]):
        expected = int(left.get("end_index") or 0) + 1
        actual = int(right.get("start_index") or 0)
        if actual != expected:
            reentry_ok = False
            break
    return {
        "direction": direction,
        "overlap_count": overlaps,
        "reentry_offset_ok": reentry_ok,
    }


def validate_independent_reentry(long_runs: list[dict[str, Any]], short_runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "long": validate_no_within_direction_overlap(long_runs, direction="long"),
        "short": validate_no_within_direction_overlap(short_runs, direction="short"),
        "trade_count_differs": len(long_runs) != len(short_runs),
        "long_trade_count": len(long_runs),
        "short_trade_count": len(short_runs),
    }


def _load_trade_block_snapshots(output_dir: Path, direction: str) -> list[dict[str, Any]]:
    pattern = f"APTUSDT_{direction}_continuous_trade_*_trade_blocks.json"
    snapshots: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob(pattern)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = payload.get("metadata") or {}
        trade_number = None
        base = str(metadata.get("base_name") or path.name)
        for token in base.split("_"):
            if token.isdigit() and len(token) == 4:
                trade_number = int(token)
        rows = payload.get("trade_blocks") or []
        fill_rows = [row for row in rows if str(row.get("row_type") or "") == "fill"]
        if not fill_rows:
            continue
        last = fill_rows[-1]
        snapshots.append(
            {
                "trade_number": trade_number,
                "start_index": metadata.get("start_index"),
                "absolute_candle_index": last.get("absolute_candle_index"),
                "long_qty": _safe_float(last.get("long_qty_after")),
                "short_qty": _safe_float(last.get("short_qty_after")),
                "long_avg": _safe_float(last.get("long_avg_after")),
                "short_avg": _safe_float(last.get("short_avg_after")),
                "fills": fill_rows,
            }
        )
    return snapshots


def build_combined_exposure_timeline(
    *,
    long_runs: list[dict[str, Any]],
    short_runs: list[dict[str, Any]],
    long_output_dir: Path,
    short_output_dir: Path,
    candle_count: int,
    candle_closes: list[float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconstruct candle occupancy and notional exposure from trade-block fill state."""
    long_intervals = _intervals_from_runs(long_runs)
    short_intervals = _intervals_from_runs(short_runs)

    # Per-candle position state from last known fill in each active trade.
    long_state: dict[int, dict[str, float]] = {}
    short_state: dict[int, dict[str, float]] = {}

    for snap in _load_trade_block_snapshots(long_output_dir, "long"):
        for fill in snap["fills"]:
            abs_idx = fill.get("absolute_candle_index")
            if abs_idx is None:
                abs_idx = fill.get("global_candle_index")
            if abs_idx is None:
                continue
            long_state[int(abs_idx)] = {
                "long_qty": _safe_float(fill.get("long_qty_after")) or 0.0,
                "short_qty": _safe_float(fill.get("short_qty_after")) or 0.0,
                "long_avg": _safe_float(fill.get("long_avg_after")) or 0.0,
                "short_avg": _safe_float(fill.get("short_avg_after")) or 0.0,
            }

    for snap in _load_trade_block_snapshots(short_output_dir, "short"):
        for fill in snap["fills"]:
            abs_idx = fill.get("absolute_candle_index")
            if abs_idx is None:
                abs_idx = fill.get("global_candle_index")
            if abs_idx is None:
                continue
            short_state[int(abs_idx)] = {
                "long_qty": _safe_float(fill.get("long_qty_after")) or 0.0,
                "short_qty": _safe_float(fill.get("short_qty_after")) or 0.0,
                "long_avg": _safe_float(fill.get("long_avg_after")) or 0.0,
                "short_avg": _safe_float(fill.get("short_avg_after")) or 0.0,
            }

    def _active(intervals: list[tuple[int, int]], idx: int) -> bool:
        return any(start <= idx <= end for start, end in intervals)

    def _last_state(state_map: dict[int, dict[str, float]], idx: int) -> dict[str, float]:
        candidates = [i for i in state_map if i <= idx]
        if not candidates:
            return {"long_qty": 0.0, "short_qty": 0.0, "long_avg": 0.0, "short_avg": 0.0}
        return state_map[max(candidates)]

    timeline: list[dict[str, Any]] = []
    only_long = only_short = both = neither = 0
    max_long_notional = max_short_notional = max_combined_notional = 0.0
    combined_notionals: list[float] = []

    for idx in range(candle_count):
        long_active = _active(long_intervals, idx)
        short_active = _active(short_intervals, idx)
        if long_active and short_active:
            both += 1
        elif long_active:
            only_long += 1
        elif short_active:
            only_short += 1
        else:
            neither += 1

        long_pos = _last_state(long_state, idx) if long_active else {
            "long_qty": 0.0, "short_qty": 0.0, "long_avg": 0.0, "short_avg": 0.0
        }
        short_pos = _last_state(short_state, idx) if short_active else {
            "long_qty": 0.0, "short_qty": 0.0, "long_avg": 0.0, "short_avg": 0.0
        }
        mark = candle_closes[idx] if idx < len(candle_closes) else 0.0
        long_notional = mark * ((long_pos["long_qty"] or 0.0) + (long_pos["short_qty"] or 0.0))
        short_notional = mark * ((short_pos["long_qty"] or 0.0) + (short_pos["short_qty"] or 0.0))
        combined_notional = long_notional + short_notional
        max_long_notional = max(max_long_notional, long_notional)
        max_short_notional = max(max_short_notional, short_notional)
        max_combined_notional = max(max_combined_notional, combined_notional)
        combined_notionals.append(combined_notional)
        timeline.append(
            {
                "absolute_candle_index": idx,
                "long_bot_active": long_active,
                "short_bot_active": short_active,
                "long_notional_exposure": long_notional,
                "short_notional_exposure": short_notional,
                "combined_notional_exposure": combined_notional,
            }
        )

    summary = {
        "candles_only_long_active": only_long,
        "candles_only_short_active": only_short,
        "candles_both_active": both,
        "candles_neither_active": neither,
        "max_long_notional_exposure": max_long_notional,
        "max_short_notional_exposure": max_short_notional,
        "max_combined_notional_exposure": max_combined_notional,
        "avg_combined_notional_exposure": statistics.mean(combined_notionals) if combined_notionals else 0.0,
        "exposure_source": "trade_block_fill_snapshots",
    }
    return timeline, summary


def build_shared_initial_start_validation(
    *,
    first_candle: dict[str, Any],
    long_first_trade: dict[str, Any],
    short_first_trade: dict[str, Any],
) -> dict[str, Any]:
    return {
        "shared_initial_start_index": 0,
        "long_initial_start_index": long_first_trade.get("start_index"),
        "short_initial_start_index": short_first_trade.get("start_index"),
        "long_initial_start_timestamp": long_first_trade.get("start_time"),
        "short_initial_start_timestamp": short_first_trade.get("start_time"),
        "start_candle": {
            "timestamp": first_candle.get("timestamp"),
            "open": first_candle.get("open"),
            "high": first_candle.get("high"),
            "low": first_candle.get("low"),
            "close": first_candle.get("close"),
        },
        "long_reference_entry_price": long_first_trade.get("entry_price"),
        "short_reference_entry_price": short_first_trade.get("entry_price"),
        "long_initial_long_qty": long_first_trade.get("final_long_qty"),
        "long_initial_short_qty": long_first_trade.get("final_short_qty"),
        "short_initial_long_qty": short_first_trade.get("final_long_qty"),
        "short_initial_short_qty": short_first_trade.get("final_short_qty"),
        "same_start_index": int(long_first_trade.get("start_index") or -1)
        == int(short_first_trade.get("start_index") or -2),
        "same_start_timestamp": long_first_trade.get("start_time") == short_first_trade.get("start_time"),
        "note": (
            "Identical market timestamp/candle; mirrored fill prices may differ by direction-neutral "
            "entry, tick, bid/ask and fee logic."
        ),
    }


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path_obj.write_text("", encoding="utf-8")
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
