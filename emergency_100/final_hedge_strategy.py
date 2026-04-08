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

from emergency_100.helpers.upside_rebuild_helper import (
    build_paired_partial_sl_short_intent_from_filled_long,
    calculate_paired_partial_sl_short_trigger,
    cancel_future_open_long_heal_orders,
    handle_filled_paired_short_close,
    handle_filled_spread_heal_long,
    rebuild_future_long_heals_from_current_long_size,
)
from emergency_100.helpers.downside_rebuild_helper import (
    build_paired_partial_sl_long_intent_from_filled_short,
    calculate_paired_partial_sl_long_trigger,
    cancel_future_open_short_heal_orders,
    handle_filled_paired_long_close,
    handle_filled_spread_heal_short,
    rebuild_future_short_heals_from_current_short_size,
)
from emergency_100.helpers.order_purpose_helper import (
    is_paired_long_close_order,
    is_paired_partial_sl_long_order,
    is_paired_partial_sl_short_order,
    is_spread_heal_long_order,
    is_spread_heal_short_order,
)
from emergency_100.helpers.post_fill_follow_up_helper import (
    handle_post_fill_follow_up_hook,
)
from emergency_100.helpers.spread_heal_intent_helper import (
    build_spread_heal_long_intent,
    build_spread_heal_short_intent,
)
from emergency_100.helpers.spread_heal_phase_helper import (
    aggressive_down_heal_complete,
    build_aggressive_down_heal_short_intent,
    build_phase2_long_reduce_from_short_profit_intent,
    build_phase3_long_rebuild_intent,
    build_phase4_short_rebuild_intent,
    confirmed_aggressive_down_heal_move,
    ensure_aggressive_down_heal_tracking,
    ensure_phase3_long_target_reference,
    ensure_phase4_short_target_reference,
    maybe_build_phase2_long_reduce_intent,
    phase2_long_reduce_ready,
    phase2_short_profit_budget_available,
    phase3_long_rebuild_ready,
    phase3_target_long_qty,
    phase3_target_reached,
    phase4_short_rebuild_enabled,
    phase4_short_rebuild_ready,
    phase4_target_reached,
    phase4_target_short_qty,
    phase5_fine_heal_ready,
    record_phase2_short_profit_budget_usage,
)
from emergency_100.helpers.preplaced_heal_helper import (
    arm_preplaced_heal_orders,
    build_preplaced_heal_limit_intents,
    cancel_order_by_client_id,
    cancel_preplaced_heal_orders,
    cancel_recovered_preplaced_heal_orders,
    collect_preplaced_heal_order_ids,
    compute_preplaced_heal_prices,
    handle_preplaced_heal_fill,
    preplaced_heal_mode_active,
    should_arm_preplaced_heal_orders,
)
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
        self._long_heal_adds = 0
        self._short_heal_adds = 0
        self._spread_healing_active = False
        self._realized_long_pnl_total = 0.0
        self._realized_short_pnl_total = 0.0
        self._long_adds_in_cycle = 0
        self._short_adds_in_cycle = 0
        self._last_relevant_high: float | None = None
        self._last_relevant_low: float | None = None
        self._pending_rebuild_side: str | None = None
        self._pending_failover_side: str | None = None
        self._wait_reference_price: float | None = None
        self._last_structure_event: str | None = None
        self._aggressive_down_heal_initial_short_size: float | None = None
        self._aggressive_down_heal_reference_price: float | None = None
        self._aggressive_down_heal_phase_completed = False
        self._phase2_short_profit_budget_reserved = 0.0
        self._phase3_long_target_reference_size: float | None = None
        self._phase4_short_target_reference_size: float | None = None
        self._preplaced_heal_orders_armed = False
        self._preplaced_heal_generation = 0
        self._active_preplaced_heal_long_client_id: str | None = None
        self._active_preplaced_heal_short_client_id: str | None = None
        self._preplaced_heal_rearm_in_progress = False
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
            record_realized_pnl_by_side=self._record_realized_pnl_by_side,
            handle_order_finalized_locked=self._handle_order_finalized_locked,
            sync_positions_with_exchange=self.sync_positions_with_exchange,
            get_position_snapshot=self._get_position_snapshot,
            verify_order_on_exchange=self.verify_order_on_exchange,
            get_last_price=self._get_last_price,
            set_dca_steps=self._set_dca_steps,
            on_intent_executed=self._on_intent_executed,
        )
        self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
        self._startup_thread = threading.Thread(
            target=self._startup_rest_worker,
            daemon=True,
        )
        self._startup_thread.start()
        self._last_fast_fill_time: float | None = None

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
            self._cancel_preplaced_heal_orders("one_side_missing")
            self._reset_cycle_progress()
            self.logger.warning("One side missing → pausing strategy")
            self._set_initialized(False)
            self.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
            return
        self._ensure_phase3_long_target_reference()
        self._ensure_phase4_short_target_reference()

        hedge_spread = abs(self.calculate_hedge_spread())
        self._initialize_structure_refs(price)
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
            self._execute_intents(state_intents)
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
                    "event": "order_execution_started",
                    "purpose": intent.purpose,
                    "side": intent.side,
                    "price": intent.price,
                    "normalized_qty": intent.qty,
                    "reduce_only": intent.reduce_only,
                    "order_type": intent.order_type,
                    "state": self.state_machine.state.value,
                    "result": "executing",
                },
            )
            self.executor.execute_intent(intent, enqueue_follow_ups=pending_intents.extend)

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

    def _handle_post_fill_follow_up_hook(
        self, client_order_id: str, purpose: str | None, source: str
    ) -> None:
        return handle_post_fill_follow_up_hook(
            self, client_order_id, purpose, source
        )

    def _on_intent_executed(
        self, intent: OrderIntent, submit_price: float
    ) -> list[OrderIntent]:
        base_extra = {
            "event": "order_submit_result",
            "purpose": intent.purpose,
            "side": intent.side,
            "price": submit_price,
            "normalized_qty": intent.qty,
            "reduce_only": intent.reduce_only,
            "order_type": intent.order_type,
            "state": self.state_machine.state.value,
            "result": "submitted",
        }
        if intent.purpose in {"normal_reduce_long", "normal_reduce_short"}:
            self.logger.info(
                "Normal-flow reduction submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose in {"rebuild_long", "rebuild_short"}:
            self.logger.info(
                "Pullback rebuild submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose in {"failover_reduce_long", "failover_reduce_short"}:
            self.logger.info(
                "No-pullback failover submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose == "aggressive_down_heal_short":
            self.logger.info(
                "Aggressive down-heal short reduction submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose == "phase2_long_reduce_from_short_profit":
            self.logger.info(
                "Phase 2 long reduction submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose == "phase3_long_rebuild":
            self.logger.info(
                "Phase 3 long rebuild submitted",
                extra=base_extra,
            )
            return []
        if intent.purpose == "phase4_short_rebuild":
            self.logger.info(
                "Phase 4 short rebuild submitted",
                extra=base_extra,
            )
            return []

        if intent.purpose == "spread_heal_long":
            self.logger.info(
                "Spread healing long order submitted",
                extra={
                    **base_extra,
                    "spread_heal_adds": self._long_heal_adds,
                },
            )
        elif intent.purpose == "preplaced_heal_long_limit":
            self.logger.info(
                "Preplaced heal long limit submitted",
                extra=base_extra,
            )
        elif intent.purpose == "preplaced_heal_short_limit":
            self.logger.info(
                "Preplaced heal short limit submitted",
                extra=base_extra,
            )
        elif intent.purpose == "spread_heal_short":
            self.logger.info(
                "Spread healing short order submitted",
                extra={
                    **base_extra,
                    "spread_heal_adds": self._short_heal_adds,
                },
            )
        elif intent.purpose == "paired_partial_sl_long":
            self.logger.info(
                "Paired partial SL long order submitted",
                extra=base_extra,
            )
        elif intent.purpose == "paired_partial_sl_short":
            self.logger.info(
                "Paired partial SL short order submitted",
                extra=base_extra,
            )
        elif intent.purpose == "basket_exit":
            self.logger.info(
                "Basket exit intent submitted",
                extra=base_extra,
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
        self.logger.info(
            "Legacy place_long_rebuy disabled under final strategy",
            extra={"price": price, "spread": spread},
        )
        return None

    def adjust_short_hedge(
        self,
        price: float,
        long_size_override: float | None = None,
        spread: float = 0.0,
    ) -> OrderIntent | None:
        self.logger.info(
            "Legacy adjust_short_hedge disabled under final strategy",
            extra={
                "price": price,
                "long_size_override": long_size_override,
                "spread": spread,
            },
        )
        return None

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
            extra={
                "event": "state_transition_evaluated",
                "state": self.state_machine.state.value,
                "price": price,
                "spread": spread,
                "result": "entered_update_state",
            },
        )
        doc_spread_pct = self._calculate_doc_spread_pct()
        basket_pnl = self._calculate_basket_pnl(price)
        self.logger.debug(
            "State evaluation snapshot",
            extra={
                "event": "state_transition_evaluated",
                "state": self.state_machine.state.value,
                "price": price,
                "spread": spread,
                "spread_pct": doc_spread_pct,
                "ratio": self._current_ratio(),
                "basket_pnl": basket_pnl,
                "result": "evaluating",
            },
        )
        if self._has_active_preplaced_heal_orders():
            self._cancel_preplaced_heal_orders("preplaced_heal_disabled")
        if self._can_fire_basket_exit(basket_pnl):
            self.logger.info(
                "Basket exit triggered",
                extra={
                    "price": price,
                    "basket_pnl": basket_pnl,
                    "spread_pct": doc_spread_pct,
                    "ratio": self._current_ratio(),
                },
            )
            self._cancel_preplaced_heal_orders("basket_exit")
            self._reset_cycle_progress()
            return self._build_full_exit_intents(price, "basket_exit")
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
        phase2_intent = self._maybe_build_phase2_long_reduce_intent(price)
        if phase2_intent:
            self.logger.info(
                "Phase 2 long reduce queued",
                extra={
                    "price": price,
                    "realized_short_pnl_total": self._realized_short_pnl_total,
                    "reserved_phase2_budget": self._phase2_short_profit_budget_reserved,
                    "remaining_short_profit_budget": self._phase2_short_profit_budget_available(),
                    "phase2_qty": phase2_intent.qty,
                },
            )
            return [phase2_intent]
        if self._phase3_long_rebuild_ready():
            phase3_intent = self._build_phase3_long_rebuild_intent(price)
            if phase3_intent:
                self._record_add_for_side("long")
                self.logger.info(
                    "Phase 3 long rebuild queued",
                    extra={
                        "price": price,
                        "current_long_size": self._get_position_snapshot()[0],
                        "target_long_qty": self._phase3_target_long_qty(),
                        "reference_long_size": self._phase3_long_target_reference_size,
                        "phase3_qty": phase3_intent.qty,
                    },
                )
                return [phase3_intent]
        if self._phase4_short_rebuild_ready():
            phase4_intent = self._build_phase4_short_rebuild_intent(price)
            if phase4_intent:
                self._record_add_for_side("short")
                self.logger.info(
                    "Phase 4 short rebuild queued",
                    extra={
                        "price": price,
                        "current_short_size": self._get_position_snapshot()[1],
                        "target_short_qty": self._phase4_target_short_qty(),
                        "reference_short_size": self._phase4_short_target_reference_size,
                        "phase4_qty": phase4_intent.qty,
                    },
                )
                return [phase4_intent]
        if doc_spread_pct > self.config.spread_heal_trigger_pct:
            target_state = (
                StrategyState.SIZE_RESET_ONLY
                if self._is_size_balanced()
                else StrategyState.SPREAD_HEALING
            )
            self._transition_state_if_needed(
                target_state,
                "spread heal triggered",
                price,
                spread,
            )
            self._spread_healing_active = True
            if self.config.preplaced_heal_enabled:
                self.logger.info("Preplaced heal disabled for new strategy")
            if (
                self._aggressive_down_heal_enabled()
                and not self._aggressive_down_heal_phase_completed
            ):
                self._ensure_aggressive_down_heal_tracking(price)
                if self._aggressive_down_heal_complete():
                    self._aggressive_down_heal_phase_completed = True
                    self.logger.info(
                        "Aggressive down-heal phase completed",
                        extra={
                            "initial_short_size": self._aggressive_down_heal_initial_short_size,
                        },
                    )
                    self._clear_aggressive_down_heal_state(
                        preserve_phase_completion=True
                    )
                else:
                    aggressive_intent = None
                    if self._confirmed_aggressive_down_heal_move(price):
                        aggressive_intent = self._build_aggressive_down_heal_short_intent(price)
                    if aggressive_intent:
                        self._aggressive_down_heal_reference_price = price
                        self.logger.info(
                            "Aggressive down-heal short reduce queued",
                            extra={
                                "price": price,
                                "initial_short_size": self._aggressive_down_heal_initial_short_size,
                                "reference_price": self._aggressive_down_heal_reference_price,
                                "current_long_size": self._get_position_snapshot()[0],
                                "current_short_size": self._get_position_snapshot()[1],
                                "aggressive_down_heal_step_pct": self.config.aggressive_down_heal_step_pct,
                                "aggressive_down_heal_size_pct": self.config.aggressive_down_heal_size_pct,
                            },
                        )
                        return [aggressive_intent]
                    self.logger.info(
                        "Aggressive down-heal active without confirmed step",
                        extra={
                            "event": "aggressive_down_heal_short_skipped",
                            "state": self.state_machine.state.value,
                            "price": price,
                            "long_size": self._get_position_snapshot()[0],
                            "short_size": self._get_position_snapshot()[1],
                            "long_avg": self._get_position_snapshot()[2],
                            "short_avg": self._get_position_snapshot()[3],
                            "spread_pct": doc_spread_pct,
                            "reference_price": self._aggressive_down_heal_reference_price,
                            "initial_short_size": self._aggressive_down_heal_initial_short_size,
                            "reason": "no_confirmed_step",
                            "result": "skipped",
                        },
                    )
                    return []
            if not self._phase5_fine_heal_ready():
                long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
                if (
                    self._phase3_long_rebuild_enabled()
                    and not self._phase3_target_reached()
                ):
                    self.logger.info(
                        "phase3_blocked_not_reached",
                        extra={
                            "event": "phase3_long_rebuild_skipped",
                            "state": self.state_machine.state.value,
                            "price": price,
                            "long_size": long_size,
                            "short_size": short_size,
                            "long_avg": long_avg,
                            "short_avg": short_avg,
                            "spread_pct": doc_spread_pct,
                            "reason": "target_not_reached",
                            "result": "skipped",
                        },
                    )
                if (
                    self._phase4_short_rebuild_enabled()
                    and not self._phase4_target_reached()
                ):
                    self.logger.info(
                        "phase4_blocked_not_reached",
                        extra={
                            "event": "phase4_short_rebuild_skipped",
                            "state": self.state_machine.state.value,
                            "price": price,
                            "long_size": long_size,
                            "short_size": short_size,
                            "long_avg": long_avg,
                            "short_avg": short_avg,
                            "spread_pct": doc_spread_pct,
                            "reason": "target_not_reached",
                            "result": "skipped",
                        },
                    )
                self.logger.info(
                    "fine_heal_blocked",
                    extra={
                        "event": "fine_heal_skipped",
                        "state": self.state_machine.state.value,
                        "price": price,
                        "long_size": long_size,
                        "short_size": short_size,
                        "long_avg": long_avg,
                        "short_avg": short_avg,
                        "spread_pct": doc_spread_pct,
                        "phase3_target_reached": self._phase3_target_reached(),
                        "phase4_target_reached": self._phase4_target_reached(),
                        "reason": "phase_targets_not_reached",
                        "result": "skipped",
                    },
                )
                return []
            short_heal_intent = self._build_spread_heal_short_intent(price, doc_spread_pct)
            if short_heal_intent:
                self._record_add_for_side("short")
                self.logger.info(
                    "Short heal intent queued",
                    extra={
                        "price": price,
                        "spread_pct": doc_spread_pct,
                        "short_heal_adds": self._short_adds_in_cycle,
                        "short_heal_remaining": self._short_heal_adds_remaining(),
                        "basket_pnl": basket_pnl,
                    },
                )
                return [short_heal_intent]
            long_heal_intent = self._build_spread_heal_long_intent(price, doc_spread_pct)
            if long_heal_intent:
                self._record_add_for_side("long")
                self.logger.info(
                    "Long heal intent queued",
                    extra={
                        "price": price,
                        "spread_pct": doc_spread_pct,
                        "long_heal_adds": self._long_adds_in_cycle,
                        "long_heal_remaining": self._long_heal_adds_remaining(),
                        "basket_pnl": basket_pnl,
                    },
                )
                return [long_heal_intent]
            wait_state = (
                StrategyState.SIZE_RESET_ONLY
                if self._is_size_balanced()
                else StrategyState.WAIT_NO_ACTION
            )
            self._transition_state_if_needed(
                wait_state,
                "spread healing active without valid trigger",
                price,
                spread,
            )
            self.logger.info(
                "Spread healing rejected -> wait",
                extra={
                    "price": price,
                    "spread_pct": doc_spread_pct,
                    "long_avg": self._get_position_snapshot()[2],
                    "short_avg": self._get_position_snapshot()[3],
                },
            )
            return []
        elif self._spread_healing_active and doc_spread_pct <= self.config.spread_heal_trigger_pct:
            self.logger.info(
                "Spread healed below threshold, resetting heal counts",
                extra={"spread_pct": doc_spread_pct},
            )
            self._cancel_preplaced_heal_orders("spread_healed_below_threshold")
            self._reset_cycle_progress()
        if self._pending_rebuild_side == "long" and self._wait_reference_price:
            self._transition_state_if_needed(
                StrategyState.WAIT_PULLBACK,
                "waiting for long rebuild pullback",
                price,
                spread,
            )
            pullback_trigger = self._wait_reference_price * (
                1 - self.config.structural_trigger_pct
            )
            failover_trigger = self._wait_reference_price * (
                1 + self.config.structural_trigger_pct
            )
            if price <= pullback_trigger:
                intent = self._build_add_side_intent("long", price, "rebuild_long")
                if intent:
                    self._record_add_for_side("long")
                    self._clear_wait_context()
                    self._set_structure_reference(
                        price,
                        "rebuild_long",
                        update_low=True,
                    )
                    self._transition_state_if_needed(
                        StrategyState.NORMAL_FLOW,
                        "confirmed pullback rebuilt long",
                        price,
                        spread,
                    )
                    return [intent]
            if price >= failover_trigger:
                self._transition_state_if_needed(
                    StrategyState.NO_PULLBACK_FAILOVER,
                    "pullback failed after long reduction",
                    price,
                    spread,
                )
                intent = self._build_reduce_side_intent(
                    "short",
                    price,
                    "failover_reduce_short",
                )
                if intent:
                    self._clear_wait_context()
                    self._set_structure_reference(
                        price,
                        "failover_reduce_short",
                        update_high=True,
                    )
                    return [intent]
            self.logger.info(
                "WAIT_PULLBACK active for long rebuild",
                extra={
                    "event": "rebuild_long_skipped",
                    "state": self.state_machine.state.value,
                    "price": price,
                    "long_size": self._get_position_snapshot()[0],
                    "short_size": self._get_position_snapshot()[1],
                    "long_avg": self._get_position_snapshot()[2],
                    "short_avg": self._get_position_snapshot()[3],
                    "pullback_trigger": pullback_trigger,
                    "failover_trigger": failover_trigger,
                    "reason": "waiting_for_pullback_or_failover",
                    "result": "skipped",
                },
            )
            return []
        if self._pending_rebuild_side == "short" and self._wait_reference_price:
            self._transition_state_if_needed(
                StrategyState.WAIT_PULLBACK,
                "waiting for short rebuild rebound",
                price,
                spread,
            )
            rebound_trigger = self._wait_reference_price * (
                1 + self.config.structural_trigger_pct
            )
            failover_trigger = self._wait_reference_price * (
                1 - self.config.structural_trigger_pct
            )
            if price >= rebound_trigger:
                intent = self._build_add_side_intent("short", price, "rebuild_short")
                if intent:
                    self._record_add_for_side("short")
                    self._clear_wait_context()
                    self._set_structure_reference(
                        price,
                        "rebuild_short",
                        update_high=True,
                    )
                    self._transition_state_if_needed(
                        StrategyState.NORMAL_FLOW,
                        "confirmed rebound rebuilt short",
                        price,
                        spread,
                    )
                    return [intent]
            if price <= failover_trigger:
                self._transition_state_if_needed(
                    StrategyState.NO_PULLBACK_FAILOVER,
                    "rebound failed after short reduction",
                    price,
                    spread,
                )
                intent = self._build_reduce_side_intent(
                    "long",
                    price,
                    "failover_reduce_long",
                )
                if intent:
                    self._clear_wait_context()
                    self._set_structure_reference(
                        price,
                        "failover_reduce_long",
                        update_low=True,
                    )
                    return [intent]
            self.logger.info(
                "WAIT_PULLBACK active for short rebuild",
                extra={
                    "event": "rebuild_short_skipped",
                    "state": self.state_machine.state.value,
                    "price": price,
                    "long_size": self._get_position_snapshot()[0],
                    "short_size": self._get_position_snapshot()[1],
                    "long_avg": self._get_position_snapshot()[2],
                    "short_avg": self._get_position_snapshot()[3],
                    "rebound_trigger": rebound_trigger,
                    "failover_trigger": failover_trigger,
                    "reason": "waiting_for_rebound_or_failover",
                    "result": "skipped",
                },
            )
            return []
        if self._confirmed_up_move(price):
            intent = self._build_reduce_side_intent(
                "long",
                price,
                "normal_reduce_long",
            )
            if intent:
                self._set_wait_pullback("long", price)
                self._set_structure_reference(
                    price,
                    "normal_reduce_long",
                    update_high=True,
                )
                self._transition_state_if_needed(
                    StrategyState.WAIT_PULLBACK,
                    "long reduced, waiting for pullback",
                    price,
                    spread,
                )
                return [intent]
        if self._confirmed_down_move(price):
            intent = self._build_reduce_side_intent(
                "short",
                price,
                "normal_reduce_short",
            )
            if intent:
                self._set_wait_pullback("short", price)
                self._set_structure_reference(
                    price,
                    "normal_reduce_short",
                    update_low=True,
                )
                self._transition_state_if_needed(
                    StrategyState.WAIT_PULLBACK,
                    "short reduced, waiting for pullback",
                    price,
                    spread,
                )
                return [intent]
        if self._is_size_balanced():
            if doc_spread_pct > self.config.spread_heal_trigger_pct:
                self._transition_state_if_needed(
                    StrategyState.SIZE_RESET_ONLY,
                    "size balanced but spread unhealthy",
                    price,
                    spread,
                )
            else:
                self._cancel_preplaced_heal_orders("full_reset")
                self._reset_cycle_progress()
                self._transition_state_if_needed(
                    StrategyState.FULL_RESET_READY,
                    "size balanced and spread healthy",
                    price,
                    spread,
                )
            return []
        self._transition_state_if_needed(
            StrategyState.WAIT_NO_ACTION,
            "no clean documented trigger",
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
                "event": "state_transition",
                "state": new_state.value,
                "reason": reason,
                "price": price,
                "spread": spread,
                "result": "transitioned",
            },
        )

    def _calculate_doc_spread_pct(self) -> float:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        if long_avg <= 0 or short_avg <= 0:
            return 0.0
        return abs(long_avg - short_avg) / long_avg * 100

    def _record_realized_pnl_by_side(self, side: str, pnl: float) -> None:
        if side == "long":
            self._realized_long_pnl_total += pnl
        elif side == "short":
            self._realized_short_pnl_total += pnl
        self.logger.info(
            "Recorded realized pnl by side",
            extra={
                "side": side,
                "pnl": pnl,
                "realized_long_pnl_total": self._realized_long_pnl_total,
                "realized_short_pnl_total": self._realized_short_pnl_total,
                "realized_pnl_total": self._realized_long_pnl_total
                + self._realized_short_pnl_total,
            },
        )

    def _realized_pnl_total(self) -> float:
        return self._realized_long_pnl_total + self._realized_short_pnl_total

    def _calculate_basket_pnl(self, price: float) -> float:
        long_size, short_size, long_avg, short_avg = self._get_position_snapshot()
        if price <= 0:
            return self._realized_pnl_total()
        unrealized_long = (price - long_avg) * long_size
        unrealized_short = (short_avg - price) * short_size
        return self._realized_pnl_total() + unrealized_long + unrealized_short

    def _current_ratio(self) -> float:
        long_size, short_size, _, _ = self._get_position_snapshot()
        if short_size <= 0:
            return 0.0
        return long_size / short_size

    def _is_size_balanced(self) -> bool:
        ratio = self._current_ratio()
        if ratio <= 0:
            return False
        tolerance = max(0.0, self.config.size_balance_tolerance)
        return abs(ratio - 1.0) <= tolerance

    def _initialize_structure_refs(self, price: float) -> None:
        if price <= 0:
            return
        if self._last_relevant_high is None:
            self._last_relevant_high = price
        if self._last_relevant_low is None:
            self._last_relevant_low = price

    def _set_structure_reference(
        self,
        price: float,
        event: str,
        *,
        update_high: bool = False,
        update_low: bool = False,
    ) -> None:
        if price <= 0:
            return
        if not update_high and not update_low:
            update_high = True
            update_low = True
        if update_high:
            self._last_relevant_high = price
        if update_low:
            self._last_relevant_low = price
        self._last_structure_event = event
        self.logger.info(
            "Updated structural reference",
            extra={
                "event": event,
                "reference_price": price,
                "update_high": update_high,
                "update_low": update_low,
                "last_relevant_high": self._last_relevant_high,
                "last_relevant_low": self._last_relevant_low,
            },
        )

    def _confirmed_up_move(self, price: float) -> bool:
        self._initialize_structure_refs(price)
        if not self._last_relevant_high or price <= 0:
            return False
        return price >= self._last_relevant_high * (1 + self.config.structural_trigger_pct)

    def _confirmed_down_move(self, price: float) -> bool:
        self._initialize_structure_refs(price)
        if not self._last_relevant_low or price <= 0:
            return False
        return price <= self._last_relevant_low * (1 - self.config.structural_trigger_pct)

    def _side_adds_remaining(self, side: str) -> int:
        max_adds = max(0, int(self.config.healing_max_adds_per_cycle))
        used = self._long_adds_in_cycle if side == "long" else self._short_adds_in_cycle
        return max(0, max_adds - used)

    def _record_add_for_side(self, side: str) -> None:
        if side == "long":
            self._long_adds_in_cycle += 1
            self._long_heal_adds = self._long_adds_in_cycle
        else:
            self._short_adds_in_cycle += 1
            self._short_heal_adds = self._short_adds_in_cycle

    def _clear_wait_context(self) -> None:
        self._pending_rebuild_side = None
        self._pending_failover_side = None
        self._wait_reference_price = None

    def _set_wait_pullback(self, reduced_side: str, reference_price: float) -> None:
        self._pending_rebuild_side = reduced_side
        self._pending_failover_side = "short" if reduced_side == "long" else "long"
        self._wait_reference_price = reference_price

    def _reset_cycle_progress(self) -> None:
        self._reset_heal_counts()
        self._clear_wait_context()
        self._clear_aggressive_down_heal_state()
        self._clear_preplaced_heal_state(reset_generation=True)

    @staticmethod
    def _normalize_purpose_name(purpose: str | None) -> str:
        return (purpose or "").strip().lower()

    def _is_preplaced_heal_purpose(self, purpose: str | None) -> bool:
        return self._normalize_purpose_name(purpose) in {
            "preplaced_heal_long_limit",
            "preplaced_heal_short_limit",
        }

    def _is_preplaced_heal_state(self, state: StrategyState | None = None) -> bool:
        current_state = state or self.state_machine.state
        return current_state in {
            StrategyState.SPREAD_HEALING,
            StrategyState.SIZE_RESET_ONLY,
        }

    def _has_active_preplaced_heal_orders(self) -> bool:
        active_statuses = {"PENDING_SUBMIT", "OPEN", "PARTIAL", "UNKNOWN"}
        active_orders = getattr(self, "active_orders", {})
        with self._order_lock:
            return any(
                self._is_preplaced_heal_purpose(order.get("purpose"))
                and order.get("status") in active_statuses
                for order in active_orders.values()
            )

    def _clear_preplaced_heal_state(self, *, reset_generation: bool = False) -> None:
        self._preplaced_heal_orders_armed = False
        self._active_preplaced_heal_long_client_id = None
        self._active_preplaced_heal_short_client_id = None
        self._preplaced_heal_rearm_in_progress = False
        if reset_generation:
            self._preplaced_heal_generation = 0

    def _clear_aggressive_down_heal_state(
        self, *, preserve_phase_completion: bool = False
    ) -> None:
        self._aggressive_down_heal_initial_short_size = None
        self._aggressive_down_heal_reference_price = None
        self._phase2_short_profit_budget_reserved = 0.0
        self._phase3_long_target_reference_size = None
        self._phase4_short_target_reference_size = None
        if not preserve_phase_completion:
            self._aggressive_down_heal_phase_completed = False

    def _aggressive_down_heal_enabled(self) -> bool:
        return bool(self.config.enable_aggressive_heal_phase)

    def _phase2_short_profit_long_reduce_enabled(self) -> bool:
        return bool(self.config.enable_phase2_short_profit_long_reduce)

    def _phase3_long_rebuild_enabled(self) -> bool:
        return bool(self.config.enable_phase3_long_rebuild)

    def _ensure_phase3_long_target_reference(self) -> None:
        return ensure_phase3_long_target_reference(self)

    def _phase4_short_rebuild_enabled(self) -> bool:
        return phase4_short_rebuild_enabled(self)

    def _ensure_phase4_short_target_reference(self) -> None:
        return ensure_phase4_short_target_reference(self)

    def _phase3_target_long_qty(self) -> float:
        return phase3_target_long_qty(self)

    def _phase3_target_reached(self) -> bool:
        return phase3_target_reached(self)

    def _phase4_target_short_qty(self) -> float:
        return phase4_target_short_qty(self)

    def _phase4_target_reached(self) -> bool:
        return phase4_target_reached(self)

    def _phase3_long_rebuild_ready(self) -> bool:
        return phase3_long_rebuild_ready(self)

    def _phase4_short_rebuild_ready(self) -> bool:
        return phase4_short_rebuild_ready(self)

    def _phase5_fine_heal_ready(self) -> bool:
        return phase5_fine_heal_ready(self)

    def _build_phase3_long_rebuild_intent(self, price: float) -> OrderIntent | None:
        return build_phase3_long_rebuild_intent(self, price)

    def _build_phase4_short_rebuild_intent(self, price: float) -> OrderIntent | None:
        return build_phase4_short_rebuild_intent(self, price)

    def _phase2_short_profit_budget_available(self) -> float:
        return phase2_short_profit_budget_available(self)

    def _maybe_build_phase2_long_reduce_intent(self, price: float) -> OrderIntent | None:
        return maybe_build_phase2_long_reduce_intent(self, price)

    def _phase2_long_reduce_ready(self) -> bool:
        return phase2_long_reduce_ready(self)

    def _build_phase2_long_reduce_from_short_profit_intent(
        self, price: float
    ) -> OrderIntent | None:
        return build_phase2_long_reduce_from_short_profit_intent(self, price)

    def _record_phase2_short_profit_budget_usage(
        self, client_order_id: str, order: dict[str, Any] | None, source: str
    ) -> None:
        return record_phase2_short_profit_budget_usage(
            self, client_order_id, order, source
        )

    def _ensure_aggressive_down_heal_tracking(self, price: float) -> None:
        return ensure_aggressive_down_heal_tracking(self, price)

    def _aggressive_down_heal_complete(self) -> bool:
        return aggressive_down_heal_complete(self)

    def _confirmed_aggressive_down_heal_move(self, price: float) -> bool:
        return confirmed_aggressive_down_heal_move(self, price)

    def _build_aggressive_down_heal_short_intent(
        self, price: float
    ) -> OrderIntent | None:
        return build_aggressive_down_heal_short_intent(self, price)

    def _compute_preplaced_heal_prices(
        self, long_avg: float, short_avg: float
    ) -> tuple[float, float]:
        return compute_preplaced_heal_prices(self, long_avg, short_avg)

    def _preplaced_heal_mode_active(self, *, spread_pct: float | None = None) -> bool:
        return preplaced_heal_mode_active(self, spread_pct=spread_pct)

    def _should_arm_preplaced_heal_orders(self) -> bool:
        return should_arm_preplaced_heal_orders(self)

    def _build_preplaced_heal_limit_intents(
        self,
    ) -> tuple[OrderIntent, OrderIntent] | None:
        return build_preplaced_heal_limit_intents(self)

    def _collect_preplaced_heal_order_ids(
        self, generation: int
    ) -> tuple[str | None, str | None]:
        return collect_preplaced_heal_order_ids(self, generation)

    def _arm_preplaced_heal_orders(self) -> bool:
        return arm_preplaced_heal_orders(self)

    def _cancel_order_by_client_id(self, client_id: str, reason: str) -> bool:
        return cancel_order_by_client_id(self, client_id, reason)

    def _cancel_preplaced_heal_orders(
        self,
        reason: str,
        *,
        exclude_client_order_id: str | None = None,
        long_client_id: str | None = None,
        short_client_id: str | None = None,
    ) -> None:
        return cancel_preplaced_heal_orders(
            self,
            reason,
            exclude_client_order_id=exclude_client_order_id,
            long_client_id=long_client_id,
            short_client_id=short_client_id,
        )

    def _cancel_recovered_preplaced_heal_orders(self, reason: str) -> None:
        return cancel_recovered_preplaced_heal_orders(self, reason)

    def _handle_preplaced_heal_fill(
        self, client_order_id: str, purpose: str | None, source: str
    ) -> None:
        return handle_preplaced_heal_fill(self, client_order_id, purpose, source)

    def _base_add_qty(self, side: str) -> float:
        long_size, short_size, _, _ = self._get_position_snapshot()
        side_size = long_size if side == "long" else short_size
        return side_size * max(0.0, self.config.action_size_pct)

    def _base_reduce_qty(self, side: str) -> float:
        long_size, short_size, _, _ = self._get_position_snapshot()
        side_size = long_size if side == "long" else short_size
        return side_size * max(0.0, self.config.action_size_pct)

    def _fine_heal_size_pct(self) -> float:
        if self.config.enable_fine_heal_phase:
            return max(0.0, self.config.fine_heal_size_pct)
        return max(0.0, self.config.action_size_pct)

    def _build_market_intent(
        self,
        side: str,
        qty: float,
        price: float,
        purpose: str,
        *,
        reduce_only: bool,
    ) -> OrderIntent | None:
        self.logger.debug(
            "Market intent evaluation started",
            extra={
                "event": "order_intent_prepared",
                "purpose": purpose,
                "side": side,
                "price": price,
                "raw_qty": qty,
                "reduce_only": reduce_only,
                "order_type": "Market",
                "state": self.state_machine.state.value,
                "result": "evaluating",
            },
        )
        normalized_qty = self._normalize_order_qty(qty, purpose)
        if normalized_qty <= 0:
            self.logger.info(
                "Skipping action: normalized qty zero",
                extra={
                    "event": "order_intent_skipped",
                    "purpose": purpose,
                    "side": side,
                    "price": price,
                    "raw_qty": qty,
                    "normalized_qty": normalized_qty,
                    "reduce_only": reduce_only,
                    "order_type": "Market",
                    "state": self.state_machine.state.value,
                    "reason": "normalized_qty_zero",
                    "result": "skipped",
                },
            )
            return None
        if not self._meets_min_order_value(price, normalized_qty, purpose):
            self.logger.info(
                "Skipping action: below minimum order value",
                extra={
                    "event": "order_intent_skipped",
                    "purpose": purpose,
                    "side": side,
                    "price": price,
                    "raw_qty": qty,
                    "normalized_qty": normalized_qty,
                    "reduce_only": reduce_only,
                    "order_type": "Market",
                    "state": self.state_machine.state.value,
                    "reason": "below_min_order_value",
                    "result": "skipped",
                },
            )
            return None
        intent = OrderIntent(
            side=side,
            qty=normalized_qty,
            price=price,
            purpose=purpose,
            order_type="Market",
            reduce_only=reduce_only,
        )
        self.logger.info(
            "Market intent prepared",
            extra={
                "event": "order_intent_prepared",
                "purpose": purpose,
                "side": side,
                "price": price,
                "raw_qty": qty,
                "normalized_qty": normalized_qty,
                "reduce_only": reduce_only,
                "order_type": "Market",
                "state": self.state_machine.state.value,
                "result": "prepared",
            },
        )
        return intent

    def _long_heal_add_qty(self) -> float:
        long_size, _, _, _ = self._get_position_snapshot()
        qty = long_size * self._fine_heal_size_pct()
        self.logger.debug(
            "Long heal quantity calculated",
            extra={
                "event": "spread_heal_long_qty_calculated",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": self.state_machine.state.value,
                "long_size": long_size,
                "raw_qty": qty,
                "result": "calculated",
            },
        )
        return qty

    def _short_heal_add_qty(self) -> float:
        long_size, short_size, _, _ = self._get_position_snapshot()
        qty = min(short_size, short_size * self._fine_heal_size_pct())
        self.logger.debug(
            "Short heal quantity calculated",
            extra={
                "event": "spread_heal_short_qty_calculated",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": self.state_machine.state.value,
                "long_size": long_size,
                "short_size": short_size,
                "raw_qty": qty,
                "result": "calculated",
            },
        )
        return qty

    def _long_heal_adds_remaining(self) -> int:
        return self._side_adds_remaining("long")

    def _short_heal_adds_remaining(self) -> int:
        return self._side_adds_remaining("short")

    def _long_heal_improves_avg(
        self, long_size: float, long_avg: float, price: float, add_qty: float
    ) -> bool:
        if long_size <= 0 or add_qty <= 0:
            self.logger.debug(
                "Long avg improvement check skipped",
                extra={
                    "event": "avg_improvement_checked",
                    "purpose": "spread_heal_long",
                    "side": "long",
                    "state": self.state_machine.state.value,
                    "price": price,
                    "long_size": long_size,
                    "long_avg": long_avg,
                    "raw_qty": add_qty,
                    "reason": "invalid_size_or_qty",
                    "result": False,
                },
            )
            return False
        new_avg = (long_avg * long_size + price * add_qty) / (long_size + add_qty)
        improved = new_avg < long_avg - 1e-9
        self.logger.debug(
            "Long avg improvement evaluated",
            extra={
                "event": "avg_improvement_checked",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": self.state_machine.state.value,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "raw_qty": add_qty,
                "new_avg": new_avg,
                "result": improved,
            },
        )
        return improved

    def _short_heal_improves_avg(
        self, short_size: float, short_avg: float, price: float, add_qty: float
    ) -> bool:
        if short_size <= 0 or add_qty <= 0:
            self.logger.debug(
                "Short avg improvement check skipped",
                extra={
                    "event": "avg_improvement_checked",
                    "purpose": "spread_heal_short",
                    "side": "short",
                    "state": self.state_machine.state.value,
                    "price": price,
                    "short_size": short_size,
                    "short_avg": short_avg,
                    "raw_qty": add_qty,
                    "reason": "invalid_size_or_qty",
                    "result": False,
                },
            )
            return False
        new_avg = (short_avg * short_size + price * add_qty) / (short_size + add_qty)
        improved = new_avg > short_avg + 1e-9
        self.logger.debug(
            "Short avg improvement evaluated",
            extra={
                "event": "avg_improvement_checked",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": self.state_machine.state.value,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "raw_qty": add_qty,
                "new_avg": new_avg,
                "result": improved,
            },
        )
        return improved

    def _reset_heal_counts(self) -> None:
        self._long_heal_adds = 0
        self._short_heal_adds = 0
        self._long_adds_in_cycle = 0
        self._short_adds_in_cycle = 0
        self._spread_healing_active = False

    def _build_spread_heal_long_intent(
        self, price: float, spread_pct: float
    ) -> OrderIntent | None:
        return build_spread_heal_long_intent(self, price, spread_pct)

    def _build_spread_heal_short_intent(
        self, price: float, spread_pct: float
    ) -> OrderIntent | None:
        return build_spread_heal_short_intent(self, price, spread_pct)

    def _is_spread_heal_short_order(self, order: dict[str, Any] | None) -> bool:
        return is_spread_heal_short_order(self, order)

    def _is_spread_heal_long_order(self, order: dict[str, Any] | None) -> bool:
        return is_spread_heal_long_order(self, order)

    def _is_paired_partial_sl_long_order(self, order: dict[str, Any] | None) -> bool:
        return is_paired_partial_sl_long_order(self, order)

    def _is_paired_partial_sl_short_order(self, order: dict[str, Any] | None) -> bool:
        return is_paired_partial_sl_short_order(self, order)

    def _is_paired_long_close_order(self, order: dict[str, Any] | None) -> bool:
        return is_paired_long_close_order(self, order)

    def _calculate_paired_partial_sl_long_trigger(self, short_fill_price: float) -> float:
        return calculate_paired_partial_sl_long_trigger(self, short_fill_price)

    def _calculate_paired_partial_sl_short_trigger(self, long_fill_price: float) -> float:
        return calculate_paired_partial_sl_short_trigger(self, long_fill_price)

    def _build_paired_partial_sl_long_intent_from_filled_short(
        self,
        client_order_id: str,
        order: dict[str, Any],
    ) -> OrderIntent | None:
        return build_paired_partial_sl_long_intent_from_filled_short(
            self,
            client_order_id,
            order,
        )

    def _build_paired_partial_sl_short_intent_from_filled_long(
        self,
        client_order_id: str,
        order: dict[str, Any],
    ) -> OrderIntent | None:
        return build_paired_partial_sl_short_intent_from_filled_long(
            self,
            client_order_id,
            order,
        )

    def _handle_filled_spread_heal_short(
        self, client_order_id: str, order: dict[str, Any], source: str
    ) -> None:
        return handle_filled_spread_heal_short(
            self,
            client_order_id,
            order,
            source,
        )

    def _handle_filled_spread_heal_long(
        self, client_order_id: str, order: dict[str, Any], source: str
    ) -> None:
        return handle_filled_spread_heal_long(
            self,
            client_order_id,
            order,
            source,
        )

    def _cancel_future_open_short_heal_orders(self) -> None:
        return cancel_future_open_short_heal_orders(self)

    def _cancel_future_open_long_heal_orders(self) -> None:
        return cancel_future_open_long_heal_orders(self)

    def _rebuild_future_short_heals_from_current_short_size(self) -> None:
        return rebuild_future_short_heals_from_current_short_size(self)

    def _rebuild_future_long_heals_from_current_long_size(self) -> None:
        return rebuild_future_long_heals_from_current_long_size(self)

    def _handle_filled_paired_long_close(
        self, client_order_id: str, order: dict[str, Any], source: str
    ) -> None:
        return handle_filled_paired_long_close(
            self,
            client_order_id,
            order,
            source,
        )

    def _handle_filled_paired_short_close(
        self, client_order_id: str, order: dict[str, Any], source: str
    ) -> None:
        return handle_filled_paired_short_close(
            self,
            client_order_id,
            order,
            source,
        )

    def _build_reduce_side_intent(
        self, side: str, price: float, purpose: str
    ) -> OrderIntent | None:
        qty = self._base_reduce_qty(side)
        self.logger.debug(
            "Reduce-side intent quantity calculated",
            extra={
                "event": "order_intent_prepared",
                "purpose": purpose,
                "side": side,
                "price": price,
                "raw_qty": qty,
                "reduce_only": True,
                "order_type": "Market",
                "state": self.state_machine.state.value,
                "result": "calculating",
            },
        )
        return self._build_market_intent(
            side,
            qty,
            price,
            purpose,
            reduce_only=True,
        )

    def _build_add_side_intent(
        self, side: str, price: float, purpose: str
    ) -> OrderIntent | None:
        if self._side_adds_remaining(side) <= 0:
            self.logger.info(
                "Skipping add: max adds reached for side",
                extra={
                    "event": "order_intent_skipped",
                    "side": side,
                    "purpose": purpose,
                    "adds_remaining": self._side_adds_remaining(side),
                    "price": price,
                    "reduce_only": False,
                    "order_type": "Market",
                    "state": self.state_machine.state.value,
                    "reason": "max_adds_reached",
                    "result": "skipped",
                },
            )
            return None
        qty = self._base_add_qty(side)
        self.logger.debug(
            "Add-side intent quantity calculated",
            extra={
                "event": "order_intent_prepared",
                "purpose": purpose,
                "side": side,
                "price": price,
                "raw_qty": qty,
                "reduce_only": False,
                "order_type": "Market",
                "state": self.state_machine.state.value,
                "result": "calculating",
            },
        )
        return self._build_market_intent(
            side,
            qty,
            price,
            purpose,
            reduce_only=False,
        )

    def _can_fire_basket_exit(self, basket_pnl: float) -> bool:
        return basket_pnl >= 0.0

    def _fast_fill_cooldown_active(self) -> bool:
        if not self._last_fast_fill_time:
            return False
        return (
            _elapsed_seconds_since(self._last_fast_fill_time)
            < self.config.fast_fill_rebuy_cooldown_seconds
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
                "rebuy_order_active": False,
            },
        )
        self._last_status_log = _utcnow()

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
                summarized: list[str] = []
                for pos in positions or []:
                    symbol = (pos.get("symbol") or "").upper()
                    side = (pos.get("side") or pos.get("positionSide") or "").lower()
                    size = float(pos.get("size") or pos.get("positionQty") or 0.0)
                    avg = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
                    summarized.append(f"{symbol}:{side}:{size:.4f}@{avg:.6f}")
                self.logger.info(
                    "Startup positions fetch",
                    extra={
                        "results": summarized,
                        "symbol": self.config.default_symbol,
                        "category": self.config.category,
                    },
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
                if not active_symbol:
                    self.logger.info(
                        "REST startup: hedge symbol not ready",
                        extra={
                            "symbol": self.config.default_symbol,
                            "state": self.state_machine.state.value,
                            "long_size": long_size,
                            "short_size": short_size,
                            "positions": summarized,
                        },
                    )

                if active_symbol:
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
                    self._cancel_recovered_preplaced_heal_orders(
                        "startup_recovered_preplaced_heal_cleanup"
                    )
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
                    self._set_initialized(True)
                    with self._position_sync_lock:
                        self.last_rebuy_price = self.position_manager.long_avg
                    _, _, _, short_avg = self._get_position_snapshot()
                    reference_price = self.last_price or short_avg or long_avg
                    if reference_price and reference_price > 0:
                        self._set_structure_reference(
                            reference_price,
                            "startup_hedge_ready",
                            update_high=True,
                            update_low=True,
                        )
                    if self._is_size_balanced() and self._calculate_doc_spread_pct() <= self.config.spread_heal_trigger_pct:
                        self._cancel_preplaced_heal_orders("full_reset")
                        self.state_machine.transition(StrategyState.FULL_RESET_READY)
                    else:
                        self.state_machine.transition(StrategyState.WAIT_NO_ACTION)

                    self.logger.info(
                        "REST startup: both positions detected → strategy started",
                        extra={
                            "symbol": active_symbol,
                            "long_size": long_size,
                            "short_size": short_size,
                            "auto_cycle_armed": False,
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
        self.logger.info(
            "Legacy ensure_hedge_integrity disabled under final strategy",
            extra={"current_price": current_price},
        )
        return None

    def _transition_state_if_needed(
        self,
        new_state: StrategyState,
        reason: str,
        price: float,
        spread: float,
    ) -> bool:
        current_state = self.state_machine.state
        will_cancel_preplaced = (
            self._has_active_preplaced_heal_orders()
            and self._is_preplaced_heal_state(current_state)
            and not self._is_preplaced_heal_state(new_state)
        )
        self.logger.debug(
            "State transition evaluated",
            extra={
                "event": "state_transition_evaluated",
                "state": current_state.value,
                "target_state": new_state.value,
                "reason": reason,
                "price": price,
                "spread": spread,
                "will_cancel_preplaced_heals": will_cancel_preplaced,
                "result": "evaluating",
            },
        )
        if self.state_machine.state == new_state:
            self.logger.debug(
                "State transition skipped",
                extra={
                    "event": "state_transition_evaluated",
                    "state": current_state.value,
                    "target_state": new_state.value,
                    "reason": reason,
                    "price": price,
                    "spread": spread,
                    "result": "already_in_state",
                },
            )
            return False
        if will_cancel_preplaced:
            self._cancel_preplaced_heal_orders(
                f"state_transition_to_{new_state.value}"
            )
        self.state_machine.transition(new_state)
        self._log_state_transition(new_state, reason, price, spread)
        return True

    def _normalize_order_qty(self, qty: float, purpose: str) -> float:
        if qty <= 0 or not self.order_manager:
            self.logger.debug(
                "Order qty normalization bypassed",
                extra={
                    "event": "order_qty_normalized",
                    "purpose": purpose,
                    "requested_qty": qty,
                    "state": self.state_machine.state.value,
                    "reason": "non_positive_qty_or_missing_order_manager",
                    "result": qty,
                },
            )
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
                    "event": "order_qty_normalized",
                    "symbol": self.config.default_symbol,
                    "purpose": purpose,
                    "requested_qty": qty,
                    "state": self.state_machine.state.value,
                    "normalized_qty": normalized_qty,
                    "result": "zero",
                },
            )
            return 0.0
        if abs(normalized_qty - qty) > 1e-9:
            self.logger.info(
                "Order qty normalized",
                extra={
                    "event": "order_qty_normalized",
                    "symbol": self.config.default_symbol,
                    "purpose": purpose,
                    "requested_qty": qty,
                    "normalized_qty": normalized_qty,
                    "state": self.state_machine.state.value,
                    "result": "adjusted",
                },
            )
        else:
            self.logger.debug(
                "Order qty normalization unchanged",
                extra={
                    "event": "order_qty_normalized",
                    "symbol": self.config.default_symbol,
                    "purpose": purpose,
                    "requested_qty": qty,
                    "normalized_qty": normalized_qty,
                    "state": self.state_machine.state.value,
                    "result": "unchanged",
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
            self.logger.debug(
                "Minimum order value check bypassed",
                extra={
                    "event": "min_order_value_checked",
                    "purpose": purpose,
                    "price": price,
                    "qty": qty,
                    "state": self.state_machine.state.value,
                    "reason": "disabled_or_invalid_input",
                    "result": True,
                },
            )
            return True
        notional = price * qty
        if notional >= self.config.min_order_value:
            self.logger.debug(
                "Minimum order value satisfied",
                extra={
                    "event": "min_order_value_checked",
                    "purpose": purpose,
                    "price": price,
                    "qty": qty,
                    "notional": notional,
                    "min_notional": self.config.min_order_value,
                    "state": self.state_machine.state.value,
                    "result": True,
                },
            )
            return True
        self.logger.warning(
            "Order skipped: below minimum notional",
            extra={
                "event": "min_order_value_checked",
                "purpose": purpose,
                "symbol": self.config.default_symbol,
                "price": price,
                "qty": qty,
                "notional": notional,
                "min_notional": self.config.min_order_value,
                "state": self.state_machine.state.value,
                "result": False,
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
                    local["metadata"]["last_fill_price"] = float(
                        exchange_order.get("avgPrice")
                        or exchange_order.get("price")
                        or local.get("price")
                        or 0.0
                    )
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
        if (
            long_size > 0
            and short_size > 0
            and self._phase3_long_target_reference_size is None
            and self._aggressive_down_heal_initial_short_size is None
        ):
            self._phase3_long_target_reference_size = long_size
        if (
            long_size > 0
            and short_size > 0
            and self._phase4_short_target_reference_size is None
            and self._aggressive_down_heal_initial_short_size is None
        ):
            self._phase4_short_target_reference_size = short_size
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
        log_path = Path(self.config.log_file)
        if log_path.parent:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        has_file = any(
            isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == str(log_path)
            for handler in self.logger.handlers
        )
        if not has_file:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(format_str))
            self.logger.addHandler(file_handler)


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
            metadata["last_fill_price"] = float(
                history_order.get("avgPrice")
                or history_order.get("price")
                or current_order.get("price")
                or 0.0
            )
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
            if self._normalize_purpose_name(order.get("purpose")) in {
                "paired_long_close",
                "paired_partial_sl_long",
                "paired_partial_sl_short",
            }:
                key = (
                    order["side"],
                    round(order["size"], 4),
                    order["purpose"],
                    round(float(order.get("price") or 0.0), 8),
                )
            else:
                key = (
                    order["side"],
                    round(order["size"], 4),
                    order["purpose"],
                )
            self._submitted_orders.discard(key)
            self.logger.info(
                "Order tracking updated after fill",
                extra={
                    "event": "order_tracking_updated",
                    "client_order_id": client_order_id,
                    "purpose": order["purpose"],
                    "side": order["side"],
                    "qty": order["size"],
                    "reason": "mark_order_filled",
                    "result": "removed_from_submitted_tracking",
                },
            )

    def _handle_order_finalized_locked(self, client_id: str, order: dict[str, Any]) -> None:
        finalized_order = self.active_orders.pop(client_id, None) or order
        if self._normalize_purpose_name(finalized_order.get("purpose")) in {
            "paired_long_close",
            "paired_partial_sl_long",
            "paired_partial_sl_short",
        }:
            key = (
                finalized_order["side"],
                round(finalized_order["size"], 4),
                finalized_order["purpose"],
                round(float(finalized_order.get("price") or 0.0), 8),
            )
        else:
            key = (
                finalized_order["side"],
                round(finalized_order["size"], 4),
                finalized_order["purpose"],
            )
        self._submitted_orders.discard(key)
        self.logger.info(
            "Order tracking updated after finalization",
            extra={
                "event": "order_tracking_updated",
                "client_order_id": client_id,
                "purpose": finalized_order["purpose"],
                "side": finalized_order["side"],
                "qty": finalized_order["size"],
                "reason": "handle_order_finalized",
                "result": "removed_from_submitted_tracking",
            },
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
            order_snapshot = dict(order)
            order["retry_count"] = 0
        if not exchange_confirmed:
            self.logger.info(
                "SKIP FILL EVENT – NOT CONFIRMED BY EXCHANGE",
                extra={
                    "event": "fill_processed",
                    "client_order_id": client_order_id,
                    "purpose": purpose,
                    "source": source,
                    "result": "skipped_not_confirmed",
                },
            )
            return
        self._log_hedge_snapshot(
            "fill_before_processing",
            reference_price=float(order_snapshot.get("price") or 0.0) or None,
            extra={
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
            },
        )
        self.mark_order_filled(client_order_id)
        fast_fill = source in {"fast_poll", "verify"}
        msg = "FAST FILL DETECTED" if fast_fill else "ORDER FILL CONFIRMED"
        self.logger.info(
            msg,
            extra={
                "event": "fill_processed",
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
                "result": "confirmed",
            },
        )
        if fast_fill:
            self._last_fast_fill_time = _utcnow()
        self._record_phase2_short_profit_budget_usage(
            client_order_id,
            order_snapshot,
            source,
        )
        if self._is_spread_heal_short_order(order_snapshot):
            self._handle_filled_spread_heal_short(
                client_order_id,
                order_snapshot,
                source,
            )
        elif self._is_spread_heal_long_order(order_snapshot):
            self._handle_filled_spread_heal_long(
                client_order_id,
                order_snapshot,
                source,
            )
        elif self._is_paired_partial_sl_short_order(order_snapshot):
            self._handle_filled_paired_short_close(
                client_order_id,
                order_snapshot,
                source,
            )
        elif self._is_paired_long_close_order(order_snapshot):
            self._handle_filled_paired_long_close(
                client_order_id,
                order_snapshot,
                source,
            )
        else:
            self._handle_preplaced_heal_fill(client_order_id, purpose, source)
            if not self._is_preplaced_heal_purpose(purpose):
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
                "event": "fill_processed",
                "client_order_id": client_order_id,
                "source": source,
                "purpose": purpose,
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "result": "positions_updated",
            },
        )
        self._log_hedge_snapshot(
            "fill_after_processing",
            reference_price=float(order_snapshot.get("price") or 0.0) or None,
            extra={
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
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
            metadata["last_fill_price"] = price
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


if __name__ == "__main__":
    config = StrategyConfig()
    strategy = PSRHStrategy(config)
    logger.info("Strategy initialized; waiting for hedge positions", extra={"state": strategy.state_machine.state.value})
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested, exiting.")
