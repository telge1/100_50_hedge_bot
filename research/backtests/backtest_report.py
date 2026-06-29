"""Backtest result structures and logging helpers (Phase 4)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import FillEvent

from .simulated_order_book import SimulatedOrderBook, VirtualOrder

SUMMARY_CSV_FIELDS = (
    "symbol",
    "direction",
    "start_time",
    "end_time",
    "candles_processed",
    "entry_price",
    "final_status",
    "exit_reason",
    "realized_pnl",
    "realized_pnl_pct",
    "max_drawdown_pct",
    "fills_count",
    "orders_submitted",
    "active_orders_count",
    "cycles_seen",
)


def build_fill_log_entry(
    fill: FillEvent,
    book: SimulatedOrderBook,
    *,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    ts = timestamp or fill.occurred_at
    metadata = dict(fill.metadata or {})
    return {
        "timestamp": ts.isoformat() if ts is not None else None,
        "symbol": metadata.get("symbol") or book.symbol,
        "side": fill.side,
        "qty": float(fill.exec_qty),
        "fill_price": float(fill.exec_price),
        "purpose": fill.purpose,
        "order_id": fill.client_order_id,
        "closed_pnl": float(metadata.get("closed_pnl") or metadata.get("confirmed_closed_pnl") or 0.0),
        "position_long_qty": float(book.long_qty),
        "position_short_qty": float(book.short_qty),
    }


def build_order_log_entry(
    order: VirtualOrder,
    *,
    timestamp: datetime | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    ts = timestamp or order.created_at
    return {
        "timestamp": ts.isoformat() if ts is not None else None,
        "purpose": order.purpose,
        "side": order.side,
        "qty": float(order.qty),
        "price": order.price,
        "trigger_price": order.trigger_price,
        "status": status or order.status,
        "order_id": order.order_id,
    }


@dataclass
class BacktestResult:
    symbol: str
    direction: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    candles_processed: int = 0
    entry_price: float | None = None
    final_status: str = "open"
    realized_pnl: float = 0.0
    realized_pnl_pct: float | None = None
    max_drawdown_pct: float | None = None
    fills_count: int = 0
    orders_submitted: int = 0
    active_orders_count: int = 0
    cycles_seen: int | None = None
    exit_reason: str = ""
    fill_log: list[dict[str, Any]] = field(default_factory=list)
    order_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.start_time is not None:
            payload["start_time"] = self.start_time.isoformat()
        if self.end_time is not None:
            payload["end_time"] = self.end_time.isoformat()
        return payload


def result_to_summary_row(result: BacktestResult) -> dict[str, Any]:
    payload = result.to_dict()
    row: dict[str, Any] = {}
    for key in SUMMARY_CSV_FIELDS:
        value = payload.get(key)
        if value is None:
            row[key] = ""
        else:
            row[key] = value
    return row


def write_summary_csv(path: str | Path, results: Iterable[BacktestResult]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = [result_to_summary_row(result) for result in results]
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SUMMARY_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_results_json(
    path: str | Path,
    *,
    symbol: str,
    limit: int | None,
    max_candles: int | None,
    results: dict[str, BacktestResult],
    meta: dict[str, Any] | None = None,
) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "symbol": symbol.upper(),
        "limit": limit,
        "max_candles": max_candles,
        "runs": {direction: result.to_dict() for direction, result in results.items()},
    }
    if meta:
        payload["meta"] = meta
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj


def default_output_paths(output_dir: str | Path, symbol: str) -> tuple[Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    json_path = base / f"{symbol_upper}_original_hedge_5m_results.json"
    csv_path = base / f"{symbol_upper}_original_hedge_5m_summary.csv"
    return json_path, csv_path
