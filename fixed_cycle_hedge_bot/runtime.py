from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime
import time
from logging.handlers import RotatingFileHandler
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
from .trailing_fallback import TrailingFallbackManager


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
            order_manager=self.order_manager,
            refresh_snapshot=self.refresh_snapshot,
            cancel_open_orders_by_purpose=self.cancel_open_orders_by_purpose,
        )
        self._stop_event = threading.Event()
        self._price_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._max_leverage_ready_symbols: set[tuple[str, str]] = set()
        self._trailing_fallback = TrailingFallbackManager()

    def bootstrap(self) -> HedgeSnapshot:
        self._load_strategy_state()
        if self.config.ensure_exchange_ready:
            self.order_manager.ensure_hedge_mode(self.config.symbol, self.config.category)
            self.order_manager.ensure_max_leverage(self.config.symbol, self.config.category)
        self._recover_active_orders_from_exchange()
        self._ensure_max_leverage_before_trading()
        snapshot = self.refresh_snapshot("startup")
        state = self.runtime_state.strategy_state
        allow_start = True
        startup_flat_confirmed = False
        if (
            snapshot.long_qty <= 0.0
            and snapshot.short_qty <= 0.0
            and not snapshot.active_orders
        ):
            snapshot, startup_flat_confirmed = self._confirm_startup_flat_snapshot(snapshot)
        if startup_flat_confirmed:
            startup_state_cleaned = self.strategy.prepare_for_clean_startup(
                snapshot,
                self.runtime_state,
                self.context,
            )
            if startup_state_cleaned:
                self.logger.info(
                    "startup_state_cleaned_for_fresh_entry %s",
                    {
                        "symbol": self.config.symbol,
                        "strategy": self.strategy.name,
                        "snapshot_long_qty": snapshot.long_qty,
                        "snapshot_short_qty": snapshot.short_qty,
                    },
                )
            conflict, conflict_details = self._startup_state_conflict()
            if conflict:
                self.logger.warning(
                    "startup_state_indicates_existing_context %s",
                    conflict_details,
                )
                block_payload = {
                    "state_file": self.config.strategy_state_file or "<none>",
                    "active_orders": [order.client_order_id for order in self.runtime_state.active_orders.values()],
                    "conflict_details": conflict_details,
                    "snapshot_long_qty": snapshot.long_qty,
                    "snapshot_short_qty": snapshot.short_qty,
                }
                self.logger.warning(
                    "startup_fresh_entry_blocked_by_state %s",
                    block_payload,
                )
                allow_start = False
        self.audit.log_event(
            "runtime_bootstrap",
            strategy=self.strategy.name,
            symbol=self.config.symbol,
            category=self.config.category,
            snapshot=snapshot,
        )
        if allow_start:
            self._dispatch(
                "start",
                self.strategy.on_start(snapshot, self.runtime_state, self.context),
                snapshot,
            )
        self._save_strategy_state()
        return snapshot

    def start(self) -> None:
        self._start_websocket()
        self.bootstrap()
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
        with self._lock:
            snapshot = self.refresh_snapshot("tick")
            if self._trailing_fallback.active:
                self._trailing_fallback.update(snapshot.current_price)
                if self._trailing_fallback.should_submit():
                    fallback_intent = StrategyIntent(
                        purpose="TRAILING_SHORT_REDUCE",
                        side="short",
                        position_idx=2,
                        qty=self._trailing_fallback.qty,
                        order_type="Market",
                        reduce_only=True,
                    )

                    self.audit.log_event(
                        "trailing_fallback_triggered",
                        strategy=self.strategy.name,
                        intent=fallback_intent,
                        trailing_lowest_price=self._trailing_fallback.state.lowest_price,
                        trailing_max_rebound=self._trailing_fallback.state.max_rebound_price,
                        trailing_dist=self._trailing_fallback.state.trailing_dist,
                        snapshot_price=snapshot.current_price,
                    )
                    submitted_client_id = self.submit_intent(fallback_intent, snapshot, source="trailing_fallback")
                    if submitted_client_id:
                        self._trailing_fallback.mark_submitted()
                        self._trailing_fallback.reset()
                        self.runtime_state.strategy_state.pop("trailing_active", None)
            self._dispatch("tick", self.strategy.on_tick(snapshot, self.runtime_state, self.context), snapshot)
            return snapshot

    def reconcile_once(self) -> HedgeSnapshot:
        with self._lock:
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
        positions = self._fetch_exchange_position_mapping(source)
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

    def _fetch_exchange_position_mapping(self, source: str) -> dict[str, float]:
        symbol = self.config.symbol
        category = self.config.category
        max_attempts = 5 if source == "startup" else 1
        attempt = 0
        long_qty = 0.0
        short_qty = 0.0
        long_avg = 0.0
        short_avg = 0.0
        rows: list[dict[str, Any]] = []
        while attempt < max_attempts:
            attempt += 1
            fetched = self.order_manager.fetch_positions(symbol, category) or []
            rows = fetched
            rows_count = len(rows)
            rows_preview: list[dict[str, Any]] = []
            for row in rows[:5]:
                rows_preview.append(
                    {
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "size": row.get("size") or row.get("positionQty"),
                        "qty": row.get("qty"),
                        "positionIdx": row.get("positionIdx"),
                    }
                )
            log_extra_base = {
                "reason": source,
                "source": source,
                "attempt": attempt,
                "category": category,
                "symbol": symbol,
            }
            fetch_started_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
            }
            self.logger.debug(
                "bootstrap_positions_fetch_started %s",
                fetch_started_payload,
            )
            raw_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
            }
            self.logger.debug(
                "bootstrap_positions_raw %s",
                raw_payload,
            )
            parsed_long_qty = 0.0
            parsed_short_qty = 0.0
            parsed_long_avg = 0.0
            parsed_short_avg = 0.0
            for position in rows:
                side = str(position.get("side") or position.get("positionSide") or "").lower()
                size = float(position.get("size") or position.get("positionQty") or 0.0)
                avg = float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                if side in {"buy", "long"}:
                    parsed_long_qty = size
                    parsed_long_avg = avg
                elif side in {"sell", "short"}:
                    parsed_short_qty = size
                    parsed_short_avg = avg
            long_qty = parsed_long_qty
            short_qty = parsed_short_qty
            long_avg = parsed_long_avg
            short_avg = parsed_short_avg
            parsed_payload = {
                **log_extra_base,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
                "parsed_long_qty": long_qty,
                "parsed_short_qty": short_qty,
                "parsed_long_avg": long_avg,
                "parsed_short_avg": short_avg,
            }
            self.logger.debug(
                "bootstrap_positions_parsed %s",
                parsed_payload,
            )
            if long_qty > 0.0 or short_qty > 0.0:
                break
            if attempt < max_attempts:
                retry_payload = {
                    **log_extra_base,
                    "rows_count": rows_count,
                    "rows_preview": rows_preview,
                    "parsed_long_qty": long_qty,
                    "parsed_short_qty": short_qty,
                }
                self.logger.debug(
                    "bootstrap_positions_empty_retry %s",
                    retry_payload,
                )
                time.sleep(0.3)
        if long_qty <= 0.0 and short_qty <= 0.0:
            rows_count = len(rows)
            rows_preview = []
            for row in rows[:5]:
                rows_preview.append(
                    {
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "size": row.get("size") or row.get("positionQty"),
                        "qty": row.get("qty"),
                        "positionIdx": row.get("positionIdx"),
                    }
                )
            final_payload = {
                "reason": source,
                "source": source,
                "attempt": attempt,
                "category": category,
                "symbol": symbol,
                "rows_count": rows_count,
                "rows_preview": rows_preview,
                "parsed_long_qty": long_qty,
                "parsed_short_qty": short_qty,
                "parsed_long_avg": long_avg,
                "parsed_short_avg": short_avg,
            }
            self.logger.debug(
                "bootstrap_positions_final_flat %s",
                final_payload,
            )
        self.position_manager.sync_positions(long_qty, long_avg, short_qty, short_avg)
        return {
            "long_qty": self.position_manager.long_size,
            "short_qty": self.position_manager.short_size,
            "long_avg": self.position_manager.long_avg,
            "short_avg": self.position_manager.short_avg,
        }

    def _confirm_startup_flat_snapshot(
        self,
        initial_snapshot: HedgeSnapshot,
    ) -> tuple[HedgeSnapshot, bool]:
        attempt_payload = {
            "reason": "startup",
            "symbol": self.config.symbol,
            "initial_long_qty": initial_snapshot.long_qty,
            "initial_short_qty": initial_snapshot.short_qty,
            "active_orders": [order.client_order_id for order in initial_snapshot.active_orders],
        }
        self.logger.info(
            "startup_flat_confirm_attempt_1 %s",
            attempt_payload,
        )
        time.sleep(1.0)
        confirm_snapshot = self.refresh_snapshot("startup_confirm")
        confirm_payload = {
            "reason": "startup",
            "symbol": self.config.symbol,
            "long_qty": confirm_snapshot.long_qty,
            "short_qty": confirm_snapshot.short_qty,
            "active_orders": [order.client_order_id for order in confirm_snapshot.active_orders],
        }
        self.logger.info(
            "startup_flat_confirm_attempt_2 %s",
            confirm_payload,
        )
        confirmed = (
            confirm_snapshot.long_qty <= 0.0
            and confirm_snapshot.short_qty <= 0.0
            and not confirm_snapshot.active_orders
        )
        if confirmed:
            self.logger.info(
                "startup_flat_confirmed_allow_fresh_entry %s",
                confirm_payload,
            )
        else:
            confirm_payload["reason"] = "non_flat"
            self.logger.info(
                "startup_flat_not_confirmed_block_fresh_entry %s",
                confirm_payload,
            )
        return confirm_snapshot, confirmed

    def _startup_state_conflict(self) -> tuple[bool, dict[str, Any]]:
        state = self.runtime_state.strategy_state
        cycle_state = state.get("cycle_state") or {}
        conflict = bool(
            state.get("initial_entry_confirmed")
            or state.get("initial_entry_submitted")
            or int(state.get("cycle_completed_count") or 0) > 0
            or cycle_state.get("trade_active")
            or cycle_state.get("long_add_pending")
            or cycle_state.get("cycle_waiting_for_short_tp")
            or int(cycle_state.get("short_tp_pending_cycle") or 0) > 0
        )
        details = {
            "initial_entry_confirmed": state.get("initial_entry_confirmed"),
            "initial_entry_submitted": state.get("initial_entry_submitted"),
            "cycle_completed_count": state.get("cycle_completed_count"),
            "trade_active": cycle_state.get("trade_active"),
            "long_add_pending": cycle_state.get("long_add_pending"),
            "cycle_waiting_for_short_tp": cycle_state.get("cycle_waiting_for_short_tp"),
            "short_tp_pending_cycle": cycle_state.get("short_tp_pending_cycle"),
            "pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
        }
        return conflict, details

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
        managed_order = self.runtime_state.active_orders.get(client_id)
        if not managed_order:
            return
        previous_status = managed_order.status
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
        normalized_status = managed_order.status
        if normalized_status in {"CANCELED", "REJECTED"}:
            self.audit.log_event(
                "ws_order_terminal_diagnostics",
                strategy=self.strategy.name,
                symbol=self.config.symbol,
                category=self.config.category,
                client_order_id=client_id,
                exchange_order_id=order_id,
                purpose=managed_order.purpose,
                side=managed_order.side,
                managed_status_before=previous_status,
                normalized_status=normalized_status,
                raw_order_status=payload.get("orderStatus"),
                cancel_type=payload.get("cancelType"),
                reject_reason=payload.get("rejectReason"),
                cancel_reason=payload.get("cancelReason"),
                stop_order_type=payload.get("stopOrderType"),
                order_type=payload.get("orderType"),
                side_raw=payload.get("side"),
                position_idx=payload.get("positionIdx"),
                reduce_only=payload.get("reduceOnly"),
                close_on_trigger=payload.get("closeOnTrigger"),
                trigger_price=payload.get("triggerPrice"),
                trigger_by=payload.get("triggerBy"),
                trigger_direction=payload.get("triggerDirection"),
                qty=payload.get("qty"),
                cum_exec_qty=payload.get("cumExecQty"),
                leaves_qty=payload.get("leavesQty"),
                price=payload.get("price"),
                avg_price=payload.get("avgPrice"),
                created_time=payload.get("createdTime"),
                updated_time=payload.get("updatedTime"),
                full_payload=payload,
            )
        if normalized_status in {"CANCELED", "REJECTED", "FILLED"}:
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
    ) -> None:
        client_id = (
            self.runtime_state.exchange_to_client_id.get(exchange_order_id)
            or (order_link_id if order_link_id in self.runtime_state.active_orders else None)
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
        self._ingest_fill_event(
            exchange_order_id=exchange_order_id,
            client_id=client_id,
            qty=qty,
            price=price,
            exec_id=exec_id,
            cumulative_qty=cumulative_qty,
            source="websocket",
        )

    def _dispatch(self, source: str, intents: list[StrategyIntent], snapshot: HedgeSnapshot) -> None:
        strategy_state = self.runtime_state.strategy_state
        if not intents:
            self.audit.log_event(
                "strategy_noop",
                strategy=self.strategy.name,
                source=source,
                snapshot=snapshot,
                initial_entry_confirmed=bool(strategy_state.get("initial_entry_confirmed")),
                current_long_cycle_index=int(strategy_state.get("current_long_cycle_index") or 0),
                current_short_cycle_index=int(strategy_state.get("current_short_cycle_index") or 0),
                current_effective_cycle=int(strategy_state.get("current_effective_cycle") or 0),
                cycle_waiting_for_short_tp=bool(strategy_state.get("cycle_waiting_for_short_tp")),
                pending_long_cycle_index=int(strategy_state.get("pending_long_cycle_index") or 0),
            )
            return
        entry_purposes = {self.strategy.LONG_ENTRY_PURPOSE, self.strategy.SHORT_ENTRY_PURPOSE}
        entry_intents = [intent for intent in intents if intent.purpose in entry_purposes]
        if entry_intents:
            self.audit.log_event(
                "strategy_initial_entry_dispatched",
                strategy=self.strategy.name,
                intent_count=len(entry_intents),
                snapshot=snapshot,
                source=source,
            )
        self.logger.debug(
            "strategy_intents_handoff %s",
            {
                "source": source,
                "intent_count": len(intents),
                "purposes": [intent.purpose for intent in intents],
                "sides": [intent.side for intent in intents],
                "reduce_only_flags": [intent.reduce_only for intent in intents],
                "trigger_prices": [intent.trigger_price for intent in intents],
                "prices": [intent.price for intent in intents],
                "initial_entry_confirmed": bool(strategy_state.get("initial_entry_confirmed")),
                "current_long_cycle_index": int(strategy_state.get("current_long_cycle_index") or 0),
                "current_short_cycle_index": int(strategy_state.get("current_short_cycle_index") or 0),
                "current_effective_cycle": int(strategy_state.get("current_effective_cycle") or 0),
                "cycle_waiting_for_short_tp": bool(strategy_state.get("cycle_waiting_for_short_tp")),
                "pending_long_cycle_index": int(strategy_state.get("pending_long_cycle_index") or 0),
            },
        )
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
        self._ensure_max_leverage_before_trading()
        equivalent_order, reason, candidate_id, existing_trigger, existing_qty = self._find_equivalent_open_order(intent)
        decision = "reuse" if reason.startswith("match") else "replace"
        self.audit.log_event(
            "intent_equivalence_check",
            strategy=self.strategy.name,
            purpose=intent.purpose,
            side=intent.side,
            candidate_client_order_id=candidate_id,
            result=decision,
            reject_reason=reason,
            existing_trigger_price=existing_trigger,
            existing_qty=existing_qty,
            new_trigger_price=intent.trigger_price,
            new_qty=intent.qty,
        )
        self.audit.log_event(
            "intent_replace_decision",
            strategy=self.strategy.name,
            purpose=intent.purpose,
            side=intent.side,
            decision=decision,
            reason=reason,
        )
        replace_purposes_raw = intent.metadata.get("replace_open_purpose")
        if (
            reason == "no_candidate"
            and replace_purposes_raw
            and not self.runtime_state.active_orders
            and not snapshot.active_orders
        ):
            skip_reason = (
                "active_orders_empty_race_condition"
                if snapshot.active_orders
                else "no_runtime_orders"
            )
            self.audit.log_event(
                "intent_skip_due_to_empty_snapshot",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                reason=skip_reason,
            )
            return None
        if equivalent_order:
            self.audit.log_event(
                "intent_reuse_existing_order",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                client_order_id=equivalent_order.client_order_id,
                exchange_order_id=equivalent_order.exchange_order_id,
            )
            return equivalent_order.client_order_id

        final_symbol_payload = {"symbol": self.config.symbol}
        if hasattr(self.strategy, "config"):
            final_symbol_payload["strategy_symbol"] = getattr(self.strategy.config, "symbol", None)
        self.logger.info("final_symbol_used", final_symbol_payload)
        self.audit.log_event(
            "intent_submit_started",
            strategy=self.strategy.name,
            purpose=intent.purpose,
            side=intent.side,
            qty=intent.qty,
            order_type=intent.order_type,
            trigger_price=intent.trigger_price,
            reduce_only=intent.reduce_only,
            position_idx=intent.position_idx,
        )
        replace_purposes_raw = intent.metadata.get("replace_open_purpose")
        if replace_purposes_raw:
            replace_purposes = [replace_purposes_raw] if isinstance(replace_purposes_raw, str) else list(replace_purposes_raw)
            replace_context = (
                {
                    "reason": reason,
                    "existing_trigger_price": existing_trigger,
                    "new_trigger_price": intent.trigger_price,
                    "existing_qty": existing_qty,
                    "new_qty": intent.qty,
                }
                if reason != "match"
                else None
            )
            self._cancel_open_orders_by_purpose_internal(replace_purposes, replace_context)
        client_id = f"{self.strategy.name}-{intent.purpose.lower()}-{uuid4().hex[:10]}"
        current_price = snapshot.current_price
        strategy_state = self.runtime_state.strategy_state
        fallback_context = self._build_long_add_market_fallback_context(
            intent=intent,
            snapshot=snapshot,
            normalized_qty=normalized_qty,
        )
        should_force_fallback = bool(fallback_context and fallback_context.get("should_fallback"))
        if intent.trigger_price is not None:
            trigger_price = intent.trigger_price
            invalid_reason = None
            if current_price is None or current_price <= 0:
                self.audit.log_event(
                    "intent_trigger_invalid",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    direction=intent.trigger_direction,
                    reason="missing_current_price",
                )
                return None
            if (
                intent.position_idx == 2
                and intent.trigger_direction == 2
                and current_price <= trigger_price
                and intent.metadata.get("cycle_role") == "short_reduce"
            ):
                if self._trailing_fallback.active:
                    return None
                self._trailing_fallback.activate(
                    purpose=intent.purpose,
                    position_idx=intent.position_idx,
                    qty=intent.qty,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    trailing_dist=float(self.strategy.config.trailing_stop_dist),
                )
                strategy_state["trailing_active"] = intent.purpose
                return None
            if intent.trigger_direction == 2 and trigger_price >= current_price:
                invalid_reason = "falling_trigger_not_below_market"
            elif intent.trigger_direction == 1 and trigger_price <= current_price:
                invalid_reason = "rising_trigger_not_above_market"
            if invalid_reason is not None and not should_force_fallback:
                self.audit.log_event(
                    "intent_trigger_invalid",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=trigger_price,
                    current_price=current_price,
                    direction=intent.trigger_direction,
                    reason=invalid_reason,
                )
                return None
        submit_qty = normalized_qty
        fallback_reason: str | None = None
        force_market_fallback = False
        if fallback_context:
            cycle_role = str(intent.metadata.get("cycle_role") or "")
            self.audit.log_event(
                "pre_long_add_trigger_validation",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                trigger_price=fallback_context["trigger_price"],
                current_price=fallback_context["current_price"],
                qty=normalized_qty,
                clamped_qty=fallback_context["fallback_qty"],
                available_long_qty=fallback_context["available_long_qty"],
                should_fallback=fallback_context["should_fallback"],
                cycle_role=cycle_role,
            )
            if fallback_context["should_fallback"]:
                fallback_reason = "stale_trigger"
                force_market_fallback = True
                submit_qty = fallback_context["fallback_qty"]
                self.audit.log_event(
                    "stale_long_add_trigger_market_fallback",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=fallback_context["trigger_price"],
                    current_price=fallback_context["current_price"],
                    fallback_qty=fallback_context["fallback_qty"],
                    available_long_qty=fallback_context["available_long_qty"],
                    cycle_role=cycle_role,
                )
        submit_notional = submit_qty * submit_price
        managed_order = ManagedOrder(
            client_order_id=client_id,
            side=intent.side,
            qty=submit_qty,
            purpose=intent.purpose,
            price=intent.price,
            order_type=intent.order_type,
            reduce_only=intent.reduce_only,
            remaining_qty=submit_qty,
            metadata={
                **dict(intent.metadata),
                "source": source,
                "entry_price": snapshot.long_avg if intent.side == "long" else snapshot.short_avg,
                "snapshot_price": snapshot.current_price,
                "trigger_price": intent.trigger_price,
                "trigger_direction": intent.trigger_direction,
                "trigger_by": intent.trigger_by,
                "close_on_trigger": intent.close_on_trigger,
                "position_idx": intent.position_idx,
                "order_filter": intent.order_filter,
                "market_fallback": force_market_fallback,
                "market_fallback_reason": fallback_reason,
            },
            trace=list(intent.trace),
        )
        try:
            response = self._submit_to_exchange(
                managed_order,
                snapshot,
                force_market_fallback=force_market_fallback,
            )
        except Exception as exc:
            self.audit.log_event(
                "order_rejected",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                order_link_id=managed_order.client_order_id,
                status="rejected",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                reason="exception",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        if not response:
            self.audit.log_event(
                "order_rejected",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                order_link_id=managed_order.client_order_id,
                status="rejected",
                error_code="no_response",
                error_message="exchange returned no response",
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                reason="no_response",
                error_code="no_response",
                error_message="exchange returned no response",
            )
            self.audit.log_event(
                "intent_submit_failed",
                strategy=self.strategy.name,
                source=source,
                client_order_id=client_id,
                intent=intent,
                traces=trace_dicts(intent.trace),
            )
            error_info = getattr(self.order_manager, "last_post_error", None)
            if force_market_fallback:
                self.audit.log_event(
                    "long_add_market_fallback_failed",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    client_order_id=client_id,
                    qty=managed_order.qty,
                    reason=fallback_reason,
                    error_code=self._long_add_error_code(error_info),
                    error_message=self._long_add_error_message(error_info),
                )
                return None
            if fallback_context and self._should_trigger_rejection_market_fallback(error_info):
                self.audit.log_event(
                    "long_add_conditional_rejected_market_fallback",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    trigger_price=fallback_context["trigger_price"],
                    current_price=fallback_context["current_price"],
                    ret_code=self._long_add_error_code(error_info),
                    ret_msg=self._long_add_error_message(error_info),
                )
                fallback_reason = "conditional_rejection"
                force_market_fallback = True
                managed_order.qty = fallback_context["fallback_qty"]
                managed_order.remaining_qty = managed_order.qty
                managed_order.metadata["market_fallback"] = True
                managed_order.metadata["market_fallback_reason"] = fallback_reason
                response = self._submit_to_exchange(
                    managed_order,
                    snapshot,
                    force_market_fallback=True,
                )
                if not response:
                    error_info = getattr(self.order_manager, "last_post_error", error_info)
                    self.audit.log_event(
                        "long_add_market_fallback_failed",
                        strategy=self.strategy.name,
                        purpose=intent.purpose,
                        side=intent.side,
                        client_order_id=client_id,
                        qty=managed_order.qty,
                        reason=fallback_reason,
                        error_code=self._long_add_error_code(error_info),
                        error_message=self._long_add_error_message(error_info),
                    )
                    return None
            else:
                return None
        exchange_order_id = ((response.get("result") or {}).get("orderId")) if isinstance(response, dict) else None
        response_code = None
        if isinstance(response, dict):
            for key in ("retCode", "ret_code", "code"):
                if key in response:
                    response_code = response[key]
                    break
        self.audit.log_event(
            "order_submitted",
            strategy=self.strategy.name,
            purpose=managed_order.purpose,
            side=managed_order.side,
            order_type=managed_order.order_type,
            qty=managed_order.qty,
            price=managed_order.price,
            order_link_id=managed_order.client_order_id,
            exchange_order_id=exchange_order_id,
            status="submitted",
            response_code=response_code,
        )
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
            normalized_qty=submit_qty,
            submit_notional=submit_notional,
            traces=trace_dicts(intent.trace),
        )
        if fallback_reason:
            fallback_event = {
                "strategy": self.strategy.name,
                "purpose": intent.purpose,
                "side": intent.side,
                "client_order_id": client_id,
                "qty": managed_order.qty,
                "reason": fallback_reason,
            }
            if fallback_context:
                fallback_event.update(
                    {
                        "trigger_price": fallback_context["trigger_price"],
                        "current_price": fallback_context["current_price"],
                        "available_long_qty": fallback_context["available_long_qty"],
                        "fallback_qty": fallback_context["fallback_qty"],
                    }
                )
            self.audit.log_event("long_add_market_fallback_submitted", **fallback_event)
        self._save_strategy_state()
        return client_id

    def _build_long_add_market_fallback_context(
        self,
        *,
        intent: StrategyIntent,
        snapshot: HedgeSnapshot,
        normalized_qty: float,
    ) -> dict[str, Any] | None:
        purpose = str(intent.purpose or "").upper()
        if not ((purpose.startswith("CYCLE_") and purpose.endswith("_LONG_ADD")) or purpose == "LONG_REDUCE"):
            return None
        trigger_price = self._safe_float(intent.trigger_price, None)
        if trigger_price is None:
            trigger_price = self._safe_float(intent.metadata.get("trigger_price"), None)
        if trigger_price is None:
            return None
        current_price = float(snapshot.current_price or 0.0)
        if current_price <= 0:
            return None
        available_long_qty = float(snapshot.long_qty or 0.0)
        fallback_qty = normalized_qty
        if available_long_qty > 0 and fallback_qty > available_long_qty:
            fallback_qty = available_long_qty
        if fallback_qty <= 0:
            return None
        return {
            "trigger_price": trigger_price,
            "current_price": current_price,
            "available_long_qty": available_long_qty,
            "fallback_qty": fallback_qty,
            "should_fallback": current_price <= trigger_price,
        }

    def _should_trigger_rejection_market_fallback(
        self,
        error_info: dict[str, Any] | None,
    ) -> bool:
        if not error_info:
            return False
        code = str(
            error_info.get("retCode")
            or error_info.get("ret_code")
            or error_info.get("code")
            or ""
        )
        msg = str(
            error_info.get("retMsg")
            or error_info.get("ret_msg")
            or error_info.get("message")
            or error_info.get("error")
            or ""
        )
        if code == "110093":
            return True
        return "expect falling" in msg.lower()

    @staticmethod
    def _long_add_error_code(error_info: dict[str, Any] | None) -> str | None:
        if not error_info:
            return None
        return (
            error_info.get("retCode")
            or error_info.get("ret_code")
            or error_info.get("code")
        )

    @staticmethod
    def _long_add_error_message(error_info: dict[str, Any] | None) -> str | None:
        if not error_info:
            return None
        return (
            error_info.get("retMsg")
            or error_info.get("ret_msg")
            or error_info.get("message")
            or error_info.get("error")
            or str(error_info)
        )

    def cancel_open_orders_by_purpose(self, purposes: list[str]) -> None:
        with self._lock:
            self._cancel_open_orders_by_purpose_internal(purposes)


    def _find_equivalent_open_order(
        self, intent: StrategyIntent
    ) -> tuple[
        ManagedOrder | None,
        str,
        str | None,
        float | None,
        float | None,
    ]:
        tick_size = float(self.strategy.config.price_tick_size or 0.0) or 1e-8
        price_tol = tick_size * 3
        qty_tol = (float(self.strategy.config.qty_step or 0.0) or 1e-9) * 2
        target_trigger = intent.trigger_price or 0.0
        is_long_add = "LONG_ADD" in str(intent.purpose)
        is_exit_order = intent.purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        long_add_qty_tol = max(qty_tol, 50.0)
        exit_trigger_tol = max(price_tol, tick_size * 2)
        last_candidate_id = None
        last_trigger = None
        last_qty = None
        last_reason = "no_candidate"
        candidate_count = 0
        rejected_count = 0
        for order in self.runtime_state.active_orders.values():
            if order.status not in {"OPEN", "PARTIAL"}:
                continue
            if order.purpose != intent.purpose:
                continue
            if order.side != intent.side or order.order_type != intent.order_type:
                continue
            if order.reduce_only != intent.reduce_only:
                continue
            existing_idx = int(
                order.metadata.get("position_idx") or (1 if order.side == "long" else 2)
            )
            intent_idx = int(intent.position_idx or (1 if intent.side == "long" else 2))
            candidate_count += 1
            if existing_idx != intent_idx:
                last_candidate_id = order.client_order_id
                last_reason = "position_idx_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.position_idx,
                    actual=order.metadata.get("position_idx"),
                )
                continue
            if str(order.metadata.get("trigger_direction") or "") != str(intent.trigger_direction or ""):
                last_candidate_id = order.client_order_id
                last_reason = "trigger_direction_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.trigger_direction,
                    actual=order.metadata.get("trigger_direction"),
                )
                continue
            if str(order.metadata.get("trigger_by") or "") != str(intent.trigger_by or ""):
                last_candidate_id = order.client_order_id
                last_reason = "trigger_by_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.trigger_by,
                    actual=order.metadata.get("trigger_by"),
                )
                continue
            if order.metadata.get("close_on_trigger") != intent.close_on_trigger:
                last_candidate_id = order.client_order_id
                last_reason = "close_on_trigger_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.close_on_trigger,
                    actual=order.metadata.get("close_on_trigger"),
                )
                continue
            existing_filter = str(order.metadata.get("order_filter") or "")
            intent_filter = str(intent.order_filter or "")
            if existing_filter != intent_filter:
                last_candidate_id = order.client_order_id
                last_reason = "order_filter_mismatch"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent_filter,
                    actual=existing_filter,
                )
                continue
            existing_trigger = self._safe_float(order.metadata.get("trigger_price"), None)
            existing_qty = order.qty
            last_candidate_id = order.client_order_id
            last_trigger = existing_trigger
            last_qty = existing_qty
            self.audit.log_event(
                "intent_equivalence_candidate",
                strategy=self.strategy.name,
                purpose=intent.purpose,
                side=intent.side,
                candidate_client_order_id=order.client_order_id,
                candidate_exchange_order_id=order.exchange_order_id,
                result="match" if existing_trigger is not None and abs(existing_trigger - target_trigger) <= price_tol and abs(existing_qty - intent.qty) <= qty_tol else "reject",
                reject_reason=last_reason,
                existing_trigger_price=existing_trigger,
                new_trigger_price=target_trigger,
                existing_qty=existing_qty,
                new_qty=intent.qty,
            )
            if is_exit_order:
                trigger_limit = exit_trigger_tol
            else:
                trigger_limit = price_tol
            if existing_trigger is None or abs(existing_trigger - target_trigger) > trigger_limit:
                last_reason = "trigger_diff"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=target_trigger,
                    actual=existing_trigger,
                )
                continue
            if is_long_add:
                qty_limit = long_add_qty_tol
            else:
                qty_limit = qty_tol
            if abs(existing_qty - intent.qty) > qty_limit:
                last_reason = "qty_diff"
                rejected_count += 1
                self.audit.log_event(
                    "intent_equivalence_reject",
                    strategy=self.strategy.name,
                    purpose=intent.purpose,
                    side=intent.side,
                    candidate_client_order_id=order.client_order_id,
                    reason=last_reason,
                    expected=intent.qty,
                    actual=existing_qty,
                )
                continue
            return order, "match", last_candidate_id, existing_trigger, existing_qty
        final_reason = last_reason if candidate_count > 0 else "no_candidate"
        self.audit.log_event(
            "intent_equivalence_summary",
            strategy=self.strategy.name,
            purpose=intent.purpose,
            total_candidates=candidate_count,
            rejected_candidates=rejected_count,
            final_reason=final_reason,
        )
        return None, final_reason, last_candidate_id, last_trigger, last_qty

    def _ensure_max_leverage_before_trading(self) -> None:
        cache_key = (self.config.category, self.config.symbol.upper())
        if cache_key in self._max_leverage_ready_symbols:
            return
        ensured = self.order_manager.ensure_max_leverage(self.config.symbol, self.config.category)
        self.audit.log_event(
            "max_leverage_preflight",
            strategy=self.strategy.name,
            symbol=self.config.symbol,
            category=self.config.category,
            ensured=ensured,
        )
        if ensured:
            self._max_leverage_ready_symbols.add(cache_key)

        rules = self.order_manager.get_cached_instrument_rules(
            self.config.symbol, self.config.category
        )
        symbol_upper = self.config.symbol.upper()
        if rules:
            self.runtime_state.instrument_rules[symbol_upper] = rules
            self.logger.info(
                "loaded_instrument_rules %s",
                {
                    "symbol": symbol_upper,
                    "tick_size": str(rules.get("tick_size") or "0"),
                    "qty_step": str(rules.get("qty_step") or "0"),
                    "min_order_qty": str(rules.get("min_order_qty") or "0"),
                    "min_notional_value": str(rules.get("min_notional") or "0"),
                    "source": "bybit",
                },
            )
        else:
            self.logger.warning(
                "loaded_instrument_rules_missing %s",
                {"symbol": symbol_upper, "reason": "rules_not_found_in_runtime_state"},
            )
            rules = self.order_manager.get_cached_instrument_rules(
                self.config.symbol, self.config.category
            )
            if rules:
                symbol_upper = self.config.symbol.upper()
                self.runtime_state.instrument_rules[symbol_upper] = rules
                self.logger.info(
                    "loaded_instrument_rules %s",
                    {
                        "symbol": symbol_upper,
                        "tick_size": str(rules.get("tick_size") or "0"),
                        "qty_step": str(rules.get("qty_step") or "0"),
                        "min_order_qty": str(rules.get("min_order_qty") or "0"),
                        "min_notional_value": str(rules.get("min_notional") or "0"),
                    },
                )
            else:
                self.logger.warning(
                    "loaded_instrument_rules missing for %s", self.config.symbol.upper()
                )
            rules = self.order_manager.get_cached_instrument_rules(self.config.symbol, self.config.category)
            if rules:
                self.runtime_state.instrument_rules[self.config.symbol.upper()] = rules
            else:
                self.logger.warning(
                    "Instrument rules missing while ensuring max leverage for %s",
                    self.config.symbol.upper(),
                )

    def _cancel_open_orders_by_purpose_internal(
        self,
        purposes: list[str],
        replace_context: dict[str, Any] | None = None,
    ) -> None:
        purposes_set = {purpose for purpose in purposes if purpose}
        if not purposes_set:
            return
        snapshot = self.runtime_state.last_snapshot
        long_qty = float(snapshot.long_qty or 0.0) if snapshot else 0.0
        short_qty = float(snapshot.short_qty or 0.0) if snapshot else 0.0
        for client_id, order in list(self.runtime_state.active_orders.items()):
            if order.purpose not in purposes_set or order.status in {"FILLED", "CANCELED", "REJECTED"}:
                continue
            # CRITICAL EXIT PROTECTION GUARD
            if order.purpose == self.strategy.SHORT_SL_EXIT_PURPOSE:
                if long_qty <= 0.0 and short_qty > 0.0:
                    self.audit.log_event(
                        "cancel_blocked_protect_short_sl",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        reason="short_position_still_open",
                        long_qty=long_qty,
                        short_qty=short_qty,
                    )
                    continue
            if order.purpose == self.strategy.LONG_TP_EXIT_PURPOSE:
                if short_qty <= 0.0 and long_qty > 0.0:
                    self.audit.log_event(
                        "cancel_blocked_protect_long_tp",
                        strategy=self.strategy.name,
                        client_order_id=client_id,
                        reason="long_position_still_open",
                        long_qty=long_qty,
                        short_qty=short_qty,
                    )
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
                reason=replace_context.get("reason") if replace_context else None,
                existing_trigger_price=replace_context.get("existing_trigger_price") if replace_context else None,
                new_trigger_price=replace_context.get("new_trigger_price") if replace_context else None,
                existing_qty=replace_context.get("existing_qty") if replace_context else None,
                new_qty=replace_context.get("new_qty") if replace_context else None,
            )

    def _submit_to_exchange(
        self,
        managed_order: ManagedOrder,
        snapshot: HedgeSnapshot,
        force_market_fallback: bool = False,
    ) -> Any:
        exchange_side = self._exchange_side(managed_order.side, managed_order.reduce_only)
        position_idx_raw = managed_order.metadata.get("position_idx")
        position_idx = int(position_idx_raw) if position_idx_raw is not None else (1 if managed_order.side == "long" else 2)
        exit_api = managed_order.metadata.get("exit_api")
        trigger_price = self._safe_float(managed_order.metadata.get("trigger_price"), None)
        trigger_direction = managed_order.metadata.get("trigger_direction")
        trigger_by = managed_order.metadata.get("trigger_by")
        close_on_trigger = managed_order.metadata.get("close_on_trigger")
        order_filter = managed_order.metadata.get("order_filter")
        tp_limit_price = self._safe_float(managed_order.metadata.get("tp_limit_price"), None)
        slippage_tolerance_type = managed_order.metadata.get("slippage_tolerance_type")
        slippage_tolerance = self._safe_float(managed_order.metadata.get("slippage_tolerance"), None)
        if force_market_fallback and managed_order.reduce_only:
            self.audit.log_event(
                "order_payload_ready",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                trigger_price=trigger_price,
                reduce_only=managed_order.reduce_only,
                order_link_id=managed_order.client_order_id,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
                market_fallback=True,
                fallback_reason=managed_order.metadata.get("market_fallback_reason"),
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
            )
        if managed_order.reduce_only and trigger_price is not None:
            self.audit.log_event(
                "order_payload_ready",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                trigger_price=trigger_price,
                reduce_only=managed_order.reduce_only,
                order_link_id=managed_order.client_order_id,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
                trigger_price=trigger_price,
                trigger_direction=int(trigger_direction) if trigger_direction is not None else None,
                trigger_by=str(trigger_by) if trigger_by else None,
                close_on_trigger=bool(close_on_trigger) if close_on_trigger is not None else False,
            )
        if exit_api == "short_tp_limit":
            self.audit.log_event(
                "order_payload_ready",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                trigger_price=trigger_price,
                reduce_only=managed_order.reduce_only,
                order_link_id=managed_order.client_order_id,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.set_short_take_profit_limit(
                symbol=self.config.symbol,
                tp_price=trigger_price or 0.0,
                tp_limit_price=tp_limit_price or float(managed_order.price or trigger_price or 0.0),
                position_size=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                trigger_by=str(trigger_by or "LastPrice"),
            )
        if managed_order.order_type == "Limit" or trigger_price is not None:
            payload = OrderPayload(
                category=self.config.category,
                symbol=self.config.symbol,
                side=exchange_side,
                order_type=managed_order.order_type,
                price=managed_order.price,
                qty=managed_order.qty,
                reduce_only=managed_order.reduce_only,
                position_idx=position_idx,
                order_link_id=managed_order.client_order_id,
                trigger_price=trigger_price,
                trigger_direction=int(trigger_direction) if trigger_direction is not None else None,
                trigger_by=str(trigger_by) if trigger_by else None,
                close_on_trigger=bool(close_on_trigger) if close_on_trigger is not None else None,
                order_filter=str(order_filter) if order_filter else None,
                slippage_tolerance_type=slippage_tolerance_type,
                slippage_tolerance=slippage_tolerance,
            )
            self.audit.log_event(
                "order_payload_ready",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                trigger_price=trigger_price,
                reduce_only=managed_order.reduce_only,
                order_link_id=managed_order.client_order_id,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_limit_order(payload)
        if managed_order.reduce_only:
            self.audit.log_event(
                "order_payload_ready",
                strategy=self.strategy.name,
                purpose=managed_order.purpose,
                side=managed_order.side,
                order_type=managed_order.order_type,
                qty=managed_order.qty,
                price=managed_order.price,
                trigger_price=trigger_price,
                reduce_only=managed_order.reduce_only,
                order_link_id=managed_order.client_order_id,
                exchange_side=exchange_side,
                reference_price=snapshot.current_price,
                trigger_direction=trigger_direction,
                trigger_by=trigger_by,
                order_filter=order_filter,
            )
            return self.order_manager.place_reduce_market_order(
                symbol=self.config.symbol,
                side=exchange_side,
                qty=managed_order.qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=managed_order.client_order_id,
            )
        self.audit.log_event(
            "order_payload_ready",
            strategy=self.strategy.name,
            purpose=managed_order.purpose,
            side=managed_order.side,
            order_type=managed_order.order_type,
            qty=managed_order.qty,
            price=managed_order.price,
            trigger_price=trigger_price,
            reduce_only=managed_order.reduce_only,
            order_link_id=managed_order.client_order_id,
            exchange_side=exchange_side,
            reference_price=snapshot.current_price,
            trigger_direction=trigger_direction,
            trigger_by=trigger_by,
            order_filter=order_filter,
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
        managed_order = self.runtime_state.active_orders.get(client_id)
        if not managed_order:
            processed_qty = self.runtime_state.processed_fill_cumulative.get(client_id, 0.0)
            if processed_qty > 0:
                self.audit.log_event(
                    "fill_duplicate_ignored",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    cumulative_qty=cumulative_qty,
                    processed_cumulative=processed_qty,
                    source=source,
                )
            return
        processed_qty = self.runtime_state.processed_fill_cumulative.get(client_id, 0.0)
        if cumulative_qty is not None and cumulative_qty <= processed_qty:
            return
        with self._lock:
            processed_exec_ids = set(managed_order.metadata.get("processed_exec_ids") or [])
            if exec_id and exec_id in processed_exec_ids:
                return
            if exec_id:
                processed_exec_ids.add(exec_id)
                managed_order.metadata["processed_exec_ids"] = sorted(processed_exec_ids)
            if processed_qty >= managed_order.qty and managed_order.qty > 0:
                return
            previous_filled = managed_order.filled_qty
            if cumulative_qty is not None and cumulative_qty > previous_filled:
                incremental_qty = cumulative_qty - previous_filled
                managed_order.filled_qty = min(managed_order.qty, cumulative_qty)
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
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
                else:
                    self.runtime_state.realized_short_pnl_total += pnl
                    self.runtime_state.temporary_pnl_by_order[client_id] = (
                        self.runtime_state.temporary_pnl_by_order.get(client_id, 0.0) + pnl
                    )
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
            self.runtime_state.processed_fill_cumulative[client_id] = max(
                processed_qty, managed_order.filled_qty
            )
        with self._lock:
            self.audit.log_event(
                "fill_received",
                strategy=self.strategy.name,
                source=source,
                fill=fill_event.to_dict(),
            )
            snapshot = self.refresh_snapshot("fill")
            self._dispatch("fill", self.strategy.on_fill(fill_event, snapshot, self.runtime_state, self.context), snapshot)
            if managed_order.status == "FILLED":
                self._finalize_managed_order(client_id, managed_order)
            self._save_strategy_state()

    def _reconcile_active_orders(self) -> None:
        if not self.runtime_state.active_orders:
            self.audit.log_event(
                "reconcile_skipped",
                strategy=self.strategy.name,
                reason="no_active_orders",
            )
            return
        open_orders = self.order_manager.fetch_open_orders(self.config.symbol, self.config.category) or []
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
        self._save_strategy_state()

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
                self.runtime_state.active_orders.pop(client_id, None)
                if order.exchange_order_id:
                    self.runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
                self.audit.log_event(
                    "startup_order_recovery_pruned_stale_order",
                    strategy=self.strategy.name,
                    client_order_id=client_id,
                    exchange_order_id=order.exchange_order_id,
                    purpose=order.purpose,
                    side=order.side,
                    status=order.status,
                )
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
        if managed_order.exchange_order_id:
            self.runtime_state.exchange_to_client_id.pop(managed_order.exchange_order_id, None)
        self.audit.log_event(
            "order_finalized",
            strategy=self.strategy.name,
            client_order_id=client_id,
            exchange_order_id=managed_order.exchange_order_id,
            status=managed_order.status,
            filled_qty=managed_order.filled_qty,
            remaining_qty=managed_order.remaining_qty,
        )

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
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)