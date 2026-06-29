"""Backtest result structures and logging helpers (Phase 4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from fixed_cycle_hedge_bot.models import FillEvent

from .simulated_order_book import SimulatedOrderBook, VirtualOrder


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
