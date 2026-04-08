from __future__ import annotations

import logging
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from strategy.config import StrategyConfig
from strategy.order_manager import BybitOrderManager, OrderPayload
from strategy.position_manager import PositionManager
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine, StrategyState
from utils.math_utils import adjust_qty_to_min_notional, calculate_pnl


@dataclass
class OrderIntent:
    """Strategy-level order intent.

    `price` is intentionally semantic rather than purely technical:
    - for limit intents it is the intended limit price
    - for market intents it is the reference/trigger price observed by strategy logic
    """

    side: str
    qty: float
    price: float | None
    purpose: str
    reduce_only: bool | None = None
    order_type: str | None = None
    metadata: dict[str, Any] | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderExecutor:
    def __init__(
        self,
        *,
        config: StrategyConfig,
        logger: logging.Logger,
        order_manager: BybitOrderManager | None,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        state_machine: StateMachine,
        active_orders: dict[str, dict[str, Any]],
        submitted_orders: set[tuple[str, float, str]],
        recent_orders: deque[str],
        exchange_to_client_id: dict[str, str],
        order_lock: Any,
        normalize_order_qty: Callable[[float, str], float],
        current_qty_step: Callable[[], float],
        has_active_intent: Callable[[str, str, float, float], bool],
        generate_client_order_id: Callable[[str], str],
        log_slippage_check: Callable[[str], None],
        safe_update_order: Callable[[str, dict[str, Any]], None],
        mark_order_filled: Callable[[str], None],
        record_realized_pnl_by_side: Callable[[str, float], None],
        handle_order_finalized_locked: Callable[[str, dict[str, Any]], None],
        sync_positions_with_exchange: Callable[[], None],
        get_position_snapshot: Callable[[], tuple[float, float, float, float]],
        verify_order_on_exchange: Callable[..., bool],
        get_last_price: Callable[[], float | None],
        set_dca_steps: Callable[[int], None],
        on_intent_executed: Callable[[OrderIntent, float], list[OrderIntent]],
    ) -> None:
        self.config = config
        self.logger = logger
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.state_machine = state_machine
        self.active_orders = active_orders
        self._submitted_orders = submitted_orders
        self._recent_orders = recent_orders
        self._exchange_to_client_id = exchange_to_client_id
        self._order_lock = order_lock
        self._normalize_order_qty = normalize_order_qty
        self._current_qty_step = current_qty_step
        self._has_active_intent = has_active_intent
        self._generate_client_order_id = generate_client_order_id
        self._log_slippage_check = log_slippage_check
        self.safe_update_order = safe_update_order
        self.mark_order_filled = mark_order_filled
        self._record_realized_pnl_by_side = record_realized_pnl_by_side
        self._handle_order_finalized_locked = handle_order_finalized_locked
        self.sync_positions_with_exchange = sync_positions_with_exchange
        self._get_position_snapshot = get_position_snapshot
        self.verify_order_on_exchange = verify_order_on_exchange
        self._get_last_price = get_last_price
        self._set_dca_steps = set_dca_steps
        self._on_intent_executed = on_intent_executed

    def _debug_active_orders_summary(self) -> list[str]:
        with self._order_lock:
            summaries: list[str] = []
            for client_id, order in self.active_orders.items():
                summaries.append(
                    f"{client_id}:{order.get('purpose')}:{order.get('side')}:"
                    f"{order.get('status')}:{order.get('size')}"
                )
            return summaries

    def _local_position_snapshot_dict(self) -> dict[str, float]:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        return {
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
        }

    def _exchange_position_snapshot_dict(self) -> dict[str, float | None]:
        snapshot: dict[str, float | None] = {
            "exchange_long_size": None,
            "exchange_short_size": None,
            "exchange_long_avg": None,
            "exchange_short_avg": None,
        }
        if not self.order_manager or not hasattr(self.order_manager, "fetch_positions"):
            return snapshot
        try:
            positions = self.order_manager.fetch_positions(
                self.config.default_symbol, self.config.category
            )
        except Exception as exc:
            self.logger.warning(
                "Exchange position snapshot failed",
                extra={"symbol": self.config.default_symbol, "error": str(exc)},
            )
            return snapshot
        long_size = 0.0
        short_size = 0.0
        long_avg = 0.0
        short_avg = 0.0
        for pos in positions or []:
            side = (pos.get("side") or pos.get("positionSide") or "").lower()
            size = float(pos.get("size") or pos.get("positionQty") or 0.0)
            avg_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
            if side in {"buy", "long"}:
                long_size = size
                long_avg = avg_price
            elif side in {"sell", "short"}:
                short_size = size
                short_avg = avg_price
        snapshot.update(
            {
                "exchange_long_size": long_size,
                "exchange_short_size": short_size,
                "exchange_long_avg": long_avg,
                "exchange_short_avg": short_avg,
            }
        )
        return snapshot

    def _log_order_observability(
        self,
        event: str,
        *,
        side: str,
        purpose: str,
        order_type: str,
        reduce_only: bool,
        requested_qty: float,
        submit_qty: float | None,
        price: float | None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        result: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "state": self.state_machine.state.value,
            "symbol": self.config.default_symbol,
            "side": side,
            "purpose": purpose,
            "order_type": order_type,
            "reduce_only": reduce_only,
            "requested_qty": requested_qty,
            "submit_qty": submit_qty,
            "price": price,
            "last_price": self._get_last_price(),
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "result": result,
        }
        payload.update(self._local_position_snapshot_dict())
        payload.update(self._exchange_position_snapshot_dict())
        self.logger.info("ORDER OBSERVABILITY", extra=payload)

    @staticmethod
    def _exchange_side(side: str) -> str:
        return "Buy" if side.lower() == "long" else "Sell"

    @staticmethod
    def _default_order_type_for_purpose(purpose: str) -> str:
        if purpose in {"LONG_REBUY", "LONG_REBUY_HEDGE"}:
            return "Limit"
        if purpose == "SHORT_REBALANCE":
            return "Market"
        if purpose in {
            "HEDGE_RECOVER",
            "TP_LONG",
            "TP_SHORT",
            "DD_EXIT",
            "EMERGENCY",
        }:
            return "Market"
        if purpose in {"spread_heal_long", "spread_heal_short", "basket_exit"}:
            return "Market"
        return "Limit"

    @staticmethod
    def _default_reduce_only_for_purpose(purpose: str) -> bool:
        if purpose in {"TP_LONG", "TP_SHORT", "DD_EXIT", "EMERGENCY"}:
            return True
        if purpose in {
            "LONG_REBUY",
            "LONG_REBUY_HEDGE",
            "SHORT_REBALANCE",
            "HEDGE_RECOVER",
        }:
            return False
        if purpose in {"spread_heal_long", "spread_heal_short"}:
            return False
        if purpose == "basket_exit":
            return True
        return False

    @staticmethod
    def _is_full_close_purpose(purpose: str | None) -> bool:
        if not purpose:
            return False
        if purpose in {"basket_exit", "DD_EXIT", "EMERGENCY", "TP_LONG", "TP_SHORT"}:
            return True
        return purpose.startswith("CLOSE_")

    def execute_intent(
        self,
        intent: OrderIntent,
        enqueue_follow_ups: Callable[[list[OrderIntent]], None] | None = None,
        allow_tp_short: bool = True,
    ) -> bool:
        self.logger.info(
            "[EXEC DEBUG] execute_intent_called "
            f"tick={getattr(self, '_sim_current_tick', None)} "
            f"purpose={intent.purpose} side={intent.side} qty={intent.qty:.12f} "
            f"price={intent.price} state={self.state_machine.state.value}"
        )
        order_type = intent.order_type or self._default_order_type_for_purpose(
            intent.purpose
        )
        reduce_only = (
            intent.reduce_only
            if intent.reduce_only is not None
            else self._default_reduce_only_for_purpose(intent.purpose)
        )
        self._log_order_observability(
            "pre_execute",
            side=intent.side,
            purpose=intent.purpose,
            order_type=order_type,
            reduce_only=reduce_only,
            requested_qty=intent.qty,
            submit_qty=None,
            price=intent.price,
        )
        if reduce_only and order_type == "Market":
            if intent.price is None:
                self.logger.info("[EXEC DEBUG] decision=OTHER_SKIP reason=missing_market_price")
                return False
            if self._is_full_close_purpose(intent.purpose):
                if self.close_position(intent.side, intent.price, intent.purpose):
                    self.logger.info("[EXEC DEBUG] decision=EXECUTE path=close_position")
                    return True
                self.logger.info("[EXEC DEBUG] decision=EXECUTE path=force_reduce_market_exit")
                return self._force_reduce_market_exit(intent.side, intent.purpose)
            return self._submit_partial_reduce_market(
                intent.side,
                intent.qty,
                intent.price,
                intent.purpose,
            )

        executed = True
        if order_type == "Market" and not reduce_only:
            if intent.price is None:
                self.logger.info("[EXEC DEBUG] decision=OTHER_SKIP reason=missing_reference_price")
                return False
            reference_price = intent.price
            executed = self._place_market_order_on_exchange(
                intent.side,
                intent.qty,
                reference_price,
                intent.purpose,
                metadata=intent.metadata,
            )
            if not executed:
                self.logger.info("[EXEC DEBUG] decision=OTHER_SKIP reason=market_execution_blocked")
                self.logger.info(
                    "Intent execution blocked",
                    extra={
                        "side": intent.side,
                        "purpose": intent.purpose,
                    },
                )
                return False
            self.logger.info("[EXEC DEBUG] decision=EXECUTE path=market_order")
            follow_up_intents = self._on_intent_executed(intent, reference_price)
            if enqueue_follow_ups is not None:
                enqueue_follow_ups(follow_up_intents)
            else:
                for follow_up_intent in follow_up_intents:
                    self.execute_intent(follow_up_intent)
            return True

        if order_type != "Limit" or intent.price is None:
            self.logger.info(
                "[EXEC DEBUG] decision=OTHER_SKIP "
                f"reason=invalid_limit_input order_type={order_type} price={intent.price}"
            )
            return False

        submit_price = intent.price
        if intent.side == "long" and intent.purpose in {"LONG_REBUY", "LONG_REBUY_HEDGE"}:
            latest_price = self._get_last_price()
            if latest_price is not None:
                submit_price = min(submit_price, latest_price)
        self._log_order_observability(
            "pre_submit_limit",
            side=intent.side,
            purpose=intent.purpose,
            order_type=order_type,
            reduce_only=reduce_only,
            requested_qty=intent.qty,
            submit_qty=intent.qty,
            price=submit_price,
        )
        self.logger.info(
            "[EXEC DEBUG] limit_submit_prepared "
            f"purpose={intent.purpose} side={intent.side} qty={intent.qty:.12f} "
            f"requested_price={intent.price} submit_price={submit_price}"
        )

        executed = self._place_order_on_exchange(
            intent.side,
            intent.qty,
            submit_price,
            intent.purpose,
            metadata=intent.metadata,
        )
        if not executed:
            self.logger.info("[EXEC DEBUG] decision=OTHER_SKIP reason=limit_execution_blocked")
            self.logger.info(
                "Intent execution blocked",
                extra={
                    "side": intent.side,
                    "purpose": intent.purpose,
                },
            )
            return False
        self.logger.info("[EXEC DEBUG] decision=EXECUTE path=limit_order")

        follow_up_intents = self._on_intent_executed(intent, submit_price)
        if enqueue_follow_ups is not None:
            enqueue_follow_ups(follow_up_intents)
        else:
            for follow_up_intent in follow_up_intents:
                self.execute_intent(follow_up_intent)
        return True

    def close_position(self, side: str, price: float, purpose: str | None = None) -> bool:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        size_to_close = short_size if side == "short" else long_size
        if size_to_close <= 0:
            return False

        entry_price = short_avg if side == "short" else long_avg
        purpose = purpose or f"CLOSE_{side.upper()}"
        intent_price = 0.0
        normalized_size = self._normalize_order_qty(size_to_close, purpose)
        if normalized_size <= 0:
            return False
        if self._has_active_intent(side, purpose, intent_price, normalized_size):
            self.logger.info(
                "Duplicate close intent detected, skipping close",
                extra={"side": side, "purpose": purpose, "size": normalized_size},
            )
            return False

        key = (side, round(normalized_size, 4), purpose)
        with self._order_lock:
            if key in self._submitted_orders:
                self.logger.info(
                    "Skipping duplicate close submission",
                    extra={"side": side, "purpose": purpose, "size": normalized_size},
                )
                return False
            self._submitted_orders.add(key)
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_add "
                f"client_id=pending_close key={key} side={side} qty={normalized_size:.12f} "
                f"purpose={purpose} submitted_orders_after={list(self._submitted_orders)}"
            )

        client_id = self._generate_client_order_id(purpose)
        now = _utcnow()
        with self._order_lock:
            self.active_orders[client_id] = {
                "side": side,
                "purpose": purpose,
                "price": intent_price,
                "size": normalized_size,
                "status": "PENDING_SUBMIT",
                "created_at": now,
                "verify_attempts": 0,
                "remaining_qty": normalized_size,
                "partial_handled": False,
                "metadata": {"reduce_only": True, "trigger_price": price},
            }
            self._recent_orders.append(client_id)

        exchange_side = "Buy" if side == "short" else "Sell"
        self._log_slippage_check(exchange_side)
        self._log_order_observability(
            "pre_submit_reduce_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
            requested_qty=size_to_close,
            submit_qty=normalized_size,
            price=price,
            client_order_id=client_id,
        )
        position_idx = 2 if side == "short" else 1
        response = (
            self.order_manager.place_reduce_market_order(
                symbol=self.config.default_symbol,
                side=exchange_side,
                qty=normalized_size,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=client_id,
            )
            if self.order_manager
            else None
        )
        if not response:
            with self._order_lock:
                self._submitted_orders.discard(key)
                self.logger.info(
                    "[EXEC DEBUG] submitted_orders_remove "
                    f"client_id={client_id} key={key} reason=close_order_no_response "
                    f"submitted_orders_after={list(self._submitted_orders)}"
                )
                self.active_orders.pop(client_id, None)
                try:
                    self._recent_orders.remove(client_id)
                except ValueError:
                    pass
            self.logger.error(
                "Close order placement failed",
                extra={
                    "side": side,
                    "purpose": purpose,
                    "symbol": self.config.default_symbol,
                    "size": normalized_size,
                },
            )
            return False

        result = response.get("result") or {}
        exchange_id = result.get("orderId")
        self.safe_update_order(
            client_id,
            {
                "status": "OPEN",
                "exchange_confirmed": True,
                "exchange_order_id": exchange_id,
                "updated_at": _utcnow(),
            },
        )
        if exchange_id:
            self._exchange_to_client_id[exchange_id] = client_id
        self.logger.info(
            "Close order submitted",
            extra={
                "client_order_id": client_id,
                "side": side,
                "purpose": purpose,
                "symbol": self.config.default_symbol,
                "size": normalized_size,
                "price": price,
            },
        )
        self._log_order_observability(
            "post_submit_reduce_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
            requested_qty=size_to_close,
            submit_qty=normalized_size,
            price=price,
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            result="submitted",
        )

        closed = 0.0
        for _ in range(3):
            self.sync_positions_with_exchange()
            time.sleep(0.2)
            long_size, short_size, _, _ = self._get_position_snapshot()
            remaining = short_size if side == "short" else long_size
            if remaining < size_to_close - 1e-9:
                closed = min(size_to_close - remaining, normalized_size)
                break

        if closed > 0:
            pnl = calculate_pnl(entry_price, price, closed, side)
            self.risk_manager.record_realized_pnl(pnl)
            self._record_realized_pnl_by_side(side, pnl)
            self.mark_order_filled(client_id)
            with self._order_lock:
                order = self.active_orders.pop(client_id, None)
                if order:
                    self._handle_order_finalized_locked(client_id, order)
            self.logger.info(
                f"Closed {side}",
                extra={"size": closed, "price": price, "pnl": pnl},
            )
            if self.state_machine.state == StrategyState.NORMAL:
                self._set_dca_steps(0)
            return (short_size if side == "short" else long_size) <= 1e-9

        self.logger.warning(
            "Close order submitted but not yet confirmed by position sync",
            extra={"client_order_id": client_id, "side": side, "purpose": purpose},
        )
        return False

    def _submit_partial_reduce_market(
        self,
        side: str,
        qty: float,
        price: float,
        purpose: str,
    ) -> bool:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        live_size = short_size if side == "short" else long_size
        if live_size <= 0 or qty <= 0:
            return False

        entry_price = short_avg if side == "short" else long_avg
        normalized_qty = self._normalize_order_qty(qty, purpose)
        if normalized_qty <= 0:
            return False
        if self._has_active_intent(side, purpose, price, normalized_qty):
            self.logger.info(
                "Duplicate partial reduce intent detected, skipping order",
                extra={"side": side, "purpose": purpose, "size": normalized_qty},
            )
            return False

        key = (side, round(normalized_qty, 4), purpose)
        with self._order_lock:
            if key in self._submitted_orders:
                self.logger.info(
                    "Skipping duplicate partial reduce submission",
                    extra={"side": side, "purpose": purpose, "size": normalized_qty},
                )
                return False
            self._submitted_orders.add(key)
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_add "
                f"client_id=pending_partial_reduce key={key} side={side} qty={normalized_qty:.12f} "
                f"purpose={purpose} submitted_orders_after={list(self._submitted_orders)}"
            )

        client_id = self._generate_client_order_id(purpose)
        now = _utcnow()
        with self._order_lock:
            self.active_orders[client_id] = {
                "side": side,
                "purpose": purpose,
                "price": price,
                "size": normalized_qty,
                "qty": normalized_qty,
                "status": "PENDING_SUBMIT",
                "created_at": now,
                "verify_attempts": 0,
                "remaining_qty": normalized_qty,
                "partial_handled": False,
                "retry_count": 0,
                "metadata": {
                    "reduce_only": True,
                    "trigger_price": price,
                    "order_type": "Market",
                    "full_close": False,
                },
            }
            self._recent_orders.append(client_id)

        exchange_side = "Buy" if side == "short" else "Sell"
        self._log_slippage_check(exchange_side)
        self._log_order_observability(
            "pre_submit_partial_reduce_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
            requested_qty=qty,
            submit_qty=normalized_qty,
            price=price,
            client_order_id=client_id,
        )
        position_idx = 2 if side == "short" else 1
        response = (
            self.order_manager.place_reduce_market_order(
                symbol=self.config.default_symbol,
                side=exchange_side,
                qty=normalized_qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=client_id,
            )
            if self.order_manager
            else None
        )
        if not response:
            with self._order_lock:
                self._submitted_orders.discard(key)
                self.logger.info(
                    "[EXEC DEBUG] submitted_orders_remove "
                    f"client_id={client_id} key={key} reason=partial_reduce_no_response "
                    f"submitted_orders_after={list(self._submitted_orders)}"
                )
                self.active_orders.pop(client_id, None)
                try:
                    self._recent_orders.remove(client_id)
                except ValueError:
                    pass
            self.logger.error(
                "Partial reduce order placement failed",
                extra={
                    "side": side,
                    "purpose": purpose,
                    "symbol": self.config.default_symbol,
                    "size": normalized_qty,
                },
            )
            return False

        result = response.get("result") or {}
        exchange_id = result.get("orderId")
        self.safe_update_order(
            client_id,
            {
                "status": "OPEN",
                "exchange_confirmed": True,
                "exchange_order_id": exchange_id,
                "updated_at": _utcnow(),
            },
        )
        if exchange_id:
            self._exchange_to_client_id[exchange_id] = client_id
        self.logger.info(
            "Partial reduce order submitted",
            extra={
                "client_order_id": client_id,
                "side": side,
                "purpose": purpose,
                "symbol": self.config.default_symbol,
                "size": normalized_qty,
                "price": price,
            },
        )
        self._log_order_observability(
            "post_submit_partial_reduce_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
            requested_qty=qty,
            submit_qty=normalized_qty,
            price=price,
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            result="submitted",
        )

        reduced = 0.0
        for _ in range(3):
            self.sync_positions_with_exchange()
            time.sleep(0.2)
            long_size, short_size, _, _ = self._get_position_snapshot()
            remaining = short_size if side == "short" else long_size
            if remaining < live_size - 1e-9:
                reduced = min(live_size - remaining, normalized_qty)
                break

        if reduced > 0:
            pnl = calculate_pnl(entry_price, price, reduced, side)
            self.risk_manager.record_realized_pnl(pnl)
            self._record_realized_pnl_by_side(side, pnl)
            self.mark_order_filled(client_id)
            with self._order_lock:
                order = self.active_orders.pop(client_id, None)
                if order:
                    self._handle_order_finalized_locked(client_id, order)
            self.logger.info(
                f"Partially reduced {side}",
                extra={"size": reduced, "price": price, "pnl": pnl, "purpose": purpose},
            )
            return True

        self.logger.warning(
            "Partial reduce order submitted but not yet confirmed by position sync",
            extra={"client_order_id": client_id, "side": side, "purpose": purpose},
        )
        return False

    def _force_reduce_market_exit(self, side: str, purpose: str) -> bool:
        if not self.order_manager:
            return False
        long_size, short_size, _, _ = self._get_position_snapshot()
        size = short_size if side == "short" else long_size
        qty = self._normalize_order_qty(size, purpose)
        if qty <= 0:
            return False
        response = self.order_manager.place_reduce_market_order(
            symbol=self.config.default_symbol,
            side="Buy" if side == "short" else "Sell",
            qty=qty,
            position_idx=2 if side == "short" else 1,
            category=self.config.category,
            order_link_id=self._generate_client_order_id(purpose),
        )
        if response:
            result = response.get("result") or {}
            exchange_id = result.get("orderId")
            self.logger.critical(
                "Emergency reduce-only market order submitted",
                extra={"side": side, "purpose": purpose, "qty": qty},
            )
            self._log_order_observability(
                "post_force_reduce_market",
                side=side,
                purpose=purpose,
                order_type="Market",
                reduce_only=True,
                requested_qty=size,
                submit_qty=qty,
                price=self._get_last_price(),
                exchange_order_id=exchange_id,
                result="submitted",
            )
            return True
        return False

    def _place_order_on_exchange(
        self,
        side: str,
        size: float,
        price: float,
        purpose: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        self.logger.info(
            "[EXEC DEBUG] place_order_called "
            f"tick={getattr(self, '_sim_current_tick', None)} "
            f"side={side} purpose={purpose} raw_size={size:.12f} price={price}"
        )
        if not self.order_manager or size <= 0:
            self.logger.info(
                f"[EXEC DEBUG] decision=OTHER_SKIP reason=missing_order_manager_or_size side={side} purpose={purpose} size={size}"
            )
            return False
        normalized_size = self._normalize_order_qty(size, purpose)
        if normalized_size <= 0:
            self.logger.info(
                f"[EXEC DEBUG] decision=OTHER_SKIP reason=normalized_size_zero side={side} purpose={purpose}"
            )
            return False
        qty_step = self._current_qty_step()
        qty = adjust_qty_to_min_notional(
            normalized_size,
            price,
            self.config.min_order_value,
            qty_step,
        )
        notional = qty * price
        if notional < self.config.min_order_value:
            self.logger.info(
                "[EXEC DEBUG] decision=OTHER_SKIP reason=min_notional "
                f"qty={qty:.12f} notional={notional:.12f} min_order_value={self.config.min_order_value}"
            )
            self.logger.warning(
                "[ORDER BLOCKED] even after adjustment qty=%s notional=%.6f",
                qty,
                notional,
            )
            return False
        size = qty
        active_summaries = self._debug_active_orders_summary()
        has_active = self._has_active_intent(side, purpose, price, size)
        self.logger.info(
            "[EXEC DEBUG] active_intent_check "
            f"side={side} purpose={purpose} size={size:.12f} price={price} "
            f"has_active_intent={has_active} active_orders={active_summaries}"
        )
        if has_active:
            self.logger.info(
                "[EXEC DEBUG] decision=SKIP_ACTIVE_INTENT "
                f"side={side} purpose={purpose} size={size:.12f} price={price}"
            )
            self.logger.info(
                "Duplicate intent detected, skipping order",
                extra={"side": side, "purpose": purpose, "price": price},
            )
            return False
        self.logger.debug(
            "Submitting order to exchange",
            extra={
                "side": side,
                "size": size,
                "price": price,
                "symbol": self.config.default_symbol,
            },
        )
        if purpose in {"paired_long_close", "paired_partial_sl_long", "paired_partial_sl_short"}:
            key = (side, round(size, 4), purpose, round(price, 8))
        else:
            key = (side, round(size, 4), purpose)
        with self._order_lock:
            already_submitted = key in self._submitted_orders
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_check "
                f"tracking_key={key} already_submitted={already_submitted} "
                f"submitted_orders={list(self._submitted_orders)}"
            )
            if key in self._submitted_orders:
                self.logger.info(
                    "[EXEC DEBUG] decision=SKIP_ALREADY_SUBMITTED "
                    f"tracking_key={key}"
                )
                self.logger.debug(
                    "Skipping duplicate order submission", extra={"order_key": key}
                )
                return False
            self._submitted_orders.add(key)
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_add "
                f"tracking_key={key} submitted_orders_after={list(self._submitted_orders)}"
            )
        client_id = self._generate_client_order_id(purpose)
        now = _utcnow()
        stored_metadata = dict(metadata) if metadata else {}
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        stored_metadata.setdefault(
            "pre_submit_snapshot",
            {
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
            },
        )
        stored_metadata.setdefault("order_type", "Limit")
        stored_metadata.setdefault("reduce_only", False)
        with self._order_lock:
            self.active_orders[client_id] = {
                "side": side,
                "purpose": purpose,
                "price": price,
                "size": size,
                "qty": size,
                "status": "PENDING_SUBMIT",
                "created_at": now,
                "verify_attempts": 0,
                "remaining_qty": size,
                "partial_handled": False,
                "metadata": stored_metadata,
                "retry_count": 0,
            }
            self._recent_orders.append(client_id)
        self.logger.info(
            "[EXEC DEBUG] active_order_created "
            f"client_id={client_id} tracking_key={key} status=PENDING_SUBMIT "
            f"active_orders={self._debug_active_orders_summary()}"
        )
        self.logger.info(
            "Order created",
            extra={
                "client_order_id": client_id,
                "side": side,
                "purpose": purpose,
                "price": price,
                "size": size,
            },
        )
        reduce_only_limit = bool(stored_metadata.get("reduce_only", False))
        exchange_side = self._exchange_side(side)
        position_idx = 1 if side == "long" else 2
        if reduce_only_limit:
            exchange_side = "Sell" if side == "long" else "Buy"
        payload = OrderPayload(
            category=self.config.category,
            symbol=self.config.default_symbol,
            side=exchange_side,
            order_type="Limit",
            price=price,
            qty=qty,
            reduce_only=reduce_only_limit,
            position_idx=position_idx,
            order_link_id=client_id,
        )
        self.logger.info(
            "[EXEC DEBUG] place_limit_order_payload "
            f"client_id={client_id} side={payload.side} qty={payload.qty} price={payload.price} "
            f"symbol={payload.symbol} tracking_key={key}"
        )
        def cleanup_rejected_limit(reason: str) -> None:
            with self._order_lock:
                self._submitted_orders.discard(key)
                self.logger.info(
                    "[EXEC DEBUG] submitted_orders_remove "
                    f"client_id={client_id} key={key} reason={reason} "
                    f"submitted_orders_after={list(self._submitted_orders)}"
                )
                self.active_orders.pop(client_id, None)
                try:
                    self._recent_orders.remove(client_id)
                except ValueError:
                    pass

        try:
            response = self.order_manager.place_limit_order(payload)
            result = (response.get("result") if response else {}) or {}
            exchange_id = result.get("orderId")
            self.logger.info(
                "[EXEC DEBUG] place_limit_order_result "
                f"client_id={client_id} response_ok={bool(response)} exchange_id={exchange_id}"
            )
            if exchange_id:
                with self._order_lock:
                    self.active_orders[client_id]["exchange_order_id"] = exchange_id
                self._exchange_to_client_id[exchange_id] = client_id
            if not response:
                cleanup_rejected_limit("limit_order_no_response")
                current_price = self._get_last_price()
                if (
                    purpose in {"LONG_REBUY", "LONG_REBUY_HEDGE"}
                    and current_price is not None
                    and current_price <= price
                ):
                    self.logger.info(
                        "[EXEC DEBUG] decision=EXECUTE path=limit_reject_market_fallback "
                        f"client_id={client_id} current_price={current_price} intended_limit_price={price}"
                    )
                    executed = self._place_market_order_on_exchange(
                        side,
                        size,
                        current_price,
                        purpose,
                        metadata={
                            **stored_metadata,
                            "fallback_from_limit_reject": True,
                            "fallback_reference_limit_price": price,
                        },
                    )
                    if executed:
                        self._on_intent_executed(
                            OrderIntent(
                                side=side,
                                qty=size,
                                price=current_price,
                                purpose=purpose,
                                order_type="Market",
                            ),
                            current_price,
                        )
                    return executed
                self.logger.info(
                    "[EXEC DEBUG] decision=OTHER_SKIP "
                    f"reason=limit_order_no_response client_id={client_id}"
                )
                return False
        except Exception as exc:
            self.logger.info(
                "[EXEC DEBUG] decision=OTHER_SKIP "
                f"reason=placement_exception client_id={client_id} error={exc}"
            )
            self.logger.exception(
                "[ORDER ERROR] placement failed",
                extra={"client_order_id": client_id, "error": str(exc)},
            )
            cleanup_rejected_limit("limit_order_exception")
            return False
        verified = self.verify_order_on_exchange(
            client_id, source="placement", log_missing=True
        )
        self.logger.info(
            "[EXEC DEBUG] verify_order_result "
            f"client_id={client_id} verified={verified} active_orders={self._debug_active_orders_summary()} "
            f"submitted_orders={list(self._submitted_orders)}"
        )
        self._log_order_observability(
            "post_submit_limit",
            side=side,
            purpose=purpose,
            order_type="Limit",
            reduce_only=bool((metadata or {}).get("reduce_only", False)),
            requested_qty=size,
            submit_qty=qty,
            price=price,
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            result="verified" if verified else "unverified",
        )
        return verified

    def _place_market_order_on_exchange(
        self,
        side: str,
        size: float,
        reference_price: float,
        purpose: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not self.order_manager or size <= 0:
            return False
        normalized_size = self._normalize_order_qty(size, purpose)
        if normalized_size <= 0:
            return False
        if self._has_active_intent(side, purpose, reference_price, normalized_size):
            self.logger.info(
                "Duplicate market intent detected, skipping order",
                extra={"side": side, "purpose": purpose, "price": reference_price},
            )
            return False
        key = (side, round(normalized_size, 4), purpose)
        with self._order_lock:
            if key in self._submitted_orders:
                self.logger.debug(
                    "Skipping duplicate market order submission",
                    extra={"order_key": key},
                )
                return False
            self._submitted_orders.add(key)
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_add "
                f"client_id=pending_market key={key} side={side} qty={normalized_size:.12f} "
                f"purpose={purpose} submitted_orders_after={list(self._submitted_orders)}"
            )

        client_id = self._generate_client_order_id(purpose)
        now = _utcnow()
        stored_metadata = dict(metadata) if metadata else {}
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        stored_metadata.setdefault(
            "pre_submit_snapshot",
            {
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
            },
        )
        stored_metadata.setdefault("order_type", "Market")
        stored_metadata.setdefault("reduce_only", False)
        self._log_order_observability(
            "pre_submit_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=False,
            requested_qty=size,
            submit_qty=normalized_size,
            price=reference_price,
        )
        with self._order_lock:
            self.active_orders[client_id] = {
                "side": side,
                "purpose": purpose,
                "price": reference_price,
                "size": normalized_size,
                "qty": normalized_size,
                "status": "PENDING_SUBMIT",
                "created_at": now,
                "verify_attempts": 0,
                "remaining_qty": normalized_size,
                "partial_handled": False,
                "metadata": stored_metadata,
                "retry_count": 0,
            }
            self._recent_orders.append(client_id)

        exchange_side = self._exchange_side(side)
        self._log_slippage_check(exchange_side)
        if hasattr(self.order_manager, "place_market_order"):
            response = self.order_manager.place_market_order(
                symbol=self.config.default_symbol,
                side=exchange_side,
                qty=normalized_size,
                price=reference_price,
                position_idx=1 if side == "long" else 2,
                category=self.config.category,
                order_link_id=client_id,
            )
        else:
            response = self.order_manager._post(  # type: ignore[attr-defined]
                "/v5/order/create",
                json.dumps(
                    {
                        "category": self.config.category,
                        "symbol": self.config.default_symbol.upper(),
                        "side": exchange_side,
                        "orderType": "Market",
                        "qty": f"{normalized_size}",
                        "positionIdx": 1 if side == "long" else 2,
                        "timeInForce": "IOC",
                        "orderLinkId": client_id,
                    }
                ),
            )
        if not response:
            with self._order_lock:
                self._submitted_orders.discard(key)
                self.logger.info(
                    "[EXEC DEBUG] submitted_orders_remove "
                    f"client_id={client_id} key={key} reason=market_order_no_response "
                    f"submitted_orders_after={list(self._submitted_orders)}"
                )
                self.active_orders.pop(client_id, None)
                try:
                    self._recent_orders.remove(client_id)
                except ValueError:
                    pass
            return False

        result = response.get("result") or {}
        exchange_id = result.get("orderId")
        if exchange_id:
            with self._order_lock:
                self.active_orders[client_id]["exchange_order_id"] = exchange_id
            self._exchange_to_client_id[exchange_id] = client_id
        verified = self.verify_order_on_exchange(
            client_id, source="placement", log_missing=True
        )
        self._log_order_observability(
            "post_submit_market",
            side=side,
            purpose=purpose,
            order_type="Market",
            reduce_only=False,
            requested_qty=size,
            submit_qty=normalized_size,
            price=reference_price,
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            result="verified" if verified else "unverified",
        )
        return verified
