"""In-memory position book for Phase-1 backtest smoke tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SyntheticCandle:
    symbol: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None

    def __post_init__(self) -> None:
        if self.open is None:
            self.open = self.close
        if self.high is None:
            self.high = self.close
        if self.low is None:
            self.low = self.close


@dataclass
class SimulatedOrderBook:
    symbol: str
    long_qty: float = 0.0
    short_qty: float = 0.0
    long_avg: float = 0.0
    short_avg: float = 0.0
    open_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    _order_seq: int = 0

    def positions_mapping(self) -> dict[str, float]:
        return {
            "long_qty": self.long_qty,
            "short_qty": self.short_qty,
            "long_avg": self.long_avg,
            "short_avg": self.short_avg,
        }

    def next_client_order_id(self, purpose: str) -> str:
        self._order_seq += 1
        slug = str(purpose or "order").lower().replace("_", "-")
        return f"sim-fixed_cycle-{slug}-{self._order_seq}"

    def register_intent(
        self,
        *,
        client_order_id: str,
        side: str,
        qty: float,
        purpose: str,
        price: float | None,
        order_type: str,
        reduce_only: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.open_orders[client_order_id] = {
            "client_order_id": client_order_id,
            "side": side,
            "qty": float(qty),
            "purpose": purpose,
            "price": price,
            "order_type": order_type,
            "reduce_only": reduce_only,
            "status": "NEW",
            "metadata": dict(metadata or {}),
        }

    def apply_market_fill(
        self,
        *,
        client_order_id: str,
        fill_price: float,
        qty: float | None = None,
    ) -> dict[str, Any]:
        order = self.open_orders.pop(client_order_id, None)
        if order is None:
            raise KeyError(f"unknown simulated order: {client_order_id}")
        fill_qty = float(qty if qty is not None else order["qty"])
        side = str(order["side"]).lower()
        if side == "long":
            prev_qty = self.long_qty
            new_qty = prev_qty + fill_qty
            if new_qty > 0:
                self.long_avg = (
                    (prev_qty * self.long_avg + fill_qty * fill_price) / new_qty
                    if prev_qty > 0
                    else fill_price
                )
            self.long_qty = new_qty
        elif side == "short":
            prev_qty = self.short_qty
            new_qty = prev_qty + fill_qty
            if new_qty > 0:
                self.short_avg = (
                    (prev_qty * self.short_avg + fill_qty * fill_price) / new_qty
                    if prev_qty > 0
                    else fill_price
                )
            self.short_qty = new_qty
        else:
            raise ValueError(f"unsupported simulated fill side: {side}")
        return {
            **order,
            "exec_qty": fill_qty,
            "exec_price": float(fill_price),
            "status": "FILLED",
        }
