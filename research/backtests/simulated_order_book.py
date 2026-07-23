"""In-memory order book and positions for backtest harness (Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fixed_cycle_hedge_bot.models import ManagedOrder, RuntimeState, StrategyIntent

from .purpose_utils import enrich_purpose_metadata, preserve_bot_purpose
from .simulated_pnl import attach_closed_pnl_metadata, closed_pnl_for_virtual_order_fill
from .backtest_audit_recorder import BacktestAuditRecorder, FillAuditRecord


def _coerce_timestamp(value: object | None) -> datetime | None:
    if value is None:
        return None
    from .candle_loader import _parse_timestamp

    return _parse_timestamp(value)

ACTIVE_ORDER_STATUSES = frozenset(
    {"NEW", "OPEN", "UNTRIGGERED", "SUBMITTED", "PARTIALLY_FILLED", "PENDING_SUBMIT"}
)
TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DEACTIVATED"})


@dataclass
class SyntheticCandle:
    symbol: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    timestamp: datetime | None = None
    volume: float | None = None

    def __post_init__(self) -> None:
        if self.open is None:
            self.open = self.close
        if self.high is None:
            self.high = self.close
        if self.low is None:
            self.low = self.close

    @classmethod
    def from_row(cls, symbol: str, row: dict[str, Any]) -> SyntheticCandle:
        return cls(
            symbol=symbol,
            timestamp=_coerce_timestamp(row.get("timestamp")),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=row.get("volume"),
        )


@dataclass
class VirtualOrder:
    order_id: str
    exchange_order_id: str
    symbol: str
    side: str
    qty: float
    price: float | None
    trigger_price: float | None
    trigger_direction: int | None
    order_type: str
    reduce_only: bool
    purpose: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_index: int = 0
    created_candle_index: int | None = None
    # Earliest candle index at which this order may be filled against OHLC range
    # (or as a deferred market fill). Orders created/replaced in candle X use X+1.
    eligible_from_candle_index: int | None = None
    filled_qty: float = 0.0
    remaining_qty: float = 0.0

    def __post_init__(self) -> None:
        if self.remaining_qty <= 0:
            self.remaining_qty = float(self.qty)

    def to_managed_order(self) -> ManagedOrder:
        return ManagedOrder(
            client_order_id=self.order_id,
            exchange_order_id=self.exchange_order_id,
            side=self.side,
            qty=float(self.qty),
            purpose=self.purpose,
            price=self.price,
            order_type=self.order_type,
            reduce_only=self.reduce_only,
            status=self.status,
            filled_qty=float(self.filled_qty),
            remaining_qty=float(self.remaining_qty),
            metadata=dict(self.metadata),
            created_at=self.created_at,
            updated_at=self.created_at,
        )


@dataclass
class SimulatedOrderBook:
    symbol: str
    long_qty: float = 0.0
    short_qty: float = 0.0
    long_avg: float = 0.0
    short_avg: float = 0.0
    fee_rate: float | None = None
    _orders: dict[str, VirtualOrder] = field(default_factory=dict)
    _order_seq: int = 0
    # Backtest-only audit recorder and candle index context.
    audit_recorder: BacktestAuditRecorder | None = None
    current_candle_index: int | None = None

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

    def submit_intent(self, intent: StrategyIntent, *, replace: bool = True) -> tuple[VirtualOrder, list[str]]:
        purpose = preserve_bot_purpose(intent.purpose)
        replaced_ids: list[str] = []
        if replace and purpose:
            replaced_ids = self.cancel_by_purpose(purpose)
        order_id = self.next_client_order_id(purpose or "order")
        exchange_order_id = f"sim-ex-{uuid4().hex[:12]}"
        initial_status = "NEW"
        order_type = str(intent.order_type or "Market")
        metadata = enrich_purpose_metadata(purpose, dict(intent.metadata or {}))
        order = VirtualOrder(
            order_id=order_id,
            exchange_order_id=exchange_order_id,
            symbol=self.symbol,
            side=str(intent.side),
            qty=float(intent.qty),
            price=float(intent.price) if intent.price is not None else None,
            trigger_price=float(intent.trigger_price) if intent.trigger_price is not None else None,
            trigger_direction=intent.trigger_direction,
            order_type=order_type,
            reduce_only=bool(intent.reduce_only),
            purpose=purpose,
            status=initial_status,
            metadata=metadata,
            created_index=self._order_seq,
        )
        self._orders[order_id] = order
        return order, replaced_ids

    def get_order(self, order_id: str) -> VirtualOrder | None:
        return self._orders.get(order_id)

    def cancel_by_purpose(self, purpose: str) -> list[str]:
        normalized = str(purpose or "").strip()
        canceled: list[str] = []
        for order_id, order in list(self._orders.items()):
            if order.purpose != normalized:
                continue
            if order.status not in ACTIVE_ORDER_STATUSES:
                continue
            order.status = "CANCELED"
            order.remaining_qty = 0.0
            canceled.append(order_id)
        return canceled

    def cancel_by_order_id(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.status not in ACTIVE_ORDER_STATUSES:
            return False
        order.status = "CANCELED"
        order.remaining_qty = 0.0
        return True

    def active_orders(self) -> list[VirtualOrder]:
        return [
            order
            for order in self._orders.values()
            if order.status in ACTIVE_ORDER_STATUSES
        ]

    def active_orders_by_purpose(self, purpose: str) -> list[VirtualOrder]:
        normalized = str(purpose or "").strip()
        return [order for order in self.active_orders() if order.purpose == normalized]

    def sync_runtime_state(self, runtime_state: RuntimeState) -> None:
        active_ids = {order.order_id for order in self.active_orders()}
        for client_id in list(runtime_state.active_orders.keys()):
            if client_id not in active_ids:
                runtime_state.active_orders.pop(client_id, None)
        for order in self.active_orders():
            managed = order.to_managed_order()
            runtime_state.active_orders[order.order_id] = managed
            runtime_state.exchange_to_client_id[order.exchange_order_id] = order.order_id
            runtime_state.client_to_exchange_id[order.order_id] = order.exchange_order_id

    def apply_fill(
        self,
        *,
        order_id: str,
        fill_price: float,
        qty: float | None = None,
    ) -> tuple[VirtualOrder, float]:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown simulated order: {order_id}")
        if order.status not in ACTIVE_ORDER_STATUSES:
            raise ValueError(f"order not fillable: {order_id} status={order.status}")

        # Snapshot main position state before mutation for audit purposes.
        pre_long_qty = float(self.long_qty)
        pre_long_avg = float(self.long_avg)
        pre_short_qty = float(self.short_qty)
        pre_short_avg = float(self.short_avg)

        fill_qty = float(qty if qty is not None else order.qty)
        side = str(order.side).lower()
        close_qty = fill_qty
        avg_for_pnl = 0.0

        if side == "long":
            if order.reduce_only:
                close_qty = min(fill_qty, self.long_qty)
                avg_for_pnl = self.long_avg
                self.long_qty = max(0.0, self.long_qty - close_qty)
                if self.long_qty <= 1e-12:
                    self.long_qty = 0.0
                    self.long_avg = 0.0
            else:
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
            if order.reduce_only:
                close_qty = min(fill_qty, self.short_qty)
                avg_for_pnl = self.short_avg
                self.short_qty = max(0.0, self.short_qty - close_qty)
                if self.short_qty <= 1e-12:
                    self.short_qty = 0.0
                    self.short_avg = 0.0
            else:
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

        realized_pnl, pnl_details = closed_pnl_for_virtual_order_fill(
            side=side,
            reduce_only=bool(order.reduce_only),
            avg_entry_price=float(avg_for_pnl),
            fill_price=float(fill_price),
            qty=float(close_qty if order.reduce_only else fill_qty),
            fee_rate=self.fee_rate,
        )

        order.status = "FILLED"
        order.filled_qty = fill_qty
        order.remaining_qty = 0.0
        order.metadata["fill_price"] = float(fill_price)
        if order.reduce_only and avg_for_pnl > 0:
            order.metadata["entry_price_for_pnl"] = float(avg_for_pnl)
        attach_closed_pnl_metadata(order.metadata, realized_pnl, pnl_details=pnl_details)

        # Snapshot main position state after mutation.
        post_long_qty = float(self.long_qty)
        post_long_avg = float(self.long_avg)
        post_short_qty = float(self.short_qty)
        post_short_avg = float(self.short_avg)

        # Backtest-only audit record (if recorder is attached and enabled).
        if self.audit_recorder is not None and self.audit_recorder.enabled:
            global_seq, candle_seq = self.audit_recorder.next_event_sequence(
                self.current_candle_index
            )
            created_candle_index = getattr(order, "created_candle_index", None)
            fee_rate_value = None
            if pnl_details is not None:
                fee_rate_value = (
                    float(pnl_details.get("fee_rate"))
                    if pnl_details.get("fee_rate") is not None
                    else None
                )
            record = FillAuditRecord(
                global_event_sequence=global_seq,
                event_sequence_in_candle=candle_seq,
                candle_index=self.current_candle_index,
                order_created_timestamp=order.created_at.isoformat() if order.created_at else None,
                fill_timestamp=None,
                event_type="fill",
                order_id=order.order_id,
                order_purpose=order.purpose,
                order_side=side,
                reduce_only=bool(order.reduce_only),
                requested_qty=fill_qty,
                executed_qty=float(close_qty if order.reduce_only else fill_qty),
                fill_price=float(fill_price),
                created_candle_index=created_candle_index,
                fill_candle_index=self.current_candle_index,
                long_qty_before=pre_long_qty,
                long_avg_before=pre_long_avg,
                short_qty_before=pre_short_qty,
                short_avg_before=pre_short_avg,
                long_qty_after=post_long_qty,
                long_avg_after=post_long_avg,
                short_qty_after=post_short_qty,
                short_avg_after=post_short_avg,
                closed_pnl=float(realized_pnl),
                gross_pnl=(
                    float(pnl_details.get("gross_pnl"))
                    if pnl_details and pnl_details.get("gross_pnl") is not None
                    else None
                ),
                entry_fee=(
                    float(pnl_details.get("entry_fee"))
                    if pnl_details and pnl_details.get("entry_fee") is not None
                    else None
                ),
                exit_fee=(
                    float(pnl_details.get("exit_fee"))
                    if pnl_details and pnl_details.get("exit_fee") is not None
                    else None
                ),
                fee_rate=fee_rate_value,
                record_source="SimulatedOrderBook.apply_fill",
                runtime_logged=True,
            )
            self.audit_recorder.record_fill(record)

        return order, float(realized_pnl)

    def apply_market_fill(
        self,
        *,
        order_id: str,
        fill_price: float,
        qty: float | None = None,
    ) -> VirtualOrder:
        order, _ = self.apply_fill(order_id=order_id, fill_price=fill_price, qty=qty)
        return order
