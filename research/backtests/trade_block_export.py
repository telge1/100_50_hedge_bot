"""Export backtest fills/orders/intents grouped by trade_block_id (Phase 14)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .backtest_report import BacktestResult
from .purpose_utils import preserve_bot_purpose

ROW_TYPE_ORDER = {
    "intent": 0,
    "order": 1,
    "fill": 2,
    "final_active_order": 3,
}

TRADE_BLOCK_ROW_FIELDS = (
    "symbol",
    "direction",
    "trade_block_id",
    "trade_block_id_missing",
    "row_type",
    "timestamp",
    "candle_index",
    "event_type",
    "order_id",
    "purpose",
    "purpose_original",
    "cycle_index",
    "cycle_role",
    "side",
    "qty",
    "price",
    "trigger_price",
    "trigger_direction",
    "fill_price",
    "order_check_price",
    "closed_pnl",
    "runtime_calculated_pnl",
    "confirmed_closed_pnl",
    "exec_pnl",
    "cumulative_pnl",
    "long_qty_after",
    "short_qty_after",
    "long_avg_after",
    "short_avg_after",
    "active_orders_after_count",
    "status",
    "mapping_warning",
    "trigger_warning",
    "open_reason_detail",
)

TRADE_BLOCK_SUMMARY_FIELDS = (
    "symbol",
    "direction",
    "trade_block_id",
    "start_time",
    "end_time",
    "status",
    "final_status",
    "exit_reason",
    "open_reason_detail",
    "rows_count",
    "fills_count",
    "orders_count",
    "intents_count",
    "first_purpose",
    "last_purpose",
    "purposes_sequence",
    "cycle_indices",
    "realized_pnl",
    "cumulative_pnl",
    "final_long_qty",
    "final_short_qty",
    "final_active_order_purposes",
    "has_missing_trade_block_id",
)


def parse_trade_block_start_indices(text: str | None) -> set[int] | None:
    if not text or not str(text).strip():
        return None
    indices: set[int] = set()
    for part in str(text).split(","):
        part = part.strip()
        if part:
            indices.add(int(part))
    return indices or None


def _safe_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_timestamp(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _extract_trade_block_id_from_record(record: dict[str, Any]) -> str | None:
    direct = record.get("trade_block_id")
    if direct:
        return str(direct)
    excerpt = record.get("metadata_excerpt")
    if isinstance(excerpt, dict):
        nested = excerpt.get("trade_block_id")
        if nested:
            return str(nested)
    return None


def fallback_trade_block_id(result: BacktestResult) -> str:
    start = result.start_index if result.start_index is not None else 0
    return f"backtest_{result.direction}_start{start}"


def resolve_trade_block_id(
    record: dict[str, Any],
    result: BacktestResult,
) -> tuple[str, bool]:
    found = _extract_trade_block_id_from_record(record)
    if found:
        return found, False
    state = result.final_strategy_state_excerpt or {}
    for key in ("trade_block_id", "active_trade_block_id"):
        value = state.get(key)
        if value:
            return str(value), True
    return fallback_trade_block_id(result), True


def _empty_row(result: BacktestResult) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "direction": result.direction,
        "open_reason_detail": result.open_reason_detail,
    }


def _base_row(
    result: BacktestResult,
    *,
    row_type: str,
    record: dict[str, Any],
    trade_block_id: str,
    trade_block_id_missing: bool,
) -> dict[str, Any]:
    row = _empty_row(result)
    row.update(
        {
            "trade_block_id": trade_block_id,
            "trade_block_id_missing": trade_block_id_missing,
            "row_type": row_type,
            "timestamp": _format_timestamp(record.get("timestamp")),
            "candle_index": record.get("candle_index"),
            "event_type": record.get("event_type") or record.get("event_source") or "",
            "order_id": record.get("order_id") or "",
            "purpose": preserve_bot_purpose(record.get("purpose")),
            "purpose_original": preserve_bot_purpose(
                record.get("purpose_original") or record.get("purpose")
            ),
            "cycle_index": record.get("cycle_index"),
            "cycle_role": record.get("cycle_role"),
            "side": record.get("side"),
            "qty": record.get("qty"),
            "price": record.get("price"),
            "trigger_price": record.get("trigger_price"),
            "trigger_direction": record.get("trigger_direction"),
            "fill_price": record.get("fill_price"),
            "order_check_price": record.get("order_check_price"),
            "closed_pnl": record.get("closed_pnl"),
            "runtime_calculated_pnl": record.get("runtime_calculated_pnl"),
            "confirmed_closed_pnl": record.get("confirmed_closed_pnl"),
            "exec_pnl": record.get("exec_pnl"),
            "long_qty_after": record.get("long_qty_after"),
            "short_qty_after": record.get("short_qty_after"),
            "long_avg_after": record.get("long_avg_after"),
            "short_avg_after": record.get("short_avg_after"),
            "active_orders_after_count": record.get("active_orders_after_count"),
            "status": record.get("status"),
            "mapping_warning": record.get("mapping_warning"),
            "trigger_warning": record.get("trigger_warning"),
        }
    )
    excerpt = record.get("metadata_excerpt")
    if isinstance(excerpt, dict):
        if row.get("exec_pnl") is None and excerpt.get("exec_pnl") is not None:
            row["exec_pnl"] = excerpt.get("exec_pnl")
        if row.get("cycle_index") is None and excerpt.get("cycle_index") is not None:
            row["cycle_index"] = excerpt.get("cycle_index")
        if not row.get("cycle_role") and excerpt.get("cycle_role"):
            row["cycle_role"] = excerpt.get("cycle_role")
    return row


def build_trade_block_rows(result: BacktestResult) -> list[dict[str, Any]]:
    """Build flat rows from intent/order/fill logs grouped by trade_block_id."""
    rows: list[dict[str, Any]] = []

    for record in result.intent_log or []:
        trade_block_id, missing = resolve_trade_block_id(record, result)
        rows.append(
            _base_row(
                result,
                row_type="intent",
                record=record,
                trade_block_id=trade_block_id,
                trade_block_id_missing=missing,
            )
        )

    for record in result.order_log or []:
        trade_block_id, missing = resolve_trade_block_id(record, result)
        rows.append(
            _base_row(
                result,
                row_type="order",
                record=record,
                trade_block_id=trade_block_id,
                trade_block_id_missing=missing,
            )
        )

    for record in result.fill_log or []:
        trade_block_id, missing = resolve_trade_block_id(record, result)
        rows.append(
            _base_row(
                result,
                row_type="fill",
                record=record,
                trade_block_id=trade_block_id,
                trade_block_id_missing=missing,
            )
        )

    for order in result.final_active_orders or []:
        trade_block_id, missing = resolve_trade_block_id(order, result)
        record = dict(order)
        record.setdefault("event_type", "final_active")
        record.setdefault("status", order.get("status") or "ACTIVE")
        rows.append(
            _base_row(
                result,
                row_type="final_active_order",
                record=record,
                trade_block_id=trade_block_id,
                trade_block_id_missing=missing,
            )
        )

    rows = sort_trade_block_rows(rows)
    rows = drop_stale_rows_after_flat(rows)
    return apply_cumulative_pnl(rows)


def _is_initial_entry_purpose(purpose: object) -> bool:
    return str(purpose or "") in {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}


def _trade_block_row_sort_group(row: dict[str, Any]) -> int:
    """
    Initial entry rows can carry run/export timestamps in simulator logs.
    Keep them at the beginning of the trade block so they do not appear after
    final flat exits in CSV/JSON exports.
    """
    if _is_initial_entry_purpose(row.get("purpose")):
        return 0
    return 1


def sort_trade_block_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    main_rows = [row for row in rows if row.get("row_type") != "final_active_order"]
    final_rows = [row for row in rows if row.get("row_type") == "final_active_order"]
    main_sorted = sorted(
        main_rows,
        key=lambda row: (
            str(row.get("trade_block_id") or ""),
            _trade_block_row_sort_group(row),
            str(row.get("timestamp") or ""),
            int(row.get("candle_index") if row.get("candle_index") is not None else -1),
            ROW_TYPE_ORDER.get(str(row.get("row_type") or ""), 99),
        ),
    )
    final_sorted = sorted(
        final_rows,
        key=lambda row: (
            str(row.get("trade_block_id") or ""),
            str(row.get("purpose") or ""),
        ),
    )
    return main_sorted + final_sorted



def _row_float_or_zero(value: object) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trade_block_export_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("symbol") or ""),
        str(row.get("direction") or ""),
        str(row.get("trade_block_id") or ""),
    )


def _is_flat_fill_export_row(row: dict[str, Any]) -> bool:
    if row.get("row_type") != "fill":
        return False

    long_qty_after = row.get("long_qty_after")
    short_qty_after = row.get("short_qty_after")

    # Missing position-after fields must not be interpreted as flat. Some
    # synthetic/unit-test rows do not carry snapshot quantities, and treating
    # empty values as zero would incorrectly drop later legitimate rows.
    if long_qty_after in (None, "") or short_qty_after in (None, ""):
        return False

    return (
        _row_float_or_zero(long_qty_after) == 0.0
        and _row_float_or_zero(short_qty_after) == 0.0
    )


def _is_stale_row_after_flat(row: dict[str, Any]) -> bool:
    row_type = str(row.get("row_type") or "")
    event_type = str(row.get("event_type") or "")
    status = str(row.get("status") or "")
    purpose = str(row.get("purpose") or "")

    if row_type == "fill" and _is_initial_entry_purpose(purpose):
        return True

    if row_type != "order":
        return False

    if event_type != "submitted":
        return False
    if status not in {"", "NEW"}:
        return False

    return (
        _is_initial_entry_purpose(purpose)
        or purpose.startswith("CYCLE_")
        or purpose
        in {
            "LONG_TP_EXIT",
            "SHORT_TP_EXIT",
            "LONG_SL_EXIT",
            "SHORT_SL_EXIT",
        }
    )


def drop_stale_rows_after_flat(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Remove stale rows after a trade block is already flat.

    These rows can be old order-log entries whose timestamp is generated during
    export/report creation. They make the CSV look as if cycle/exit orders stayed
    active after the final flat fill, even when active_orders_after_count is zero.
    """
    flat_seen: set[tuple[str, str, str]] = set()
    filtered: list[dict[str, Any]] = []

    for row in rows:
        key = _trade_block_export_key(row)

        if key in flat_seen and _is_stale_row_after_flat(row):
            continue

        filtered.append(row)

        if _is_flat_fill_export_row(row):
            flat_seen.add(key)

    return filtered


