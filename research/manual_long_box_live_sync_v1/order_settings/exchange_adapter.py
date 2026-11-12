"""Live Bybit adapter — wraps canonical BybitOrderManager (no duplicate API client)."""

from __future__ import annotations

import logging
from typing import Any

from research.manual_long_box_live_sync_v1.exchange_dry_run import ExchangeAck, OrderStatus

from .config import LiveOrderSettings
from .credentials import build_order_manager
from .reconcile import confirmed_to_dry_view, map_bybit_order_status, wait_for_order_rest

logger = logging.getLogger(__name__)


class BybitLiveExchangeAdapter:
    """Same surface as DryRunExchangeAdapter for sync_engine hooks."""

    supports_native_amend = True

    def __init__(self, settings: LiveOrderSettings, order_manager=None) -> None:
        self.settings = settings
        self._om = order_manager
        self._pending_amend: set[str] = set()

    @property
    def order_manager(self):
        if self._om is None:
            raise RuntimeError("order_manager not injected and live not armed")
        return self._om

    def _guard_writes(self) -> None:
        if not self.settings.writes_allowed:
            raise RuntimeError(
                "Live writes blocked: MANUAL_LONG_BOX_LIVE_ARMED=1, BYBIT credentials, "
                "fixed notional / MANUAL_LONG_BOX_ORDER_QTY / activation notional"
            )

    @classmethod
    def from_settings(cls, settings: LiveOrderSettings) -> "BybitLiveExchangeAdapter":
        om = None
        if settings.writes_allowed:
            om = build_order_manager(settings)
        return cls(settings, order_manager=om)

    def _resolve_order_qty(self, qty: float | None) -> float | None:
        if self.settings.fixed_test_notional_usd is not None:
            if qty is not None and qty > 0:
                return float(qty)
            return None
        if self.settings.order_qty is not None and self.settings.order_qty > 0:
            return float(self.settings.order_qty)
        if self.settings.use_activation_notional and qty is not None and qty > 0:
            return float(qty)
        return None

    def _manual_long_create_kwargs(self) -> dict[str, Any]:
        if self.settings.fixed_test_notional_usd is None:
            return {}
        return {"skip_leverage_adjustment": True, "skip_qty_rounding": True}

    def _base_order_kwargs(self, *, position_idx: int | None = None) -> dict[str, Any]:
        return {
            "time_in_force": self.settings.time_in_force,
            "position_idx": int(position_idx if position_idx is not None else self.settings.position_idx),
            "category": self.settings.category,
        }

    def _short_order_kwargs(self) -> dict[str, Any]:
        return {
            "time_in_force": self.settings.time_in_force,
            "position_idx": 2,
            "category": self.settings.category,
        }

    def _tpsl_kwargs(
        self,
        *,
        stop_loss: float | None,
        take_profit: float | None,
        include_tp: bool = True,
    ) -> dict[str, Any]:
        if not self.settings.attach_tpsl_on_create or stop_loss is None:
            return {}
        out: dict[str, Any] = {
            "stop_loss": stop_loss,
            "tpsl_mode": self.settings.tpsl_mode,
            "sl_order_type": self.settings.sl_order_type,
            "sl_trigger_by": self.settings.sl_trigger_by,
        }
        if include_tp and take_profit is not None:
            out.update(
                take_profit=take_profit,
                tp_order_type=self.settings.tp_order_type,
                tp_trigger_by=self.settings.tp_trigger_by,
            )
        return out

    def _open_and_reconcile(
        self,
        *,
        sym: str,
        price: float,
        size: float,
        order_link_id: str,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> ExchangeAck:
        kwargs = {
            **self._base_order_kwargs(),
            **self._tpsl_kwargs(stop_loss=stop_loss, take_profit=take_profit, include_tp=True),
            **self._manual_long_create_kwargs(),
        }
        raw = self.order_manager.open_long_limit(
            sym,
            price,
            size,
            order_link_id=order_link_id,
            **kwargs,
        )
        if (
            not raw
            and stop_loss is not None
            and take_profit is not None
            and self.settings.attach_tpsl_on_create
        ):
            sl_only = self._tpsl_kwargs(
                stop_loss=stop_loss, take_profit=take_profit, include_tp=False
            )
            raw = self.order_manager.open_long_limit(
                sym,
                price,
                size,
                order_link_id=order_link_id,
                **self._base_order_kwargs(),
                **sl_only,
                **self._manual_long_create_kwargs(),
            )
            if raw:
                oid = str(raw.get("orderId") or "")
                tp_amend = self.order_manager.amend_order_direct(
                    sym,
                    order_id=oid,
                    take_profit=take_profit,
                    tp_trigger_by=self.settings.tp_trigger_by,
                )
                if not tp_amend:
                    logger.warning(
                        "create_ok_tp_amend_failed order_id=%s link=%s",
                        oid,
                        order_link_id,
                    )
        if not raw:
            return ExchangeAck(ok=False, message="open_long_limit_failed")
        oid = str(raw.get("orderId") or "")
        ack = ExchangeAck(
            ok=True,
            order_id=oid,
            status=OrderStatus.PENDING_ACK.value,
            raw={"create": raw},
        )
        ok, confirmed, reason = wait_for_order_rest(
            self.order_manager,
            sym,
            order_id=oid,
            order_link_id=order_link_id,
            expected_price=price,
        )
        if not ok:
            ack.status = OrderStatus.PENDING_ACK.value
            ack.message = reason
            return ack
        ack.status = confirmed.order_status if confirmed else OrderStatus.OPEN.value
        ack.raw["reconcile"] = confirmed.raw if confirmed else {}
        return ack

    def _open_and_reconcile_short(
        self,
        *,
        sym: str,
        price: float,
        size: float,
        order_link_id: str,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> ExchangeAck:
        kwargs = {
            **self._short_order_kwargs(),
            **self._tpsl_kwargs(stop_loss=stop_loss, take_profit=take_profit, include_tp=True),
            **self._manual_long_create_kwargs(),
        }
        raw = self.order_manager.open_short_limit(
            sym,
            price,
            size,
            order_link_id=order_link_id,
            **kwargs,
        )
        if (
            not raw
            and stop_loss is not None
            and take_profit is not None
            and self.settings.attach_tpsl_on_create
        ):
            sl_only = self._tpsl_kwargs(
                stop_loss=stop_loss, take_profit=take_profit, include_tp=False
            )
            raw = self.order_manager.open_short_limit(
                sym,
                price,
                size,
                order_link_id=order_link_id,
                **self._short_order_kwargs(),
                **sl_only,
                **self._manual_long_create_kwargs(),
            )
            if raw:
                oid = str(raw.get("orderId") or "")
                tp_amend = self.order_manager.amend_order_direct(
                    sym,
                    order_id=oid,
                    take_profit=take_profit,
                    tp_trigger_by=self.settings.tp_trigger_by,
                )
                if not tp_amend:
                    logger.warning(
                        "create_ok_tp_amend_failed order_id=%s link=%s",
                        oid,
                        order_link_id,
                    )
        if not raw:
            return ExchangeAck(ok=False, message="open_short_limit_failed")
        oid = str(raw.get("orderId") or "")
        ack = ExchangeAck(
            ok=True,
            order_id=oid,
            status=OrderStatus.PENDING_ACK.value,
            raw={"create": raw},
        )
        ok, confirmed, reason = wait_for_order_rest(
            self.order_manager,
            sym,
            order_id=oid,
            order_link_id=order_link_id,
            expected_price=price,
        )
        if not ok:
            ack.status = OrderStatus.PENDING_ACK.value
            ack.message = reason
            return ack
        ack.status = confirmed.order_status if confirmed else OrderStatus.OPEN.value
        ack.raw["reconcile"] = confirmed.raw if confirmed else {}
        return ack

    def create_limit_long(
        self,
        *,
        symbol: str,
        price: float,
        qty: float | None = None,
        order_link_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: Any,
    ) -> ExchangeAck:
        self._guard_writes()
        sym = symbol.upper()
        size = self._resolve_order_qty(qty)
        if size is None or size <= 0:
            return ExchangeAck(ok=False, message="order_qty_not_configured")
        return self._open_and_reconcile(
            sym=sym,
            price=price,
            size=size,
            order_link_id=order_link_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def create_limit_short(
        self,
        *,
        symbol: str,
        price: float,
        qty: float | None = None,
        order_link_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: Any,
    ) -> ExchangeAck:
        self._guard_writes()
        sym = symbol.upper()
        size = self._resolve_order_qty(qty)
        if size is None or size <= 0:
            return ExchangeAck(ok=False, message="order_qty_not_configured")
        return self._open_and_reconcile_short(
            sym=sym,
            price=price,
            size=size,
            order_link_id=order_link_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def amend_limit(self, order_id: str, *, new_price: float, **_: Any) -> ExchangeAck:
        self._guard_writes()
        if order_id in self._pending_amend:
            return ExchangeAck(ok=False, message="amend_in_flight", order_id=order_id)
        self._pending_amend.add(order_id)
        try:
            sym = self.settings.symbol
            live = wait_for_order_rest(
                self.order_manager, sym, order_id=order_id, attempts=1
            )
            if live[1] and live[1].order_status in ("Filled",):
                return ExchangeAck(
                    ok=False,
                    message="order_filled",
                    order_id=order_id,
                    status=OrderStatus.FILLED.value,
                )
            raw = self.order_manager.amend_order_direct(
                sym,
                order_id=order_id,
                price=new_price,
            )
            if not raw:
                return ExchangeAck(ok=False, message="amend_failed", order_id=order_id)
            ack = ExchangeAck(
                ok=True,
                order_id=order_id,
                status=OrderStatus.PENDING_ACK.value,
                raw={"amend": raw},
            )
            ok, confirmed, reason = wait_for_order_rest(
                self.order_manager,
                sym,
                order_id=order_id,
                expected_price=new_price,
            )
            if not ok:
                ack.message = reason
                return ack
            ack.status = confirmed.order_status if confirmed else OrderStatus.OPEN.value
            return ack
        finally:
            self._pending_amend.discard(order_id)

    def amend_limit_fields(
        self,
        order_id: str,
        *,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> ExchangeAck:
        self._guard_writes()
        raw = self.order_manager.amend_order_direct(
            self.settings.symbol,
            order_id=order_id,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            sl_trigger_by=self.settings.sl_trigger_by if stop_loss is not None else None,
            tp_trigger_by=self.settings.tp_trigger_by if take_profit is not None else None,
        )
        if not raw:
            return ExchangeAck(ok=False, message="amend_failed", order_id=order_id)
        ack = ExchangeAck(
            ok=True,
            order_id=order_id,
            status=OrderStatus.PENDING_ACK.value,
            raw=raw,
        )
        ok, confirmed, reason = wait_for_order_rest(
            self.order_manager,
            self.settings.symbol,
            order_id=order_id,
            expected_price=price,
        )
        if not ok and reason:
            ack.message = reason
        elif confirmed:
            ack.status = confirmed.order_status
        return ack

    def cancel_limit(self, order_id: str) -> ExchangeAck:
        self._guard_writes()
        res = self.order_manager.cancel_order_direct(
            order_id, self.settings.symbol, timeout=5
        )
        if res is None:
            return ExchangeAck(ok=False, message="cancel_failed", order_id=order_id)
        return ExchangeAck(ok=True, order_id=order_id, status=OrderStatus.CANCELLED.value, raw=res)

    def get_order(self, order_id: str):
        found, row, _ = wait_for_order_rest(
            self.order_manager, self.settings.symbol, order_id=order_id, attempts=1
        )
        if not found or row is None:
            return None
        return confirmed_to_dry_view(
            row, amend_in_flight=order_id in self._pending_amend
        )

    def get_open_by_link(self, order_link_id: str):
        found, row, _ = wait_for_order_rest(
            self.order_manager,
            self.settings.symbol,
            order_link_id=order_link_id,
            attempts=1,
        )
        if not found or row is None:
            return None
        st = map_bybit_order_status(row.order_status)
        if st not in (OrderStatus.OPEN, OrderStatus.AMENDING, OrderStatus.PARTIALLY_FILLED):
            return None
        view = confirmed_to_dry_view(row)
        view.order_link_id = order_link_id
        return view

    def reconcile_open_orders(self, symbol: str | None = None) -> list:
        sym = (symbol or self.settings.symbol).upper()
        if not self._om:
            return []
        return self.order_manager.fetch_open_orders_direct(
            sym, timeout=5, order_filter="Order"
        ) or []
