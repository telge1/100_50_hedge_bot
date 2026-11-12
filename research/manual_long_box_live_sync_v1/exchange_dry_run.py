"""In-memory Bybit-style limit order simulator (Phase 1 — no network)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderStatus(str, Enum):
    PENDING_ACK = "PENDING_ACK"
    OPEN = "OPEN"
    AMENDING = "AMENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class DryRunOrder:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    price: float
    qty: float
    status: OrderStatus
    filled_qty: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    amend_in_flight: bool = False
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class ExchangeAck:
    ok: bool
    order_id: str | None = None
    status: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class DryRunExchangeAdapter:
    """Mimics create / amend / cancel / reconcile used by sync engine."""

    supports_native_amend = True

    def __init__(self) -> None:
        self._orders: dict[str, DryRunOrder] = {}
        self._link_index: dict[str, str] = {}

    def _new_id(self) -> str:
        return f"dry-{uuid.uuid4().hex[:16]}"

    def create_limit_long(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        order_link_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: Any,
    ) -> ExchangeAck:
        if order_link_id in self._link_index:
            oid = self._link_index[order_link_id]
            o = self._orders.get(oid)
            if o and o.status in (OrderStatus.OPEN, OrderStatus.AMENDING, OrderStatus.PARTIALLY_FILLED):
                return ExchangeAck(
                    ok=False,
                    message="duplicate_link_id_open",
                    order_id=oid,
                    status=o.status.value,
                )
        oid = self._new_id()
        order = DryRunOrder(
            order_id=oid,
            order_link_id=order_link_id,
            symbol=symbol.upper(),
            side="Buy",
            price=float(price),
            qty=float(qty),
            status=OrderStatus.OPEN,
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            take_profit=float(take_profit) if take_profit is not None else None,
        )
        self._orders[oid] = order
        self._link_index[order_link_id] = oid
        return ExchangeAck(ok=True, order_id=oid, status=OrderStatus.OPEN.value, raw={"retCode": 0})

    def create_limit_short(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        order_link_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: Any,
    ) -> ExchangeAck:
        if order_link_id in self._link_index:
            oid = self._link_index[order_link_id]
            o = self._orders.get(oid)
            if o and o.status in (OrderStatus.OPEN, OrderStatus.AMENDING, OrderStatus.PARTIALLY_FILLED):
                return ExchangeAck(
                    ok=False,
                    message="duplicate_link_id_open",
                    order_id=oid,
                    status=o.status.value,
                )
        oid = self._new_id()
        order = DryRunOrder(
            order_id=oid,
            order_link_id=order_link_id,
            symbol=symbol.upper(),
            side="Sell",
            price=float(price),
            qty=float(qty),
            status=OrderStatus.OPEN,
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            take_profit=float(take_profit) if take_profit is not None else None,
        )
        self._orders[oid] = order
        self._link_index[order_link_id] = oid
        return ExchangeAck(ok=True, order_id=oid, status=OrderStatus.OPEN.value, raw={"retCode": 0})

    def amend_limit(self, order_id: str, *, new_price: float) -> ExchangeAck:
        o = self._orders.get(order_id)
        if not o:
            return ExchangeAck(ok=False, message="unknown_order")
        if o.status == OrderStatus.FILLED:
            return ExchangeAck(ok=False, message="order_filled", order_id=order_id, status=o.status.value)
        if o.status == OrderStatus.CANCELLED:
            return ExchangeAck(ok=False, message="order_cancelled", order_id=order_id)
        o.amend_in_flight = True
        o.status = OrderStatus.AMENDING
        o.updated_at = time.time()
        o.price = float(new_price)
        o.amend_in_flight = False
        o.status = OrderStatus.PARTIALLY_FILLED if o.filled_qty > 0 else OrderStatus.OPEN
        o.updated_at = time.time()
        return ExchangeAck(ok=True, order_id=order_id, status=o.status.value, raw={"retCode": 0, "amend": True})

    def amend_limit_fields(
        self,
        order_id: str,
        *,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: Any,
    ) -> ExchangeAck:
        o = self._orders.get(order_id)
        if not o:
            return ExchangeAck(ok=False, message="unknown_order")
        if o.status == OrderStatus.FILLED:
            return ExchangeAck(ok=False, message="order_filled", order_id=order_id)
        if o.status == OrderStatus.CANCELLED:
            return ExchangeAck(ok=False, message="order_cancelled", order_id=order_id)
        if price is not None:
            o.price = float(price)
        if stop_loss is not None:
            o.stop_loss = float(stop_loss)
        if take_profit is not None:
            o.take_profit = float(take_profit)
        o.updated_at = time.time()
        return ExchangeAck(ok=True, order_id=order_id, status=o.status.value, raw={"retCode": 0})

    def cancel_limit(self, order_id: str) -> ExchangeAck:
        o = self._orders.get(order_id)
        if not o:
            return ExchangeAck(ok=False, message="unknown_order")
        if o.status == OrderStatus.FILLED:
            return ExchangeAck(ok=False, message="already_filled", order_id=order_id)
        o.status = OrderStatus.CANCELLED
        o.updated_at = time.time()
        return ExchangeAck(ok=True, order_id=order_id, status=o.status.value)

    def get_order(self, order_id: str) -> DryRunOrder | None:
        return self._orders.get(order_id)

    def get_open_by_link(self, order_link_id: str) -> DryRunOrder | None:
        oid = self._link_index.get(order_link_id)
        if not oid:
            return None
        o = self._orders.get(oid)
        if not o:
            return None
        if o.status in (OrderStatus.OPEN, OrderStatus.AMENDING, OrderStatus.PARTIALLY_FILLED):
            return o
        return None

    def reconcile_open_orders(self, symbol: str | None = None) -> list[DryRunOrder]:
        out = []
        for o in self._orders.values():
            if symbol and o.symbol != symbol.upper():
                continue
            if o.status in (OrderStatus.OPEN, OrderStatus.AMENDING, OrderStatus.PARTIALLY_FILLED):
                out.append(o)
        return out

    def simulate_fill(self, order_id: str, *, fraction: float = 1.0) -> ExchangeAck:
        o = self._orders.get(order_id)
        if not o:
            return ExchangeAck(ok=False, message="unknown_order")
        fill_qty = o.qty * fraction
        o.filled_qty += fill_qty
        if o.filled_qty >= o.qty * 0.999999:
            o.status = OrderStatus.FILLED
        else:
            o.status = OrderStatus.PARTIALLY_FILLED
        o.updated_at = time.time()
        return ExchangeAck(ok=True, order_id=order_id, status=o.status.value)
