from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping
from uuid import uuid4

import math

from strategy.execution.order_executor import OrderExecutor, OrderIntent
from strategy.order_manager import BybitOrderManager

from models.order import Order
from strategy.config import StrategyConfig
from strategy.position_manager import PositionManager
from strategy.risk_manager import ExposureReport, RiskManager
from strategy.state_machine import StateMachine, StrategyState
from utils.math_utils import adjust_qty_to_min_notional


logger = logging.getLogger("psrh")
logger.setLevel(logging.INFO)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_seconds_since(value: datetime | float | None) -> float:
    if isinstance(value, datetime):
        now = _utcnow()
        if value.tzinfo is None:
            return (now.replace(tzinfo=None) - value).total_seconds()
        return (now - value).total_seconds()
    return time.time() - float(value or 0.0)


class PSRHStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.position_manager = PositionManager()
        self.state_machine = StateMachine()
        self.risk_manager = RiskManager(config)
        self.orders: List[Order] = []
        self.dca_steps = 0
        self.last_price: float | None = None
        self.last_rebuy_time: datetime | None = None
        self.initialized = False
        self.order_manager = (
            BybitOrderManager(config.api_key, config.secret_key)
            if config.api_key and config.secret_key
            else None
        )
        self._exchange_ready = False
        self.logger = logger
        self._configure_logger()
        self.last_rebuy_price: float | None = None
        self._extend_requested = False
        self._submitted_orders: set[tuple[str, float, str]] = set()
        self.active_orders: Dict[str, Dict[str, Any]] = {}
        self._order_lock = threading.Lock()
        self._recent_orders = deque(maxlen=20)
        self._reconcile_thread: threading.Thread | None = None
        self._fast_poll_thread: threading.Thread | None = None
        self._position_sync_queue: deque[bool] = deque()
        self._reconcile_stop = threading.Event()
        self._fast_poll_stop = threading.Event()
        self._exchange_lock = threading.Lock()
        self._position_sync_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._recovery_lock = threading.RLock()
        self._has_recovered = False
        self._last_status_log: datetime | None = None
        self._last_mismatch_log: datetime | None = None
        self._last_recovered_order_guard_log: datetime | None = None
        self._last_hedge_time: datetime | None = None
        self._startup_waiting_logged = False
        self._exchange_to_client_id: dict[str, str] = {}
        self._initial_hedge_checked = False
        self._last_rebuy_attempted: bool = False
        self._last_rebuy_intent_created: bool = False
        self._last_tp_short_suppressed: bool = False
        self._last_priority_state_before: str | None = None
        self._last_priority_state_after: str | None = None
        self._post_rebuy_exit_target: float | None = None
        self._last_short_add_pre_spread_pct: float | None = None
        self.executor = OrderExecutor(
            config=self.config,
            logger=self.logger,
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            state_machine=self.state_machine,
            active_orders=self.active_orders,
            submitted_orders=self._submitted_orders,
            recent_orders=self._recent_orders,
            exchange_to_client_id=self._exchange_to_client_id,
            order_lock=self._order_lock,
            normalize_order_qty=self._normalize_order_qty,
            current_qty_step=self._current_qty_step,
            has_active_intent=self._has_active_intent,
            generate_client_order_id=self._generate_client_order_id,
            log_slippage_check=self._log_slippage_check,
            safe_update_order=self.safe_update_order,
            mark_order_filled=self.mark_order_filled,
            handle_order_finalized_locked=self._handle_order_finalized_locked,
            sync_positions_with_exchange=self.sync_positions_with_exchange,
            get_position_snapshot=self._get_position_snapshot,
            verify_order_on_exchange=self.verify_order_on_exchange,
            get_last_price=self._get_last_price,
            set_dca_steps=self._set_dca_steps,
            on_intent_executed=self._on_intent_executed,
            record_realized_pnl_by_side=self._record_realized_pnl_by_side,
        )
        self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
        self._startup_thread = threading.Thread(
            target=self._startup_rest_worker,
            daemon=True,
        )
        self._startup_thread.start()

    def stop(self) -> None:
        self._reconcile_stop.set()
        self._fast_poll_stop.set()
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=1)
        if self._fast_poll_thread and self._fast_poll_thread.is_alive():
            self._fast_poll_thread.join(timeout=1)

    def _is_order_stale(self, order: dict[str, Any], timeout_sec: float = 5.0) -> bool:
        created_at = order.get("created_at")
        return _elapsed_seconds_since(created_at) > timeout_sec

    def _record_realized_pnl_by_side(self, side: str, pnl: float) -> None:
        self.risk_manager.record_realized_pnl(pnl)

    def on_price_update(self, price: float) -> None:
        intents: list[OrderIntent] = []
        self.last_price = price
        if not self._is_initialized():
            return
        self._ensure_exchange_ready()
        blocking_recovered_orders = self._get_blocking_recovered_order_ids()
        if blocking_recovered_orders and self.state_machine.state != StrategyState.FAIL:
            if self._should_log_recovered_order_guard():
                self.logger.warning(
                    "Recovered exchange orders still active, pausing strategy tick",
                    extra={
                        "price": price,
                        "blocking_order_count": len(blocking_recovered_orders),
                        "blocking_order_ids": blocking_recovered_orders[:5],
                    },
                )
                self._last_recovered_order_guard_log = _utcnow()
            return
        self.logger.debug(
            "Price update",
            extra={"price": price, "state": self.state_machine.state.value},
        )

        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        if long_size <= 0 or short_size <= 0:
            self._post_rebuy_exit_target = None
            self.logger.warning("One side missing → pausing strategy")
            self._set_initialized(False)
            self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
            return

        state_before = self.state_machine.state.value
        hedge_spread = abs(self.calculate_hedge_spread())
        market_deviation = self.calculate_market_deviation(price)
        if hedge_spread < 0.005:
            self.dca_steps = 0
        state_intents = self.update_state(price, hedge_spread)
        self.logger.info(
            "TRACE on_price_update RECEIVED",
            extra={
                "count": len(state_intents) if state_intents else 0,
                "purposes": [getattr(i, "purpose", None) for i in (state_intents or [])],
            },
        )

        with self._position_sync_lock:
            total_notional = self.position_manager.total_notional()
        exposure_report = self.risk_manager.update_margins(total_notional)
        self.enforce_risk_limits(exposure_report)

        if self.state_machine.state == StrategyState.FAIL:
            exit_intents = [
                intent
                for intent in state_intents
                if intent.purpose in {"DD_EXIT", "EMERGENCY"}
            ]
            if exit_intents:
                self._execute_intents(exit_intents)
            else:
                self.logger.warning(
                    "FAIL state active, skipping non-exit intents for this tick",
                    extra={"price": price},
                )
            self.log_status(price, hedge_spread, exposure_report)
            return

        if state_intents:
            intents.extend(state_intents)
            self._execute_intents(intents)
            self.log_status(price, hedge_spread, exposure_report)
            return

        rebuy_attempted = False
        rebuy_intent_created = False
        if self.state_machine.is_recovery():
            hedge_recovery_intent = self.ensure_hedge_integrity(price)
            if hedge_recovery_intent:
                intents.append(hedge_recovery_intent)
            rebuy_attempted = True
            rebuy_intent = self.place_long_rebuy(price, market_deviation)
            if rebuy_intent:
                intents.append(rebuy_intent)
                rebuy_intent_created = True
        state_after = self.state_machine.state.value
        tp_short_suppressed = (
            self.state_machine.is_recovery() and rebuy_attempted
        )
        self._last_rebuy_attempted = rebuy_attempted
        self._last_rebuy_intent_created = rebuy_intent_created
        self._last_tp_short_suppressed = tp_short_suppressed
        self._last_priority_state_before = state_before
        self._last_priority_state_after = state_after
        if tp_short_suppressed:
            self.logger.info(
                "[PRIORITY FIX DEBUG] recovery_active=True rebuy_attempted=True "
                f"rebuy_intent_created={rebuy_intent_created} tp_short_suppressed=True"
            )
        else:
            self.logger.info(
                "[PRIORITY FIX DEBUG] recovery_active="
                f"{self.state_machine.is_recovery()} rebuy_attempted={rebuy_attempted} "
                f"rebuy_intent_created={rebuy_intent_created} tp_short_suppressed=False"
            )
        take_profit_intents = self.execute_take_profit(
            price, allow_tp_short=not tp_short_suppressed
        )
        if tp_short_suppressed:
            self.logger.info("[PRIORITY FIX DEBUG] tp_short suppressed this tick")
        intents.extend(take_profit_intents)
        self._execute_intents(intents)

        self.log_status(price, hedge_spread, exposure_report)

    def _get_last_price(self) -> float | None:
        return self.last_price

    def _set_dca_steps(self, value: int) -> None:
        self.dca_steps = value

    def _execute_intents(self, intents: list[OrderIntent]) -> None:
        pending_intents: deque[OrderIntent] = deque(intents)
        while pending_intents:
            intent = pending_intents.popleft()
            self.logger.info(
                "TRACE EXECUTE",
                extra={
                    "purpose": intent.purpose,
                    "side": intent.side,
                    "qty": intent.qty,
                },
            )
            self.executor.execute_intent(intent, enqueue_follow_ups=pending_intents.extend)

    @staticmethod
    def _should_follow_up_hedge_after_rebuy_fill(purpose: str | None) -> bool:
        return purpose in {"LONG_REBUY", "LONG_REBUY_HEDGE"}

    @staticmethod
    def _should_arm_cycle_after_hedge_ready(purpose: str | None) -> bool:
        return purpose == "HEDGE_RECOVER"

    @staticmethod
    def _should_prepare_next_rebuy_after_short_fill(purpose: str | None) -> bool:
        return purpose == "SHORT_REBALANCE"

    def _cancel_active_exit_orders(self) -> None:
        if not self.order_manager:
            return
        self.logger.info(
            "Refreshing exit orders: clearing previous TP/SL before re-placing",
            extra={"symbol": self.config.default_symbol, "category": self.config.category},
        )
        long_clear_result = self.order_manager.clear_long_take_profit(
            symbol=self.config.default_symbol,
            category=self.config.category,
        )
        short_clear_result = self.order_manager.clear_short_stop_loss(
            symbol=self.config.default_symbol,
            category=self.config.category,
        )
        self.logger.info(
            "Exit clear requests finished",
            extra={
                "symbol": self.config.default_symbol,
                "long_tp_cleared": bool(long_clear_result),
                "short_sl_cleared": bool(short_clear_result),
            },
        )
        with self._order_lock:
            candidates = [
                (
                    client_id,
                    order.get("exchange_order_id"),
                    order.get("purpose"),
                )
                for client_id, order in self.active_orders.items()
                if order.get("purpose") in {"TP_LONG", "TP_SHORT"}
                and order.get("status")
                in {"PENDING_SUBMIT", "OPEN", "UNKNOWN", "PARTIAL"}
                and order.get("exchange_order_id")
            ]
        for client_id, exchange_order_id, purpose in candidates:
            try:
                cancelled = self.order_manager.cancel_order(
                    exchange_order_id,
                    symbol=self.config.default_symbol,
                    category=self.config.category,
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to cancel stale exit order after rebuy fill",
                    extra={
                        "client_order_id": client_id,
                        "exchange_order_id": exchange_order_id,
                        "purpose": purpose,
                        "error": str(exc),
                    },
                )
                continue
            if not cancelled:
                self.logger.warning(
                    "Exchange declined stale exit order cancellation",
                    extra={
                        "client_order_id": client_id,
                        "exchange_order_id": exchange_order_id,
                        "purpose": purpose,
                    },
                )
                continue
            with self._order_lock:
                order = self.active_orders.get(client_id)
                if order:
                    order["status"] = "CANCELED"
                    self._handle_order_finalized_locked(client_id, order)
            self.logger.info(
                "Canceled stale exit order after rebuy fill",
                extra={
                    "client_order_id": client_id,
                    "exchange_order_id": exchange_order_id,
                    "purpose": purpose,
                },
            )

    def _calculate_hedge_break_even_price(self) -> float | None:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        net_long = long_size - short_size
        if (
            long_size <= 1e-9
            or short_size <= 1e-9
            or long_avg <= 0
            or short_avg <= 0
            or net_long <= 1e-9
        ):
            return None
        return ((long_size * long_avg) - (short_size * short_avg)) / net_long

    def _log_hedge_snapshot(
        self,
        event: str,
        *,
        reference_price: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        spread_gap = long_avg - short_avg if long_avg > 0 and short_avg > 0 else 0.0
        spread_pct = spread_gap / long_avg if long_avg > 0 else 0.0
        hedge_ratio = short_size / long_size if long_size > 0 else 0.0
        payload: dict[str, Any] = {
            "event": event,
            "symbol": self.config.default_symbol,
            "reference_price": reference_price,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "spread_gap": spread_gap,
            "spread_pct": spread_pct,
            "spread_pct_display": round(spread_pct * 100, 4),
            "hedge_ratio": hedge_ratio,
        }
        if extra:
            payload.update(extra)
        self.logger.info(
            "HEDGE SNAPSHOT event=%s symbol=%s ref=%s long_size=%.6f short_size=%.6f long_avg=%.8f short_avg=%.8f spread_gap=%.8f spread_pct=%.6f spread_pct_display=%.4f hedge_ratio=%.6f",
            event,
            self.config.default_symbol,
            f"{reference_price:.8f}" if reference_price is not None else "n/a",
            long_size,
            short_size,
            long_avg,
            short_avg,
            spread_gap,
            spread_pct,
            spread_pct * 100,
            hedge_ratio,
            extra=payload,
        )

    def _recalculate_post_rebuy_exit_target(self) -> float | None:
        break_even = self._calculate_hedge_break_even_price()
        if break_even is None or break_even <= 0:
            self._post_rebuy_exit_target = None
            return None
        target = break_even * (1 + self.config.long_percentage_tp)
        self._post_rebuy_exit_target = target
        self.logger.info(
            "Recalculated post-rebuy exit target",
            extra={
                "break_even_price": break_even,
                "target_exit_price": target,
                "tp_markup_pct": self.config.long_percentage_tp,
            },
        )
        return target

    def _set_cycle_profit_exit_orders(self) -> bool:
        if not self.order_manager:
            return False
        target = self._recalculate_post_rebuy_exit_target()
        if target is None or target <= 0:
            self.logger.warning("Skipping cycle exit order refresh: invalid break-even target")
            return False
        long_size, short_size, _, _ = self._get_position_snapshot()
        if long_size <= 1e-9 or short_size <= 1e-9:
            self.logger.warning(
                "Skipping cycle exit order refresh: hedge sizes unavailable",
                extra={"long_size": long_size, "short_size": short_size},
            )
            return False
        self._cancel_active_exit_orders()
        long_result = self.order_manager.set_long_take_profit(
            symbol=self.config.default_symbol,
            tp_price=target,
            position_size=long_size,
            category=self.config.category,
        )
        short_result = self.order_manager.set_short_stop_loss(
            symbol=self.config.default_symbol,
            sl_price=target,
            position_size=short_size,
            category=self.config.category,
        )
        success = bool(long_result) and bool(short_result)
        if success:
            self.logger.info(
                "Cycle profit exit orders refreshed",
                extra={
                    "symbol": self.config.default_symbol,
                    "target_exit_price": target,
                    "long_tp_price": target,
                    "short_sl_price": target,
                },
            )
        else:
            self.logger.warning(
                "Cycle profit exit refresh incomplete",
                extra={
                    "symbol": self.config.default_symbol,
                    "target_exit_price": target,
                    "long_tp_set": bool(long_result),
                    "short_sl_set": bool(short_result),
                },
            )
        return success

    def _submit_next_short_based_rebuy(self, reference_price: float) -> bool:
        if not self.state_machine.allow_new_long():
            return False
        rebuy_intent = self.place_long_rebuy(
            reference_price,
            self.calculate_market_deviation(reference_price),
        )
        if not rebuy_intent:
            return False
        self.logger.info(
            "Submitting short-anchored rebuy limit",
            extra={
                "reference_price": reference_price,
                "intent_price": rebuy_intent.price,
                "qty": rebuy_intent.qty,
                "purpose": rebuy_intent.purpose,
            },
        )
        self._execute_intents([rebuy_intent])
        return True

    def _arm_cycle_after_hedge_ready(self, reference_price: float, *, source: str) -> None:
        if not self.state_machine.allow_new_long():
            self.state_machine.transition(StrategyState.NORMAL)
        self.logger.info(
            "Hedge complete: setting exits first, then short-based rebuy",
            extra={
                "source": source,
                "reference_price": reference_price,
            },
        )
        self._log_hedge_snapshot(
            "hedge_ready_cycle_arm",
            reference_price=reference_price,
            extra={"source": source},
        )
        self._set_cycle_profit_exit_orders()
        self._submit_next_short_based_rebuy(reference_price)

    def _handle_post_fill_follow_up_hook(
        self, client_order_id: str, purpose: str | None, source: str
    ) -> None:
        if not (
            self._should_follow_up_hedge_after_rebuy_fill(purpose)
            or self._should_arm_cycle_after_hedge_ready(purpose)
            or self._should_prepare_next_rebuy_after_short_fill(purpose)
        ):
            return
        with self._order_lock:
            order = self.active_orders.get(client_order_id)
            if not order:
                return
            metadata = order.setdefault("metadata", {})
            if metadata.get("post_fill_follow_up_handled"):
                return
            metadata["post_fill_follow_up_handled"] = True
            reference_price = order.get("price")

        if not reference_price or reference_price <= 0:
            reference_price = self.last_price
        if not reference_price or reference_price <= 0:
            self.logger.warning(
                "Skipping fill-aware hedge follow-up: missing reference price",
                extra={
                    "client_order_id": client_order_id,
                    "purpose": purpose,
                    "source": source,
                },
            )
            return

        if self._should_arm_cycle_after_hedge_ready(purpose):
            self._arm_cycle_after_hedge_ready(reference_price, source=source)
            return

        if self._should_follow_up_hedge_after_rebuy_fill(purpose):
            long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
            pre_short_add_spread_pct = (
                (long_avg - short_avg) / long_avg
                if long_avg > 0 and short_avg > 0
                else 0.0
            )
            self._last_short_add_pre_spread_pct = pre_short_add_spread_pct
            self._log_hedge_snapshot(
                "post_long_rebuy_fill_before_short_add",
                reference_price=reference_price,
                extra={
                    "client_order_id": client_order_id,
                    "purpose": purpose,
                    "source": source,
                    "spread_before_short_add_pct": pre_short_add_spread_pct,
                    "spread_before_short_add_pct_display": round(
                        pre_short_add_spread_pct * 100, 4
                    ),
                },
            )
            hedge_intent = self.adjust_short_hedge(
                reference_price,
                spread=self.calculate_market_deviation(reference_price),
            )
            if hedge_intent:
                self.logger.info(
                    "Fill-aware hedge follow-up triggered",
                    extra={
                        "client_order_id": client_order_id,
                        "purpose": purpose,
                        "source": source,
                        "short_rebalance_qty": hedge_intent.qty,
                        "short_rebalance_price": hedge_intent.price,
                    },
                )
                self._execute_intents([hedge_intent])
                return

        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        post_short_add_spread_pct = (
            (long_avg - short_avg) / long_avg if long_avg > 0 and short_avg > 0 else 0.0
        )
        spread_before_short_add_pct = getattr(
            self, "_last_short_add_pre_spread_pct", None
        )
        spread_reduction_pct = (
            spread_before_short_add_pct - post_short_add_spread_pct
            if spread_before_short_add_pct is not None
            else None
        )
        self._log_hedge_snapshot(
            "post_short_rebalance_fill",
            reference_price=reference_price,
            extra={
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
                "spread_after_short_add_pct": post_short_add_spread_pct,
                "spread_after_short_add_pct_display": round(
                    post_short_add_spread_pct * 100, 4
                ),
                "spread_before_short_add_pct": spread_before_short_add_pct,
                "spread_before_short_add_pct_display": (
                    round(spread_before_short_add_pct * 100, 4)
                    if spread_before_short_add_pct is not None
                    else None
                ),
                "spread_reduction_pct": spread_reduction_pct,
                "spread_reduction_pct_display": (
                    round(spread_reduction_pct * 100, 4)
                    if spread_reduction_pct is not None
                    else None
                ),
            },
        )
        self.logger.info(
            "SPREAD CYCLE UPDATE before_short_add_pct=%s after_short_add_pct=%.6f after_short_add_pct_display=%.4f reduction_pct=%s reduction_pct_display=%s",
            (
                f"{spread_before_short_add_pct:.6f}"
                if spread_before_short_add_pct is not None
                else "n/a"
            ),
            post_short_add_spread_pct,
            post_short_add_spread_pct * 100,
            (
                f"{spread_reduction_pct:.6f}"
                if spread_reduction_pct is not None
                else "n/a"
            ),
            (
                f"{spread_reduction_pct * 100:.4f}"
                if spread_reduction_pct is not None
                else "n/a"
            ),
        )
        self._last_short_add_pre_spread_pct = None
        self.logger.info(
            "Short rebalance filled: setting next rebuy first, then refreshing exits",
            extra={
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
            },
        )
        # Keep the next rebuy resting in the book as early as possible. Exit
        # orders are refreshed immediately afterwards, and that refresh path
        # cancels the old exits before recalculating and setting the new ones.
        self._submit_next_short_based_rebuy(reference_price)
        self._set_cycle_profit_exit_orders()

    def _on_intent_executed(
        self, intent: OrderIntent, submit_price: float
    ) -> list[OrderIntent]:
        if intent.purpose in {"LONG_REBUY", "LONG_REBUY_HEDGE"}:
            self.logger.info(
                "DCA long order submitted",
                extra={"price": submit_price, "size": intent.qty},
            )
            self.dca_steps += 1
            self.last_rebuy_price = intent.price
            self.last_rebuy_time = _utcnow()
            return []

        if intent.purpose == "SHORT_REBALANCE":
            self.logger.info(
                "Hedge rebalance order submitted",
                extra={"requested_short_size": intent.qty, "price": submit_price},
            )
        elif intent.purpose == "HEDGE_RECOVER":
            self._last_hedge_time = _utcnow()
            self.logger.info(
                "Hedge recovery order submitted",
                extra={"requested_short_size": intent.qty, "price": submit_price},
            )
        return []

    def _build_full_exit_intents(self, price: float, purpose: str) -> list[OrderIntent]:
        long_size, short_size, _, _ = self._get_position_snapshot()
        intents: list[OrderIntent] = []
        if long_size > 1e-9:
            intents.append(
                OrderIntent(
                    side="long",
                    qty=long_size,
                    price=price,
                    purpose=purpose,
                )
            )
        if short_size > 1e-9:
            intents.append(
                OrderIntent(
                    side="short",
                    qty=short_size,
                    price=price,
                    purpose=purpose,
                )
            )
        return intents

    def calculate_spread(self, price: float) -> float:
        return self.calculate_hedge_spread()

    def calculate_hedge_spread(self) -> float:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        if long_avg <= 0 or short_avg <= 0:
            return 0.0
        mid = (long_avg + short_avg) / 2
        if mid <= 0:
            return 0.0
        return (short_avg - long_avg) / mid

    def calculate_market_deviation(self, price: float) -> float:
        _, _, long_avg, short_avg = self._get_position_snapshot()
        if long_avg <= 0 or short_avg <= 0:
            return 0.0
        if long_avg <= 0:
            return 0.0
        return (long_avg - price) / long_avg

    def place_long_rebuy(
        self, price: float, spread: float, allow_bypass_recovery_low: bool = False
    ) -> OrderIntent | None:
        def log_skip(reason: str, **extra: Any) -> None:
            self.logger.info(f"[REBUY DEBUG] skip: {reason}", extra=extra)

        if not self.state_machine.allow_new_long():
            log_skip("state not allow new long", state=self.state_machine.state.value)
            return None
        if price <= self.config.recovery_low and not allow_bypass_recovery_low:
            self.logger.debug("Price below pool boundary, skipping DCA")
            log_skip("price below recovery low", price=price, recovery_low=self.config.recovery_low)
            return None
        if not self.risk_manager.max_rebuys_allowed(self.dca_steps):
            self.logger.debug("Reached max DCA steps")
            log_skip("max rebuy reached", dca_steps=self.dca_steps)
            return None
        base_step = self.config.step_size_pct
        if base_step <= 0:
            log_skip("invalid base step", base_step=base_step)
            return None

        if (
            self.last_rebuy_time
            and _elapsed_seconds_since(self.last_rebuy_time)
            < self.config.min_rebuy_interval
        ):
            self.logger.debug("DCA cooldown in effect")
            log_skip("cooldown", cooldown=self.config.min_rebuy_interval)
            return None

        current_price = price

        seeded_rebuy_anchor = False
        if self.last_rebuy_price is None:
            self.last_rebuy_price = current_price
            seeded_rebuy_anchor = True

        adjust = False
        steps = 0
        max_steps = self.config.max_rebuy_loops
        with self._position_sync_lock:
            initial_long_size = self.position_manager.long_size
        max_total_multiplier = 3.0
        _, _, _, short_avg = self._get_position_snapshot()
        short_distance = 0.0
        if short_avg > 0:
            short_distance = (short_avg - current_price) / short_avg
        forced_rebuy = False
        if short_distance > self.config.max_short_deviation:
            self.logger.warning(
                "FORCED REBUY TRIGGERED",
                extra={
                    "short_distance": short_distance,
                    "threshold": self.config.max_short_deviation,
                    "price": current_price,
                    "short_avg": short_avg,
                },
            )
            forced_rebuy = True

        while self.last_rebuy_price is not None and steps < max_steps:
            with self._position_sync_lock:
                latest_price = self.last_price
            if latest_price is not None:
                current_price = latest_price
            if (
                not forced_rebuy
                and not seeded_rebuy_anchor
                and self._has_active_rebuy_order()
                and self.last_rebuy_price > 0
                and abs(current_price - self.last_rebuy_price) / self.last_rebuy_price < 0.001
            ):
                self.logger.info(
                    "[REBUY DEBUG] skip identical price",
                    extra={
                        "current_price": current_price,
                        "last_rebuy_price": self.last_rebuy_price,
                        "active_rebuy_order": True,
                        "dca_steps": self.dca_steps,
                    },
                )
                break
            loop_spread = self.calculate_market_deviation(current_price)
            with self._position_sync_lock:
                current_notional = self.position_manager.total_notional()
            if not self.risk_manager.check_exposure_limit(
                current_notional
            ):
                self.logger.info(
                    "[REBUY DEBUG] exposure limit",
                    extra={"current_notional": current_notional},
                )
                self.logger.warning("Stopping rebuy loop due to exposure limit")
                break
            _, _, long_avg, short_avg = self._get_position_snapshot()
            spread_gap = long_avg - short_avg
            if spread_gap <= 0:
                self.logger.info(
                    "[REBUY DEBUG] spread missing",
                    extra={
                        "short_avg": short_avg,
                        "long_avg": long_avg,
                        "spread_gap": spread_gap,
                    },
                )
                spread_gap = 0.0

            spread_pct = spread_gap / long_avg if long_avg > 0 else 0.0

            rebuy_distance_pct, info = self._calc_rebuy_distance_pct(spread_pct)
            next_rebuy_level = short_avg * (1 - rebuy_distance_pct)
            self.logger.info(
                "[REBUY DEBUG] distance calc",
                extra={
                    "current_price": current_price,
                    "loop_spread": loop_spread,
                    "spread_gap": spread_gap,
                    **info,
                    "next_rebuy_level": next_rebuy_level,
                    "last_rebuy_price": self.last_rebuy_price,
                },
            )
            if forced_rebuy and current_price < self.last_rebuy_price:
                next_rebuy_level = current_price
            else:
                forced_rebuy = False
            resting_limit_below_market = current_price >= next_rebuy_level
            if not forced_rebuy and resting_limit_below_market:
                self.logger.info(
                    "[REBUY DEBUG] staging resting rebuy limit",
                    extra={
                        "current_price": current_price,
                        "next_level": next_rebuy_level,
                        "spread_gap": spread_gap,
                    },
                )
                self.logger.info(
                    "Preparing short-anchored rebuy limit below market",
                    extra={
                        "current_price": current_price,
                        "next_level": next_rebuy_level,
                        "steps": steps,
                    },
                )

            slippage = (
                0.0
                if resting_limit_below_market
                else abs(current_price - next_rebuy_level) / next_rebuy_level
            )
            simplified_slippage = slippage * 100
            self.logger.info(
                "[REBUY DEBUG] slippage_check "
                f"current_price={current_price:.6f} "
                f"next_rebuy_level={next_rebuy_level:.6f} "
                f"slippage={slippage:.6f} "
                f"slippage_pct={simplified_slippage:.4f}% "
                f"max_slippage_pct={self.config.max_slippage_pct:.6f} "
                f"forced_rebuy={forced_rebuy} "
                f"slippage_ok={slippage <= self.config.max_slippage_pct}"
            )
            if not forced_rebuy and slippage > self.config.max_slippage_pct:
                self.logger.warning(
                    "Skipping rebuy due to slippage",
                    extra={
                        "slippage": slippage,
                        "price": current_price,
                        "next_rebuy_level": next_rebuy_level,
                        "forced_rebuy": forced_rebuy,
                    },
                )
                self.logger.info(
                    "[REBUY DEBUG] decision=SKIP_SLIPPAGE "
                    f"slippage={slippage:.6f} "
                    f"max_slippage_pct={self.config.max_slippage_pct:.6f} "
                    f"forced_rebuy={forced_rebuy}"
                )
                log_skip("slippage too high", slippage=slippage, max_slippage_pct=self.config.max_slippage_pct)
                break

            long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
            spread_gap_for_size = long_avg - short_avg
            spread_for_size = max(spread_gap_for_size / long_avg if long_avg > 0 else 0.0, 0.0)
            # Rebuy sizing is defined in quote notional terms (for example 70 USDT),
            # then converted into base-asset quantity at the target rebuy price.
            base_notional = self.config.long_entry_size
            if (
                initial_long_size > 0
                and long_size > initial_long_size * max_total_multiplier
            ):
                self.logger.warning(
                    "Scaling brake triggered",
                    extra={
                        "long_size": long_size,
                        "limit": initial_long_size * max_total_multiplier,
                    },
                )
                break
            size_multiplier = self._calc_rebuy_size_multiplier(spread_pct)
            raw_rebuy_notional = base_notional * size_multiplier * (1 + self.dca_steps * 0.5)
            self.logger.info(
                "[REBUY DEBUG] size calc",
                extra={
                    "base_long_notional": base_notional,
                    "size_multiplier": size_multiplier,
                    "dca_steps": self.dca_steps,
                    "raw_rebuy_notional_before_min_checks": raw_rebuy_notional,
                    "spread_pct": spread_pct,
                },
            )
            if next_rebuy_level <= 0:
                log_skip("invalid rebuy level", next_rebuy_level=next_rebuy_level)
                return None
            raw_fill_quantity = raw_rebuy_notional / next_rebuy_level
            min_qty_for_notional = self.config.min_order_value / next_rebuy_level
            qty_step = self._current_qty_step() or 0.0001
            min_qty_for_notional = math.ceil(min_qty_for_notional / qty_step) * qty_step
            raw_fill_quantity = max(raw_fill_quantity, min_qty_for_notional)
            max_rebuy_usdt = (
                self.config.max_rebuy_usdt if hasattr(self.config, "max_rebuy_usdt") else 50.0
            )
            if raw_fill_quantity * next_rebuy_level > max_rebuy_usdt:
                raw_fill_quantity = max_rebuy_usdt / next_rebuy_level

            with self._position_sync_lock:
                current_notional = self.position_manager.total_notional()
            current_long_notional = long_size * long_avg
            if spread_for_size < 0.03:
                max_rebuy_notional = current_long_notional * 0.25
            elif spread_for_size < 0.04:
                max_rebuy_notional = current_long_notional * 0.35
            else:
                max_rebuy_notional = current_long_notional * 0.5
            rebuy_notional = raw_fill_quantity * next_rebuy_level
            if rebuy_notional > max_rebuy_notional and next_rebuy_level > 0:
                raw_fill_quantity = max_rebuy_notional / next_rebuy_level
            fill_quantity = self._normalize_order_qty(raw_fill_quantity, "LONG_REBUY")
            if fill_quantity <= 0:
                self.logger.warning(
                    "Rebuy skipped after qty normalization",
                    extra={"requested_qty": raw_fill_quantity, "symbol": self.config.default_symbol},
                )
                steps += 1
                log_skip("qty normalization zero", requested_qty=raw_fill_quantity)
                return None
            projected_long_notional = long_size * long_avg + fill_quantity * next_rebuy_level
            if (
                self.config.max_total_notional
                and projected_long_notional > self.config.max_total_notional
            ):
                self.logger.warning(
                    "Rebuy skipped: would exceed max total notional",
                    extra={"projected_notional": projected_long_notional},
                )
                steps += 1
                log_skip("max total notional exceeded", projected_long_notional=projected_long_notional)
                return None

            intent_price = next_rebuy_level
            executed_price = min(next_rebuy_level, current_price)
            if not self._meets_min_order_value(executed_price, fill_quantity, "LONG_REBUY"):
                steps += 1
                log_skip("min order value", executed_price=executed_price, fill_quantity=fill_quantity)
                return None
            next_step = self.dca_steps + 1
            if spread_for_size < 0.03:
                adjust = True
            elif spread_for_size < 0.05:
                adjust = next_step % 2 == 0
            else:
                adjust = next_step % 3 == 0
            return OrderIntent(
                side="long",
                qty=fill_quantity,
                price=intent_price,
                purpose="LONG_REBUY_HEDGE" if adjust else "LONG_REBUY",
            )
        log_skip(
            "loop ended without order",
            steps=steps,
            last_rebuy_price=self.last_rebuy_price,
            current_price=current_price,
        )
        return None

    def _select_rebuy_divider(self, spread_pct: float) -> float:
        bands = getattr(self.config, "recovery_rebuy_bands", None)
        if bands:
            for i, band in enumerate(bands):
                is_last = i == len(bands) - 1
                if band.min_spread <= spread_pct and (
                    spread_pct < band.max_spread or is_last
                ):
                    return band.divider if band.divider > 0 else 3.0
        return 3.0

    def _calc_rebuy_distance_pct(self, spread_pct: float) -> tuple[float, dict[str, float]]:
        base_step = (
            self.config.recovery_base_step_pct
            if self.config.recovery_base_step_pct is not None
            else self.config.step_size_pct
        )
        max_distance_pct = (
            self.config.recovery_max_rebuy_distance_pct
            if self.config.recovery_max_rebuy_distance_pct is not None
            else 0.04
        )

        if spread_pct > 0:
            divider = self._select_rebuy_divider(spread_pct)
            rebuy_distance_pct = spread_pct / divider
        else:
            divider = 0.0
            rebuy_distance_pct = base_step

        rebuy_distance_pct = max(base_step, rebuy_distance_pct)
        rebuy_distance_pct = min(max_distance_pct, rebuy_distance_pct)

        info = {
            "spread_pct": spread_pct,
            "divider": divider,
            "base_step": base_step,
            "max_distance_pct": max_distance_pct,
            "rebuy_distance_pct": rebuy_distance_pct,
        }
        return rebuy_distance_pct, info

    def _calc_rebuy_size_multiplier(self, spread_pct: float) -> float:
        threshold = self.config.user.spread_threshold
        base = self.config.user.rebuy_size_multiplier_base
        increment = self.config.user.rebuy_size_multiplier_increment
        span = self.config.user.rebuy_size_multiplier_span

        if span <= 0:
            return base
        extra_spread = max(0.0, spread_pct - threshold)
        steps = math.floor((extra_spread + 1e-9) / span)
        return base + steps * increment

    def adjust_short_hedge(
        self,
        price: float,
        long_size_override: float | None = None,
        spread: float = 0.0,
    ) -> OrderIntent | None:
        snapshot_long, short_size, _, _ = self._get_position_snapshot()
        long_size = long_size_override if long_size_override is not None else snapshot_long
        if long_size <= 0:
            self.logger.info(
                "[SHORT ADD DEBUG] skip: no long position",
                extra={"price": price, "snapshot_long": snapshot_long},
            )
            return None
        current_ratio = short_size / long_size if long_size > 0 else 0.0
        hedge_ratio = 0.5
        if abs(current_ratio - hedge_ratio) < 0.05:
            self.logger.info(
                "[SHORT ADD DEBUG] skip: hedge ratio already aligned",
                extra={
                    "price": price,
                    "current_ratio": current_ratio,
                    "target_ratio": hedge_ratio,
                    "spread": spread,
                },
            )
            return None
        target_short = long_size * hedge_ratio
        current_short = short_size
        if current_short >= target_short:
            self.logger.info(
                "[SHORT ADD DEBUG] skip: current short already at/above target",
                extra={
                    "price": price,
                    "current_short": current_short,
                    "target_short": target_short,
                    "current_ratio": current_ratio,
                    "spread": spread,
                },
            )
            return None
        short_gap = target_short - current_short
        self.logger.info(
            "[SHORT ADD DEBUG] rebalance calculation",
            extra={
                "price": price,
                "long_size": long_size,
                "short_size": short_size,
                "current_ratio": current_ratio,
                "target_ratio": hedge_ratio,
                "target_short": target_short,
                "short_gap": short_gap,
                "market_deviation_spread": spread,
            },
        )

        raw_fill_quantity = short_gap
        fill_quantity = self._normalize_order_qty(raw_fill_quantity, "SHORT_REBALANCE")
        if fill_quantity <= 0:
            self.logger.warning(
                "Short rebalance skipped after qty normalization",
                extra={"requested_qty": raw_fill_quantity, "symbol": self.config.default_symbol},
            )
            return None
        if not self._meets_min_order_value(price, fill_quantity, "SHORT_REBALANCE"):
            self.logger.info(
                "[SHORT ADD DEBUG] skip: below minimum notional",
                extra={
                    "price": price,
                    "fill_quantity": fill_quantity,
                    "notional": price * fill_quantity if price > 0 else 0.0,
                    "min_notional": self.config.min_order_value,
                },
            )
            return None
        self.logger.info(
            "[SHORT ADD DEBUG] submitting short rebalance",
            extra={
                "price": price,
                "fill_quantity": fill_quantity,
                "notional": price * fill_quantity if price > 0 else 0.0,
                "current_ratio": current_ratio,
                "target_ratio": hedge_ratio,
            },
        )
        return OrderIntent(
            side="short",
            qty=fill_quantity,
            price=price,
            purpose="SHORT_REBALANCE",
            order_type="Market",
        )

    def calculate_take_profits(self) -> tuple[float, float]:
        _, _, long_avg, short_avg = self._get_position_snapshot()
        return (
            short_avg * (1 + self.config.tp_short_pct),
            long_avg * (1 + self.config.tp_long_pct),
        )

    def execute_take_profit(self, price: float, allow_tp_short: bool = True) -> list[OrderIntent]:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        intents: list[OrderIntent] = []

        long_notional = long_size * long_avg
        target_profit = long_notional * self.config.long_percentage_tp
        long_pnl = (price - long_avg) * long_size
        short_pnl = (short_avg - price) * short_size
        combined_pnl = long_pnl + short_pnl

        if (
            long_size > 1e-9
            and short_size > 1e-9
            and target_profit > 0
            and combined_pnl >= target_profit
        ):
            intents.append(
                OrderIntent(
                    side="short",
                    qty=short_size,
                    price=price,
                    purpose="TP_SHORT",
                )
            )
            intents.append(
                OrderIntent(
                    side="long",
                    qty=long_size,
                    price=price,
                    purpose="TP_LONG",
                )
            )
            return intents

        short_tp, long_tp = self.calculate_take_profits()
        short_open = short_size > 1e-9
        both_hedged = (
            long_size > 1e-9
            and short_size > 1e-9
            and target_profit > 0
        )
        if (
            both_hedged
            and self._post_rebuy_exit_target is not None
            and price >= self._post_rebuy_exit_target
        ):
            self.logger.info(
                "Post-rebuy exit target reached",
                extra={
                    "price": price,
                    "target_exit_price": self._post_rebuy_exit_target,
                    "long_size": long_size,
                    "short_size": short_size,
                    "long_avg": long_avg,
                    "short_avg": short_avg,
                },
            )
            self._post_rebuy_exit_target = None
            intents.append(
                OrderIntent(
                    side="short",
                    qty=short_size,
                    price=price,
                    purpose="TP_SHORT",
                )
            )
            intents.append(
                OrderIntent(
                    side="long",
                    qty=long_size,
                    price=price,
                    purpose="TP_LONG",
                )
            )
            return intents
        if allow_tp_short and short_open and price >= short_tp:
            if both_hedged:
                return intents
            intents.append(
                OrderIntent(
                    side="short",
                    qty=short_size,
                    price=price,
                    purpose="TP_SHORT",
                )
            )
        if long_size > 1e-9 and price >= long_tp:
            if both_hedged:
                return intents
            intents.append(
                OrderIntent(
                    side="long",
                    qty=long_size,
                    price=price,
                    purpose="TP_LONG",
                )
            )
        return intents

    def update_state(self, price: float, spread: float) -> list[OrderIntent]:
        self.logger.info(
            "TRACE ENTER update_state",
            extra={"price": price},
        )
        fail_threshold = self.config.recovery_low * (1 - self.config.pool_fail_buffer)
        if self.risk_manager.realized_pnl < -self.config.max_drawdown:
            self.logger.critical(
                "MAX DRAWDOWN HIT",
                extra={"price": price, "realized_pnl": self.risk_manager.realized_pnl},
            )
            self.state_machine.transition(StrategyState.FAIL)
            return self._build_full_exit_intents(price, "DD_EXIT")
        if spread > 0.10:
            self.logger.critical("EMERGENCY EXIT", extra={"price": price, "spread": spread})
            self.state_machine.transition(StrategyState.FAIL)
            return self._build_full_exit_intents(price, "EMERGENCY")
        if price > self.config.recovery_low and self.state_machine.state == StrategyState.POOL_EDGE:
            # Pool-edge is purely an informational boundary state; when price
            # recovers above recovery_low, we clear any manual extend request flag.
            self._extend_requested = False

        fail_due_to_low_price = False
        if price <= self.config.recovery_low:
            self._transition_state_if_needed(
                StrategyState.POOL_EDGE,
                "price touched pool boundary",
                price,
                spread,
            )
            if price <= fail_threshold:
                fail_due_to_low_price = True
        if spread > self.config.spread_threshold:
            self._transition_state_if_needed(
                StrategyState.RECOVERY,
                "spread exceeded threshold",
                price,
                spread,
            )
            return []
        if self.state_machine.state in {StrategyState.RECOVERY, StrategyState.EXTEND}:
            # extend_trigger_pct simply widens the recovery exit check boundary
            # when in RECOVERY/EXTEND states; it does not change how rebuy orders
            # are calculated.
            price_recovered = price > self.config.recovery_low * (
                1 + self.config.extend_trigger_pct
            )
            long_size, short_size, _, _ = self._get_position_snapshot()
            current_ratio = short_size / long_size if long_size > 0 else 0.0
            spread_ok = spread <= self.config.recovery_exit_spread_threshold
            ratio_ok = (
                current_ratio
                >= self.config.short_ratio - self.config.recovery_ratio_tolerance
            )
            if price_recovered or (spread_ok and ratio_ok):
                self._transition_state_if_needed(
                    StrategyState.NORMAL,
                    "price/spread/ratio satisfied recovery exit",
                    price,
                    spread,
                )
        if fail_due_to_low_price:
            self._transition_state_if_needed(
                StrategyState.FAIL,
                "price breached fail buffer",
                price,
                spread,
            )
        return []


    def _log_state_transition(
        self,
        new_state: StrategyState,
        reason: str,
        price: float,
        spread: float,
    ) -> None:
        self.logger.info(
            "State transition",
            extra={
                "state": new_state.value,
                "reason": reason,
                "price": price,
                "spread": spread,
            },
        )

    def check_pool_boundary(self, price: float) -> bool:
        return price > self.config.recovery_low

    def enforce_risk_limits(self, report: ExposureReport) -> None:
        self.logger.debug(
            "Risk report",
            extra={
                "used_margin": report.used_margin,
                "free_margin": report.free_margin,
                "exposure_pct": report.exposure_pct,
            },
        )
        if (
            self.config.max_total_notional
            and report.total_notional > self.config.max_total_notional
        ):
            self.state_machine.transition(StrategyState.FAIL)
            self.logger.error(
                "Max total notional exceeded", extra={"notional": report.total_notional}
            )
            return
        if report.free_margin <= 0 or not self.risk_manager.check_exposure_limit(report.total_notional):
            self.state_machine.transition(StrategyState.FAIL)
            self.logger.warning("Risk limits breached, moving to FAIL")

    def log_status(
        self, price: float, spread: float, exposure_report: ExposureReport
    ) -> None:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        if not self._should_log_status():
            return
        rebuy_status: dict[str, Any] = {}
        with self._order_lock:
            for client_id, order in self.active_orders.items():
                if (
                    order.get("purpose") in {"LONG_REBUY", "LONG_REBUY_HEDGE"}
                    and order.get("status") in {"PENDING_SUBMIT", "OPEN", "PARTIAL", "UNKNOWN"}
                ):
                    rebuy_status = {
                        "rebuy_order_active": True,
                        "rebuy_client_order_id": client_id,
                        "rebuy_order_status": order.get("status"),
                        "rebuy_order_price": order.get("price"),
                        "rebuy_order_qty": order.get("size"),
                        "rebuy_order_purpose": order.get("purpose"),
                    }
                    break
        if not rebuy_status:
            rebuy_status = {"rebuy_order_active": False}
        self.logger.info(
            "Status",
            extra={
                "state": self.state_machine.state.value,
                "price": price,
                "spread": spread,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "long_size": long_size,
                "short_size": short_size,
                "exposure_pct": exposure_report.exposure_pct,
                "free_margin": exposure_report.free_margin,
                **rebuy_status,
            },
        )
        self._last_status_log = _utcnow()

    def _has_active_rebuy_order(self) -> bool:
        with self._order_lock:
            return any(
                order.get("purpose") in {"LONG_REBUY", "LONG_REBUY_HEDGE"}
                and order.get("status") in {"PENDING_SUBMIT", "OPEN", "PARTIAL", "UNKNOWN"}
                for order in self.active_orders.values()
            )

    def record_order(self, side: str, size: float, price: float) -> None:
        order = Order(side=side, size=size, price=price, filled_size=size, timestamp=_utcnow())
        self.orders.append(order)

    def _ensure_exchange_ready(self) -> None:
        if not self.order_manager:
            return
        with self._exchange_lock:
            if self._exchange_ready:
                return

            self.logger.info(
                "Preparing exchange",
                extra={
                    "symbol": self.config.default_symbol,
                    "category": self.config.category,
                    "leverage": self.config.safe_leverage(),
                },
            )
            self.order_manager.ensure_hedge_mode(self.config.default_symbol, self.config.category)
            self.order_manager.ensure_max_leverage(
                self.config.default_symbol, self.config.category
            )
            self._exchange_ready = True
        self._recover_state_from_exchange()

    def _recover_state_from_exchange(self) -> None:
        if not self.order_manager:
            return
        with self._recovery_lock:
            if self._has_recovered:
                return

            self.sync_orders_with_exchange(event_source="startup")
            self.sync_positions_with_exchange()

            self._has_recovered = True

            self._start_reconciliation_loop()

    def _startup_rest_worker(self) -> None:
        while not self._is_initialized():
            try:
                if not self.order_manager:
                    self.logger.error("Startup REST error", extra={"error": "order_manager_missing"})
                    time.sleep(2)
                    continue

                positions = self.order_manager.fetch_positions(
                    None,
                    self.config.category,
                    self._default_settle_coin(),
                )
                detected_symbol = self._find_active_symbol_from_positions(positions)
                if detected_symbol:
                    if self.config.default_symbol != detected_symbol:
                        self.logger.info(
                            "REST startup: active position symbol detected",
                            extra={
                                "configured_symbol": self.config.default_symbol,
                                "active_symbol": detected_symbol,
                            },
                        )
                        self.config.default_symbol = detected_symbol
                    self.order_manager.ensure_max_leverage(
                        detected_symbol, self.config.category
                    )
                active_symbol, long_size, short_size, long_avg = (
                    self._find_active_hedge_symbol(positions)
                )

                if active_symbol:
                    startup_intents: list[OrderIntent] = []
                    if self.config.default_symbol != active_symbol:
                        self.logger.info(
                            "REST startup: active symbol detected",
                            extra={
                                "configured_symbol": self.config.default_symbol,
                                "active_symbol": active_symbol,
                            },
                        )
                        self.config.default_symbol = active_symbol
                    self._ensure_exchange_ready()
                    self.sync_positions_with_exchange()
                    self._initialize_recovery_low_from_positions()
                    blocking_recovered_orders = self._get_blocking_recovered_order_ids()
                    if blocking_recovered_orders:
                        self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
                        if self._should_log_recovered_order_guard():
                            self.logger.warning(
                                "REST startup: recovered exchange orders still active, delaying strategy start",
                                extra={
                                    "symbol": active_symbol,
                                    "blocking_order_count": len(blocking_recovered_orders),
                                    "blocking_order_ids": blocking_recovered_orders[:5],
                                },
                            )
                            self._last_recovered_order_guard_log = _utcnow()
                        time.sleep(2)
                        continue
                    hedge_recovery_intent = self.ensure_hedge_integrity(self.last_price)
                    if hedge_recovery_intent:
                        startup_intents.append(hedge_recovery_intent)
                    self._execute_intents(startup_intents)
                    self._set_initialized(True)
                    with self._position_sync_lock:
                        self.last_rebuy_price = self.position_manager.long_avg
                    self.state_machine.transition(StrategyState.NORMAL)
                    _, _, _, short_avg = self._get_position_snapshot()
                    reference_price = short_avg if short_avg > 0 else (self.last_price or long_avg)
                    if reference_price and reference_price > 0:
                        self._arm_cycle_after_hedge_ready(
                            reference_price,
                            source="startup_hedge_detected",
                        )

                    self.logger.info(
                        "REST startup: both positions detected → strategy started",
                        extra={
                            "symbol": active_symbol,
                            "long_size": long_size,
                            "short_size": short_size,
                        },
                    )
                    self._startup_waiting_logged = False
                    return

                self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
                if not self._startup_waiting_logged:
                    self.logger.info(
                        "REST startup: waiting for active hedged symbol",
                        extra={
                            "long_size": long_size,
                            "short_size": short_size,
                        },
                    )
                    self._startup_waiting_logged = True

            except Exception as exc:
                self.logger.error("Startup REST error", extra={"error": str(exc)})

            time.sleep(2)

    def _start_reconciliation_loop(self) -> None:
        with self._recovery_lock:
            if self._reconcile_thread and self._reconcile_thread.is_alive():
                return

            self._reconcile_thread = threading.Thread(
                target=self._reconciliation_worker, daemon=True
            )
            self._reconcile_thread.start()

            if not self._fast_poll_thread or not self._fast_poll_thread.is_alive():
                self._fast_poll_thread = threading.Thread(
                    target=self._fast_poll_worker, daemon=True
                )
                self._fast_poll_thread.start()

    def _reconciliation_worker(self) -> None:
        while not self._reconcile_stop.wait(self.config.order_sync_interval_seconds):
            self.sync_orders_with_exchange(event_source="reconcile")
            self.sync_positions_with_exchange()

    def _get_position_snapshot(self) -> tuple[float, float, float, float]:
        with self._position_sync_lock:
            return (
                self.position_manager.long_size,
                self.position_manager.short_size,
                self.position_manager.long_avg,
                self.position_manager.short_avg,
            )

    def _is_initialized(self) -> bool:
        with self._init_lock:
            return self.initialized

    def _set_initialized(self, value: bool) -> None:
        with self._init_lock:
            self.initialized = value

    def _default_settle_coin(self) -> str | None:
        symbol = self.config.default_symbol.upper()
        for suffix in ("USDT", "USDC", "USD"):
            if symbol.endswith(suffix):
                return suffix
        return None

    def _find_active_symbol_from_positions(
        self, positions: list[Mapping[str, Any]]
    ) -> str | None:
        first_any_symbol: str | None = None
        for pos in positions:
            symbol = (pos.get("symbol") or "").upper()
            if not symbol:
                continue
            side = (pos.get("side") or pos.get("positionSide") or "").lower()
            size = float(pos.get("size") or pos.get("positionQty") or 0.0)
            if size <= 0:
                continue
            if first_any_symbol is None:
                first_any_symbol = symbol
            if side in {"buy", "long"}:
                return symbol
        return first_any_symbol

    def _find_active_hedge_symbol(
        self, positions: list[Mapping[str, Any]]
    ) -> tuple[str | None, float, float, float]:
        symbols: dict[str, dict[str, float]] = {}
        for pos in positions:
            symbol = (pos.get("symbol") or "").upper()
            if not symbol:
                continue
            side = (pos.get("side") or pos.get("positionSide") or "").lower()
            size = float(pos.get("size") or pos.get("positionQty") or 0.0)
            avg_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
            if size <= 0:
                continue
            bucket = symbols.setdefault(
                symbol,
                {"long_size": 0.0, "short_size": 0.0, "long_avg": 0.0},
            )
            if side in {"buy", "long"}:
                bucket["long_size"] = size
                bucket["long_avg"] = avg_price
            elif side in {"sell", "short"}:
                bucket["short_size"] = size

        for symbol, snapshot in symbols.items():
            if snapshot["long_size"] > 0 and snapshot["short_size"] > 0:
                return (
                    symbol,
                    snapshot["long_size"],
                    snapshot["short_size"],
                    snapshot["long_avg"],
                )

        return None, 0.0, 0.0, 0.0

    def _initialize_recovery_low_from_positions(self) -> None:
        _, _, long_avg, short_avg = self._get_position_snapshot()
        reference_price = min(
            value for value in (long_avg, short_avg) if value > 0
        ) if any(value > 0 for value in (long_avg, short_avg)) else 0.0
        if reference_price <= 0:
            return
        if 0 < self.config.recovery_low < reference_price:
            return
        buffer_pct = max(
            self.config.short_entry_buffer * 2,
            self.config.step_size_pct * 3,
            self.config.spread_threshold * 2,
        )
        new_recovery_low = reference_price * (1 - buffer_pct)
        self.logger.info(
            "Adjusted recovery low for active symbol",
            extra={
                "symbol": self.config.default_symbol,
                "old_recovery_low": self.config.recovery_low,
                "new_recovery_low": new_recovery_low,
                "reference_price": reference_price,
            },
        )
        self.config.recovery_low = new_recovery_low

    def ensure_hedge_integrity(self, current_price: float | None = None) -> OrderIntent | None:
        if not self.order_manager:
            return None
        long_size, short_size, _, _ = self._get_position_snapshot()
        if long_size <= 0:
            return None
        price = current_price or self.last_price
        if not price or price <= 0:
            self.logger.warning(
                "Hedge check skipped: missing or invalid price",
                extra={"symbol": self.config.default_symbol, "price": price},
            )
            return None
        if (
            self._last_hedge_time
            and _elapsed_seconds_since(self._last_hedge_time) < 5
        ):
            return None
        hedge_ratio = 0.5
        target_short = long_size * hedge_ratio
        if short_size + 1e-9 >= target_short:
            return None
        missing_short = target_short - short_size
        if missing_short <= 0:
            return None
        qty = self._normalize_order_qty(missing_short, "HEDGE_RECOVER")
        if qty <= 0:
            self.logger.warning(
                "Hedge qty normalized to zero",
                extra={"symbol": self.config.default_symbol, "missing_short": missing_short},
            )
            return None
        qty_step = self._current_qty_step()
        qty = adjust_qty_to_min_notional(
            qty,
            price,
            self.config.min_order_value,
            qty_step,
        )
        notional = qty * price
        if notional < self.config.min_order_value:
            self.logger.warning(
                "[HEDGE BLOCKED] below min notional even after adjustment",
                extra={
                    "symbol": self.config.default_symbol,
                    "qty": qty,
                    "price": price,
                    "notional": notional,
                    "min_notional": self.config.min_order_value,
                },
            )
            return None
        self.logger.info(
            "Enforcing hedge integrity (market)",
            extra={"symbol": self.config.default_symbol, "qty": qty, "price": price},
        )
        return OrderIntent(
            side="short",
            qty=qty,
            price=price,
            purpose="HEDGE_RECOVER",
        )

    def _transition_state_if_needed(
        self,
        new_state: StrategyState,
        reason: str,
        price: float,
        spread: float,
    ) -> bool:
        if self.state_machine.state == new_state:
            return False
        self.state_machine.transition(new_state)
        self._log_state_transition(new_state, reason, price, spread)
        return True

    def _normalize_order_qty(self, qty: float, purpose: str) -> float:
        if qty <= 0 or not self.order_manager:
            return qty
        normalized_qty = self.order_manager.normalize_qty(
            self.config.default_symbol,
            qty,
            self.config.category,
        )
        if normalized_qty <= 0:
            self.logger.warning(
                "Order qty normalization returned zero",
                extra={
                    "symbol": self.config.default_symbol,
                    "purpose": purpose,
                    "requested_qty": qty,
                },
            )
            return 0.0
        if abs(normalized_qty - qty) > 1e-9:
            self.logger.info(
                "Order qty normalized",
                extra={
                    "symbol": self.config.default_symbol,
                    "purpose": purpose,
                    "requested_qty": qty,
                    "normalized_qty": normalized_qty,
                },
            )
        return normalized_qty

    def _should_log_status(self) -> bool:
        interval = self.config.status_log_interval_seconds
        if interval <= 0:
            return True
        now = _utcnow()
        if not self._last_status_log:
            return True
        return (now - self._last_status_log).total_seconds() >= interval

    def _should_log_mismatch(self) -> bool:
        interval = self.config.status_log_interval_seconds
        if interval <= 0:
            return True
        now = _utcnow()
        if not self._last_mismatch_log:
            return True
        return (now - self._last_mismatch_log).total_seconds() >= interval

    def _should_log_recovered_order_guard(self) -> bool:
        interval = self.config.status_log_interval_seconds
        if interval <= 0:
            return True
        now = _utcnow()
        last_log = getattr(self, "_last_recovered_order_guard_log", None)
        if not last_log:
            return True
        return (now - last_log).total_seconds() >= interval

    def _get_blocking_recovered_order_ids(self) -> list[str]:
        blocking_statuses = {"PENDING_SUBMIT", "OPEN", "PARTIAL", "UNKNOWN"}
        with self._order_lock:
            return [
                client_id
                for client_id, order in self.active_orders.items()
                if (order.get("metadata") or {}).get("recovered_from_exchange")
                and order.get("status") in blocking_statuses
            ]

    def _meets_min_order_value(
        self, price: float, qty: float, purpose: str
    ) -> bool:
        if self.config.min_order_value <= 0 or price <= 0 or qty <= 0:
            return True
        notional = price * qty
        if notional >= self.config.min_order_value:
            return True
        self.logger.warning(
            "Order skipped: below minimum notional",
            extra={
                "purpose": purpose,
                "symbol": self.config.default_symbol,
                "price": price,
                "qty": qty,
                "notional": notional,
                "min_notional": self.config.min_order_value,
            },
        )
        return False

    def _current_qty_step(self) -> float:
        if not self.order_manager:
            return 0.0
        info = self.order_manager.fetch_instrument_info(
            self.config.default_symbol, self.config.category
        )
        lot_filter = (info or {}).get("lotSizeFilter") or {}
        step = lot_filter.get("qtyStep") or lot_filter.get("step") or "0"
        try:
            return float(step)
        except (TypeError, ValueError):
            return 0.0

    def _fast_poll_worker(self) -> None:
        while not self._fast_poll_stop.wait(self.config.fast_poll_interval_seconds):
            if not self._has_recovered:
                time.sleep(0.05)
                continue
            self._check_recent_orders()
            self._cleanup_pending_orders()

    def _check_recent_orders(self) -> None:
        if not self.order_manager:
            return
        with self._order_lock:
            client_ids = list(self._recent_orders)
        for client_id in client_ids:
            with self._order_lock:
                order = self.active_orders.get(client_id)
            if not order:
                with self._order_lock:
                    try:
                        self._recent_orders.remove(client_id)
                    except ValueError:
                        pass
                continue
            if order["status"] in {"FILLED", "FILLED_HANDLED", "CANCELED"}:
                with self._order_lock:
                    try:
                        self._recent_orders.remove(client_id)
                    except ValueError:
                        pass
                continue
            self.verify_order_on_exchange(
                client_id,
                source="fast_poll",
                retries=1,
                delay=0.1,
                log_missing=False,
            )

    def _cleanup_pending_orders(self) -> None:
        now = _utcnow()
        with self._order_lock:
            pending = [
                cid
                for cid, order in self.active_orders.items()
                if order["status"] == "PENDING_SUBMIT"
                and (now - order["created_at"]).total_seconds() > 5
                and order.get("purpose") not in {"LONG_REBUY", "LONG_REBUY_HEDGE"}
            ]
        for client_id in pending:
            verified = self.verify_order_on_exchange(
                client_id,
                source="pending_cleanup",
                retries=1,
                delay=0.1,
                log_missing=True,
            )
            if verified:
                continue
            with self._order_lock:
                order = self.active_orders.get(client_id)
                if not order or order["status"] != "PENDING_SUBMIT":
                    continue
                metadata = order.setdefault("metadata", {})
                metadata["pending_cleanup_checked_at"] = now.isoformat()
                metadata["pending_cleanup_result"] = "unverified_pending_submit"
                order["status"] = "UNKNOWN"
                order["updated_at"] = now
            self.logger.critical(
                "ORDER STUCK IN PENDING_SUBMIT → MARKED UNKNOWN",
                extra={"client_order_id": client_id},
            )

    @staticmethod
    def _recover_purpose_from_client_order_id(client_id: str) -> str:
        if client_id.startswith("psrh-"):
            parts = client_id.split("-")
            if len(parts) >= 4:
                purpose_parts = parts[1:-2]
                if purpose_parts:
                    return "_".join(part.upper() for part in purpose_parts)
        return "RECOVERED_OPEN_ORDER"

    def _hydrate_exchange_only_orders(
        self,
        exchange_map: Mapping[str, Mapping[str, Any]],
        now: datetime,
        event_source: str,
    ) -> None:
        with self._order_lock:
            for client_id, exchange_order in exchange_map.items():
                if client_id in self.active_orders:
                    continue
                if not client_id.startswith("psrh-"):
                    continue
                position_idx = int(exchange_order.get("positionIdx") or 0)
                if position_idx == 1:
                    local_side = "long"
                elif position_idx == 2:
                    local_side = "short"
                else:
                    exchange_side = (exchange_order.get("side") or "").lower()
                    local_side = "long" if exchange_side == "buy" else "short"
                raw_qty = float(
                    exchange_order.get("qty")
                    or exchange_order.get("orderQty")
                    or 0.0
                )
                if raw_qty <= 0:
                    continue
                price = float(
                    exchange_order.get("price")
                    or exchange_order.get("avgPrice")
                    or exchange_order.get("triggerPrice")
                    or self.last_price
                    or 0.0
                )
                status = exchange_order.get("orderStatus") or "New"
                recovered_order = {
                    "side": local_side,
                    "purpose": self._recover_purpose_from_client_order_id(client_id),
                    "price": price,
                    "size": raw_qty,
                    "qty": raw_qty,
                    "status": "PARTIAL" if status == "PartiallyFilled" else "OPEN",
                    "created_at": now,
                    "updated_at": now,
                    "verify_attempts": 0,
                    "remaining_qty": max(
                        raw_qty - float(exchange_order.get("cumExecQty") or 0.0),
                        0.0,
                    ),
                    "partial_handled": status == "PartiallyFilled",
                    "exchange_confirmed": True,
                    "exchange_order_id": exchange_order.get("orderId"),
                    "filled_qty": float(exchange_order.get("cumExecQty") or 0.0),
                    "retry_count": 0,
                    "metadata": {
                        "recovered_from_exchange": True,
                        "recovery_source": event_source,
                        "exchange_status": status,
                        "order_type": exchange_order.get("orderType") or "Limit",
                        "reduce_only": bool(exchange_order.get("reduceOnly") or False),
                    },
                }
                self.active_orders[client_id] = recovered_order
                self._submitted_orders.add(
                    (
                        recovered_order["side"],
                        round(recovered_order["size"], 4),
                        recovered_order["purpose"],
                    )
                )
                if client_id not in self._recent_orders:
                    self._recent_orders.append(client_id)
                exchange_id = recovered_order.get("exchange_order_id")
                if exchange_id:
                    self._exchange_to_client_id[exchange_id] = client_id
                self.logger.warning(
                    "[ORDER RECOVERED FROM EXCHANGE]",
                    extra={
                        "client_order_id": client_id,
                        "purpose": recovered_order["purpose"],
                        "side": recovered_order["side"],
                        "size": recovered_order["size"],
                        "price": recovered_order["price"],
                        "source": event_source,
                    },
                )

    def sync_orders_with_exchange(self, event_source: str = "reconcile") -> None:
        if not self.order_manager:
            return
        orders = self.order_manager.fetch_open_orders(
            self.config.default_symbol, self.config.category
        )
        if orders is None:
            self.logger.warning(
                "[ORDER SYNC SKIPPED] open-order snapshot fetch failed",
                extra={"source": event_source},
            )
            return
        exchange_map: Dict[str, Mapping[str, Any]] = {}
        for exchange_order in orders:
            client_id = (
                exchange_order.get("orderLinkId")
                or exchange_order.get("clientOrderId")
                or exchange_order.get("orderId")
            )
            if client_id:
                exchange_map[client_id] = exchange_order
        to_process_fills: list[str] = []
        to_process_partials: list[str] = []
        to_process_missing: list[str] = []
        now = _utcnow()
        self._hydrate_exchange_only_orders(exchange_map, now, event_source)

        with self._order_lock:
            for client_id, local in list(self.active_orders.items()):
                exchange_order = exchange_map.get(client_id)
                if exchange_order:
                    filled_qty = float(exchange_order.get("cumExecQty") or 0.0)
                    orig_qty = float(
                        exchange_order.get("qty")
                        or exchange_order.get("orderQty")
                        or 0.0
                    )
                    status = exchange_order.get("orderStatus")
                    local["exchange_confirmed"] = True
                    local["filled_qty"] = filled_qty
                    local["remaining_qty"] = max(orig_qty - filled_qty, 0.0)
                    local["exchange_order_id"] = exchange_order.get("orderId")
                    local["updated_at"] = now
                    local["metadata"]["exchange_status"] = status
                    local["metadata"]["missing_from_exchange_count"] = 0
                    local["metadata"].pop("missing_since", None)
                    if status == "Filled" and local["status"] not in {
                        "FILLED",
                        "FILLED_HANDLED",
                    }:
                        local["status"] = "FILLED"
                        to_process_fills.append(client_id)
                    elif (
                        status == "PartiallyFilled"
                        and local["status"] not in {"FILLED", "FILLED_HANDLED", "CANCELED"}
                        and not local.get("partial_handled")
                    ):
                        local["status"] = "PARTIAL"
                        local["partial_handled"] = True
                        local["remaining_qty"] = max(orig_qty - filled_qty, 0.0)
                        to_process_partials.append(client_id)
                    elif status == "New" and local["status"] in {
                        "PENDING_SUBMIT",
                        "OPEN",
                        "UNKNOWN",
                    }:
                        local["status"] = "OPEN"
                else:
                    if (
                        local.get("exchange_confirmed")
                        and local["status"] in {"OPEN", "PARTIAL", "UNKNOWN"}
                    ):
                        metadata = local.setdefault("metadata", {})
                        missing_count = int(
                            metadata.get("missing_from_exchange_count", 0)
                        ) + 1
                        metadata["missing_from_exchange_count"] = missing_count
                        metadata.setdefault("missing_since", now.isoformat())
                        local["updated_at"] = now
                        if local["status"] != "UNKNOWN" and missing_count >= 2:
                            to_process_missing.append(client_id)

            for client_id, local in list(self.active_orders.items()):
                if local["status"] in {"FILLED_HANDLED", "CANCELED"}:
                    self._handle_order_finalized_locked(client_id, local)

        for client_id in to_process_partials:
            self.logger.info(
                "PARTIAL FILL DETECTED",
                extra={"client_order_id": client_id},
            )
            self.sync_positions_with_exchange()

        for client_id in to_process_fills:
            self.on_order_fill_event(client_id, event_source)

        for client_id in to_process_missing:
            resolved = self._resolve_order_via_history(
                client_id,
                source=f"{event_source}_missing_history",
            )
            if resolved:
                continue
            order = self.safe_get_order(client_id)
            self.logger.warning(
                "[ORDER UNKNOWN] local order missing from exchange snapshot",
                extra={
                    "client_order_id": client_id,
                    "previous_status": (order or {}).get("status"),
                    "side": (order or {}).get("side"),
                    "purpose": (order or {}).get("purpose"),
                    "exchange_order_id": (order or {}).get("exchange_order_id"),
                    "exchange_status": ((order or {}).get("metadata") or {}).get(
                        "exchange_status"
                    ),
                    "recovered_from_exchange": ((order or {}).get("metadata") or {}).get(
                        "recovered_from_exchange", False
                    ),
                    "verification_last_result": ((order or {}).get("metadata") or {}).get(
                        "verification_last_result"
                    ),
                    "missing_count": ((order or {}).get("metadata") or {}).get(
                        "missing_from_exchange_count"
                    ),
                },
            )
            self.safe_update_order(client_id, {"status": "UNKNOWN"})

        for client_id, order in list(self.active_orders.items()):
            if order["status"] not in {"PENDING_SUBMIT", "OPEN"}:
                continue
            if order.get("purpose") in {"LONG_REBUY", "LONG_REBUY_HEDGE"}:
                continue
            if not self._is_order_stale(order, timeout_sec=5):
                continue
            if order.get("retry_count", 0) >= 3:
                self.logger.error("[ORDER FAILED PERMANENTLY] %s", client_id)
                continue
            self.logger.warning("[ORDER STALE] %s retrying", client_id)
            exchange_id = order.get("exchange_order_id")
            metadata = order.get("metadata") or {}
            missing_count = int(metadata.get("missing_from_exchange_count", 0))
            if order["status"] == "OPEN":
                if client_id not in exchange_map or missing_count > 0:
                    self.logger.warning(
                        "[MARKET FALLBACK DEFERRED] exchange visibility unresolved",
                        extra={
                            "client_order_id": client_id,
                            "missing_count": missing_count,
                        },
                    )
                    continue
                if not exchange_id:
                    self.logger.warning(
                        "[MARKET FALLBACK DEFERRED] exchange order id missing",
                        extra={"client_order_id": client_id},
                    )
                    continue
                self.logger.warning("[LIMIT TIMEOUT] switching to market for %s", client_id)
                if not self.order_manager:
                    self.logger.warning(
                        "Order manager missing for market fallback", extra={"client_order_id": client_id}
                    )
                    continue
                try:
                    self.order_manager.cancel_order(
                        exchange_id, symbol=self.config.default_symbol
                    )
                except Exception:
                    self.logger.warning(
                        "[MARKET FALLBACK DEFERRED] failed to cancel limit order",
                        extra={
                            "client_order_id": client_id,
                            "order_id": exchange_id,
                        },
                    )
                    continue
                is_reduce_only_order = bool(
                    (order.get("metadata") or {}).get("reduce_only", False)
                )
                if not is_reduce_only_order:
                    self.logger.warning(
                        "[MARKET FALLBACK DEFERRED] stale opening order requires non-reduce retry path",
                        extra={
                            "client_order_id": client_id,
                            "purpose": order.get("purpose"),
                            "side": order.get("side"),
                        },
                    )
                    continue
                exchange_side = self._exchange_side(order["side"])
                self._log_slippage_check(exchange_side)
                executed = self.order_manager.place_reduce_market_order(
                    symbol=self.config.default_symbol,
                    side=exchange_side,
                    qty=order.get("qty", order["size"]),
                    position_idx=1 if order["side"] == "long" else 2,
                )
                if executed:
                    self.logger.info("[MARKET FALLBACK SUCCESS] %s", client_id)
                else:
                    self.logger.warning("[MARKET FALLBACK FAILED] %s", client_id)
                continue
            if exchange_id:
                self.logger.warning(
                    "[RETRY DEFERRED] pending submit unresolved with exchange order id",
                    extra={
                        "client_order_id": client_id,
                        "order_id": exchange_id,
                        "verify_attempts": order.get("verify_attempts", 0),
                    },
                )
                continue
            current_price = self.last_price or order.get("price")
            if not current_price or current_price <= 0:
                self.logger.warning(
                    "[RETRY SKIPPED] current price missing for %s", client_id
                )
                continue
            qty_step = self._current_qty_step()
            desired_qty = order.get("qty", order["size"])
            adjusted_qty = adjust_qty_to_min_notional(
                desired_qty,
                current_price,
                self.config.min_order_value,
                qty_step,
            )
            executed = self.executor._place_order_on_exchange(
                order["side"],
                adjusted_qty,
                current_price,
                order["purpose"],
                metadata=order.get("metadata"),
            )
            if not executed:
                self.logger.warning("[RETRY FAILED] %s", client_id)
            else:
                with self._order_lock:
                    order["retry_count"] = order.get("retry_count", 0) + 1

    def sync_positions_with_exchange(self) -> None:
        if not self.order_manager:
            return
        positions = self.order_manager.fetch_positions(
            self.config.default_symbol, self.config.category
        )
        long_size = 0.0
        short_size = 0.0
        long_avg = 0.0
        short_avg = 0.0
        for pos in positions:
            size = float(pos.get("size") or pos.get("positionQty") or 0.0)
            avg_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
            side = (pos.get("side") or pos.get("positionSide") or "").lower()
            if side in {"buy", "long"}:
                long_size = size
                long_avg = avg_price
            elif side in {"sell", "short"}:
                short_size = size
                short_avg = avg_price
        current_long_size, current_short_size, _, _ = self._get_position_snapshot()
        long_mismatch = (
            abs(current_long_size - long_size) / (long_size + 1e-9) > 0.02
        )
        short_mismatch = (
            abs(current_short_size - short_size) / (short_size + 1e-9) > 0.02
        )
        mismatch = long_mismatch or short_mismatch
        if mismatch:
            with self._position_sync_lock:
                self.position_manager.sync_positions(
                    long_size, long_avg, short_size, short_avg
                )
        if mismatch and self._should_log_mismatch():
            self.logger.warning(
                "Position mismatch resolved via sync",
                extra={
                    "long_size": long_size,
                    "long_avg": long_avg,
                    "short_size": short_size,
                    "short_avg": short_avg,
                },
            )
        self._last_mismatch_log = _utcnow()

    def request_extend(self) -> None:
        if not self.config.extend_enabled:
            return
        self.logger.info("Extend requested")
        if self.state_machine.state != StrategyState.POOL_EDGE:
            self.logger.warning("Extend request ignored (not at pool edge)")
            return
        # request_extend is a manual hook that lets an operator keep the strategy
        # in the informational EXTEND state for monitoring/logging; it does not
        # alter the automated order logic beyond adjusting recovery_low.
        self._extend_requested = True

    def _configure_logger(self) -> None:
        format_str = "%(asctime)s %(levelname)s %(message)s"
        has_stream = any(isinstance(handler, logging.StreamHandler) for handler in self.logger.handlers)
        if not has_stream:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(logging.Formatter(format_str))
            self.logger.addHandler(stream_handler)


    def _exchange_side(self, side: str) -> str:
        return "Buy" if side.lower() == "long" else "Sell"

    def _log_slippage_check(self, exchange_side: str) -> None:
        price = self.last_price
        if not price or price <= 0:
            return
        max_slippage = 0.002
        if exchange_side == "Buy":
            worst_price = price * (1 + max_slippage)
        else:
            worst_price = price * (1 - max_slippage)
        self.logger.info(
            "[SLIPPAGE CHECK] worst_price=%.6f",
            worst_price,
            extra={"exchange_side": exchange_side, "price": price},
        )

    def _generate_client_order_id(self, purpose: str) -> str:
        prefix = purpose.replace("_", "-")
        return f"psrh-{prefix}-{uuid4().hex[:6]}-{int(_utcnow().timestamp() * 1000)}"

    def safe_get_order(self, client_id: str) -> dict[str, Any] | None:
        with self._order_lock:
            order = self.active_orders.get(client_id)
            return dict(order) if order else None

    def safe_update_order(self, client_id: str, updates: Dict[str, Any]) -> None:
        with self._order_lock:
            if client_id in self.active_orders:
                self.active_orders[client_id].update(updates)

    def _resolve_order_via_history(
        self,
        client_order_id: str,
        *,
        source: str,
    ) -> bool:
        if not self.order_manager or not hasattr(self.order_manager, "fetch_order_history"):
            return False
        order = self.safe_get_order(client_order_id)
        if not order:
            return False
        exchange_order_id = order.get("exchange_order_id")
        history = self.order_manager.fetch_order_history(
            self.config.default_symbol,
            self.config.category,
            order_id=exchange_order_id,
            order_link_id=client_order_id,
            limit=10,
        )
        if not history:
            return False
        history_order = history[0]
        history_status = history_order.get("orderStatus") or "UNKNOWN"
        filled_qty = float(history_order.get("cumExecQty") or 0.0)
        order_qty = float(
            history_order.get("qty") or history_order.get("orderQty") or order.get("qty") or order.get("size") or 0.0
        )
        remaining_qty = max(order_qty - filled_qty, 0.0)
        checked_at = _utcnow().isoformat()
        with self._order_lock:
            current_order = self.active_orders.get(client_order_id)
            if not current_order:
                return False
            metadata = current_order.setdefault("metadata", {})
            metadata["history_last_checked_at"] = checked_at
            metadata["history_last_source"] = source
            metadata["history_last_status"] = history_status
            metadata["verification_last_checked_at"] = checked_at
            metadata["verification_last_source"] = source
            metadata["verification_last_result"] = "order_history_match"
            current_order["exchange_confirmed"] = True
            current_order["exchange_order_id"] = (
                history_order.get("orderId") or current_order.get("exchange_order_id")
            )
            current_order["filled_qty"] = filled_qty
            current_order["remaining_qty"] = remaining_qty
            current_order["updated_at"] = _utcnow()
        if history_status == "Filled":
            with self._order_lock:
                current_order = self.active_orders.get(client_order_id)
                if current_order:
                    current_order["status"] = "FILLED"
            self.logger.info(
                "ORDER HISTORY RESOLVED",
                extra={
                    "client_order_id": client_order_id,
                    "source": source,
                    "reason_code": "history_filled",
                    "history_status": history_status,
                },
            )
            self.on_order_fill_event(client_order_id, source="history")
            return True
        if history_status == "PartiallyFilled":
            with self._order_lock:
                current_order = self.active_orders.get(client_order_id)
                if current_order:
                    current_order["status"] = "PARTIAL"
                    current_order["partial_handled"] = True
            self.logger.warning(
                "ORDER HISTORY RESOLVED PARTIAL",
                extra={
                    "client_order_id": client_order_id,
                    "source": source,
                    "history_status": history_status,
                    "filled_qty": filled_qty,
                    "remaining_qty": remaining_qty,
                },
            )
            self.sync_positions_with_exchange()
            return True
        if history_status in {"Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}:
            self.sync_positions_with_exchange()
            with self._order_lock:
                current_order = self.active_orders.get(client_order_id)
                if current_order:
                    current_order["status"] = "CANCELED"
                    self._handle_order_finalized_locked(client_order_id, current_order)
            self.logger.warning(
                "ORDER HISTORY RESOLVED TERMINAL",
                extra={
                    "client_order_id": client_order_id,
                    "source": source,
                    "history_status": history_status,
                    "filled_qty": filled_qty,
                },
            )
            return True
        if history_status in {"New", "Untriggered", "Triggered"}:
            with self._order_lock:
                current_order = self.active_orders.get(client_order_id)
                if current_order:
                    current_order["status"] = "OPEN"
            self.logger.info(
                "ORDER HISTORY RESOLVED",
                extra={
                    "client_order_id": client_order_id,
                    "source": source,
                    "reason_code": "history_open_like",
                    "history_status": history_status,
                },
            )
            return True
        return False

    @staticmethod
    def _price_equal(a: float, b: float, tol: float = 1e-6) -> bool:
        return abs(a - b) < tol

    def _has_active_intent(self, side: str, purpose: str, price: float, qty: float) -> bool:
        with self._order_lock:
            for order in self.active_orders.values():
                if (
                    order["side"] == side
                    and order["purpose"] == purpose
                    and self._price_equal(order["price"], price)
                    and abs(order["size"] - qty) < 1e-6
                    and order["status"] not in {"FILLED", "FILLED_HANDLED", "CANCELED"}
                ):
                    return True
        return False

    def mark_order_filled(self, client_order_id: str) -> None:
        with self._order_lock:
            order = self.active_orders.get(client_order_id)
            if not order:
                return
            order["status"] = "FILLED"
            key = (
                order["side"],
                round(order["size"], 4),
                order["purpose"],
            )
            self._submitted_orders.discard(key)
            self.logger.info(
                "[EXEC DEBUG] submitted_orders_remove "
                f"client_id={client_order_id} key={key} side={order['side']} "
                f"qty={order['size']:.12f} purpose={order['purpose']} "
                f"reason=mark_order_filled submitted_orders_after={list(self._submitted_orders)}"
            )

    def _handle_order_finalized_locked(self, client_id: str, order: dict[str, Any]) -> None:
        finalized_order = self.active_orders.pop(client_id, None) or order
        key = (
            finalized_order["side"],
            round(finalized_order["size"], 4),
            finalized_order["purpose"],
        )
        self._submitted_orders.discard(key)
        self.logger.info(
            "[EXEC DEBUG] submitted_orders_remove "
            f"client_id={client_id} key={key} side={finalized_order['side']} "
            f"qty={finalized_order['size']:.12f} purpose={finalized_order['purpose']} "
            f"reason=handle_order_finalized status={finalized_order.get('status')} "
            f"submitted_orders_after={list(self._submitted_orders)}"
        )
        exchange_order_id = finalized_order.get("exchange_order_id")
        if exchange_order_id:
            self._exchange_to_client_id.pop(exchange_order_id, None)
        self._recent_orders = deque(
            (cid for cid in self._recent_orders if cid != client_id),
            maxlen=20,
        )

    def verify_order_on_exchange(
        self,
        client_order_id: str,
        *,
        source: str = "verify",
        retries: int = 3,
        delay: float = 0.25,
        log_missing: bool = False,
    ) -> bool:
        if not self.order_manager:
            return False
        local = self.safe_get_order(client_order_id)
        if not local:
            return False
        start_time = time.time()
        while True:
            if time.time() - start_time > 2:
                break
            with self._order_lock:
                current_order = self.active_orders.get(client_order_id)
                if not current_order:
                    return False
                status = current_order["status"]
                attempts = current_order.get("verify_attempts", 0)
            if status != "PENDING_SUBMIT":
                break
            if attempts >= retries:
                break
            orders = self.order_manager.fetch_open_orders(
                self.config.default_symbol, self.config.category
            )
            checked_at = _utcnow().isoformat()
            if orders is None:
                with self._order_lock:
                    current_order = self.active_orders.get(client_order_id)
                    if not current_order:
                        return False
                    metadata = current_order.setdefault("metadata", {})
                    metadata["verification_last_checked_at"] = checked_at
                    metadata["verification_last_source"] = source
                    metadata["verification_last_result"] = "fetch_failed"
                    current_order["verify_attempts"] = attempts + 1
                self.logger.warning(
                    "ORDER VERIFICATION FETCH FAILED",
                    extra={
                        "client_order_id": client_order_id,
                        "source": source,
                        "side": current_order.get("side"),
                        "purpose": current_order.get("purpose"),
                        "status": current_order.get("status"),
                    },
                )
                time.sleep(delay)
                continue
            for exchange_order in orders:
                candidate_id = (
                    exchange_order.get("orderLinkId")
                    or exchange_order.get("clientOrderId")
                    or exchange_order.get("orderId")
                )
                if candidate_id == client_order_id:
                    with self._order_lock:
                        current_order = self.active_orders.get(client_order_id)
                        if not current_order:
                            return False
                        metadata = current_order.setdefault("metadata", {})
                        metadata["verification_last_checked_at"] = checked_at
                        metadata["verification_last_source"] = source
                        metadata["verification_last_result"] = "open_order_match"
                        metadata["verification_last_snapshot_size"] = len(orders)
                        current_order["status"] = "OPEN"
                        current_order["exchange_confirmed"] = True
                    self.logger.info(
                        "ORDER VERIFIED",
                        extra={
                            "client_order_id": client_order_id,
                            "source": source,
                            "reason_code": "open_order_match",
                        },
                    )
                    return True
            current_order = self.safe_get_order(client_order_id)
            metadata = (current_order or {}).get("metadata") or {}
            pre_submit_snapshot = metadata.get("pre_submit_snapshot") or {}
            is_non_reduce_market = (
                metadata.get("order_type") == "Market"
                and not metadata.get("reduce_only", False)
            )
            if current_order and is_non_reduce_market:
                positions = self.order_manager.fetch_positions(
                    self.config.default_symbol, self.config.category
                )
                current_long_size = 0.0
                current_short_size = 0.0
                current_long_avg = 0.0
                current_short_avg = 0.0
                for pos in positions:
                    side = (pos.get("side") or pos.get("positionSide") or "").lower()
                    size = float(pos.get("size") or pos.get("positionQty") or 0.0)
                    avg_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
                    if side in {"buy", "long"}:
                        current_long_size = size
                        current_long_avg = avg_price
                    elif side in {"sell", "short"}:
                        current_short_size = size
                        current_short_avg = avg_price
                pre_long_size = float(pre_submit_snapshot.get("long_size") or 0.0)
                pre_short_size = float(pre_submit_snapshot.get("short_size") or 0.0)
                pre_long_avg = float(pre_submit_snapshot.get("long_avg") or 0.0)
                pre_short_avg = float(pre_submit_snapshot.get("short_avg") or 0.0)
                market_fill_confirmed = False
                if current_order.get("side") == "long":
                    market_fill_confirmed = (
                        current_long_size > pre_long_size + 1e-9
                        or abs(current_long_avg - pre_long_avg) > 1e-9
                    )
                elif current_order.get("side") == "short":
                    market_fill_confirmed = (
                        current_short_size > pre_short_size + 1e-9
                        or abs(current_short_avg - pre_short_avg) > 1e-9
                    )
                if market_fill_confirmed:
                    with self._order_lock:
                        refreshed_order = self.active_orders.get(client_order_id)
                        if refreshed_order:
                            refreshed_order["exchange_confirmed"] = True
                            refreshed_order["status"] = "FILLED"
                            refreshed_order["metadata"]["verification_last_checked_at"] = checked_at
                            refreshed_order["metadata"]["verification_last_source"] = source
                            refreshed_order["metadata"]["verification_last_result"] = (
                                "position_delta_match"
                            )
                    self.logger.info(
                        "ORDER VERIFIED",
                        extra={
                            "client_order_id": client_order_id,
                            "source": source,
                            "reason_code": "position_delta_match",
                        },
                    )
                    self.on_order_fill_event(client_order_id, source="verify")
                    return True
            history_resolved = self._resolve_order_via_history(
                client_order_id,
                source=f"{source}_history",
            )
            if history_resolved:
                return True
            with self._order_lock:
                    current_order = self.active_orders.get(client_order_id)
                    if not current_order:
                        return False
                    metadata = current_order.setdefault("metadata", {})
                    metadata["verification_last_checked_at"] = checked_at
                    metadata["verification_last_source"] = source
                    metadata["verification_last_result"] = "not_in_open_orders_snapshot"
                    metadata["verification_last_snapshot_size"] = len(orders)
                    current_order["verify_attempts"] = attempts + 1
            time.sleep(delay)
        with self._order_lock:
            order = self.active_orders.get(client_order_id)
        if (
            order
            and order.get("verify_attempts", 0) >= retries
            and _elapsed_seconds_since(order.get("created_at")) > 2
            and log_missing
        ):
            self.logger.critical(
                "ORDER NOT FOUND AFTER MULTIPLE RETRIES",
                extra={
                    "client_order_id": client_order_id,
                    "source": source,
                    "side": order.get("side"),
                    "purpose": order.get("purpose"),
                    "status": order.get("status"),
                    "reason_code": ((order.get("metadata") or {}).get("verification_last_result")),
                    "exchange_order_id": order.get("exchange_order_id"),
                    "recovered_from_exchange": ((order.get("metadata") or {}).get(
                        "recovered_from_exchange", False
                    )),
                },
            )
        return False

    def on_order_fill_event(self, client_order_id: str, source: str = "reconcile") -> None:
        with self._order_lock:
            order = self.active_orders.get(client_order_id)
            if not order or order.get("status") == "FILLED_HANDLED":
                return
            exchange_confirmed = order.get("exchange_confirmed")
            purpose = order.get("purpose")
            order["retry_count"] = 0
        if not exchange_confirmed:
            self.logger.info(
                "SKIP FILL EVENT – NOT CONFIRMED BY EXCHANGE",
                extra={"client_order_id": client_order_id},
            )
            return
        self.mark_order_filled(client_order_id)
        msg = (
            "FAST FILL DETECTED"
            if source in {"fast_poll", "verify"}
            else "ORDER FILL CONFIRMED"
        )
        self.logger.info(
            msg,
            extra={"client_order_id": client_order_id, "source": source},
        )
        self.sync_positions_with_exchange()
        self._handle_post_fill_follow_up_hook(client_order_id, purpose, source)
        self.safe_update_order(client_order_id, {"status": "FILLED_HANDLED"})
        with self._order_lock:
            handled_order = self.active_orders.pop(client_order_id, None)
            if handled_order:
                handled_order["status"] = "FILLED_HANDLED"
                self._handle_order_finalized_locked(client_order_id, handled_order)
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        self.logger.info(
            "POSITION UPDATED AFTER FILL",
            extra={
                "client_order_id": client_order_id,
                "source": source,
                "purpose": purpose,
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
            },
        )

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
        with self._order_lock:
            client_id = self._exchange_to_client_id.get(exchange_order_id) or order_link_id
        if not client_id:
            self.logger.warning(
                "[WS FILL] Unknown exchange order: %s orderLinkId=%s qty=%.6f price=%.6f",
                exchange_order_id,
                order_link_id,
                qty,
                price,
            )
            return
        self.logger.info(
            "[WS FILL] mapped %s → %s qty=%.6f price=%.6f",
            exchange_order_id,
            client_id,
            qty,
            price,
        )
        with self._order_lock:
            order = self.active_orders.get(client_id)
            if not order or order.get("status") == "FILLED_HANDLED":
                return
            metadata = order.setdefault("metadata", {})
            if exec_id and metadata.get("last_ws_exec_id") == exec_id:
                self.logger.info(
                    "[WS FILL] duplicate exec ignored",
                    extra={
                        "client_order_id": client_id,
                        "exchange_order_id": exchange_order_id,
                        "exec_id": exec_id,
                    },
                )
                return
            total_qty = float(order.get("qty", order["size"]))
            filled_before = float(order.get("filled_qty", 0.0))
            if cumulative_qty is not None and cumulative_qty <= filled_before + 1e-9:
                self.logger.info(
                    "[WS FILL] stale cumulative fill ignored",
                    extra={
                        "client_order_id": client_id,
                        "exchange_order_id": exchange_order_id,
                        "exec_id": exec_id,
                        "cumulative_qty": cumulative_qty,
                        "filled_before": filled_before,
                    },
                )
                return
            fill_increment = max(float(qty), 0.0)
            if cumulative_qty is not None and cumulative_qty > filled_before:
                filled_after = min(total_qty, cumulative_qty)
            else:
                filled_after = min(total_qty, filled_before + fill_increment)
            remaining_qty = max(total_qty - filled_after, 0.0)
            order["exchange_confirmed"] = True
            order["filled_qty"] = filled_after
            order["remaining_qty"] = remaining_qty
            order["updated_at"] = _utcnow()
            if exec_id:
                metadata["last_ws_exec_id"] = exec_id
            if cumulative_qty is not None:
                metadata["last_ws_cumulative_qty"] = cumulative_qty
            if remaining_qty <= 1e-9:
                order["status"] = "FILLED"
            else:
                order["status"] = "PARTIAL"
                order["partial_handled"] = True
        if remaining_qty <= 1e-9:
            self.on_order_fill_event(client_id, source="websocket")
            return
        self.logger.info(
            "[WS PARTIAL] awaiting final fill before follow-up",
            extra={
                "client_order_id": client_id,
                "filled_qty": filled_after,
                "remaining_qty": remaining_qty,
                "exec_id": exec_id,
                "cumulative_qty": cumulative_qty,
            },
        )
        self.sync_positions_with_exchange()


def generate_price_path(base: float = 100.0, length: int = 80) -> list[float]:
    path: list[float] = []
    for step in range(length):
        drop_segment = (step // 10) * -0.4
        rebound = 0.25 if (step % 15) > 10 else 0.0
        oscillation = ((step % 3) - 1) * 0.05
        path.append(base + drop_segment + rebound + oscillation)
    return path


def run_backtest(price_series: Iterable[float], config: StrategyConfig) -> PSRHStrategy:
    strategy = PSRHStrategy(config)
    for price in price_series:
        strategy.on_price_update(price)
    return strategy


if __name__ == "__main__":  # pragma: no cover - simple simulation
    sample_config = StrategyConfig()
    sample_series = generate_price_path()
    strategy_instance = run_backtest(sample_series, sample_config)
    logger.info("Backtest finished", extra={"realized_pnl": strategy_instance.risk_manager.realized_pnl})
