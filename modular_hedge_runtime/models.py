from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CalculationTrace:
    name: str
    formula: str
    inputs: dict[str, float]
    result: dict[str, float]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "formula": self.formula,
            "inputs": dict(self.inputs),
            "result": dict(self.result),
            "details": dict(self.details),
        }


@dataclass
class ActiveOrderSnapshot:
    client_order_id: str
    exchange_order_id: str | None
    side: str
    qty: float
    price: float | None
    purpose: str
    order_type: str
    reduce_only: bool
    status: str
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_open(self) -> bool:
        return self.status not in {"FILLED", "CANCELED", "REJECTED"}


@dataclass
class HedgeSnapshot:
    symbol: str
    current_price: float
    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float
    realized_long_pnl_total: float = 0.0
    realized_short_pnl_total: float = 0.0
    active_orders: tuple[ActiveOrderSnapshot, ...] = ()
    source: str = "rest"
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def realized_pnl_total(self) -> float:
        return self.realized_long_pnl_total + self.realized_short_pnl_total

    @property
    def unrealized_long_pnl(self) -> float:
        return (self.current_price - self.long_avg) * self.long_qty if self.long_avg > 0 else 0.0

    @property
    def unrealized_short_pnl(self) -> float:
        return (self.short_avg - self.current_price) * self.short_qty if self.short_avg > 0 else 0.0

    @property
    def basket_pnl(self) -> float:
        return self.realized_pnl_total + self.unrealized_long_pnl + self.unrealized_short_pnl

    @property
    def spread_pct(self) -> float:
        if self.long_avg <= 0 or self.short_avg <= 0:
            return 0.0
        return abs(self.long_avg - self.short_avg) / self.long_avg

    @property
    def short_ratio(self) -> float:
        if self.long_qty <= 0:
            return 0.0
        return self.short_qty / self.long_qty

    def has_open_purpose(self, purpose: str) -> bool:
        return any(order.is_open() and order.purpose == purpose for order in self.active_orders)


@dataclass
class StrategyIntent:
    side: str
    qty: float
    purpose: str
    price: float | None = None
    order_type: str = "Market"
    reduce_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    trace: list[CalculationTrace] = field(default_factory=list)


@dataclass
class ManagedOrder:
    client_order_id: str
    side: str
    qty: float
    purpose: str
    price: float | None
    order_type: str
    reduce_only: bool
    exchange_order_id: str | None = None
    status: str = "PENDING_SUBMIT"
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    trace: list[CalculationTrace] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_snapshot(self) -> ActiveOrderSnapshot:
        return ActiveOrderSnapshot(
            client_order_id=self.client_order_id,
            exchange_order_id=self.exchange_order_id,
            side=self.side,
            qty=self.qty,
            price=self.price,
            purpose=self.purpose,
            order_type=self.order_type,
            reduce_only=self.reduce_only,
            status=self.status,
            filled_qty=self.filled_qty,
            remaining_qty=self.remaining_qty,
            metadata=dict(self.metadata),
        )


@dataclass
class FillEvent:
    exchange_order_id: str
    client_order_id: str | None
    side: str
    purpose: str
    exec_qty: float
    exec_price: float
    order_type: str
    reduce_only: bool
    status: str
    cumulative_qty: float | None = None
    incremental_qty: float | None = None
    exec_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    traces: list[CalculationTrace] = field(default_factory=list)
    occurred_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange_order_id": self.exchange_order_id,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "purpose": self.purpose,
            "exec_qty": self.exec_qty,
            "exec_price": self.exec_price,
            "order_type": self.order_type,
            "reduce_only": self.reduce_only,
            "status": self.status,
            "cumulative_qty": self.cumulative_qty,
            "incremental_qty": self.incremental_qty,
            "exec_id": self.exec_id,
            "metadata": dict(self.metadata),
            "traces": [trace.to_dict() for trace in self.traces],
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass
class RuntimeState:
    strategy_state: dict[str, Any] = field(default_factory=dict)
    active_orders: dict[str, ManagedOrder] = field(default_factory=dict)
    exchange_to_client_id: dict[str, str] = field(default_factory=dict)
    realized_long_pnl_total: float = 0.0
    realized_short_pnl_total: float = 0.0
    last_snapshot: HedgeSnapshot | None = None
    started_at: datetime = field(default_factory=utcnow)
    sequence: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


def trace_dicts(traces: list[CalculationTrace]) -> list[dict[str, Any]]:
    return [trace.to_dict() for trace in traces]


def snapshot_from_mapping(
    *,
    symbol: str,
    current_price: float,
    positions: Mapping[str, float],
    runtime_state: RuntimeState,
    source: str,
) -> HedgeSnapshot:
    active_orders = tuple(
        order.to_snapshot()
        for order in runtime_state.active_orders.values()
        if order.status not in {"FILLED", "CANCELED", "REJECTED"}
    )
    return HedgeSnapshot(
        symbol=symbol,
        current_price=current_price,
        long_qty=float(positions.get("long_qty") or 0.0),
        short_qty=float(positions.get("short_qty") or 0.0),
        long_avg=float(positions.get("long_avg") or 0.0),
        short_avg=float(positions.get("short_avg") or 0.0),
        realized_long_pnl_total=runtime_state.realized_long_pnl_total,
        realized_short_pnl_total=runtime_state.realized_short_pnl_total,
        active_orders=active_orders,
        source=source,
    )
