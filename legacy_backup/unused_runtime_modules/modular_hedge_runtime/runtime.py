from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.websocket_client import BybitWebSocketClient
from utils.math_utils import calculate_pnl

from .audit_logger import AuditLogger
from .base import HedgeStrategy, StrategyContext
from .models import (
    FillEvent,
    HedgeSnapshot,
    ManagedOrder,
    RuntimeState,
    StrategyIntent,
    snapshot_from_mapping,
    trace_dicts,
    utcnow,
)
from .order_manager import BybitOrderManager, OrderPayload
from .position_manager import PositionManager


@dataclass
class GenericRuntimeConfig:
    api_key: str
    secret_key: str
    symbol: str = "BTCUSDT"
    category: str = "linear"
    min_order_value: float = 7.0
    price_poll_interval_seconds: float = 1.0
    reconcile_interval_seconds: float = 8.0
    log_file: str = "logs/generic_hedge_runtime.log"
    audit_log_file: str = "logs/generic_hedge_runtime_audit.jsonl"
    strategy_state_file: str | None = None
    health_file: str | None = None
    ensure_exchange_ready: bool = True


class GenericHedgeRuntime:
    def __init__(
        self,
        config: GenericRuntimeConfig,
        strategy: HedgeStrategy,
        *,
        logger: logging.Logger | None = None,
        order_manager: BybitOrderManager | None = None,
        websocket_client: BybitWebSocketClient | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.logger = logger or logging.getLogger(f"runtime.{strategy.name}")
        self.order_manager = order_manager or BybitOrderManager(config.api_key, config.secret_key)
        self.websocket_client = websocket_client
        self.runtime_state = RuntimeState()
        self.position_manager = PositionManager()
        self.audit = AuditLogger(self.logger, config.audit_log_file)
        self.context = StrategyContext(
            audit=self.audit,
            runtime_name=strategy.name,
            symbol=config.symbol,
            category=config.category,
            min_order_value=config.min_order_value,
            cancel_open_orders_by_purpose=self._cancel_open_orders_by_purpose,
        )
        self._stop_event = threading.Event()
        self._price_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def bootstrap(self) -> HedgeSnapshot:
        self._load_strategy_state()
        if self.config.ensure_exchange_ready:
            self.order_manager.ensure_hedge_mode(self.config.symbol, self.config.category)
            self.order_manager.ensure_max_leverage(self.config.symbol, self.config.category)
        self._recover_active_orders_from_exchange()
        snapshot = self.refresh_snapshot("startup")
        self.audit.log_event(
            "runtime_bootstrap",
            strategy=self.strategy.name,
            symbol=self.config.symbol,
            category=self.config.category,
            snapshot=snapshot,
        )
        self._dispatch(
            "start",
            self.strategy.on_start(snapshot, self.runtime_state, self.context),
            snapshot,
        )
        self._save_strategy_state()
        return snapshot

    def start(self) -> None:
        self.bootstrap()
        self._start_websocket()
        self._start_price_loop()
        self._start_reconcile_loop()

    def stop(self) -> None:
        self._stop_event.set()
        if self.websocket_client:
            self.websocket_client.stop()
        for thread in (self._price_thread, self._reconcile_thread, self._ws_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        self._save_strategy_state()

    def process_tick(self) -> HedgeSnapshot:
        snapshot = self.refresh_snapshot("tick")
        self._dispatch("tick", self.strategy.on_tick(snapshot, self.runtime_state, self.context), snapshot)
        return snapshot

    def reconcile_once(self) -> HedgeSnapshot:
        self._reconcile_active_orders()
        snapshot = self.refresh_snapshot("reconcile")
        self._dispatch(
            "reconcile",
            self.strategy.on_reconcile(snapshot, self.runtime_state, self.context),
            snapshot,
        )
        self._save_strategy_state()
        return snapshot

    def refresh_snapshot(self, source: str) -> HedgeSnapshot:
        positions = self._fetch_exchange_position_mapping()
        current_price = self.order_manager.fetch_mark_price(self.config.symbol, self.config.category)
        if current_price is None:
            current_price = self.runtime_state.last_snapshot.current_price if self.runtime_state.last_snapshot else 0.0
        snapshot = snapshot_from_mapping(
            symbol=self.config.symbol,
            current_price=current_price,
            positions=positions,
            runtime_state=self.runtime_state,
            source=source,
        )
        self.runtime_state.last_snapshot = snapshot
        self.audit.log_event(
            "snapshot_refreshed",
            strategy=self.strategy.name,
            source=source,
            symbol=self.config.symbol,
            snapshot=snapshot,
        )
        return snapshot

    def _fetch_exchange_position_mapping(self) -> dict[str, float]:
        positions = self.order_manager.fetch_positions(self.config.symbol, self.config.category)
        long_qty = 0.0
        short_qty = 0.0
        long_avg = 0.0
        short_avg = 0.0
        for position in positions:
            side = str(position.get("side") or position.get("positionSide") or "").lower()
            size = float(position.get("size") or position.get("positionQty") or 0.0)
            avg = float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
            if side in {"buy", "long"}:
                long_qty = size
                long_avg = avg
            elif side in {"sell", "short"}:
                short_qty = size
                short_avg = avg
        self.position_manager.sync_positions(long_qty, long_avg, short_qty, short_avg)
        return {
            "long_qty": self.position_manager.long_size,
            "short_qty": self.position_manager.short_size,
            "long_avg": self.position_manager.long_avg,
            "short_avg": self.position_manager.short_avg,
        }

    def handle_websocket_event(self, topic: str, payload: Any) -> None:
        if isinstance(payload, list):
            return
        if topic not in {"order", "position"}:
            return
        self.audit.log_event("ws_event", strategy=self.strategy.name, topic=topic, payload=payload)
        if topic == "position":
            self._sync_position_manager_from_ws(payload)
            return
        if topic != "order":
            return
        order_id = payload.get("orderId")
        if not order_id:
            return
        client_id = self.runtime_state.exchange_to_client_id.get(order_id)
        if not client_id:
            return
        managed_order = self._get_managed_order(client_id)
        if not managed_order:
            return
        managed_order.status = self._normalize_order_status(payload.get("orderStatus"), managed_order.status)
        managed_order.updated_at = utcnow()
        if managed_order.status in {"PARTIAL", "FILLED"}:
            managed_order.filled_qty = float(payload.get("cumExecQty") or managed_order.filled_qty or 0.0)
            managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
        snapshot = self.runtime_state.last_snapshot or self.refresh_snapshot("ws_order")
        self._dispatch(
            "order_update",
            self.strategy.on_order_update(payload, snapshot, self.runtime_state, self.context),
            snapshot,
        )
        if managed_order.status in {"CANCELED", "REJECTED", "FILLED"}:
            self._finalize_managed_order(client_id, managed_order)
        self._save_strategy_state()

    def on_websocket_fill(
        self,
        exchange_order_id: str,
        qty: float,
        price: float,
        *,
        exec_id: str | None = None,
        cumulative_qty: float | None = None,
        order_link_id: str | None = None,
        order_side: str | None = None,
        position_idx: int | None = None,
    ) -> None:
        client_id = self.runtime_state.exchange_to_client_id.get(exchange_order_id)
        if not client_id and order_link_id and self._get_managed_order(order_link_id):
            client_id = order_link_id
        if not client_id:
            client_id = self._match_exit_order_for_fill(
                order_side=order_side,
                qty=qty,
                exchange_order_id=exchange_order_id,
                price=price,
            )
        if not client_id:
            client_id = self._match_initial_entry_order_for_fill(
                order_side=order_side,
                position_idx=position_idx,
                qty=qty,
                exchange_order_id=exchange_order_id,
                price=price,
            )
        if not client_id:
            self.audit.log_event(
                "unmatched_fill",
                strategy=self.strategy.name,
                exchange_order_id=exchange_order_id,
                order_link_id=order_link_id,
                qty=qty,
                price=price,
                exec_id=exec_id,
                cumulative_qty=cumulative_qty,
            )
            return
        self._ensure_exchange_order_mapping(client_id, exchange_order_id)
        self._ingest_fill_event(
            exchange_order_id=exchange_order_id,
            client_id=client_id,
            qty=qty,
            price=price,
            exec_id=exec_id,
            cumulative_qty=cumulative_qty,
            source="websocket",
        )

    def _match_exit_order_for_fill(
        self,
        *,
        order_side: str | None,
        qty: float,
        exchange_order_id: str,
        price: float,
    ) -> str | None:
        if not order_side:
            return None
        normalized_side = order_side.strip().lower()
        target_side: str | None = None
        if normalized_side == "sell":
            target_side = "long"
        elif normalized_side == "buy":
            target_side = "short"
        if not target_side:
            return None

        candidates: list[tuple[ManagedOrder, int, float, float]] = []
        for order in self.runtime_state.active_orders.values():
            if order.status in {"FILLED", "CANCELED", "REJECTED"}:
                continue
            if not self._is_fill_match_candidate(order) or order.side != target_side:
                continue
            remaining = order.remaining_qty or order.qty
            if remaining <= 0.0 or qty <= 0.0:
                continue
            if qty > remaining + 1e-9:
                continue
            priority = 3 if order.purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT"} else 2 if "EXIT" in order.purpose else 1
            gap_after = remaining - qty
            price_diff = (
                abs(price - order.price) if order.price is not None and price > 0 else float("inf")
            )
            candidates.append((order, priority, gap_after, price_diff))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (-entry[1], entry[2], entry[3]))
        best_order, best_priority, best_gap, best_price_diff = candidates[0]
        tie_orders = [
            order
            for order, priority, gap, price_gap in candidates
            if priority == best_priority
            and abs(gap - best_gap) < 1e-9
            and abs(price_gap - best_price_diff) < 1e-9
        ]
        if len(tie_orders) > 1:
            self.audit.log_event(
                "unmatched_fill_match_ambiguous",
                strategy=self.strategy.name,
                exchange_order_id=exchange_order_id,
                qty=qty,
                price=price,
                side=order_side,
                candidates=[order.client_order_id for order in tie_orders],
            )
            return None
        previous_exchange_id = best_order.exchange_order_id
        if previous_exchange_id and previous_exchange_id != exchange_order_id:
            self.runtime_state.exchange_to_client_id.pop(previous_exchange_id, None)
        best_order.exchange_order_id = exchange_order_id
        self.runtime_state.exchange_to_client_id[exchange_order_id] = best_order.client_order_id
        self.audit.log_event(
            "unmatched_fill_matched",
            strategy=self.strategy.name,
            exchange_order_id=exchange_order_id,
            matched_client_order_id=best_order.client_order_id,
            purpose=best_order.purpose,
            matched_side=best_order.side,
            reduce_only=best_order.reduce_only,
            qty=qty,
            price=price,
            side=order_side,
            remaining_qty=best_order.remaining_qty,
            previous_exchange_id=previous_exchange_id,
        )
        return best_order.client_order_id

    def _match_initial_entry_order_for_fill(
        self,
        *,
        order_side: str | None,
        position_idx: int | None,
        qty: float,
        exchange_order_id: str,
        price: float,
    ) -> str | None:
        target_side = self._infer_entry_fill_side(order_side=order_side, position_idx=position_idx)
        if not target_side:
            return None

        expected_purpose = "INITIAL_LONG_ENTRY" if target_side == "long" else "INITIAL_SHORT_ENTRY"
        candidates: list[ManagedOrder] = []
        for order in self.runtime_state.active_orders.values():
            if order.status in {"FILLED", "CANCELED", "REJECTED"}:
                continue
            if order.reduce_only:
                continue
            if order.side != target_side or order.purpose != expected_purpose:
                continue
            remaining = order.remaining_qty or max(order.qty - order.filled_qty, 0.0)
            if remaining <= 0.0 or qty <= 0.0:
                continue
            if qty > remaining + 1e-9:
                continue
            candidates.append(order)

        if len(candidates) != 1:
            if len(candidates) > 1:
                self.audit.log_event(
                    "initial_entry_fill_match_ambiguous",
                    strategy=self.strategy.name,
                    exchange_order_id=exchange_order_id,
                    order_side=order_side,
                    position_idx=position_idx,
                    qty=qty,
                    price=price,
                    candidates=[order.client_order_id for order in candidates],
                )
            return None

        matched_order = candidates[0]
        previous_exchange_id = matched_order.exchange_order_id
        if previous_exchange_id and previous_exchange_id != exchange_order_id:
            self.runtime_state.exchange_to_client_id.pop(previous_exchange_id, None)
        matched_order.exchange_order_id = exchange_order_id
        self.runtime_state.exchange_to_client_id[exchange_order_id] = matched_order.client_order_id
        self.audit.log_event(
            "initial_entry_fill_matched",
            strategy=self.strategy.name,
            exchange_order_id=exchange_order_id,
            matched_client_order_id=matched_order.client_order_id,
            purpose=matched_order.purpose,
            matched_side=matched_order.side,
            order_side=order_side,
            position_idx=position_idx,
            qty=qty,
            price=price,
            previous_exchange_id=previous_exchange_id,
        )
        return matched_order.client_order_id

    @staticmethod
    def _infer_entry_fill_side(*, order_side: str | None, position_idx: int | None) -> str | None:
        if position_idx == 1:
            return "long"
        if position_idx == 2:
            return "short"
        normalized_side = str(order_side or "").strip().lower()
        if normalized_side in {"buy", "long"}:
            return "long"
        if normalized_side in {"sell", "short"}:
            return "short"
        return None

    @staticmethod
    def _is_fill_match_candidate(order: ManagedOrder) -> bool:
        if not order.reduce_only:
            return False
        purpose = str(order.purpose or "").upper()
        if purpose.startswith("INITIAL_"):
            return False
        if "EXIT" in purpose or "REDUCE" in purpose:
            return True
        cycle_role = str(order.metadata.get("cycle_role") or "").lower()
        exit_type = str(order.metadata.get("exit_type") or "").lower()
        return cycle_role.endswith("reduce") or exit_type.endswith(("tp", "sl"))

    def _dispatch(self, source: str, intents: list[StrategyIntent], snapshot: HedgeSnapshot) -> None:
        if not intents:
            self.audit.log_event(
                "strategy_noop",
                strategy=self.strategy.name,
                source=source,
                snapshot=snapshot,
            )
            return
        for intent in intents:
            self.submit_intent(intent, snapshot, source)

    def submit_intent(self, intent: StrategyIntent, snapshot: HedgeSnapshot, source: str) -> str | None:
        submit_price = intent.price if intent.price is not None else snapshot.current_price
        if intent.qty <= 0 or submit_price <= 0:
            self.audit.log_event(
                "intent_rejected",
                strategy=self.strategy.name,
                source=source,
                reason="invalid_qty_or_price",
                intent=intent,
                snapshot=snapshot,
            )
            return None
        normalized_qty = self.order_manager.normalize_qty(self.config.symbol, intent.qty, self.config.category)
        notional = normalized_qty * submit_price
        if normalized_qty <= 0 or notional < self.config.min_order_value:
            self.audit.log_event(
                "intent_rejected",
                strategy=self.strategy.name,
                source=source,
                reason="below_min_order_value",
                normalized_qty=normalized_qty,
                notional=notional,
                min_order_value=self.config.min_order_value,
                intent=intent,
            )
            return None
        replace_purposes_raw = intent.metadata.get("replace_open_purpose")
        if replace_purposes_raw:
            replace_purposes = [replace_purposes_raw] if isinstance(replace_purposes_raw, str) else list(replace_purposes_raw)
            self._cancel_open_orders_by_purpose(replace_purposes)
        client_id = f"{self.strategy.name}-{intent.purpose.lower()}-{uuid4().hex[:10]}"
        managed_order = ManagedOrder(
            client_order_id=client_id,
            side=intent.side,
            qty=normalized_qty,
            purpose=intent.purpose,
            price=intent.price,
            order_type=intent.order_type,
            reduce_only=intent.reduce_only,
            remaining_qty=normalized_qty,
            metadata={
                **dict(intent.metadata),
                "source": source,
                "entry_price": snapshot.long_avg if intent.side == "long" else snapshot.short_avg,
                "snapshot_price": snapshot.current_price,
            },
            trace=list(intent.trace),
        )
        response = self._submit_to_exchange(managed_order, snapshot)
        if not response:
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                source=source,
                client_order_id=client_id,
                intent=intent,
                traces=trace_dicts(intent.trace),
            )
            return None
        exchange_order_id = ((response.get("result") or {}).get("orderId")) if isinstance(response, dict) else None
        managed_order.exchange_order_id = exchange_order_id
        managed_order.status = "OPEN"
        self.runtime_state.active_orders[client_id] = managed_order
        if exchange_order_id:
            self.runtime_state.exchange_to_client_id[exchange_order_id] = client_id
        self.audit.log_event(
            "intent_submitted",
            strategy=self.strategy.name,
            source=source,
            client_order_id=client_id,
            exchange_order_id=exchange_order_id,
            intent=intent,
            normalized_qty=normalized_qty,
            submit_notional=notional,
            traces=trace_dicts(intent.trace),
        )
        self._save_strategy_state()
        return client_id

    def _cancel_open_orders_by_purpose(self, purposes: list[str]) -> None:
        purposes_set = {purpose for purpose in purposes if purpose}
        if not purposes_set:
            return
        for client_id, order in list(self.runtime_state.active_orders.items()):
            if order.purpose not in purposes_set or order.status in {"FILLED", "CANCELED", "REJECTED"}:
                continue
            canceled = False
            if order.exchange_order_id:
                canceled = self.order_manager.cancel_order(
                    order.exchange_order_id,
                    symbol=self.config.symbol,
                    category=self.config.category,
                )
            order.status = "CANCELED" if canceled or not order.exchange_order_id else order.status
            self.runtime_state.active_orders.pop(client_id, None)
            if order.exchange_order_id:
                self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
            self.audit.log_event(
                "intent_replaced_cancel",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=order.exchange_order_id,
                purpose=order.purpose,
                canceled=canceled,
            )

    def _submit_to_exchange(self, managed_order: ManagedOrder, snapshot: HedgeSnapshot) -> Any:
        exchange_side = self._exchange_side(managed_order.side, managed_order.reduce_only)
        position_idx = 1 if managed_order.side == "long" else 2
        if managed_order.order_type == "Limit":
            payload = OrderPayload(
                category=self.config.category,
                symbol=self.config.symbol,
                side=exchange_side,
                order_type="Limit",
                price=managed_order.price,
                qty=managed_order.qty,
                reduce_only=managed_order.reduce_only,
                position_idx=position_idx,
                order_link_id=managed_order.client_order_id,
            )
            return self.order_manager.place_limit_order(payload)
        if managed_order.reduce_only:
            if intent.close_on_trigger:
                return self.order_manager.place_reduce_market_order(
                    symbol=self.config.symbol,
                    side=exchange_side,
                    qty=managed_order.qty,
                    position_idx=position_idx,
                    category=self.config.category,
                    order_link_id=managed_order.client_order_id,
                    trigger_price=intent.trigger_price,
                    trigger_direction=intent.trigger_direction,
                    trigger_by=intent.trigger_by,
                    close_on_trigger=True,
                )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
            )
        return self.order_manager.place_market_order(
            symbol=self.config.symbol,
            side=exchange_side,
            qty=managed_order.qty,
            price=snapshot.current_price,
            position_idx=position_idx,
            category=self.config.category,
            order_link_id=managed_order.client_order_id,
        )

    @staticmethod
    def _exchange_side(side: str, reduce_only: bool) -> str:
        if side == "long":
            return "Sell" if reduce_only else "Buy"
        return "Buy" if reduce_only else "Sell"

    def _start_websocket(self) -> None:
        if self.websocket_client is None:
            self.websocket_client = BybitWebSocketClient(self.config.api_key, self.config.secret_key)
        self.websocket_client.add_callback(self.handle_websocket_event)
        self.websocket_client.set_fill_callback(self.on_websocket_fill)
        self._ws_thread = threading.Thread(target=self.websocket_client.run, daemon=True)
        self._ws_thread.start()
        self.audit.log_event(
            "websocket_started",
            strategy=self.strategy.name,
            symbol=self.config.symbol,
            health_file=self.config.health_file,
        )

    def _start_price_loop(self) -> None:
        def poll() -> None:
            while not self._stop_event.wait(self.config.price_poll_interval_seconds):
                try:
                    self.process_tick()
                except Exception as exc:
                    self.audit.log_event("price_loop_error", strategy=self.strategy.name, error=str(exc))

        self._price_thread = threading.Thread(target=poll, daemon=True)
        self._price_thread.start()

    def _start_reconcile_loop(self) -> None:
        def reconcile() -> None:
            while not self._stop_event.wait(self.config.reconcile_interval_seconds):
                try:
                    self.reconcile_once()
                except Exception as exc:
                    self.audit.log_event("reconcile_loop_error", strategy=self.strategy.name, error=str(exc))

        self._reconcile_thread = threading.Thread(target=reconcile, daemon=True)
        self._reconcile_thread.start()

    def _sync_position_manager_from_ws(self, payload: dict[str, Any]) -> None:
        side = str(payload.get("side") or "").lower()
        size = float(payload.get("size") or 0.0)
        avg = float(payload.get("entryPrice") or 0.0)
        if side in {"buy", "long"}:
            self.position_manager.sync_positions(
                long_size=size,
                long_avg=avg,
                short_size=self.position_manager.short_size,
                short_avg=self.position_manager.short_avg,
            )
        elif side in {"sell", "short"}:
            self.position_manager.sync_positions(
                long_size=self.position_manager.long_size,
                long_avg=self.position_manager.long_avg,
                short_size=size,
                short_avg=avg,
            )
        self.audit.log_event(
            "position_ws_synced",
            strategy=self.strategy.name,
            long_size=self.position_manager.long_size,
            long_avg=self.position_manager.long_avg,
            short_size=self.position_manager.short_size,
            short_avg=self.position_manager.short_avg,
        )

    def _ingest_fill_event(
        self,
        *,
        exchange_order_id: str,
        client_id: str,
        qty: float,
        price: float,
        exec_id: str | None,
        cumulative_qty: float | None,
        source: str,
    ) -> None:
        managed_order = self._get_managed_order(client_id)
        if not managed_order:
            return
        with self._lock:
            processed_exec_ids = set(managed_order.metadata.get("processed_exec_ids") or [])
            if exec_id and exec_id in processed_exec_ids:
                return
            if exec_id:
                processed_exec_ids.add(exec_id)
                managed_order.metadata["processed_exec_ids"] = sorted(processed_exec_ids)
            if cumulative_qty is None and managed_order.filled_qty >= managed_order.qty - 1e-9:
                self.audit.log_event(
                    "late_fill_ignored",
                    strategy=self.strategy.name,
                    source=source,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    purpose=managed_order.purpose,
                    exec_id=exec_id,
                    qty=qty,
                    price=price,
                )
                return
            previous_filled = managed_order.filled_qty
            if cumulative_qty is not None and cumulative_qty > previous_filled:
                incremental_qty = cumulative_qty - previous_filled
                managed_order.filled_qty = min(managed_order.qty, cumulative_qty)
            elif cumulative_qty is not None:
                self.audit.log_event(
                    "late_fill_ignored",
                    strategy=self.strategy.name,
                    source=source,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    purpose=managed_order.purpose,
                    exec_id=exec_id,
                    qty=qty,
                    price=price,
                    cumulative_qty=cumulative_qty,
                )
                return
            else:
                incremental_qty = qty
                managed_order.filled_qty = min(managed_order.qty, previous_filled + qty)
            managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
            managed_order.status = "FILLED" if managed_order.remaining_qty <= 1e-9 else "PARTIAL"
            managed_order.updated_at = utcnow()
            entry_price = float(
                managed_order.metadata.get("entry_price")
                or (
                    self.runtime_state.last_snapshot.long_avg
                    if managed_order.side == "long" and self.runtime_state.last_snapshot
                    else self.runtime_state.last_snapshot.short_avg
                    if self.runtime_state.last_snapshot
                    else 0.0
                )
            )
            if managed_order.reduce_only and incremental_qty > 0 and entry_price > 0:
                pnl = calculate_pnl(entry_price, price, incremental_qty, managed_order.side)
                if managed_order.side == "long":
                    self.runtime_state.realized_long_pnl_total += pnl
                else:
                    self.runtime_state.realized_short_pnl_total += pnl
            fill_event = FillEvent(
                exchange_order_id=exchange_order_id,
                client_order_id=client_id,
                side=managed_order.side,
                purpose=managed_order.purpose,
                exec_qty=qty,
                exec_price=price,
                order_type=managed_order.order_type,
                reduce_only=managed_order.reduce_only,
                status=managed_order.status,
                cumulative_qty=cumulative_qty,
                incremental_qty=incremental_qty,
                exec_id=exec_id,
                metadata={**dict(managed_order.metadata), "fill_source": source},
                traces=list(managed_order.trace),
            )
        self.audit.log_event(
            "fill_received",
            strategy=self.strategy.name,
            source=source,
            fill=fill_event.to_dict(),
        )
        snapshot = self.refresh_snapshot("fill")
        self._dispatch("fill", self.strategy.on_fill(fill_event, snapshot, self.runtime_state, self.context), snapshot)
        if managed_order.status == "FILLED" and client_id in self.runtime_state.active_orders:
            self._finalize_managed_order(client_id, managed_order)
        self._save_strategy_state()

    def _ensure_exchange_order_mapping(self, client_id: str | None, exchange_order_id: str | None) -> None:
        if not client_id or not exchange_order_id:
            return
        existing = self.runtime_state.exchange_to_client_id.get(exchange_order_id)
        if existing and existing != client_id:
            self.runtime_state.exchange_to_client_id.pop(exchange_order_id, None)
        managed_order = self._get_managed_order(client_id)
        if managed_order and managed_order.exchange_order_id and managed_order.exchange_order_id != exchange_order_id:
            self.runtime_state.exchange_to_client_id.pop(managed_order.exchange_order_id, None)
        self.runtime_state.exchange_to_client_id[exchange_order_id] = client_id
        if managed_order:
            managed_order.exchange_order_id = exchange_order_id

    def _reconcile_active_orders(self) -> None:
        if not self.runtime_state.active_orders:
            self.audit.log_event(
                "reconcile_skipped",
                strategy=self.strategy.name,
                reason="no_active_orders",
            )
            return
        open_orders = self.order_manager.fetch_open_orders(self.config.symbol, self.config.category) or []
        positions = self._fetch_exchange_position_mapping()
        open_by_exchange_id = {
            str(order.get("orderId")): order for order in open_orders if order.get("orderId")
        }
        open_by_link_id = {
            str(order.get("orderLinkId")): order for order in open_orders if order.get("orderLinkId")
        }
        for client_id, managed_order in list(self.runtime_state.active_orders.items()):
            open_match = open_by_exchange_id.get(managed_order.exchange_order_id or "") or open_by_link_id.get(client_id)
            if open_match:
                previous_filled_qty = managed_order.filled_qty
                managed_order.status = self._normalize_order_status(open_match.get("orderStatus"), "OPEN")
                managed_order.updated_at = utcnow()
                managed_order.filled_qty = float(open_match.get("cumExecQty") or managed_order.filled_qty or 0.0)
                managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
                self.audit.log_event(
                    "order_reconciled_open",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                    exchange_order=self._exchange_order_summary(open_match),
                    previous_filled_qty=previous_filled_qty,
                    status=managed_order.status,
                    filled_qty=managed_order.filled_qty,
                    remaining_qty=managed_order.remaining_qty,
                    reconcile_source="open_orders",
                )
                continue
            self.audit.log_event(
                "reconcile_open_order_miss",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=managed_order.exchange_order_id,
                managed_order=self._managed_order_summary(managed_order),
            )
            history = self.order_manager.fetch_order_history(
                self.config.symbol,
                self.config.category,
                order_id=managed_order.exchange_order_id,
                order_link_id=client_id,
                limit=1,
            ) or []
            if not history:
                self.audit.log_event(
                    "reconcile_history_miss",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    managed_order=self._managed_order_summary(managed_order),
                )
                if self._reconcile_initial_entry_from_position(
                    client_id=client_id,
                    managed_order=managed_order,
                    positions=positions,
                ):
                    continue
                continue
            history_order = history[0]
            normalized_history_status = self._normalize_order_status(history_order.get("orderStatus"), managed_order.status)
            avg_fill_price = self._history_fill_price(history_order, managed_order.price or 0.0)
            cumulative_qty = float(history_order.get("cumExecQty") or 0.0)
            self.audit.log_event(
                "reconcile_history_found",
                strategy=self.strategy.name,
                client_order_id=client_id,
                exchange_order_id=managed_order.exchange_order_id,
                managed_order=self._managed_order_summary(managed_order),
                history_order=self._history_order_summary(history_order),
                normalized_history_status=normalized_history_status,
                inferred_fill_price=avg_fill_price,
                cumulative_qty=cumulative_qty,
            )
            if normalized_history_status in {"FILLED", "PARTIAL"} and cumulative_qty > managed_order.filled_qty:
                exec_price = avg_fill_price if avg_fill_price > 0 else float(managed_order.price or 0.0)
                incremental_qty = max(cumulative_qty - managed_order.filled_qty, 0.0)
                self.audit.log_event(
                    "reconcile_fill_inferred",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    inferred_status=normalized_history_status,
                    incremental_qty=incremental_qty,
                    cumulative_qty=cumulative_qty,
                    exec_price=exec_price,
                    history_order=self._history_order_summary(history_order),
                )
                self._ingest_fill_event(
                    exchange_order_id=managed_order.exchange_order_id or client_id,
                    client_id=client_id,
                    qty=incremental_qty,
                    price=exec_price,
                    exec_id=f"reconcile-{client_id}-{cumulative_qty}",
                    cumulative_qty=cumulative_qty,
                    source="reconcile",
                )
                managed_order = self.runtime_state.active_orders.get(client_id)
                if not managed_order:
                    continue
            if normalized_history_status in {"CANCELED", "REJECTED"}:
                managed_order.status = normalized_history_status
                managed_order.updated_at = utcnow()
                self._finalize_managed_order(client_id, managed_order)
                self.audit.log_event(
                    "order_reconciled_terminal",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    history_order=self._history_order_summary(history_order),
                    managed_order=self._managed_order_summary(managed_order),
                    status=normalized_history_status,
                )
                continue
            if normalized_history_status == "PARTIAL":
                managed_order.status = "PARTIAL"
                managed_order.updated_at = utcnow()
                managed_order.filled_qty = max(managed_order.filled_qty, cumulative_qty)
                managed_order.remaining_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
                self.audit.log_event(
                    "order_reconciled_partial",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=managed_order.exchange_order_id,
                    history_order=self._history_order_summary(history_order),
                    managed_order=self._managed_order_summary(managed_order),
                    status=normalized_history_status,
                    filled_qty=managed_order.filled_qty,
                    remaining_qty=managed_order.remaining_qty,
                )
            if self._reconcile_initial_entry_from_position(
                client_id=client_id,
                managed_order=managed_order,
                positions=positions,
            ):
                continue
        self._save_strategy_state()

    def _reconcile_initial_entry_from_position(
        self,
        *,
        client_id: str,
        managed_order: ManagedOrder,
        positions: dict[str, float],
    ) -> bool:
        if managed_order.reduce_only or managed_order.purpose not in {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}:
            return False
        if managed_order.status in {"FILLED", "CANCELED", "REJECTED"}:
            return False

        position_qty = float(positions.get("long_qty") if managed_order.side == "long" else positions.get("short_qty") or 0.0)
        position_avg = float(positions.get("long_avg") if managed_order.side == "long" else positions.get("short_avg") or 0.0)
        qty_tolerance = max(abs(managed_order.qty) * 1e-6, 1e-9)
        if position_qty + qty_tolerance < managed_order.qty:
            return False

        missing_qty = max(managed_order.qty - managed_order.filled_qty, 0.0)
        if missing_qty <= qty_tolerance:
            return False

        exchange_order_id = managed_order.exchange_order_id or client_id
        exec_price = position_avg if position_avg > 0 else float(managed_order.price or 0.0)
        exec_id = f"reconcile-position-{client_id}-{managed_order.qty}"
        self.audit.log_event(
            "initial_entry_position_reconciled",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=exchange_order_id,
            purpose=managed_order.purpose,
            side=managed_order.side,
            position_qty=position_qty,
            expected_qty=managed_order.qty,
            previous_filled_qty=managed_order.filled_qty,
            inferred_exec_price=exec_price,
        )
        self._ingest_fill_event(
            exchange_order_id=exchange_order_id,
            client_id=client_id,
            qty=missing_qty,
            price=exec_price,
            exec_id=exec_id,
            cumulative_qty=managed_order.qty,
            source="reconcile_position",
        )
        return True

    @staticmethod
    def _history_fill_price(history_order: dict[str, Any], fallback_price: float) -> float:
        avg_price = history_order.get("avgPrice")
        if avg_price not in (None, "", "0", 0):
            return float(avg_price)
        cum_exec_value = history_order.get("cumExecValue")
        cum_exec_qty = history_order.get("cumExecQty")
        try:
            if cum_exec_value not in (None, "", "0", 0) and cum_exec_qty not in (None, "", "0", 0):
                return float(cum_exec_value) / float(cum_exec_qty)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            return float(history_order.get("price") or fallback_price or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _load_strategy_state(self) -> None:
        if not self.config.strategy_state_file:
            return
        path = Path(self.config.strategy_state_file)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.audit.log_event("strategy_state_load_failed", strategy=self.strategy.name, path=str(path))
            return
        if not isinstance(payload, dict):
            return
        self.runtime_state.strategy_state = dict(payload.get("strategy_state") or {})
        self.runtime_state.realized_long_pnl_total = float(payload.get("realized_long_pnl_total") or 0.0)
        self.runtime_state.realized_short_pnl_total = float(payload.get("realized_short_pnl_total") or 0.0)
        self.runtime_state.sequence = int(payload.get("sequence") or 0)
        restored_active_orders = payload.get("active_orders") or []
        if isinstance(restored_active_orders, list):
            for item in restored_active_orders:
                order = self._managed_order_from_dict(item)
                if not order:
                    continue
                self.runtime_state.active_orders[order.client_order_id] = order
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id[order.exchange_order_id] = order.client_order_id
        self.audit.log_event(
            "strategy_state_loaded",
            strategy=self.strategy.name,
            path=str(path),
            strategy_state=self.runtime_state.strategy_state,
            realized_long_pnl_total=self.runtime_state.realized_long_pnl_total,
            realized_short_pnl_total=self.runtime_state.realized_short_pnl_total,
            sequence=self.runtime_state.sequence,
            restored_active_order_count=len(self.runtime_state.active_orders),
        )

    def _save_strategy_state(self) -> None:
        if not self.config.strategy_state_file:
            return
        path = Path(self.config.strategy_state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy": self.strategy.name,
            "symbol": self.config.symbol,
            "category": self.config.category,
            "strategy_state": self.runtime_state.strategy_state,
            "realized_long_pnl_total": self.runtime_state.realized_long_pnl_total,
            "realized_short_pnl_total": self.runtime_state.realized_short_pnl_total,
            "sequence": self.runtime_state.sequence,
            "active_orders": [
                self._managed_order_to_dict(order)
                for order in self.runtime_state.active_orders.values()
                if order.status not in {"FILLED", "CANCELED", "REJECTED"}
            ],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _recover_active_orders_from_exchange(self) -> None:
        open_orders = self.order_manager.fetch_open_orders(self.config.symbol, self.config.category) or []
        if not open_orders and not self.runtime_state.active_orders:
            return
        recovered_count = 0
        matched_client_ids: set[str] = set()
        for exchange_order in open_orders:
            exchange_order_id = str(exchange_order.get("orderId") or "")
            recovery_source = "exchange_order_link_id"
            client_id = str(
                exchange_order.get("orderLinkId")
                or self.runtime_state.exchange_to_client_id.get(exchange_order_id)
                or ""
            )
            if not exchange_order.get("orderLinkId") and client_id:
                recovery_source = "exchange_order_id_mapping"
            if not client_id:
                client_id = self._match_existing_active_order(exchange_order, matched_client_ids) or ""
                if client_id:
                    recovery_source = "persisted_order_match"
            if not client_id:
                recovered_purpose = self._classify_unknown_order_purpose(exchange_order)
                client_id = (
                    f"recovered-{self.strategy.name}--{recovered_purpose.lower()}--{exchange_order_id or uuid4().hex[:8]}"
                )
                recovery_source = "heuristic_classification"
                self.audit.log_event(
                    "startup_order_recovery_classified",
                    strategy=self.strategy.name,
                    exchange_order=self._exchange_order_summary(exchange_order),
                    classified_purpose=recovered_purpose,
                    classified_client_order_id=client_id,
                    classification_inputs=self._classification_inputs(exchange_order),
                )
            existing = self.runtime_state.active_orders.get(client_id)
            recovered = self._recover_managed_order(client_id, exchange_order, existing)
            if existing and existing.exchange_order_id and existing.exchange_order_id != recovered.exchange_order_id:
                self.runtime_state.exchange_to_client_id.pop(existing.exchange_order_id, None)
            self.runtime_state.active_orders[client_id] = recovered
            if recovered.exchange_order_id:
                self.runtime_state.exchange_to_client_id[recovered.exchange_order_id] = client_id
            self.audit.log_event(
                "startup_order_recovery_attached",
                strategy=self.strategy.name,
                recovery_source=recovery_source,
                exchange_order=self._exchange_order_summary(exchange_order),
                client_order_id=client_id,
                purpose=recovered.purpose,
                side=recovered.side,
                reduce_only=recovered.reduce_only,
                order_type=recovered.order_type,
                had_existing_state=existing is not None,
            )
            matched_client_ids.add(client_id)
            recovered_count += 1

        for client_id, order in list(self.runtime_state.active_orders.items()):
            if order.status in {"FILLED", "CANCELED", "REJECTED"}:
                self.runtime_state.active_orders.pop(client_id, None)
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
                continue
            has_open_match = any(
                (
                    str(item.get("orderId") or "") == str(order.exchange_order_id or "")
                    or str(item.get("orderLinkId") or "") == client_id
                )
                for item in open_orders
            )
            if not has_open_match:
                order.status = "OPEN"
        self.audit.log_event(
            "startup_order_recovery_completed",
            strategy=self.strategy.name,
            recovered_order_count=recovered_count,
            active_order_count=len(self.runtime_state.active_orders),
        )

    def _match_existing_active_order(
        self,
        exchange_order: dict[str, Any],
        matched_client_ids: set[str],
    ) -> str | None:
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for client_id, existing in self.runtime_state.active_orders.items():
            if client_id in matched_client_ids:
                continue
            if existing.status in {"FILLED", "CANCELED", "REJECTED"}:
                continue
            score, details = self._score_recovery_match(existing, exchange_order)
            if score > 0:
                candidates.append((score, client_id, details))
        if not candidates:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="no_candidates",
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        candidates.sort(reverse=True)
        best_score, best_client_id, best_details = candidates[0]
        second_best_score = candidates[1][0] if len(candidates) > 1 else -1
        if best_score < 80:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="score_below_threshold",
                threshold=80,
                best_match_client_order_id=best_client_id,
                best_match_score=best_score,
                best_match_details=best_details,
                candidate_scores=[
                    {
                        "client_order_id": candidate_client_id,
                        "score": candidate_score,
                        "details": candidate_details,
                    }
                    for candidate_score, candidate_client_id, candidate_details in candidates[:5]
                ],
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        if best_score == second_best_score:
            self.audit.log_event(
                "startup_order_recovery_match_skipped",
                strategy=self.strategy.name,
                reason="ambiguous_candidates",
                best_match_score=best_score,
                candidate_scores=[
                    {
                        "client_order_id": candidate_client_id,
                        "score": candidate_score,
                        "details": candidate_details,
                    }
                    for candidate_score, candidate_client_id, candidate_details in candidates[:5]
                ],
                exchange_order=self._exchange_order_summary(exchange_order),
            )
            return None
        self.audit.log_event(
            "startup_order_recovery_matched_existing",
            strategy=self.strategy.name,
            matched_client_order_id=best_client_id,
            match_score=best_score,
            match_details=best_details,
            candidate_scores=[
                {
                    "client_order_id": candidate_client_id,
                    "score": candidate_score,
                    "details": candidate_details,
                }
                for candidate_score, candidate_client_id, candidate_details in candidates[:5]
            ],
            exchange_order=self._exchange_order_summary(exchange_order),
        )
        return best_client_id

    def _score_recovery_match(self, existing: ManagedOrder, exchange_order: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        exchange_order_id = str(exchange_order.get("orderId") or "")
        exchange_side = self._runtime_side_from_exchange(exchange_order)
        exchange_reduce_only = bool(exchange_order.get("reduceOnly"))
        exchange_order_type = str(exchange_order.get("orderType") or existing.order_type or "")
        exchange_qty = self._safe_float(exchange_order.get("qty"), None)
        exchange_price = self._safe_float(exchange_order.get("price"), None)

        score = 0
        details: dict[str, Any] = {
            "existing_exchange_order_id": existing.exchange_order_id,
            "existing_side": existing.side,
            "exchange_side": exchange_side,
            "existing_reduce_only": existing.reduce_only,
            "exchange_reduce_only": exchange_reduce_only,
            "existing_order_type": existing.order_type,
            "exchange_order_type": exchange_order_type,
            "existing_qty": existing.qty,
            "exchange_qty": exchange_qty,
            "existing_price": existing.price,
            "exchange_price": exchange_price,
        }
        if exchange_order_id and existing.exchange_order_id and exchange_order_id == existing.exchange_order_id:
            score += 1000
            details["matched_exchange_order_id"] = True
        if existing.side == exchange_side:
            score += 30
        else:
            details["rejected_reason"] = "side_mismatch"
            return 0, details
        if existing.reduce_only == exchange_reduce_only:
            score += 20
        else:
            details["rejected_reason"] = "reduce_only_mismatch"
            return 0, details
        if str(existing.order_type or "").lower() == str(exchange_order_type or "").lower():
            score += 15
        if exchange_qty is not None:
            if abs(existing.qty - exchange_qty) <= 1e-9:
                score += 25
            elif existing.qty > 0:
                relative_diff = abs(existing.qty - exchange_qty) / existing.qty
                if relative_diff <= 0.01:
                    score += 15
                elif relative_diff <= 0.05:
                    score += 5
                else:
                    details["rejected_reason"] = "qty_mismatch"
                    details["qty_relative_diff"] = relative_diff
                    return 0, details
        if existing.order_type == "Limit":
            if existing.price is None or exchange_price is None:
                details["rejected_reason"] = "missing_limit_price"
                return 0, details
            if abs(existing.price - exchange_price) <= 1e-9:
                score += 25
            elif existing.price and abs(existing.price - exchange_price) / abs(existing.price) <= 0.001:
                score += 15
            else:
                details["rejected_reason"] = "price_mismatch"
                return 0, details
        details["final_score"] = score
        return score, details

    def _recover_managed_order(
        self,
        client_id: str,
        exchange_order: dict[str, Any],
        existing: ManagedOrder | None,
    ) -> ManagedOrder:
        side = self._runtime_side_from_exchange(exchange_order)
        reduce_only = bool(exchange_order.get("reduceOnly") or (existing.reduce_only if existing else False))
        order_type = str(exchange_order.get("orderType") or (existing.order_type if existing else "Market"))
        qty = float(exchange_order.get("qty") or (existing.qty if existing else 0.0))
        price = self._safe_float(exchange_order.get("price"), existing.price if existing else None)
        recovered_purpose = self._recover_purpose_from_client_id(client_id)
        purpose = (
            existing.purpose
            if existing
            else recovered_purpose
            if recovered_purpose != "RECOVERED_ORDER"
            else self._classify_unknown_order_purpose(exchange_order)
        )
        status = self._normalize_order_status(exchange_order.get("orderStatus"), existing.status if existing else "OPEN")
        filled_qty = float(exchange_order.get("cumExecQty") or (existing.filled_qty if existing else 0.0))
        remaining_qty = max(qty - filled_qty, 0.0)
        metadata = dict(existing.metadata) if existing else {}
        metadata.setdefault("recovered_from_exchange", True)
        metadata["recovery_source"] = "startup"
        metadata.setdefault("position_idx", exchange_order.get("positionIdx"))
        metadata.setdefault("recovered_purpose_classification", purpose)
        return ManagedOrder(
            client_order_id=client_id,
            side=side,
            qty=qty,
            purpose=purpose,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
            exchange_order_id=str(exchange_order.get("orderId") or (existing.exchange_order_id if existing else "")) or None,
            status=status,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            metadata=metadata,
            trace=list(existing.trace) if existing else [],
            created_at=existing.created_at if existing else utcnow(),
            updated_at=utcnow(),
        )

    @staticmethod
    def _managed_order_to_dict(order: ManagedOrder) -> dict[str, Any]:
        return {
            "client_order_id": order.client_order_id,
            "side": order.side,
            "qty": order.qty,
            "purpose": order.purpose,
            "price": order.price,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "exchange_order_id": order.exchange_order_id,
            "status": order.status,
            "filled_qty": order.filled_qty,
            "remaining_qty": order.remaining_qty,
            "metadata": order.metadata,
            "trace": trace_dicts(order.trace),
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
        }

    def _managed_order_from_dict(self, item: dict[str, Any]) -> ManagedOrder | None:
        if not isinstance(item, dict):
            return None
        client_order_id = str(item.get("client_order_id") or "")
        if not client_order_id:
            return None
        created_at_raw = item.get("created_at")
        updated_at_raw = item.get("updated_at")
        return ManagedOrder(
            client_order_id=client_order_id,
            side=str(item.get("side") or "long"),
            qty=float(item.get("qty") or 0.0),
            purpose=str(item.get("purpose") or self._recover_purpose_from_client_id(client_order_id)),
            price=self._safe_float(item.get("price"), None),
            order_type=str(item.get("order_type") or "Market"),
            reduce_only=bool(item.get("reduce_only")),
            exchange_order_id=str(item.get("exchange_order_id") or "") or None,
            status=self._normalize_order_status(item.get("status"), "OPEN"),
            filled_qty=float(item.get("filled_qty") or 0.0),
            remaining_qty=float(item.get("remaining_qty") or 0.0),
            metadata=dict(item.get("metadata") or {}),
            trace=[],
            created_at=self._safe_datetime(created_at_raw) or utcnow(),
            updated_at=self._safe_datetime(updated_at_raw) or utcnow(),
        )

    @staticmethod
    def _normalize_order_status(raw_status: Any, default: str = "OPEN") -> str:
        status = str(raw_status or "").strip().lower()
        if not status:
            return default
        if status in {"new", "open", "untriggered", "triggered", "active"}:
            return "OPEN"
        if status in {"partiallyfilled", "partial", "partially_filled"}:
            return "PARTIAL"
        if status in {"filled", "done"}:
            return "FILLED"
        if status in {"cancelled", "canceled", "deactivated", "partiallyfilledcanceled", "partially_filled_canceled"}:
            return "CANCELED"
        if status in {"rejected", "reject"}:
            return "REJECTED"
        if status in {"pending_submit", "pending"}:
            return "PENDING_SUBMIT"
        return default

    def _finalize_managed_order(self, client_id: str, managed_order: ManagedOrder) -> None:
        self.runtime_state.active_orders.pop(client_id, None)
        self.runtime_state.finalized_orders[client_id] = managed_order
        self._prune_finalized_orders()
        self.audit.log_event(
            "order_finalized",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=managed_order.exchange_order_id,
            status=managed_order.status,
            filled_qty=managed_order.filled_qty,
            remaining_qty=managed_order.remaining_qty,
        )

    def _get_managed_order(self, client_id: str | None) -> ManagedOrder | None:
        if not client_id:
            return None
        return self.runtime_state.active_orders.get(client_id) or self.runtime_state.finalized_orders.get(client_id)

    def _prune_finalized_orders(self, max_orders: int = 200) -> None:
        finalized_orders = self.runtime_state.finalized_orders
        if len(finalized_orders) <= max_orders:
            return
        stale_orders = sorted(finalized_orders.values(), key=lambda order: order.updated_at)
        for order in stale_orders[: len(finalized_orders) - max_orders]:
            finalized_orders.pop(order.client_order_id, None)
            if order.exchange_order_id:
                mapped_client_id = self.runtime_state.exchange_to_client_id.get(order.exchange_order_id)
                if mapped_client_id == order.client_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)

    @staticmethod
    def _runtime_side_from_exchange(order: dict[str, Any]) -> str:
        position_idx = str(order.get("positionIdx") or "").strip()
        if position_idx == "1":
            return "long"
        if position_idx == "2":
            return "short"
        side = str(order.get("side") or "").lower()
        reduce_only = bool(order.get("reduceOnly"))
        if reduce_only:
            return "short" if side == "buy" else "long"
        return "long" if side in {"buy", "long"} else "short"

    def _recover_purpose_from_client_id(self, client_id: str) -> str:
        prefix = f"{self.strategy.name}-"
        if client_id.startswith(prefix):
            remainder = client_id[len(prefix):]
            if "-" in remainder:
                return remainder.rsplit("-", 1)[0].upper()
        recovered_prefix = f"recovered-{self.strategy.name}-"
        if client_id.startswith(recovered_prefix):
            remainder = client_id[len(recovered_prefix):]
            if remainder.startswith("-") and "--" in remainder[1:]:
                purpose_part = remainder[1:].split("--", 1)[0]
                if purpose_part:
                    return purpose_part.upper()
            if "-" in remainder:
                return remainder.rsplit("-", 1)[0].upper()
        return "RECOVERED_ORDER"

    def _classify_unknown_order_purpose(self, exchange_order: dict[str, Any]) -> str:
        side = self._runtime_side_from_exchange(exchange_order)
        reduce_only = bool(exchange_order.get("reduceOnly"))
        order_type = str(exchange_order.get("orderType") or "").lower()
        strategy_name = self.strategy.name

        if strategy_name == "dynamic_breakeven_hedge":
            if reduce_only and side == "short":
                return "DYN_SHORT_COMPENSATE" if order_type == "limit" else "DYN_SHORT_REDUCE"
            if reduce_only and side == "long":
                return "DYN_LONG_REDUCE"
        if strategy_name == "basket_exit_hedge":
            if reduce_only and side == "long":
                return "BASKET_EXIT_LONG"
            if reduce_only and side == "short":
                return "BASKET_EXIT_SHORT"

        if reduce_only:
            return f"RECOVERED_{side.upper()}_REDUCE"
        return f"RECOVERED_{side.upper()}_ENTRY"

    def _classification_inputs(self, exchange_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy.name,
            "position_idx": exchange_order.get("positionIdx"),
            "exchange_side": exchange_order.get("side"),
            "runtime_side": self._runtime_side_from_exchange(exchange_order),
            "reduce_only": bool(exchange_order.get("reduceOnly")),
            "order_type": exchange_order.get("orderType"),
            "qty": self._safe_float(exchange_order.get("qty"), None),
            "price": self._safe_float(exchange_order.get("price"), None),
        }

    def _exchange_order_summary(self, exchange_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(exchange_order.get("orderId") or ""),
            "order_link_id": str(exchange_order.get("orderLinkId") or ""),
            "status": str(exchange_order.get("orderStatus") or ""),
            "side": str(exchange_order.get("side") or ""),
            "position_idx": exchange_order.get("positionIdx"),
            "reduce_only": bool(exchange_order.get("reduceOnly")),
            "order_type": str(exchange_order.get("orderType") or ""),
            "qty": self._safe_float(exchange_order.get("qty"), None),
            "price": self._safe_float(exchange_order.get("price"), None),
            "cum_exec_qty": self._safe_float(exchange_order.get("cumExecQty"), None),
        }

    def _history_order_summary(self, history_order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(history_order.get("orderId") or ""),
            "order_link_id": str(history_order.get("orderLinkId") or ""),
            "status": str(history_order.get("orderStatus") or ""),
            "price": self._safe_float(history_order.get("price"), None),
            "avg_price": self._safe_float(history_order.get("avgPrice"), None),
            "cum_exec_qty": self._safe_float(history_order.get("cumExecQty"), None),
            "cum_exec_value": self._safe_float(history_order.get("cumExecValue"), None),
        }

    def _managed_order_summary(self, managed_order: ManagedOrder) -> dict[str, Any]:
        return {
            "client_order_id": managed_order.client_order_id,
            "exchange_order_id": managed_order.exchange_order_id,
            "side": managed_order.side,
            "purpose": managed_order.purpose,
            "order_type": managed_order.order_type,
            "reduce_only": managed_order.reduce_only,
            "status": managed_order.status,
            "qty": managed_order.qty,
            "filled_qty": managed_order.filled_qty,
            "remaining_qty": managed_order.remaining_qty,
            "price": managed_order.price,
        }

    @staticmethod
    def _safe_float(value: Any, default: float | None) -> float | None:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_datetime(value: Any) -> Any:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None


def configure_runtime_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