def apply_cumulative_pnl(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    running: dict[str, float] = {}
    updated: list[dict[str, Any]] = []
    for row in rows:
        row_copy = dict(row)
        block_id = str(row_copy.get("trade_block_id") or "")
        if row_copy.get("row_type") == "fill":
            pnl = _safe_float(row_copy.get("closed_pnl")) or 0.0
            running[block_id] = running.get(block_id, 0.0) + pnl
            row_copy["cumulative_pnl"] = running[block_id]
        else:
            row_copy["cumulative_pnl"] = running.get(block_id, 0.0) if block_id else None
        updated.append(row_copy)
    return updated


def build_trade_block_summary_rows(result: BacktestResult) -> list[dict[str, Any]]:
    rows = build_trade_block_rows(result)
    if not rows:
        trade_block_id, missing = resolve_trade_block_id({}, result)
        purposes_joined = "|".join(
            preserve_bot_purpose(purpose)
            for purpose in (result.final_active_order_purposes or [])
            if purpose
        )
        return [
            {
                "symbol": result.symbol,
                "direction": result.direction,
                "trade_block_id": trade_block_id,
                "start_time": _format_timestamp(result.start_time),
                "end_time": _format_timestamp(result.end_time),
                "status": result.final_status,
                "final_status": result.final_status,
                "exit_reason": result.exit_reason,
                "open_reason_detail": result.open_reason_detail,
                "rows_count": 0,
                "fills_count": 0,
                "orders_count": 0,
                "intents_count": 0,
                "first_purpose": "",
                "last_purpose": "",
                "purposes_sequence": "",
                "cycle_indices": "",
                "realized_pnl": float(result.realized_pnl),
                "cumulative_pnl": 0.0,
                "final_long_qty": result.final_long_qty,
                "final_short_qty": result.final_short_qty,
                "final_active_order_purposes": purposes_joined,
                "has_missing_trade_block_id": missing,
            }
        ]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("trade_block_id") or ""), []).append(row)

    summaries: list[dict[str, Any]] = []
    purposes_joined = "|".join(
        preserve_bot_purpose(purpose)
        for purpose in (result.final_active_order_purposes or [])
        if purpose
    )

    for trade_block_id in sorted(grouped):
        block_rows = grouped[trade_block_id]
        fills = [row for row in block_rows if row.get("row_type") == "fill"]
        orders = [row for row in block_rows if row.get("row_type") == "order"]
        intents = [row for row in block_rows if row.get("row_type") == "intent"]
        purposes = [
            preserve_bot_purpose(row.get("purpose"))
            for row in block_rows
            if row.get("purpose")
        ]
        cycle_indices = sorted(
            {
                int(row["cycle_index"])
                for row in block_rows
                if row.get("cycle_index") is not None
            }
        )
        timestamps = [str(row.get("timestamp") or "") for row in block_rows if row.get("timestamp")]
        has_missing = any(bool(row.get("trade_block_id_missing")) for row in block_rows)
        last_cumulative = 0.0
        if fills:
            last_cumulative = _safe_float(fills[-1].get("cumulative_pnl")) or 0.0

        summaries.append(
            {
                "symbol": result.symbol,
                "direction": result.direction,
                "trade_block_id": trade_block_id,
                "start_time": _format_timestamp(result.start_time),
                "end_time": _format_timestamp(result.end_time),
                "status": result.final_status,
                "final_status": result.final_status,
                "exit_reason": result.exit_reason,
                "open_reason_detail": result.open_reason_detail,
                "rows_count": len(block_rows),
                "fills_count": len(fills),
                "orders_count": len(orders),
                "intents_count": len(intents),
                "first_purpose": purposes[0] if purposes else "",
                "last_purpose": purposes[-1] if purposes else "",
                "purposes_sequence": " -> ".join(purposes),
                "cycle_indices": "|".join(str(value) for value in cycle_indices),
                "realized_pnl": float(result.realized_pnl),
                "cumulative_pnl": last_cumulative,
                "final_long_qty": result.final_long_qty,
                "final_short_qty": result.final_short_qty,
                "final_active_order_purposes": purposes_joined,
                "has_missing_trade_block_id": has_missing,
            }
        )
    return summaries


def trade_block_export_base_name(result: BacktestResult) -> str:
    start = result.start_index if result.start_index is not None else 0
    config_source = result.config_source or "unknown"
    fill_model = result.fill_model or "conservative"
    return f"{result.symbol.upper()}_{result.direction}_start{start}_{fill_model}_{config_source}"


def write_trade_block_exports(
    result: BacktestResult,
    output_dir: str | Path,
    base_name: str | None = None,
) -> dict[str, str]:
    """Write trade block CSV/JSON exports for one backtest result."""
    base = trade_block_export_base_name(result) if base_name is None else base_name
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    rows = build_trade_block_rows(result)
    summaries = build_trade_block_summary_rows(result)

    blocks_csv = output_path / f"{base}_trade_blocks.csv"
    summary_csv = output_path / f"{base}_trade_block_summary.csv"
    blocks_json = output_path / f"{base}_trade_blocks.json"

    _write_trade_block_csv(blocks_csv, rows)
    _write_summary_csv(summary_csv, summaries)
    _write_trade_block_json(
        blocks_json,
        result=result,
        rows=rows,
        summaries=summaries,
        base_name=base,
    )

    return {
        "trade_blocks_csv": str(blocks_csv),
        "trade_block_summary_csv": str(summary_csv),
        "trade_blocks_json": str(blocks_json),
    }


def _write_trade_block_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TRADE_BLOCK_ROW_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        ""
                        if row.get(key) is None
                        else row.get(key)
                    )
                    for key in TRADE_BLOCK_ROW_FIELDS
                }
            )


def _write_summary_csv(path: Path, summaries: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TRADE_BLOCK_SUMMARY_FIELDS))
        writer.writeheader()
        for row in summaries:
            writer.writerow(
                {
                    key: (
                        ""
                        if row.get(key) is None
                        else row.get(key)
                    )
                    for key in TRADE_BLOCK_SUMMARY_FIELDS
                }
            )


def _write_trade_block_json(
    path: Path,
    *,
    result: BacktestResult,
    rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    base_name: str,
) -> None:
    payload = {
        "metadata": {
            "base_name": base_name,
            "symbol": result.symbol,
            "direction": result.direction,
            "start_index": result.start_index,
            "fill_model": result.fill_model,
            "config_source": result.config_source,
            "final_status": result.final_status,
            "exit_reason": result.exit_reason,
        },
        "trade_blocks": rows,
        "summary": summaries,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def export_trade_blocks_for_results(
    results: Iterable[BacktestResult],
    output_dir: str | Path,
    *,
    start_indices: set[int] | None = None,
) -> list[dict[str, str]]:
    """Export trade blocks for selected backtest results."""
    written: list[dict[str, str]] = []
    for result in results:
        if start_indices is not None:
            index = result.start_index if result.start_index is not None else 0
            if int(index) not in start_indices:
                continue
        written.append(write_trade_block_exports(result, output_dir))
    return written


def iter_payload_results(payload: dict[str, Any]) -> list[BacktestResult]:
    results = payload.get("results")
    if isinstance(results, dict):
        return list(results.values())
    if isinstance(results, list):
        return list(results)
    return []


def print_trade_block_export_summary(written: list[dict[str, str]]) -> None:
    if not written:
        print("trade_block_export: no files written")
        return
    print(f"trade_block_export: files={len(written)}")
    for files in written:
        if files.get("trade_blocks_csv"):
            print(f"  trade_blocks_csv={files['trade_blocks_csv']}")
        if files.get("trade_block_summary_csv"):
            print(f"  trade_block_summary_csv={files['trade_block_summary_csv']}")
        if files.get("trade_blocks_json"):
            print(f"  trade_blocks_json={files['trade_blocks_json']}")
