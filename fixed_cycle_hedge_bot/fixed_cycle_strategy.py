from __future__ import annotations

import json
import math
import time
import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .base import HedgeStrategy, StrategyContext
from .models import CalculationTrace, FillEvent, HedgeSnapshot, RuntimeState, StrategyIntent

EXPECTED_CONFIG_PATH = Path("fixed_cycle_hedge_bot/config/fixed_cycle_config.json")


@dataclass
class FixedCycleHedgeConfig:
    symbol: str = "BTCUSDT"
    category: str = "linear"

    base_notional_usdt: float = 100.0
    hedge_ratio_short: float = 0.5
    initial_entry_order_type: str = "Market"
    initial_entry_reference_price: float = 0.0

    # New fixed-cycle settings
    reduction_pct_per_fill: float = 0.25
    long_cycle_qty_pct_of_initial: float = 25.0
    short_cycle_qty_pct_of_initial: float = 25.0
    long_fill_distance_pct: float = 0.5
    short_fill_distance_pct: float = 0.5

    market_fallback_slippage_type: str = "Percent"
    market_fallback_slippage_value: float = 0.05
    second_order_safety_offset_pct: float = 0.05

    # Target logic
    long_exit_reduce_only: bool = True
    short_exit_reduce_only: bool = True
    tp_profit_target_pct: float = 0.5
    tp_buffer_pct: float = 0.0  # optional extra buffer on top of BE+profit target
    fee_safety_buffer_pct: float = 0.14
    target_profit_usdt: float = 0.002
    net_realized_pnl_target: float = 0.0

    hard_stop_cycle: int = 8
    hard_stop_pct: float = 1.0
    max_cycles: int = 10

    leverage_long: float = 3.0
    leverage_short: float = 3.0

    use_reduce_only: bool = True

    rest_poll_after_fill_ms: int = 250
    ws_enabled: bool = True
    restart: bool = True  # persist cycle state across restarts when enabled
    order_refresh_cooldown_ms: int = 750

    price_tick_size: float = 0.1
    qty_step: float = 0.001
    min_order_qty: float = 0.001
    min_notional_usdt: float = 5.0

    @classmethod
    def from_json_file(cls, path: str | Path | None, *, enforce_expected_path: bool = True) -> "FixedCycleHedgeConfig":
        if not path:
            return cls()
        path_obj = Path(path).resolve()
        expected_path = EXPECTED_CONFIG_PATH.resolve()
        if enforce_expected_path and path_obj != expected_path:
            raise ValueError(
                f"Invalid config path: {path_obj}\n"
                f"Expected: {expected_path}"
            )

        payload = json.loads(path_obj.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Fixed-cycle config must be a JSON object")
        return cls(**payload)


logger = logging.getLogger(__name__)


def _emit_analyzer_event(logger: logging.Logger, event: str, payload: dict[str, Any]) -> None:
    data = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    logger.info(json.dumps(data))


class FixedCycleHedgeStrategy(HedgeStrategy):
    name = "fixed_cycle"

    STATE_INIT = "INIT"
    STATE_OPENING_HEDGE = "OPENING_HEDGE"
    STATE_PREPLACING_DOWNSIDE_ORDERS = "PREPLACING_DOWNSIDE_ORDERS"
    STATE_RUNNING = "RUNNING"
    STATE_RECONCILING_AFTER_FILL = "RECONCILING_AFTER_FILL"
    STATE_RESETTING_EXITS = "RESETTING_EXITS"
    STATE_HARD_STOP_MODE = "HARD_STOP_MODE"
    STATE_EXITED = "EXITED"
    STATE_ERROR = "ERROR"

    LONG_ENTRY_PURPOSE = "INITIAL_LONG_ENTRY"
    SHORT_ENTRY_PURPOSE = "INITIAL_SHORT_ENTRY"
    LONG_TP_EXIT_PURPOSE = "LONG_TP_EXIT"
    LONG_SL_EXIT_PURPOSE = "LONG_SL_EXIT"
    SHORT_TP_EXIT_PURPOSE = "SHORT_TP_EXIT"
    SHORT_SL_EXIT_PURPOSE = "SHORT_SL_EXIT"
    SHORT_HARD_STOP_PURPOSE = "SHORT_HARD_STOP_EXIT"

    def __init__(self, config: FixedCycleHedgeConfig | None = None) -> None:
        self.config = config or FixedCycleHedgeConfig()
        logger = logging.getLogger(__name__)
        config_fields = [field.name for field in fields(self.config)]
        long_attr = hasattr(self.config, "long_exit_reduce_only")
        short_attr = hasattr(self.config, "short_exit_reduce_only")
        logger.info(
            "[RUNTIME_CONFIG_DEBUG] file=%s type=%s module=%s fields=%s long_attr=%s short_attr=%s long_value=%s short_value=%s",
            __file__,
            type(self.config),
            self.config.__class__.__module__,
            config_fields,
            long_attr,
            short_attr,
            getattr(self.config, "long_exit_reduce_only", None),
            getattr(self.config, "short_exit_reduce_only", None),
        )

    def on_start(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        state.setdefault("bot_state", self.STATE_INIT)
        state.setdefault("current_long_cycle_index", 0)
        state.setdefault("current_short_cycle_index", 0)
        state.setdefault("current_effective_cycle", 0)
        state.setdefault("completed_cycles", 0)
        state.setdefault("cycle_waiting_for_short_tp", False)
        state.setdefault("pending_long_cycle_index", 0)
        state.setdefault("short_tp_pending_cycle", 0)
        state.setdefault("long_add_pending", False)
        state.setdefault("cycle_completed_count", 0)
        state.setdefault("open_long_qty", snapshot.long_qty)
        state.setdefault("open_short_qty", snapshot.short_qty)
        state.setdefault("long_avg", snapshot.long_avg)
        state.setdefault("short_avg", snapshot.short_avg)
        state.setdefault("realized_pnl_total", snapshot.realized_pnl_total)
        state.setdefault("entry_reference_price", 0.0)
        state.setdefault("initial_long_qty", 0.0)
        state.setdefault("initial_short_qty", 0.0)
        state.setdefault("initial_total_notional_usdt", 0.0)
        state.setdefault("last_structure_refresh_ms", 0)
        state.setdefault("initial_entry_submitted", False)
        state.setdefault("initial_entry_confirmed", False)
        state.setdefault("initial_entry_retry_count", 0)
        state.setdefault("last_exit_signature", None)
        state.setdefault("net_long_loss_balance", 0.0)
        state.setdefault("net_short_loss_balance", 0.0)
        state.setdefault("processed_pnl_exec_ids", set())
        state.setdefault("processed_pnl_exec_ids_order", [])
        state["recovery_marker_emitted"] = False
        state["block_closed_marker_emitted"] = False
        state["exit_armed_marker_emitted"] = False
        state.setdefault("exit_rebuild_allowed", True)
        state.setdefault("exit_rebuild_allowed", True)

        self._ensure_cycle_state(runtime_state)

        context.audit.log_event(
            "fixed_cycle_start",
            strategy=self.name,
            config=asdict(self.config),
            snapshot=snapshot,
        )

        has_existing_positions = snapshot.long_qty > 0 or snapshot.short_qty > 0
        has_existing_orders = bool(snapshot.active_orders)
        block_type = "recovered_position_block" if has_existing_positions else "fresh_entry"
        cycle_index = int(state.get("current_effective_cycle") or 0)
        _emit_analyzer_event(
            logger,
            "analyzer_block_started",
            {
                "symbol": self.config.symbol,
                "strategy": self.name,
                "block_type": block_type,
                "has_existing_positions": has_existing_positions,
                "has_existing_orders": has_existing_orders,
                "long_size": snapshot.long_qty,
                "short_size": snapshot.short_qty,
                "long_avg_price": snapshot.long_avg,
                "short_avg_price": snapshot.short_avg,
                "bot_state": state.get("bot_state"),
                "cycle_index": cycle_index,
            },
        )

        self._update_initial_entry_confirmation(snapshot, runtime_state)
        if snapshot.long_qty > 0 and snapshot.short_qty > 0:
            context.audit.log_event(
                "fixed_cycle_initial_entry_skipped",
                strategy=self.name,
                reason="existing_positions_on_exchange",
                long_qty=snapshot.long_qty,
                short_qty=snapshot.short_qty,
            )
            state["initial_entry_confirmed"] = True
            context.audit.log_event(
                "fixed_cycle_initial_entry_skipped",
                strategy=self.name,
                reason="positions_already_exist",
                long_qty=snapshot.long_qty,
                short_qty=snapshot.short_qty,
            )
            self._seed_initial_reference_if_missing(snapshot, runtime_state)
            self._sync_state_from_snapshot(snapshot, runtime_state)
            state["bot_state"] = self.STATE_PREPLACING_DOWNSIDE_ORDERS
            return self._rebuild_structure(snapshot, runtime_state, context, reason="startup_existing_positions")

        if self._has_open_initial_entry_orders(snapshot, runtime_state):
            state["initial_entry_submitted"] = True
            context.audit.log_event(
                "fixed_cycle_initial_entry_skipped",
                strategy=self.name,
                reason="open_initial_orders_exist",
            )
            return []

        state["bot_state"] = self.STATE_OPENING_HEDGE
        retry_count = int(state.get("initial_entry_retry_count") or 0) + 1
        state["initial_entry_retry_count"] = retry_count
        context.audit.log_event(
            "fixed_cycle_initial_entry_forced",
            strategy=self.name,
            current_price=snapshot.current_price,
            retry_count=retry_count,
        )
        intents = self._build_entry_intents(snapshot, runtime_state, context)
        if intents:
            state["initial_entry_submitted"] = True
            context.audit.log_event(
                "fixed_cycle_initial_entry_submitted",
                strategy=self.name,
                current_price=snapshot.current_price,
                intent_count=len(intents),
                retry_count=retry_count,
            )
        long_intents = [intent for intent in intents if intent.side == "long"]
        short_intents = [intent for intent in intents if intent.side == "short"]
        first_long_purpose = self._cycle_purpose("long", 1)
        first_short_purpose = self._cycle_purpose("short", 1)
        logger.debug(
            "fixed_cycle_downside_build_result %s",
            {
                "long_intent_count": len(long_intents),
                "short_intent_count": len(short_intents),
                "total_intent_count": len(intents),
                "purposes": [intent.purpose for intent in intents],
                "first_long_cycle_present": any(intent.purpose == first_long_purpose for intent in intents),
                "first_short_cycle_present": any(intent.purpose == first_short_purpose for intent in intents),
            },
        )
        return intents

    def on_tick(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        self._sync_state_from_snapshot(snapshot, runtime_state)
        self._update_initial_entry_confirmation(snapshot, runtime_state)

        if (
            snapshot.long_qty <= 0
            and snapshot.short_qty <= 0
            and not state.get("initial_entry_confirmed")
        ):
            if self._has_open_initial_entry_orders(snapshot, runtime_state):
                state["initial_entry_submitted"] = True
                context.audit.log_event(
                    "fixed_cycle_initial_entry_skipped",
                    strategy=self.name,
                    reason="open_initial_orders_exist",
                )
                return []

            retry_count = int(state.get("initial_entry_retry_count") or 0) + 1
            state["initial_entry_retry_count"] = retry_count
            context.audit.log_event(
                "fixed_cycle_initial_entry_retry",
                strategy=self.name,
                current_price=snapshot.current_price,
                retry_count=retry_count,
            )
            intents = self._build_entry_intents(snapshot, runtime_state, context)
            if intents:
                state["initial_entry_submitted"] = True
                context.audit.log_event(
                    "fixed_cycle_initial_entry_submitted",
                    strategy=self.name,
                    current_price=snapshot.current_price,
                    intent_count=len(intents),
                    retry_count=retry_count,
                )
            return intents

        if state.get("bot_state") == self.STATE_EXITED:
            return []

        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            state["bot_state"] = self.STATE_EXITED
            return []

        if snapshot.long_qty <= 0 or snapshot.short_qty <= 0:
            return self._maybe_refresh_structure(snapshot, runtime_state, context, reason="tick_partial_structure")

        if self._has_no_strategy_orders(snapshot):
            return self._maybe_refresh_structure(snapshot, runtime_state, context, reason="tick_missing_strategy_orders")

        return []

    def on_fill(
        self,
        fill_event: FillEvent,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        state["bot_state"] = self.STATE_RECONCILING_AFTER_FILL
        self._advance_cycle_from_fill(fill_event, runtime_state, context)

        context.audit.log_event(
            "fixed_cycle_fill_handling_started",
            strategy=self.name,
            fill=fill_event.to_dict(),
            bot_state=state["bot_state"],
        )

        if self.config.rest_poll_after_fill_ms > 0:
            time.sleep(self.config.rest_poll_after_fill_ms / 1000.0)

        refreshed_snapshot = context.refresh_snapshot("fixed_cycle_post_fill_rest") if context.refresh_snapshot else snapshot
        self._seed_initial_reference_if_missing(refreshed_snapshot, runtime_state)
        self._sync_state_from_snapshot(refreshed_snapshot, runtime_state)
        state = runtime_state.strategy_state

        if state.get("cycle_completed_count", 0) >= 2:
            refill_intents = self._build_entry_intents(refreshed_snapshot, runtime_state, context)
            if refill_intents:
                return refill_intents
        fast_intents = self._fast_path_second_order(fill_event, refreshed_snapshot, runtime_state, context)

        return fast_intents + self._rebuild_structure(refreshed_snapshot, runtime_state, context, reason="fill_reconcile")

    def on_reconcile(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        self._seed_initial_reference_if_missing(snapshot, runtime_state)
        self._sync_state_from_snapshot(snapshot, runtime_state)
        return self._maybe_refresh_structure(snapshot, runtime_state, context, reason="reconcile_guard")

    def _build_entry_intents(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state

        if int(state.get("cycle_completed_count") or 0) >= 2:
            current_price = float(snapshot.current_price or 0.0)
            if current_price <= 0:
                return []

            initial_long_qty = float(state.get("initial_long_qty") or 0.0)
            initial_short_qty = float(state.get("initial_short_qty") or 0.0)

            current_long_qty = float(snapshot.long_qty or 0.0)
            current_short_qty = float(snapshot.short_qty or 0.0)

            missing_long_qty = max(initial_long_qty - current_long_qty, 0.0)
            missing_short_qty = max(initial_short_qty - current_short_qty, 0.0)

            intents: list[StrategyIntent] = []

            if missing_long_qty > 0:
                refill_long_qty = self._normalize_qty(missing_long_qty, runtime_state)
                if refill_long_qty > 0:
                    intents.append(
                        StrategyIntent(
                            side="long",
                            qty=refill_long_qty,
                            price=None,
                            purpose="REFILL_LONG",
                            order_type="Market",
                            reduce_only=False,
                            metadata={"entry_role": "refill_long"},
                        )
                    )

            if missing_short_qty > 0:
                refill_short_qty = self._normalize_qty(missing_short_qty, runtime_state)
                if refill_short_qty > 0:
                    intents.append(
                        StrategyIntent(
                            side="short",
                            qty=refill_short_qty,
                            price=None,
                            purpose="REFILL_SHORT",
                            order_type="Market",
                            reduce_only=False,
                            metadata={"entry_role": "refill_short"},
                        )
                    )

            return intents

        entry_reference_price = float(runtime_state.strategy_state.get("entry_reference_price") or 0.0)
        resolved_price = snapshot.current_price if snapshot.current_price > 0 else 0.0
        if resolved_price <= 0 and runtime_state.last_snapshot and runtime_state.last_snapshot.current_price > 0:
            resolved_price = runtime_state.last_snapshot.current_price
        if resolved_price <= 0 and entry_reference_price > 0:
            resolved_price = entry_reference_price
        if resolved_price <= 0 and self.config.initial_entry_reference_price > 0:
            resolved_price = self.config.initial_entry_reference_price
        if resolved_price <= 0:
            context.audit.log_event(
                "fixed_cycle_entry_deferred_no_price",
                strategy=self.name,
                current_price=snapshot.current_price,
                last_snapshot_price=runtime_state.last_snapshot.current_price if runtime_state.last_snapshot else 0.0,
                entry_reference_price=entry_reference_price,
            )
            return []
        if resolved_price != snapshot.current_price:
            context.audit.log_event(
                "fixed_cycle_entry_price_fallback_used",
                strategy=self.name,
                current_price=snapshot.current_price,
                resolved_price=resolved_price,
                last_snapshot_price=runtime_state.last_snapshot.current_price if runtime_state.last_snapshot else 0.0,
                entry_reference_price=entry_reference_price,
            )

        long_qty = self._normalize_qty(
            self.config.base_notional_usdt / resolved_price, runtime_state
        )
        short_qty = self._normalize_qty(
            (self.config.base_notional_usdt * self.config.hedge_ratio_short) / resolved_price,
            runtime_state,
        )

        if long_qty <= 0 or short_qty <= 0:
            runtime_state.strategy_state["bot_state"] = self.STATE_ERROR
            context.audit.log_event(
                "fixed_cycle_entry_failed",
                strategy=self.name,
                reason="normalized_entry_qty_zero",
                long_qty=long_qty,
                short_qty=short_qty,
            )
            return []

        runtime_state.strategy_state["entry_reference_price"] = resolved_price
        runtime_state.strategy_state["initial_long_qty"] = long_qty
        runtime_state.strategy_state["initial_short_qty"] = short_qty
        runtime_state.strategy_state["initial_total_notional_usdt"] = (
            (long_qty * resolved_price) + (short_qty * resolved_price)
        )

        order_type = self.config.initial_entry_order_type
        price = self._normalize_price(resolved_price, runtime_state) if order_type == "Limit" else None

        traces = [
            CalculationTrace(
                name="initial_hedge_sizes",
                formula="qty = notional / current_price",
                inputs={
                    "base_notional_usdt": self.config.base_notional_usdt,
                    "hedge_ratio_short": self.config.hedge_ratio_short,
                    "current_price": resolved_price,
                },
                result={"long_qty": long_qty, "short_qty": short_qty},
            )
        ]

        context.audit.log_event(
            "fixed_cycle_entry_planned",
            strategy=self.name,
            current_price=snapshot.current_price,
            resolved_price=resolved_price,
            base_notional_usdt=self.config.base_notional_usdt,
            long_qty_raw=self.config.base_notional_usdt / resolved_price,
            long_qty_formula="base_notional_usdt / current_price",
            hedge_ratio_short=self.config.hedge_ratio_short,
            short_qty_raw=(self.config.base_notional_usdt * self.config.hedge_ratio_short) / resolved_price,
            short_qty_formula="(base_notional_usdt * hedge_ratio_short) / current_price",
            normalized_long_qty=long_qty,
            normalized_short_qty=short_qty,
            order_type=order_type,
            entry_price_raw=resolved_price,
            entry_price_normalized=price,
        )

        return [
            StrategyIntent(
                side="long",
                qty=long_qty,
                price=price,
                purpose=self.LONG_ENTRY_PURPOSE,
                order_type=order_type,
                reduce_only=False,
                metadata={"entry_role": "initial_long"},
                trace=traces,
            ),
            StrategyIntent(
                side="short",
                qty=short_qty,
                price=price,
                purpose=self.SHORT_ENTRY_PURPOSE,
                order_type=order_type,
                reduce_only=False,
                metadata={"entry_role": "initial_short"},
                trace=traces,
            ),
        ]

    def _maybe_refresh_structure(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> list[StrategyIntent]:
        now_ms = int(time.time() * 1000)
        last_ms = int(runtime_state.strategy_state.get("last_structure_refresh_ms") or 0)
        if now_ms - last_ms < self.config.order_refresh_cooldown_ms:
            return []
        runtime_state.strategy_state["last_structure_refresh_ms"] = now_ms
        return self._rebuild_structure(snapshot, runtime_state, context, reason=reason)

    def _rebuild_structure(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        self._seed_initial_reference_if_missing(snapshot, runtime_state)
        self._sync_state_from_snapshot(snapshot, runtime_state)
        self._update_initial_entry_confirmation(snapshot, runtime_state)
        cycle_state = self._ensure_cycle_state(runtime_state)
        has_no_strategy_orders = self._has_no_strategy_orders(snapshot)
        active_order_purposes = [
            order.purpose
            for order in snapshot.active_orders
            if order.purpose and order.status not in {"FILLED", "CANCELED", "REJECTED"}
        ]
        logger.debug(
            "fixed_cycle_rebuild_entry %s",
            {
                "reason": reason,
                "bot_state": state.get("bot_state"),
                "hard_stop_active": state.get("bot_state") == self.STATE_HARD_STOP_MODE,
                "current_price": snapshot.current_price,
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "long_avg": snapshot.long_avg,
                "short_avg": snapshot.short_avg,
                "realized_long_pnl_total": snapshot.realized_long_pnl_total,
                "realized_short_pnl_total": snapshot.realized_short_pnl_total,
                "current_long_cycle_index": int(state.get("current_long_cycle_index") or 0),
                "current_short_cycle_index": int(state.get("current_short_cycle_index") or 0),
                "current_effective_cycle": int(state.get("current_effective_cycle") or 0),
                "cycle_waiting_for_short_tp": bool(state.get("cycle_waiting_for_short_tp")),
                "initial_entry_confirmed": bool(state.get("initial_entry_confirmed")),
                "has_no_strategy_orders": has_no_strategy_orders,
                "active_order_count": len(snapshot.active_orders),
                "active_order_purposes": active_order_purposes,
            },
        )

        current_cycle = int(state.get("current_effective_cycle") or 0)

        open_initial_orders = self._collect_open_initial_entry_orders(snapshot)
        if open_initial_orders:
            context.audit.log_event(
                "fixed_cycle_structure_skip",
                strategy=self.name,
                skip_reason="initial_entry_order_still_open",
                open_initial_orders=open_initial_orders,
            )
            return []

        if (
            snapshot.long_qty > 0
            and snapshot.short_qty > 0
            and not state.get("initial_entry_confirmed")
            and not state.get("recovery_marker_emitted")
        ):
            open_purposes = [
                order.purpose
                for order in snapshot.active_orders
                if getattr(order, "purpose", None)
                and getattr(order, "status", None) not in {"FILLED", "CANCELED", "REJECTED"}
            ]
            cycle_index = int(state.get("current_effective_cycle") or 0)
            _emit_analyzer_event(
                logger,
                "analyzer_recovery_detected",
                {
                    "symbol": self.config.symbol,
                    "strategy": self.name,
                    "existing_long_size": snapshot.long_qty,
                    "existing_short_size": snapshot.short_qty,
                    "existing_long_avg_price": snapshot.long_avg,
                    "existing_short_avg_price": snapshot.short_avg,
                    "existing_open_orders": open_purposes,
                    "cycle_index": cycle_index,
                },
            )
            state["recovery_marker_emitted"] = True
        initial_long_qty = float(state.get("initial_long_qty") or 0.0)
        initial_short_qty = float(state.get("initial_short_qty") or 0.0)
        seeded_cycle_state = False
        if snapshot.long_qty > 0 and snapshot.short_qty > 0:
            entry_price = float(cycle_state.get("entry_price") or 0.0)
            desired_entry_price = snapshot.long_avg if snapshot.long_avg > 0 else snapshot.current_price
            if entry_price <= 0 and desired_entry_price > 0:
                cycle_state["entry_price"] = desired_entry_price
                seeded_cycle_state = True
            if initial_long_qty <= 0:
                state["initial_long_qty"] = snapshot.long_qty
                seeded_cycle_state = True
            if initial_short_qty <= 0:
                state["initial_short_qty"] = snapshot.short_qty
                seeded_cycle_state = True
            if seeded_cycle_state:
                self._write_cycle_state(cycle_state)
            initial_long_qty = float(state.get("initial_long_qty") or 0.0)
            initial_short_qty = float(state.get("initial_short_qty") or 0.0)
        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0 and (initial_long_qty <= 0 or initial_short_qty <= 0):
            context.audit.log_event(
                "fixed_cycle_initial_entry",
                strategy=self.name,
                reason="initial_entry_phase",
                snapshot=snapshot,
            )
            return self._build_entry_intents(snapshot, runtime_state, context)

        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            self._cancel_all_pending_orders(context)
            state["bot_state"] = self.STATE_EXITED
            context.audit.log_event("fixed_cycle_exited", strategy=self.name, reason=reason, snapshot=snapshot)
            self._reset_cycle_state(runtime_state)
            return []

        hard_stop_active = current_cycle >= self.config.hard_stop_cycle
        if hard_stop_active and context.cancel_open_orders_by_purpose:
            context.cancel_open_orders_by_purpose(self._all_cycle_purposes())

        state["bot_state"] = self.STATE_HARD_STOP_MODE if hard_stop_active else self.STATE_RESETTING_EXITS

        break_even_price, break_even_traces = self._calculate_break_even(snapshot, runtime_state)
        tp_price = self._calculate_tp_price(break_even_price, snapshot, runtime_state)
        state["latest_break_even_price"] = break_even_price
        state["latest_tp_price"] = tp_price

        downside_intents: list[StrategyIntent] = []
        if not hard_stop_active:
            downside_intents = self._build_downside_cycle_intents(snapshot, runtime_state, context)

        exit_intents: list[StrategyIntent] = self._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle,
            break_even_price,
            tp_price,
            hard_stop_active,
            context,
        )
        intents = downside_intents + exit_intents

        state["bot_state"] = self.STATE_HARD_STOP_MODE if hard_stop_active else self.STATE_RUNNING

        has_real_structure_change = bool(downside_intents or exit_intents)
        if has_real_structure_change:
            context.audit.log_event(
                "fixed_cycle_structure_rebuilt",
                strategy=self.name,
                reason=reason,
                hard_stop_active=hard_stop_active,
                break_even_price=break_even_price,
                tp_price=tp_price,
                current_long_cycle_index=state.get("current_long_cycle_index"),
                current_short_cycle_index=state.get("current_short_cycle_index"),
                current_effective_cycle=state.get("current_effective_cycle"),
                intents=intents,
                traces=[trace.to_dict() for trace in break_even_traces],
            )
        else:
            logger.debug(
                "fixed_cycle_structure_rebuilt skipped (no new intents) %s",
                {"reason": reason},
            )

        logger.debug(
            "fixed_cycle_rebuild_result_detailed %s",
            {
                "downside_intent_count": len(downside_intents),
                "exit_intent_count": len(exit_intents),
                "total_intent_count": len(intents),
                "downside_purposes": [intent.purpose for intent in downside_intents],
                "exit_purposes": [intent.purpose for intent in exit_intents],
                "has_long_cycle_purpose": any("_LONG_" in (intent.purpose or "") for intent in intents),
                "has_short_cycle_purpose": any("_SHORT_" in (intent.purpose or "") for intent in intents),
                "only_exit_intents": len(downside_intents) == 0 and len(exit_intents) > 0,
            },
        )
        return intents

    def _build_downside_cycle_intents(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        intents: list[StrategyIntent] = []
        state = runtime_state.strategy_state
        open_initial_orders = self._collect_open_initial_entry_orders(snapshot)
        if open_initial_orders:
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="initial_entry_order_still_open",
                open_initial_orders=open_initial_orders,
            )
            return intents

        if not state.get("long_add_rebuild_allowed", True):
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="long_add_locked",
            )
            return intents

        entry_reference_price = float(state.get("entry_reference_price") or 0.0)
        initial_long_qty = float(state.get("initial_long_qty") or 0.0)
        initial_short_qty = float(state.get("initial_short_qty") or 0.0)
        completed_cycles = int(state.get("cycle_completed_count") or 0)
        cycle_state = self._ensure_cycle_state(runtime_state)
        long_fill_price = self._cycle_state_last_fill_price(cycle_state.get("long_fills") or {})
        short_fill_price = self._cycle_state_last_fill_price(cycle_state.get("short_fills") or {})
        long_add_pending = bool(state.get("long_add_pending"))
        short_tp_pending_cycle = int(state.get("short_tp_pending_cycle") or 0)
        waiting_for_short_tp = bool(state.get("cycle_waiting_for_short_tp"))

        if completed_cycles >= self.config.max_cycles:
            logger.debug(
                "fixed_cycle_downside_skip %s",
                {
                    "skip_reason": "max_cycles_reached",
                    "completed_cycles": completed_cycles,
                    "max_cycles": self.config.max_cycles,
                },
            )
            return intents

        if entry_reference_price <= 0 or initial_long_qty <= 0 or initial_short_qty <= 0:
            logger.debug(
                "fixed_cycle_downside_skip %s",
                {
                    "skip_reason": "initial_quantities_missing",
                    "entry_reference_price": entry_reference_price,
                    "initial_long_qty": initial_long_qty,
                    "initial_short_qty": initial_short_qty,
                },
            )
            context.audit.log_event(
                "fixed_cycle_cycle_skipped",
                strategy=self.name,
                reason="initial_quantities_missing",
                entry_reference_price=entry_reference_price,
                initial_long_qty=initial_long_qty,
                initial_short_qty=initial_short_qty,
            )
            return intents

        reference_price = snapshot.current_price if snapshot.current_price > 0 else entry_reference_price
        cycle_entry_price = float(cycle_state.get("entry_price") or entry_reference_price)
        last_cycle_reference_price = float(cycle_state.get("last_cycle_reference_price") or 0.0)
        if (
            last_cycle_reference_price <= 0
            and not (cycle_state.get("long_fills") or cycle_state.get("short_fills"))
            and snapshot.long_avg > 0
        ):
            last_cycle_reference_price = float(snapshot.long_avg)
            cycle_state["last_cycle_reference_price"] = last_cycle_reference_price
        long_reference_candidate = last_cycle_reference_price
        long_reference = long_reference_candidate
        short_reference = (
            short_fill_price
            or cycle_entry_price
            or reference_price
        )
        logger.debug(
            "fixed_cycle_downside_build_inputs %s",
            {
                "entry_reference_price": entry_reference_price,
                "reference_price": reference_price,
                "cycle_entry_price": cycle_entry_price,
                "last_cycle_reference_price": last_cycle_reference_price,
                "cycle_long_fill_price": long_fill_price,
                "cycle_short_fill_price": short_fill_price,
                "current_long_cycle_index": int(state.get("current_long_cycle_index") or 0),
                "current_short_cycle_index": int(state.get("current_short_cycle_index") or 0),
                "max_cycles": self.config.max_cycles,
                "hard_stop_cycle": self.config.hard_stop_cycle,
                "long_fill_distance_pct": self.config.long_fill_distance_pct,
                "short_fill_distance_pct": self.config.short_fill_distance_pct,
                "long_cycle_qty_pct_of_initial": self.config.long_cycle_qty_pct_of_initial,
                "short_cycle_qty_pct_of_initial": self.config.short_cycle_qty_pct_of_initial,
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "long_avg": snapshot.long_avg,
                "short_avg": snapshot.short_avg,
                "initial_long_qty": initial_long_qty,
                "initial_short_qty": initial_short_qty,
                "long_add_pending": long_add_pending,
                "short_tp_pending_cycle": short_tp_pending_cycle,
                "cycle_waiting_for_short_tp": waiting_for_short_tp,
            },
        )
        long_distance_pct_config = self.config.long_fill_distance_pct
        long_distance_pct = self._clamp_pct_fraction(self._pct(long_distance_pct_config))
        long_cycle_number = int(state.get("current_long_cycle_index") or 0) + 1
        short_intents = self._build_short_tp_follow_up(snapshot, runtime_state, context)
        intents.extend(short_intents)
        if long_cycle_number <= self.config.max_cycles:
            purpose = self._cycle_purpose("long", long_cycle_number)
            previous_short_purpose = self._cycle_purpose("short", long_cycle_number - 1) if long_cycle_number > 1 else None
            skip_signature: dict[str, Any] | None = None
            skip_event_kwargs: dict[str, Any] | None = None
            if waiting_for_short_tp:
                skip_signature = {
                    "reason": "waiting_for_short_tp",
                    "cycle_number": long_cycle_number,
                    "purpose": purpose,
                    "short_tp_pending_cycle": short_tp_pending_cycle,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                }
                skip_event_kwargs = {
                    "strategy": self.name,
                    "skip_reason": "waiting_for_short_tp",
                    "cycle_number": long_cycle_number,
                    "purpose": purpose,
                    "short_tp_pending_cycle": short_tp_pending_cycle,
                }
            elif previous_short_purpose and snapshot.has_open_purpose(previous_short_purpose):
                skip_signature = {
                    "reason": "short_cycle_order_still_open",
                    "cycle_number": long_cycle_number,
                    "purpose": purpose,
                    "blocking_purpose": previous_short_purpose,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                }
                skip_event_kwargs = {
                    "strategy": self.name,
                    "skip_reason": "short_cycle_order_still_open",
                    "cycle_number": long_cycle_number,
                    "purpose": purpose,
                    "blocking_purpose": previous_short_purpose,
                }
            if skip_signature and skip_event_kwargs:
                previous_signature = state.get("last_downside_skip_signature")
                if previous_signature != skip_signature:
                    state["last_downside_skip_signature"] = skip_signature
                    context.audit.log_event("fixed_cycle_downside_skip", **skip_event_kwargs)
            else:
                state.pop("last_downside_skip_signature", None)
                long_qty = self._fixed_long_cycle_qty(
                    initial_long_qty,
                    snapshot.long_qty,
                    reference_price,
                    runtime_state,
                )
                raw_trigger_price = long_reference * (1 - long_distance_pct)
                trigger_price = self._normalize_price(raw_trigger_price, runtime_state)
                # trigger stay strictly at long_fill_distance_pct below reference
                raw_qty = snapshot.long_qty * self._pct(self.config.reduction_pct_per_fill)
                will_append_intent = trigger_price > 0 and long_qty > 0
                skip_reason: str | None = None
                if not will_append_intent:
                    skip_reason = "trigger_price_non_positive" if trigger_price <= 0 else "long_qty_non_positive"
                logger.debug(
                    "fixed_cycle_downside_cycle_evaluated %s",
                    {
                        "cycle_number": long_cycle_number,
                        "step_index": 1,
                        "side": "long",
                        "long_fill_price": long_fill_price,
                        "cycle_entry_price": cycle_entry_price,
                        "live_reference_price": reference_price,
                        "long_reference_candidate": long_reference_candidate,
                        "final_long_reference": long_reference,
                        "distance_pct_used": long_distance_pct,
                        "safety_offset_used": 0.0,
                        "raw_trigger_price": raw_trigger_price,
                        "normalized_trigger_price": trigger_price,
                        "computed_qty_raw": raw_qty,
                        "computed_qty_normalized": long_qty,
                        "purpose": purpose,
                        "reduce_only": False,
                        "will_append_intent": will_append_intent,
                        "skip_reason": skip_reason,
                    },
                )
                if not will_append_intent:
                    context.audit.log_event(
                        "fixed_cycle_downside_skip",
                        strategy=self.name,
                        skip_reason=skip_reason,
                        cycle_number=long_cycle_number,
                        side="long",
                        reference_price_used=long_reference,
                        raw_trigger_price=raw_trigger_price,
                        normalized_trigger_price=trigger_price,
                        computed_qty_normalized=long_qty,
                        purpose=purpose,
                    )
                else:
                    context.audit.log_event(
                        "fixed_cycle_long_reduce_planned",
                        strategy=self.name,
                        cycle_index=long_cycle_number,
                        side="long",
                        purpose=purpose,
                        entry_reference_price=entry_reference_price,
                        distance_pct=long_distance_pct_config,
                        distance_pct_used=long_distance_pct,
                        long_fill_price=long_fill_price,
                        cycle_entry_price=cycle_entry_price,
                        live_reference_price=reference_price,
                        final_long_reference=long_reference,
                        trigger_formula="final_long_reference * (1 - distance_pct)",
                        trigger_price_raw=raw_trigger_price,
                        trigger_price_normalized=trigger_price,
                        qty_formula="current_long_qty * reduction_pct_per_fill",
                        qty_raw=raw_qty,
                        qty_normalized=long_qty,
                        order_type="Limit",
                        reduce_only=True,
                    )
                    intents.append(
                        StrategyIntent(
                            side="long",
                            qty=long_qty,
                            purpose=purpose,
                            order_type="Market",
                            reduce_only=True,
                            trigger_price=trigger_price,
                            trigger_direction=2,
                            trigger_by="LastPrice",
                            close_on_trigger=True,
                            position_idx=1,
                            metadata={
                                "cycle_index": long_cycle_number,
                                "cycle_role": "long_reduce",
                                "replace_open_purpose": purpose,
                                "entry_reference_price": entry_reference_price,
                            },
                        )
                    )
                    state["long_add_rebuild_allowed"] = False
        long_intents = [intent for intent in intents if intent.side == "long"]
        short_intents = [intent for intent in intents if intent.side == "short"]
        first_long_purpose = self._cycle_purpose("long", 1)
        first_short_purpose = self._cycle_purpose("short", 1)
        context.audit.log_event(
            "fixed_cycle_downside_build_result",
            strategy=self.name,
            long_intent_count=len(long_intents),
            short_intent_count=len(short_intents),
            total_intent_count=len(intents),
            purposes=[intent.purpose for intent in intents],
            first_long_cycle_present=any(intent.purpose == first_long_purpose for intent in intents),
            first_short_cycle_present=any(intent.purpose == first_short_purpose for intent in intents),
            long_reference=long_reference,
            short_reference=short_reference,
        )
        logger.debug(
            "fixed_cycle_downside_build_result %s",
            {
                "long_intent_count": len(long_intents),
                "short_intent_count": len(short_intents),
                "total_intent_count": len(intents),
                "purposes": [intent.purpose for intent in intents],
                "first_long_cycle_present": any(intent.purpose == first_long_purpose for intent in intents),
                "first_short_cycle_present": any(intent.purpose == first_short_purpose for intent in intents),
                "long_reference": long_reference,
                "short_reference": short_reference,
            },
        )
        self._write_cycle_state(cycle_state)
        return intents

    def _build_short_tp_follow_up(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        cycle_state = self._ensure_cycle_state(runtime_state)
        cycle_index = int(state.get("short_tp_pending_cycle") or 0)
        if cycle_index <= 0 or not state.get("cycle_waiting_for_short_tp"):
            return []

        purpose = self._cycle_purpose("short", cycle_index)
        long_purpose = self._cycle_purpose("long", cycle_index)

        def _has_pending_order(purpose_name: str) -> bool:
            for order in snapshot.active_orders:
                if order.purpose != purpose_name:
                    continue
                status = order.status
                if status in {"FILLED", "CANCELED", "REJECTED"}:
                    continue
                if float(order.remaining_qty or 0.0) <= 0:
                    continue
                return True
            return False

        if _has_pending_order(long_purpose):
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="long_cycle_order_still_open",
                cycle_number=cycle_index,
                purpose=purpose,
                blocking_purpose=long_purpose,
            )
            return []
        if _has_pending_order(purpose):
            return []

        long_fill = (cycle_state.get("long_fills") or {}).get(str(cycle_index)) or {}
        self._seed_long_fill_closed_pnl_fields(long_fill)
        long_fill_price = float(long_fill.get("price") or 0.0)
        if long_fill_price <= 0:
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="missing_long_fill_price_for_short_tp",
                cycle_number=cycle_index,
                purpose=purpose,
            )
            return []

        if not bool(long_fill.get("closed_pnl_ready")):
            closed_pnl_ready = self._refresh_long_fill_closed_pnl(
                cycle_index=cycle_index,
                long_fill=long_fill,
                runtime_state=runtime_state,
                context=context,
            )
            self._write_cycle_state(cycle_state)
            if not closed_pnl_ready:
                logger.debug(
                    "short_tp_build_deferred",
                    extra={
                        "order_id": long_fill.get("order_id"),
                        "cycle_index": cycle_index,
                        "symbol": self.config.symbol,
                        "decision": "deferred",
                        "closed_pnl": long_fill.get("closed_pnl"),
                        "closed_qty": long_fill.get("closed_qty"),
                    },
                )
                context.audit.log_event(
                    "fixed_cycle_downside_skip",
                    strategy=self.name,
                    skip_reason="closed_pnl_pending",
                    cycle_number=cycle_index,
                    purpose=purpose,
                    order_id=long_fill.get("order_id"),
                )
                # keep waiting flags set so rebuild keeps retrying
                state["cycle_waiting_for_short_tp"] = True
                cycle_state["cycle_waiting_for_short_tp"] = True
                state["short_tp_pending_cycle"] = cycle_index
                cycle_state["short_tp_pending_cycle"] = cycle_index
                return []

        reduction_multiplier = 1.0
        effective_reduction_pct = self.config.reduction_pct_per_fill * reduction_multiplier
        short_qty = self._fixed_short_cycle_qty(
            float(state.get("initial_short_qty") or 0.0),
            snapshot.short_qty,
            long_fill_price,
            reduction_multiplier=reduction_multiplier,
            runtime_state=runtime_state,
        )
        long_reduce_qty = float(long_fill.get("qty") or 0.0)
        confirmed_closed_pnl = self._safe_float(long_fill.get("confirmed_closed_pnl"), None)
        if confirmed_closed_pnl is None:
            confirmed_closed_pnl = self._safe_float(long_fill.get("closed_pnl"), None)
        confirmed_closed_qty = self._safe_float(long_fill.get("closed_qty"), None)
        confirmed_closed_avg_price = self._safe_float(long_fill.get("closed_avg_price"), None)
        confirmed_closed_cost = self._safe_float(long_fill.get("closed_cost"), None)
        logger.info(
            "short_tp_build_proceed",
            extra={
                "order_id": long_fill.get("order_id"),
                "cycle_index": cycle_index,
                "symbol": self.config.symbol,
                "decision": "proceed",
                "closed_pnl": confirmed_closed_pnl,
                "closed_qty": confirmed_closed_qty,
            },
        )
        short_entry_price = float(snapshot.short_avg or state.get("short_avg") or 0.0)
        short_reduce_reference = short_entry_price
        target_profit_usdt = float(self.config.target_profit_usdt or 0.0)
        fee_rate = 0.00055
        if (
            short_qty <= 0
            or long_reduce_qty <= 0
            or confirmed_closed_pnl is None
            or (confirmed_closed_qty is not None and confirmed_closed_qty <= 0)
            or short_entry_price <= 0
            or fee_rate >= 1.0
        ):
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="short_tp_invalid",
                cycle_number=cycle_index,
                purpose=purpose,
                long_fill_price=long_fill_price,
                long_reduce_qty=long_reduce_qty,
                confirmed_closed_pnl=confirmed_closed_pnl,
                confirmed_closed_qty=confirmed_closed_qty,
                short_entry_price=short_entry_price,
                short_qty=short_qty,
                fee_rate=fee_rate,
            )
            return []

        short_entry_price = float(short_entry_price)
        short_qty = float(short_qty)
        fee_rate = float(fee_rate)

        long_loss_usdt = max(-float(confirmed_closed_pnl or 0.0), 0.0)
        long_add_loss_usdt = float(long_fill.get("last_long_add_loss_usdt", 0.0))
        recovered_short_profit = float(
            getattr(snapshot, "realized_short_pnl_total", 0.0) or 0.0
        )
        recovered_short_profit = max(recovered_short_profit, 0.0)
        remaining_loss = max(long_add_loss_usdt - recovered_short_profit, 0.0)
        required_net = remaining_loss + float(target_profit_usdt or 0.0)

        if short_qty <= 0:
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="short_tp_invalid",
                cycle_number=cycle_index,
                purpose=purpose,
                short_entry_price=short_entry_price,
                short_qty=short_qty,
            )
            return []

        tp_price = (
            (short_entry_price * (1 - fee_rate))
            - (required_net / short_qty)
        ) / (1 + fee_rate)

        if tp_price <= 0:
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="short_tp_invalid",
                cycle_number=cycle_index,
                purpose=purpose,
                tp_price=tp_price,
            )
            return []

        symbol, rules, _ = self._resolve_instrument_rules(runtime_state)
        tp_price = self._normalize_price(tp_price, runtime_state)
        instrument_tick_size = (
            float(rules["tick_size"]) if rules and rules.get("tick_size") else 0.0
        )
        price_tick_size = instrument_tick_size or float(self.config.price_tick_size or 0.0)
        if price_tick_size <= 0:
            price_tick_size = 0.01

        expected = required_net

        def compute_net(tp: float) -> float:
            return (
                (short_entry_price - tp) * short_qty
                - (short_entry_price * short_qty * fee_rate)
                - (tp * short_qty * fee_rate)
            )

        net = compute_net(tp_price)
        max_iterations = 50
        i = 0
        while net < expected and i < max_iterations:
            tp_price -= price_tick_size
            if tp_price <= 0:
                break
            tp_price = math.floor(tp_price / price_tick_size) * price_tick_size
            net = compute_net(tp_price)
            i += 1

        raw_trigger_price = max(tp_price, price_tick_size)
        trigger_price = self._normalize_price(raw_trigger_price, runtime_state)
        required_price_move = short_entry_price - trigger_price
        required_short_gross = short_qty * required_price_move
        price = self._normalize_price(
            max(trigger_price - self.config.price_tick_size, self.config.price_tick_size),
            runtime_state,
        )
        if short_qty <= 0 or trigger_price <= 0:
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="short_tp_invalid",
                cycle_number=cycle_index,
                purpose=purpose,
                long_fill_price=long_fill_price,
                short_qty=short_qty,
                trigger_price=trigger_price,
            )
            return []

        logger.info(
            "short_tp_final_inputs",
            extra={
                "order_id": long_fill.get("order_id"),
                "cycle_index": cycle_index,
                "symbol": self.config.symbol,
                "decision": "trigger_run",
                "closed_pnl": confirmed_closed_pnl,
                "closed_qty": confirmed_closed_qty,
                "long_loss_usdt": long_loss_usdt,
                "required_net_profit": required_net,
                "trigger_price": trigger_price,
                "short_qty": short_qty,
            },
        )
        context.audit.log_event(
            "fixed_cycle_short_cycle_planned",
            strategy=self.name,
            cycle_index=cycle_index,
            side="short",
            purpose=purpose,
            entry_reference_price=float(state.get("entry_reference_price") or 0.0),
            long_reduce_qty=long_reduce_qty,
            confirmed_closed_pnl=confirmed_closed_pnl,
            confirmed_closed_qty=confirmed_closed_qty,
            confirmed_closed_avg_price=confirmed_closed_avg_price,
            confirmed_closed_cost=confirmed_closed_cost,
            confirmed_closed_pnl_updated_time=long_fill.get("closed_pnl_updated_time"),
            fill_count=long_fill.get("fill_count"),
            short_entry_price=short_entry_price,
            short_reduce_reference=short_reduce_reference,
            fee_rate=fee_rate,
            target_profit_usdt=target_profit_usdt,
            long_loss_usdt=long_loss_usdt,
            required_short_gross=required_short_gross,
            required_price_move=required_price_move,
            required_net=required_net,
            trigger_formula="((short_entry_price * (1 - fee_rate)) - (required_net / short_qty)) / (1 + fee_rate)",
            trigger_formula_details="tp_price is decremented by price_tick_size until compute_net(tp) >= required_net; compute_net subtracts both entry and exit fees",
            trigger_price_raw=raw_trigger_price,
            trigger_price_normalized=trigger_price,
            price_tick_size=price_tick_size,
            reduction_multiplier=reduction_multiplier,
            reduction_pct_used=effective_reduction_pct,
            qty_formula="current_short_qty * reduction_pct_per_fill * reduction_multiplier",
            qty_raw=snapshot.short_qty * self._pct(effective_reduction_pct),
            qty_normalized=short_qty,
            order_type="Limit",
            reduce_only=True,
        )
        return [
            StrategyIntent(
                side="short",
                qty=short_qty,
                purpose=purpose,
                order_type="Market",
                reduce_only=True,
                trigger_price=trigger_price,
                trigger_direction=2,
                trigger_by="LastPrice",
                close_on_trigger=True,
                position_idx=2,
                metadata={
                    "cycle_index": cycle_index,
                    "cycle_role": "short_reduce",
                    "replace_open_purpose": purpose,
                    "entry_reference_price": float(state.get("entry_reference_price") or 0.0),
                    "long_fill_price": long_fill_price,
                    "long_reduce_qty": long_reduce_qty,
                    "confirmed_closed_pnl": confirmed_closed_pnl,
                    "confirmed_closed_qty": confirmed_closed_qty,
                    "confirmed_closed_avg_price": confirmed_closed_avg_price,
                    "confirmed_closed_cost": confirmed_closed_cost,
                    "short_entry_price": short_entry_price,
                    "short_reduce_reference": short_reduce_reference,
                    "fee_rate": fee_rate,
                    "required_price_move": required_price_move,
                },
            )
        ]

    def _build_exit_intents(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        current_cycle: int,
        break_even_price: float,
        tp_price: float,
        hard_stop_active: bool,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        intents: list[StrategyIntent] = []
        state = runtime_state.strategy_state
        self._ensure_cycle_state(runtime_state)
        if not state.get("exit_rebuild_allowed", True):
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="exit_locked",
            )
            return intents

        if state.get("long_add_pending"):
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="long_add_pending",
                long_add_pending=True,
            )
            return intents
        if not state.get("initial_entry_confirmed"):
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="initial_entry_not_confirmed",
                initial_entry_confirmed=bool(state.get("initial_entry_confirmed")),
            )
            return intents

        exit_allowed = (
            snapshot.long_qty > 0
            and snapshot.short_qty > 0
            and snapshot.long_avg > 0
            and snapshot.short_avg > 0
        )
        if not exit_allowed:
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="exit_not_allowed",
                long_qty=snapshot.long_qty,
                short_qty=snapshot.short_qty,
                long_avg=snapshot.long_avg,
                short_avg=snapshot.short_avg,
            )
            return intents

        open_initial_orders = [
            {
                "purpose": getattr(order, "purpose", None),
                "status": getattr(order, "status", None),
            }
            for order in snapshot.active_orders
            if getattr(order, "purpose", None) in {
                self.LONG_ENTRY_PURPOSE,
                self.SHORT_ENTRY_PURPOSE,
            }
            and getattr(order, "status", None) in {"OPEN", "PARTIAL"}
        ]
        if open_initial_orders:
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="initial_entry_order_still_open",
                open_initial_orders=open_initial_orders,
            )
            return intents

        long_tp_price = tp_price
        short_sl_price = tp_price
        current_price = snapshot.current_price
        symbol, rules, source = self._resolve_instrument_rules(runtime_state)
        tick_decimal = rules["tick_size"] if rules and rules.get("tick_size", Decimal("0")) > 0 else Decimal(
            str(self.config.price_tick_size)
        )
        tick_size = float(tick_decimal)
        logger.info(
            "exit_tick_size %s",
            {
                "symbol": symbol,
                "tick_size": str(tick_decimal),
                "source": source,
                "price_tick_config": self.config.price_tick_size,
            },
        )
        if current_price > 0:
            min_valid_trigger = current_price + tick_size
            logger.info(
                "exit_trigger_clamp %s",
                {
                    "symbol": symbol,
                    "tp_price": tp_price,
                    "min_valid_trigger": min_valid_trigger,
                    "current_price": current_price,
                    "tick_size": str(tick_decimal),
                },
            )
            long_tp_price = max(long_tp_price, min_valid_trigger)
            short_sl_price = max(short_sl_price, min_valid_trigger)
            logger.info(
                "exit_trigger_result %s",
                {
                    "symbol": symbol,
                    "long_tp_price": long_tp_price,
                    "short_sl_price": short_sl_price,
                    "tp_price": tp_price,
                },
            )
        long_tp_valid = (
            current_price <= 0
            or long_tp_price >= current_price + tick_size
        )
        short_sl_valid = (
            current_price <= 0
            or short_sl_price >= current_price + tick_size
        )
        signature = {
            "basket_tp_price": tp_price,
            "basket_break_even_price": break_even_price,
            "long_tp_price": long_tp_price,
            "short_sl_price": short_sl_price,
            "long_qty": snapshot.long_qty,
            "short_qty": snapshot.short_qty,
            "long_avg": snapshot.long_avg,
            "short_avg": snapshot.short_avg,
            "hard_stop_active": hard_stop_active,
            "current_effective_cycle": int(state.get("current_effective_cycle") or 0),
        }
        if state.get("last_exit_signature") == signature:
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="exit_signature_unchanged",
                signature=signature,
            )
            return intents

        if context.cancel_open_orders_by_purpose:
            context.cancel_open_orders_by_purpose(self._exit_purposes())

        metadata_base = {
            "basket_break_even_price": break_even_price,
            "basket_tp_price": tp_price,
            "exit_mode": "basket_exit",
        }

        def build_metadata(purpose: str, exit_type: str) -> dict[str, Any]:
            metadata = dict(metadata_base)
            metadata["replace_open_purpose"] = [purpose]
            metadata["exit_type"] = exit_type
            return metadata

        if long_tp_valid:
            intents.append(
                StrategyIntent(
                    side="long",
                    qty=snapshot.long_qty,
                    purpose=self.LONG_TP_EXIT_PURPOSE,
                    order_type="Market",
                    reduce_only=True,
                    trigger_price=long_tp_price,
                    trigger_direction=1,
                    trigger_by="LastPrice",
                    close_on_trigger=True,
                    position_idx=1,
                    metadata=build_metadata(self.LONG_TP_EXIT_PURPOSE, "long_tp"),
                )
            )
        else:
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="long_trigger_not_far_enough_from_market",
                current_price=current_price,
                trigger_price=long_tp_price,
                tick_size=tick_size,
            )
        if short_sl_valid:
            intents.append(
                StrategyIntent(
                    side="short",
                    qty=snapshot.short_qty,
                    purpose=self.SHORT_SL_EXIT_PURPOSE,
                    order_type="Market",
                    reduce_only=True,
                    trigger_price=short_sl_price,
                    trigger_direction=1,
                    trigger_by="LastPrice",
                    close_on_trigger=True,
                    position_idx=2,
                    metadata=build_metadata(self.SHORT_SL_EXIT_PURPOSE, "short_sl"),
                )
            )
        else:
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="short_trigger_not_far_enough_from_market",
                current_price=current_price,
                trigger_price=short_sl_price,
                tick_size=tick_size,
            )

        if not intents:
            return intents

        state["exit_rebuild_allowed"] = False

        cycle_idx = int(state.get("current_effective_cycle") or 0)
        if not state.get("exit_armed_marker_emitted"):
            _emit_analyzer_event(
                logger,
                "analyzer_exit_armed",
                {
                    "symbol": self.config.symbol,
                    "strategy": self.name,
                    "cycle_index": cycle_idx,
                    "exit_mode": metadata_base["exit_mode"],
                    "exit_reason": "exit_manifest",
                    "expected_long_exit_price": long_tp_price,
                    "expected_short_exit_price": short_sl_price,
                    "long_size": snapshot.long_qty,
                    "short_size": snapshot.short_qty,
                },
            )
            state["exit_armed_marker_emitted"] = True

        state["last_exit_signature"] = signature
        context.audit.log_event(
            "fixed_cycle_exit_manifest",
            strategy=self.name,
            break_even_price=break_even_price,
            tp_price=tp_price,
            signature=signature,
            purposes=[intent.purpose for intent in intents],
            prices=[intent.price for intent in intents],
            trigger_prices=[intent.trigger_price for intent in intents],
        )
        return intents

    def _calc_short_tp_trigger_price_from_confirmed_loss(
        self,
        *,
        short_entry_price: float,
        short_qty: float,
        confirmed_long_closed_pnl: float,
        target_profit_usdt: float,
        fee_rate: float,
    ) -> float:
        required_net_profit = abs(float(confirmed_long_closed_pnl)) + float(target_profit_usdt)
        numerator = short_entry_price - (required_net_profit / short_qty)
        denominator = 1.0 + fee_rate
        return numerator / denominator

    def _calculate_break_even(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> tuple[float, list[CalculationTrace]]:
        state = runtime_state.strategy_state

        denominator = snapshot.long_qty - snapshot.short_qty
        if abs(denominator) <= 1e-9:
            break_even_price = snapshot.current_price
        else:
            break_even_price = (
            (snapshot.long_avg * snapshot.long_qty)
            - (snapshot.short_avg * snapshot.short_qty)
        ) / denominator
        realized_long_loss = float(state.get("net_long_loss_balance") or 0.0)
        realized_short_profit = 0.0
        realized_short_loss = float(state.get("net_short_loss_balance") or 0.0)
        loss_compensation = realized_long_loss + realized_short_loss
        if loss_compensation > 0 and abs(denominator) > 1e-9:
            break_even_price += loss_compensation / denominator
        logger.info(
            "fixed_cycle_break_even_inputs %s",
            {
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "long_avg": snapshot.long_avg,
                "short_avg": snapshot.short_avg,
                "current_price": snapshot.current_price,
                "realized_pnl_total": snapshot.realized_pnl_total,
                "realized_long_pnl_total": snapshot.realized_long_pnl_total,
                "realized_short_pnl_total": snapshot.realized_short_pnl_total,
                "realized_long_loss": realized_long_loss,
                "realized_short_profit": realized_short_profit,
                "realized_short_loss": realized_short_loss,
                "loss_compensation": loss_compensation,
                "denominator": denominator,
            },
        )

        break_even_price = self._normalize_price(
            max(break_even_price, self.config.price_tick_size), runtime_state
        )

        traces = [
            CalculationTrace(
                name="break_even_price",
                formula=(
                    "break_even = (long_avg*long_qty - short_avg*short_qty) / (long_qty - short_qty)"
                ),
                inputs={
                    "realized_long_pnl_total": snapshot.realized_long_pnl_total,
                    "realized_short_pnl_total": snapshot.realized_short_pnl_total,
                    "realized_pnl_total": snapshot.realized_pnl_total,
                    "short_avg": snapshot.short_avg,
                    "short_qty": snapshot.short_qty,
                    "long_avg": snapshot.long_avg,
                    "long_qty": snapshot.long_qty,
                    "denominator": denominator,
                },
                result={"break_even_price": break_even_price},
                details={
                    "realized_long_loss": realized_long_loss,
                    "realized_short_loss": realized_short_loss,
                    "loss_compensation": loss_compensation,
                    "realized_short_profit": realized_short_profit,
                },
            )
        ]
        return break_even_price, traces

    def _adaptive_tp_buffer_multiplier(self, snapshot: HedgeSnapshot) -> float:
        spread_reference = max(self._pct(self.config.long_fill_distance_pct), 1e-9)
        spread_penalty = min(snapshot.spread_pct / spread_reference, 1.0)
        target_short_ratio = max(self.config.hedge_ratio_short, 1e-9)
        ratio_penalty = min(abs(snapshot.short_ratio - self.config.hedge_ratio_short) / target_short_ratio, 1.0)
        structure_penalty = max(spread_penalty, ratio_penalty)
        return 0.75 + (0.5 * structure_penalty)

    def _calculate_tp_price(
        self,
        break_even_price: float,
        snapshot: HedgeSnapshot | None = None,
        runtime_state: RuntimeState | None = None,
    ) -> float:
        components = self._calculate_tp_components(snapshot, runtime_state)
        tp_price = self._normalize_price(
            break_even_price
            + components["goal_profit"]
            + components["buffer"],
            runtime_state,
        )
        logger.info(
            "fixed_cycle_tp_components %s",
            {
                "break_even_price": break_even_price,
                "reference_price": components["reference_price"],
                "loss_recovery_price_component": components["loss_recovery"],
                "goal_profit_price_component": components["goal_profit"],
                "buffer_price_component": components["buffer"],
                "tp_price": tp_price,
            },
        )
        return tp_price

    def _calculate_tp_components(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState | None,
    ) -> dict[str, float]:
        reference_price = self._tp_reference_price(snapshot, runtime_state)
        loss_recovery = self._loss_recovery_price_component(snapshot, runtime_state)
        goal_profit = reference_price * self._pct(self.config.tp_profit_target_pct)
        buffer = reference_price * self._pct(self.config.tp_buffer_pct)
        return {
            "reference_price": reference_price,
            "loss_recovery": loss_recovery,
            "goal_profit": goal_profit,
            "buffer": buffer,
        }

    def _tp_reference_price(
        self, snapshot: HedgeSnapshot | None, runtime_state: RuntimeState | None
    ) -> float:
        candidates: list[float] = []
        if runtime_state:
            entry_ref = float(runtime_state.strategy_state.get("entry_reference_price") or 0.0)
            if entry_ref > 0:
                candidates.append(entry_ref)
        if snapshot:
            if snapshot.long_avg > 0:
                candidates.append(snapshot.long_avg)
            if snapshot.current_price > 0:
                candidates.append(snapshot.current_price)
        base_price = max(candidates) if candidates else 0.0
        return max(base_price, float(self.config.price_tick_size) or 1e-9)

    def _loss_recovery_price_component(
        self, snapshot: HedgeSnapshot | None, runtime_state: RuntimeState | None
    ) -> float:
        if not snapshot:
            return 0.0
        net_qty = snapshot.long_qty - snapshot.short_qty
        if abs(net_qty) <= 1e-9:
            if max(-snapshot.realized_long_pnl_total, 0.0) + max(-snapshot.realized_short_pnl_total, 0.0) > 0:
                logger.warning(
                    "fixed_cycle_loss_recovery_denominator_zero %s",
                    {
                        "long_qty": snapshot.long_qty,
                        "short_qty": snapshot.short_qty,
                        "realized_long_loss": max(-snapshot.realized_long_pnl_total, 0.0),
                        "realized_short_loss": max(-snapshot.realized_short_pnl_total, 0.0),
                    },
                )
            return 0.0
        state = runtime_state.strategy_state if runtime_state else {}
        realized_long_loss = float(state.get("net_long_loss_balance") or 0.0)
        realized_short_loss = float(state.get("net_short_loss_balance") or 0.0)
        loss_total = realized_long_loss + realized_short_loss
        return loss_total / net_qty if loss_total > 0 else 0.0

    def _seed_initial_reference_if_missing(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> None:
        state = runtime_state.strategy_state

        cycle_state = self._ensure_cycle_state(runtime_state)

        if float(state.get("entry_reference_price") or 0.0) <= 0 and snapshot.current_price > 0:
            state["entry_reference_price"] = snapshot.current_price

        if float(state.get("initial_long_qty") or 0.0) <= 0 and snapshot.long_qty > 0:
            state["initial_long_qty"] = snapshot.long_qty

        if float(state.get("initial_short_qty") or 0.0) <= 0 and snapshot.short_qty > 0:
            state["initial_short_qty"] = snapshot.short_qty

        if float(state.get("initial_total_notional_usdt") or 0.0) <= 0:
            ref_price = float(state.get("entry_reference_price") or 0.0)
            initial_long_qty = float(state.get("initial_long_qty") or 0.0)
            initial_short_qty = float(state.get("initial_short_qty") or 0.0)
            if ref_price > 0 and (initial_long_qty > 0 or initial_short_qty > 0):
                state["initial_total_notional_usdt"] = (initial_long_qty * ref_price) + (initial_short_qty * ref_price)

        if float(state.get("entry_reference_price") or 0.0) > 0 and float(cycle_state.get("entry_price") or 0.0) <= 0:
            cycle_state["entry_price"] = float(state.get("entry_reference_price") or 0.0)
            self._write_cycle_state(cycle_state)

    def _sync_state_from_snapshot(self, snapshot: HedgeSnapshot, runtime_state: RuntimeState) -> None:
        state = runtime_state.strategy_state
        state["open_long_qty"] = snapshot.long_qty
        state["open_short_qty"] = snapshot.short_qty
        state["long_avg"] = snapshot.long_avg
        state["short_avg"] = snapshot.short_avg
        state["realized_pnl_total"] = snapshot.realized_pnl_total
        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

    def _advance_cycle_from_fill(
        self,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: StrategyContext | None = None,
    ) -> None:
        cycle_index = int(fill_event.metadata.get("cycle_index") or 0)
        if cycle_index <= 0:
            return

        state = runtime_state.strategy_state
        cycle_state = self._ensure_cycle_state(runtime_state)
        snapshot = runtime_state.last_snapshot
        order_fully_completed = (
            fill_event.status == "FILLED"
        )
        cycle_state["trade_active"] = True
        cycle_state["symbol"] = self.config.symbol
        processed = set(cycle_state.get("processed_fill_ids") or [])
        fill_key = self._fill_persistence_key(fill_event)
        if fill_key in processed:
            return

        purpose = fill_event.purpose or ""
        if purpose in {"REFILL_LONG", "REFILL_SHORT"}:
            refill_state = state.setdefault("refill_state", {})
            if order_fully_completed:
                refill_state[purpose] = True

            if refill_state.get("REFILL_LONG") and refill_state.get("REFILL_SHORT"):
                state["cycle_completed_count"] = 0
                state["cycle_waiting_for_short_tp"] = False
                state["pending_long_cycle_index"] = 0
                state["short_tp_pending_cycle"] = 0
                state["long_add_pending"] = False
                state["exit_rebuild_allowed"] = True
                state["long_add_rebuild_allowed"] = True
                state["refill_state"] = {}

                cycle_state["cycle_waiting_for_short_tp"] = False
                cycle_state["pending_long_cycle_index"] = 0
                cycle_state["short_tp_pending_cycle"] = 0
                cycle_state["long_add_pending"] = False

                if context is not None:
                    context.audit.log_event(
                        "fixed_cycle_refill_completed",
                        strategy=self.name,
                    )

            processed.add(fill_key)
            cycle_state["processed_fill_ids"] = list(processed)
            self._write_cycle_state(cycle_state)
            return

        if "_LONG_" in fill_event.purpose and "LONG_ADD" in fill_event.purpose:
            state["long_add_pending"] = fill_event.status != "FILLED"
            cycle_state["long_add_pending"] = state["long_add_pending"]
            if order_fully_completed:
                state["exit_rebuild_allowed"] = True
                state["long_add_rebuild_allowed"] = True
        if "_LONG_" in fill_event.purpose and order_fully_completed:
            state["current_long_cycle_index"] = max(int(state.get("current_long_cycle_index") or 0), cycle_index)
            state["cycle_waiting_for_short_tp"] = True
            state["pending_long_cycle_index"] = cycle_index
            state["short_tp_pending_cycle"] = cycle_index

        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

        if "_LONG_" in fill_event.purpose:
            fills = cycle_state.setdefault("long_fills", {})
            entry = dict(fills.get(str(cycle_index)) or {})
            total_qty = float(entry.get("total_qty") or 0.0) + float(fill_event.exec_qty or 0.0)
            weighted_price_sum = float(entry.get("weighted_price_sum") or 0.0) + (
                float(fill_event.exec_price or 0.0) * float(fill_event.exec_qty or 0.0)
            )
            avg_price = weighted_price_sum / total_qty if total_qty > 0 else 0.0
            fills[str(cycle_index)] = {
                "price": fill_event.exec_price,
                "qty": fill_event.exec_qty,
                "total_qty": total_qty,
                "weighted_price_sum": weighted_price_sum,
                "avg_price": avg_price,
                "client_order_id": fill_event.client_order_id,
                "exec_id": fill_event.exec_id,
                "confirmed_pnl_applied": False,
            }
            long_fill = fills[str(cycle_index)]
            self._seed_long_fill_closed_pnl_fields(long_fill, fill_event.exchange_order_id)
            cycle_state["last_cycle_reference_price"] = avg_price
            if order_fully_completed:
                long_index = int(state.get("current_long_cycle_index") or 0)
                cycle_state["long_cycle_index"] = max(long_index, cycle_index)
                cycle_state["cycle_waiting_for_short_tp"] = True
                cycle_state["pending_long_cycle_index"] = cycle_index
                cycle_state["short_tp_pending_cycle"] = cycle_index
                if context is not None:
                    self._refresh_long_fill_closed_pnl(
                        cycle_index=cycle_index,
                        long_fill=long_fill,
                        runtime_state=runtime_state,
                        context=context,
                        occurred_at_ms=int(fill_event.occurred_at.timestamp() * 1000),
                        exec_id=fill_event.exec_id,
                    )
                    if long_fill.get("confirmed_closed_pnl") is not None:
                        self._cleanup_order_pnl(runtime_state, long_fill.get("client_order_id"))
            confirmed_closed_pnl = long_fill.get("confirmed_closed_pnl")
            if confirmed_closed_pnl is not None:
                long_add_loss_usdt = max(-float(confirmed_closed_pnl), 0.0)
                long_fill["last_long_add_loss_usdt"] = long_add_loss_usdt
            if float(cycle_state.get("entry_price") or 0.0) <= 0:
                cycle_state["entry_price"] = fill_event.exec_price

        if "_SHORT_" in fill_event.purpose:
            fills = cycle_state.setdefault("short_fills", {})
            entry = dict(fills.get(str(cycle_index)) or {})
            total_qty = float(entry.get("total_qty") or 0.0) + float(fill_event.exec_qty or 0.0)
            weighted_price_sum = float(entry.get("weighted_price_sum") or 0.0) + (
                float(fill_event.exec_price or 0.0) * float(fill_event.exec_qty or 0.0)
            )
            avg_price = weighted_price_sum / total_qty if total_qty > 0 else 0.0
            fills[str(cycle_index)] = {
                "price": fill_event.exec_price,
                "qty": fill_event.exec_qty,
                "total_qty": total_qty,
                "weighted_price_sum": weighted_price_sum,
                "avg_price": avg_price,
            }
            cycle_state["last_cycle_reference_price"] = avg_price
            if order_fully_completed:
                state["current_short_cycle_index"] = max(int(state.get("current_short_cycle_index") or 0), cycle_index)
                short_index = int(state.get("current_short_cycle_index") or 0)
                cycle_state["short_cycle_index"] = max(short_index, cycle_index)
            if float(cycle_state.get("entry_price") or 0.0) <= 0:
                cycle_state["entry_price"] = fill_event.exec_price
            if order_fully_completed and "SHORT_REDUCE" in fill_event.purpose:
                state["exit_rebuild_allowed"] = True
            if order_fully_completed and state.get("cycle_waiting_for_short_tp") and int(
                state.get("pending_long_cycle_index") or 0
            ) == cycle_index:
                state["cycle_completed_count"] = int(state.get("cycle_completed_count") or 0) + 1
                state["cycle_waiting_for_short_tp"] = False
                state["pending_long_cycle_index"] = 0
                state["short_tp_pending_cycle"] = 0
                cycle_state["cycle_waiting_for_short_tp"] = False
                cycle_state["pending_long_cycle_index"] = 0
                cycle_state["short_tp_pending_cycle"] = 0
        if fill_event.purpose == self.SHORT_TP_EXIT_PURPOSE and order_fully_completed:
            state["exit_rebuild_allowed"] = True
        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

        exit_purposes = {self.LONG_TP_EXIT_PURPOSE, self.SHORT_SL_EXIT_PURPOSE}
        exit_orders_open = (
            snapshot.has_open_purpose(self.LONG_TP_EXIT_PURPOSE)
            or snapshot.has_open_purpose(self.SHORT_SL_EXIT_PURPOSE)
        ) if snapshot else False
        block_closed_marker = bool(state.get("block_closed_marker_emitted"))
        if (
            snapshot
            and snapshot.long_qty == 0
            and snapshot.short_qty == 0
            and not exit_orders_open
            and fill_event.status == "FILLED"
            and fill_event.purpose in exit_purposes
            and not block_closed_marker
        ):
            cycle_index = int(state.get("current_effective_cycle") or 0)
            long_fill_entry = cycle_state.get("long_fills", {}).get(str(cycle_index), {})
            long_exit_order_link_id = long_fill_entry.get("client_order_id")
            short_exit_order_link_id = (
                fill_event.client_order_id if "_SHORT_" in fill_event.purpose else None
            )
            payload = {
                "symbol": self.config.symbol,
                "strategy": self.name,
                "cycle_index": cycle_index,
                "positions_flat": True,
                "long_final_size": snapshot.long_qty,
                "short_final_size": snapshot.short_qty,
                "long_realized_pnl": snapshot.realized_long_pnl_total,
                "short_realized_pnl": snapshot.realized_short_pnl_total,
                "net_realized_pnl": snapshot.realized_pnl_total,
            }
            if long_exit_order_link_id:
                payload["long_exit_order_link_id"] = long_exit_order_link_id
            if short_exit_order_link_id:
                payload["short_exit_order_link_id"] = short_exit_order_link_id
            _emit_analyzer_event(logger, "analyzer_block_closed", payload)
            state["block_closed_marker_emitted"] = True
            self._reset_cycle_state(runtime_state)

        processed.add(fill_key)
        cycle_state["processed_fill_ids"] = list(processed)
        self._write_cycle_state(cycle_state)

    def _has_no_strategy_orders(self, snapshot: HedgeSnapshot) -> bool:
        valid_purposes = set(self._all_cycle_purposes() + self._exit_purposes())
        valid_purposes.update({self.LONG_ENTRY_PURPOSE, self.SHORT_ENTRY_PURPOSE})
        return not any(order.purpose in valid_purposes for order in snapshot.active_orders)

    def _has_open_initial_entry_orders(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> bool:
        entry_purposes = {self.LONG_ENTRY_PURPOSE, self.SHORT_ENTRY_PURPOSE}
        if any(order.is_open() and order.purpose in entry_purposes for order in snapshot.active_orders):
            return True
        return any(
            order.status not in {"FILLED", "CANCELED", "REJECTED"} and order.purpose in entry_purposes
            for order in runtime_state.active_orders.values()
        )

    def _update_initial_entry_confirmation(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> bool:
        state = runtime_state.strategy_state
        has_open_initial_orders = self._has_open_initial_entry_orders(snapshot, runtime_state)
        confirmed = (
            snapshot.long_qty > 0
            and snapshot.short_qty > 0
            and not has_open_initial_orders
        )
        state["initial_entry_confirmed"] = confirmed
        if confirmed:
            state["initial_entry_submitted"] = True
        return confirmed

    def _collect_open_initial_entry_orders(self, snapshot: HedgeSnapshot) -> list[dict[str, str | None]]:
        entry_purposes = {self.LONG_ENTRY_PURPOSE, self.SHORT_ENTRY_PURPOSE}
        return [
            {
                "purpose": getattr(order, "purpose", None),
                "status": getattr(order, "status", None),
            }
            for order in snapshot.active_orders
            if getattr(order, "purpose", None) in entry_purposes
            and getattr(order, "status", None) in {"OPEN", "PARTIAL"}
        ]

    def _fixed_long_cycle_qty(
        self,
        initial_long_qty: float,
        current_open_long_qty: float,
        reference_price: float,
        runtime_state: RuntimeState | None = None,
    ) -> float:
        raw_qty = current_open_long_qty * self._pct(self.config.reduction_pct_per_fill)
        normalized = self._normalize_qty(raw_qty, runtime_state)
        if normalized <= 0:
            return 0.0
        if reference_price <= 0:
            return 0.0
        if normalized * reference_price < self.config.min_notional_usdt:
            return 0.0
        return normalized

    def _fixed_short_cycle_qty(
        self,
        initial_short_qty: float,
        current_open_short_qty: float,
        reference_price: float,
        reduction_multiplier: float = 1.0,
        runtime_state: RuntimeState | None = None,
    ) -> float:
        effective_pct = self.config.reduction_pct_per_fill * reduction_multiplier
        raw_qty = current_open_short_qty * self._pct(effective_pct)
        normalized = self._normalize_qty(min(raw_qty, current_open_short_qty), runtime_state)
        if normalized <= 0:
            return 0.0
        if reference_price <= 0:
            return 0.0
        if normalized * reference_price < self.config.min_notional_usdt:
            return 0.0
        return normalized

    def _short_tp_pair_purpose(self, cycle_index: int) -> str:
        return f"CYCLE_{cycle_index}_SHORT_TP"

    def _build_short_tp_pair_intent(
        self,
        snapshot: HedgeSnapshot,
        state: dict,
        trigger_price: float,
        long_cycle_number: int,
        context: StrategyContext,
    ) -> StrategyIntent | None:
        purpose = self._short_tp_pair_purpose(long_cycle_number)
        if snapshot.has_open_purpose(purpose):
            return None

        reduction_multiplier = 0.5
        effective_pct = self.config.reduction_pct_per_fill * reduction_multiplier
        current_short_qty = (
            snapshot.short_qty
            if snapshot.short_qty > 0
            else float(state.get("initial_short_qty") or 0.0)
        )
        if current_short_qty <= 0:
            return None
        short_qty = self._fixed_short_cycle_qty(
            float(state.get("initial_short_qty") or 0.0),
            current_short_qty,
            trigger_price,
            reduction_multiplier=reduction_multiplier,
            runtime_state=runtime_state,
        )
        if short_qty <= 0 or trigger_price <= 0:
            return None

        normalized_price = self._normalize_price(trigger_price, runtime_state)
        context.audit.log_event(
            "fixed_cycle_short_tp_pair_planned",
            strategy=self.name,
            cycle_index=long_cycle_number,
            side="short",
            purpose=purpose,
            entry_reference_price=float(state.get("entry_reference_price") or 0.0),
            trigger_formula="long_reduce_trigger_price",
            current_short_qty=current_short_qty,
            trigger_price_raw=trigger_price,
            trigger_price_normalized=normalized_price,
            reduction_multiplier=reduction_multiplier,
            reduction_pct_used=effective_pct,
            qty_formula="current_short_qty * reduction_pct_per_fill * reduction_multiplier",
            qty_raw=current_short_qty * self._pct(effective_pct),
            qty_normalized=short_qty,
            order_type="Limit",
            reduce_only=True,
        )
        return StrategyIntent(
            side="short",
            qty=short_qty,
            purpose=purpose,
            order_type="Limit",
            price=normalized_price,
            reduce_only=True,
            trigger_price=normalized_price,
            trigger_direction=2,
            trigger_by="LastPrice",
            order_filter="StopOrder",
            position_idx=2,
            metadata={
                "cycle_index": long_cycle_number,
                "cycle_role": "short_tp_pair",
                "replace_open_purpose": purpose,
                "entry_reference_price": float(state.get("entry_reference_price") or 0.0),
                "long_reduce_trigger_price": trigger_price,
            },
        )

    def _normalize_qty(self, qty: float, runtime_state: RuntimeState | None = None) -> float:
        if qty <= 0:
            return 0.0
        symbol, rules, source = self._resolve_instrument_rules(runtime_state)
        qty_step = rules["qty_step"] if rules and rules.get("qty_step", Decimal("0")) > 0 else Decimal(
            str(self.config.qty_step)
        )
        min_order_qty = rules["min_order_qty"] if rules and rules.get("min_order_qty", Decimal("0")) > 0 else Decimal(
            str(self.config.min_order_qty)
        )
        min_notional = rules["min_notional"] if rules and rules.get("min_notional", Decimal("0")) > 0 else Decimal(
            str(self.config.min_notional_usdt)
        )
        qty_dec = Decimal(str(qty))
        if qty_step > 0:
            stepped = (qty_dec / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step
        else:
            stepped = qty_dec
        normalized = rounded_value = max(stepped, Decimal("0"))
        if normalized <= 0 and min_order_qty > 0:
            normalized = min_order_qty
        elif min_order_qty > 0 and normalized < min_order_qty:
            normalized = min_order_qty
        rounded_float = float(normalized)
        logger.info(
            "normalize_qty %s",
            {
                "symbol": symbol,
                "input_qty": qty,
                "qty_step_used": str(qty_step),
                "rounded_qty": rounded_float,
                "source": source,
            },
        )
        logger.info(
            "normalize_qty_debug %s", {"symbol": symbol, "has_rules": source == "instrument_rules"}
        )
        return rounded_float

    def _normalize_price(self, price: float, runtime_state: RuntimeState | None = None) -> float:
        if price <= 0:
            return 0.0
        symbol, rules, source = self._resolve_instrument_rules(runtime_state)
        tick_size = (
            rules["tick_size"]
            if rules and rules.get("tick_size", Decimal("0")) > 0
            else Decimal(str(self.config.price_tick_size))
        )
        if tick_size <= 0:
            tick_size = Decimal(str(self.config.price_tick_size))
            source = "config_fallback"
        price_dec = Decimal(str(price))
        divisor = (price_dec / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        rounded = (divisor * tick_size).quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP)
        round_mode = "up" if rounded >= price_dec else "down"
        normalized = float(rounded)
        logger.info(
            "normalize_price %s",
            {
                "symbol": symbol,
                "input_price": price,
                "tick_size_used": str(tick_size),
                "rounded_price": normalized,
                "round_mode": round_mode,
                "source": source,
            },
        )
        logger.info(
            "normalize_price_debug %s",
            {"symbol": symbol, "has_rules": source == "instrument_rules"},
        )
        return normalized

    def _resolve_instrument_rules(
        self, runtime_state: RuntimeState | None
    ) -> tuple[str, dict[str, Decimal] | None, str]:
        symbol = self.config.symbol.upper()
        if not runtime_state:
            return symbol, None, "config_fallback"
        rules = runtime_state.instrument_rules.get(symbol)
        if rules:
            return symbol, rules, "instrument_rules"
        if symbol not in runtime_state.instrument_rules_fallback_warned:
            runtime_state.instrument_rules_fallback_warned.add(symbol)
            logger.warning(
                "instrument_rules_missing_fallback %s",
                {
                    "symbol": symbol,
                    "reason": "rules_not_found_in_runtime_state",
                },
            )
        return symbol, None, "config_fallback"

    @staticmethod
    def _pct(value: float) -> float:
        return value / 100.0

    @staticmethod
    def _clamp_pct_fraction(value: float, max_fraction: float = 0.9999) -> float:
        return max(min(value, max_fraction), 0.0)

    def _fast_path_second_order(
        self,
        fill_event: FillEvent,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        open_initial_orders = self._collect_open_initial_entry_orders(snapshot)
        if open_initial_orders:
            context.audit.log_event(
                "fixed_cycle_fast_path_skip",
                strategy=self.name,
                skip_reason="initial_entry_order_still_open",
                open_initial_orders=open_initial_orders,
            )
            return []

        side = fill_event.side
        state = runtime_state.strategy_state
        key = "current_long_cycle_index" if side == "long" else "current_short_cycle_index"
        current_cycle_index = int(state.get(key) or 0)
        if current_cycle_index < 1:
            return []

        target_cycle = current_cycle_index + 1
        purpose = self._cycle_purpose(side, target_cycle)
        candidate = next(
            (
                order
                for order in snapshot.active_orders
                if order.purpose == purpose and order.is_open()
            ),
            None,
        )
        if not candidate:
            return []

        trigger_price = self._safe_float(candidate.metadata.get("trigger_price"), None)
        if trigger_price is None:
            return []

        distance_pct = self._pct(
            self.config.long_fill_distance_pct
            if side == "long"
            else self.config.short_fill_distance_pct
        )
        extra_offset = self._pct(self.config.second_order_safety_offset_pct) if target_cycle == 2 else 0.0
        multiplier = 1 - (distance_pct * target_cycle) - extra_offset
        corrected_trigger_price = self._normalize_price(
            max(fill_event.exec_price * multiplier, self.config.price_tick_size),
            runtime_state,
        )

        tick = self.config.price_tick_size or 1e-9
        too_high = (
            trigger_price > corrected_trigger_price + tick
            if side == "long"
            else trigger_price < corrected_trigger_price - tick
        )
        if not too_high:
            return []

        if context.cancel_open_orders_by_purpose:
            context.cancel_open_orders_by_purpose([purpose])

        current_price = snapshot.current_price
        limit_still_valid = (
            (side == "long" and current_price > corrected_trigger_price)
            or (side == "short" and current_price < corrected_trigger_price)
        )

        intents: list[StrategyIntent] = []
        metadata = dict(candidate.metadata)
        metadata.setdefault("cycle_index", target_cycle)
        metadata.setdefault("replace_open_purpose", purpose)
        metadata.setdefault("entry_reference_price", float(state.get("entry_reference_price") or snapshot.current_price))

        if not limit_still_valid:
            context.audit.log_event(
                "limit_rejected_market_fallback",
                strategy=self.name,
                purpose=purpose,
                side=side,
                qty=candidate.qty,
                price=trigger_price,
                order_link_id=candidate.client_order_id,
                original_order_type="Limit",
                fallback_order_type="Market",
                slippage_tolerance_type=self.config.market_fallback_slippage_type,
                slippage_tolerance=self.config.market_fallback_slippage_value,
            )
            metadata["slippage_tolerance_type"] = self.config.market_fallback_slippage_type
            metadata["slippage_tolerance"] = self.config.market_fallback_slippage_value
            intents.append(
                StrategyIntent(
                    side=side,
                    qty=candidate.qty,
                    purpose=purpose,
                    order_type="Market",
                    reduce_only=(side == "short"),
                    metadata=metadata,
                )
            )
        else:
            metadata["trigger_price"] = corrected_trigger_price
            intents.append(
                StrategyIntent(
                    side=side,
                    qty=candidate.qty,
                    purpose=purpose,
                    order_type="Limit",
                    price=corrected_trigger_price + (self.config.price_tick_size if side == "long" else -self.config.price_tick_size),
                    reduce_only=(side == "short"),
                    trigger_price=corrected_trigger_price,
                    trigger_direction=2,
                    trigger_by="LastPrice",
                    order_filter="StopOrder",
                    metadata=metadata,
                )
            )
        return intents

    @staticmethod
    def _seed_long_fill_closed_pnl_fields(long_fill: dict, order_id: str | None = None) -> None:
        if order_id:
            long_fill["order_id"] = order_id
        long_fill.setdefault("order_id", "")
        long_fill.setdefault("closed_pnl", None)
        long_fill.setdefault("closed_qty", None)
        long_fill.setdefault("closed_avg_price", None)
        long_fill.setdefault("closed_cost", None)
        long_fill.setdefault("closed_pnl_ready", False)
        long_fill.setdefault("closed_pnl_updated_time", None)
        long_fill.setdefault("fill_count", None)

    def _refresh_long_fill_closed_pnl(
        self,
        *,
        cycle_index: int,
        long_fill: dict,
        runtime_state: RuntimeState,
        context: StrategyContext,
        occurred_at_ms: int | None = None,
        exec_id: str | None = None,
    ) -> bool:
        self._seed_long_fill_closed_pnl_fields(long_fill)
        order_id = str(long_fill.get("order_id") or "").strip()
        fetcher = getattr(context.order_manager, "fetch_closed_pnl", None) if context.order_manager else None
        if not order_id or not callable(fetcher):
            return False

        logger.debug(
            "closed_pnl_fetch_started",
            extra={
                "order_id": order_id,
                "cycle_index": cycle_index,
                "symbol": self.config.symbol,
                "decision": "fetch",
                "closed_pnl": long_fill.get("closed_pnl"),
                "closed_qty": long_fill.get("closed_qty"),
            },
        )
        start_time_ms = max(occurred_at_ms - 300_000, 0) if occurred_at_ms is not None else None
        rows = fetcher(
            self.config.symbol,
            self.config.category,
            limit=100,
            start_time_ms=start_time_ms,
        )
        if not rows:
            logger.debug(
                "closed_pnl_not_yet_available",
                extra={
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": self.config.symbol,
                    "decision": "deferred",
                    "closed_pnl": None,
                    "closed_qty": None,
                },
            )
            return False

        match = next(
            (
                row
                for row in rows
                if str(row.get("orderId") or "") == order_id
                and str(row.get("symbol") or "").upper() == self.config.symbol.upper()
            ),
            None,
        )
        if match:
            matched_pnl = match.get("closedPnl")
            matched_qty = match.get("closedSize") or match.get("qty")
            logger.debug(
                "closed_pnl_row_found",
                extra={
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": self.config.symbol,
                    "decision": "found",
                    "closed_pnl": matched_pnl,
                    "closed_qty": matched_qty,
                },
            )
            logger.info(
                "closed_pnl_row_matched",
                extra={
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": self.config.symbol,
                    "decision": "matched",
                    "closed_pnl": matched_pnl,
                    "closed_qty": matched_qty,
                },
            )
        else:
            logger.debug(
                "closed_pnl_not_yet_available",
                extra={
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": self.config.symbol,
                    "decision": "deferred",
                    "closed_pnl": None,
                    "closed_qty": None,
                },
            )
            return False

        closed_qty = self._safe_float(match.get("closedSize") or match.get("qty"), None)
        closed_avg_price = self._safe_float(match.get("avgExitPrice") or match.get("orderPrice"), None)
        closed_cost = self._safe_float(match.get("cumExitValue"), None)
        if closed_cost is None and closed_qty is not None and closed_avg_price is not None:
            closed_cost = closed_qty * closed_avg_price

        long_fill["order_id"] = order_id
        long_fill["closed_pnl"] = self._safe_float(match.get("closedPnl"), None)
        long_fill["closed_qty"] = closed_qty
        long_fill["closed_avg_price"] = closed_avg_price
        long_fill["closed_cost"] = closed_cost
        long_fill["closed_pnl_ready"] = long_fill["closed_pnl"] is not None
        long_fill["closed_pnl_updated_time"] = self._safe_int(match.get("updatedTime") or match.get("createdTime"))
        long_fill["fill_count"] = self._safe_int(match.get("fillCount"))
        long_fill["confirmed_closed_pnl"] = long_fill["closed_pnl"]
        long_fill["confirmed_pnl_applied"] = long_fill.get("confirmed_pnl_applied", False)
        self._apply_confirmed_realized_pnl(
            runtime_state=runtime_state,
            client_order_id=long_fill.get("client_order_id"),
            confirmed_pnl=long_fill["confirmed_closed_pnl"],
            side="long",
            exec_id=exec_id or long_fill.get("exec_id"),
        )
        return bool(long_fill["closed_pnl_ready"])

    def _apply_confirmed_realized_pnl(
        self,
        runtime_state: RuntimeState,
        client_order_id: str | None,
        confirmed_pnl: float | None,
        side: str,
        exec_id: str | None = None,
    ) -> None:
        if not client_order_id or confirmed_pnl is None:
            return
        if not exec_id and not client_order_id:
            return
        state = runtime_state.strategy_state
        processed = state.setdefault("processed_pnl_exec_ids", set())
        if isinstance(processed, list):
            processed = set(processed)
            state["processed_pnl_exec_ids"] = processed
        order = state.setdefault("processed_pnl_exec_ids_order", [])
        exec_key = exec_id or client_order_id
        if exec_key in processed:
            return
        applied = runtime_state.confirmed_pnl_applied
        if client_order_id in applied:
            return
        had_temp = client_order_id in runtime_state.temporary_pnl_by_order
        temp_pnl = runtime_state.temporary_pnl_by_order.pop(client_order_id, 0.0)
        if not had_temp:
            logger.warning(
                "missing_temp_pnl",
                extra={
                    "client_order_id": client_order_id,
                    "side": side,
                    "confirmed_pnl": confirmed_pnl,
                },
            )
        state = runtime_state.strategy_state
        net_long = float(state.get("net_long_loss_balance") or 0.0)
        net_short = float(state.get("net_short_loss_balance") or 0.0)
        side_norm = (side or "").lower()
        if side_norm in {"long", "buy"}:
            if temp_pnl != 0.0:
                runtime_state.realized_long_pnl_total -= temp_pnl
            runtime_state.realized_long_pnl_total += confirmed_pnl
            if confirmed_pnl < 0:
                net_long += abs(confirmed_pnl)
            else:
                net_short = max(net_short - confirmed_pnl, 0.0)
        elif side_norm in {"short", "sell"}:
            if temp_pnl != 0.0:
                runtime_state.realized_short_pnl_total -= temp_pnl
            runtime_state.realized_short_pnl_total += confirmed_pnl
            if confirmed_pnl < 0:
                net_short += abs(confirmed_pnl)
            else:
                net_long = max(net_long - confirmed_pnl, 0.0)
        else:
            logger.warning(
                "invalid_side_for_pnl",
                extra={
                    "side": side,
                    "confirmed_pnl": confirmed_pnl,
                    "client_order_id": client_order_id,
                },
            )
            return
        applied.add(client_order_id)
        processed.add(exec_key)
        if isinstance(order, list):
            order.append(exec_key)
            if len(order) > 5000:
                old = order.pop(0)
                processed.discard(old)
        state["processed_pnl_exec_ids"] = processed
        state["processed_pnl_exec_ids_order"] = order
        state["net_long_loss_balance"] = net_long
        state["net_short_loss_balance"] = net_short

    def _cleanup_order_pnl(self, runtime_state: RuntimeState, client_order_id: str | None) -> None:
        if not client_order_id:
            return
        runtime_state.temporary_pnl_by_order.pop(client_order_id, None)
        runtime_state.confirmed_pnl_applied.discard(client_order_id)

    @classmethod
    @staticmethod
    def _safe_float(value: Any, default: float | None) -> float | None:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int | None = None) -> int | None:
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cycle_purpose(cls, side: str, cycle_index: int) -> str:
        if side.lower() == "long":
            return f"CYCLE_{cycle_index}_LONG_ADD"
        return f"CYCLE_{cycle_index}_SHORT_REDUCE"

    def _cycle_state_file_path(self) -> Path:
        return Path(__file__).resolve().parent / "state.json"

    def _default_cycle_state(self) -> dict:
        return {
            "trade_active": False,
            "symbol": self.config.symbol,
            "entry_price": 0.0,
            "last_cycle_reference_price": 0.0,
            "long_cycle_index": 0,
            "short_cycle_index": 0,
            "long_add_pending": False,
            "long_fills": {},
            "short_fills": {},
            "processed_fill_ids": [],
            "cycle_completed_count": 0,
            "cycle_waiting_for_short_tp": False,
            "pending_long_cycle_index": 0,
            "short_tp_pending_cycle": 0,
            "pending_short_cycle_index": 0,
            "current_effective_cycle": 0,
        }

    def _load_cycle_state(self) -> dict:
        path = self._cycle_state_file_path()
        if not path.exists():
            return self._default_cycle_state()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return self._default_cycle_state()
        state = self._default_cycle_state()
        state.update({k: v for k, v in payload.items() if k in state or k in {"entry_price", "trade_active", "symbol"}})
        state["long_fills"] = dict(payload.get("long_fills") or {})
        state["short_fills"] = dict(payload.get("short_fills") or {})
        return state

    def _write_cycle_state(self, cycle_state: dict) -> None:
        if not self.config.restart:
            return
        path = self._cycle_state_file_path()
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(cycle_state), encoding="utf-8")
        tmp_path.replace(path)

    def _ensure_cycle_state(self, runtime_state: RuntimeState) -> dict:
        state = runtime_state.strategy_state
        cycle_state = state.get("cycle_state")
        if not cycle_state:
            cycle_state = self._load_cycle_state() if self.config.restart else self._default_cycle_state()
        if cycle_state.get("symbol") != self.config.symbol:
            cycle_state = self._default_cycle_state()
            if self.config.restart:
                self._write_cycle_state(cycle_state)
        state["cycle_state"] = cycle_state
        state.setdefault("current_long_cycle_index", int(cycle_state.get("long_cycle_index") or 0))
        state.setdefault("current_short_cycle_index", int(cycle_state.get("short_cycle_index") or 0))
        state["cycle_waiting_for_short_tp"] = bool(state.get("cycle_waiting_for_short_tp")) or bool(
            cycle_state.get("cycle_waiting_for_short_tp")
        )
        state["pending_long_cycle_index"] = max(
            int(state.get("pending_long_cycle_index") or 0),
            int(cycle_state.get("pending_long_cycle_index") or 0),
        )
        state["short_tp_pending_cycle"] = max(
            int(state.get("short_tp_pending_cycle") or 0),
            int(cycle_state.get("short_tp_pending_cycle") or 0),
        )
        state.setdefault("long_add_pending", bool(cycle_state.get("long_add_pending")))
        state.setdefault("exit_rebuild_allowed", True)
        state.setdefault("long_add_rebuild_allowed", True)
        return cycle_state

    def _cancel_all_pending_orders(self, context: StrategyContext) -> None:
        canceler = context.cancel_open_orders_by_purpose
        if not canceler:
            return
        canceler(self._all_cycle_purposes() + self._exit_purposes())

    def _reset_cycle_state(self, runtime_state: RuntimeState) -> dict:
        state = runtime_state.strategy_state
        cycle_state = self._default_cycle_state()
        state["cycle_state"] = cycle_state
        state["current_long_cycle_index"] = 0
        state["current_short_cycle_index"] = 0
        state["current_effective_cycle"] = 0
        state["cycle_waiting_for_short_tp"] = False
        state["pending_long_cycle_index"] = 0
        state["short_tp_pending_cycle"] = 0
        state["long_add_pending"] = False
        state["block_closed_marker_emitted"] = False
        state["recovery_marker_emitted"] = False
        state["exit_armed_marker_emitted"] = False
        state["exit_rebuild_allowed"] = True
        state["long_add_rebuild_allowed"] = True
        state["entry_reference_price"] = None
        state["last_exit_signature"] = None
        state["current_effective_cycle"] = 0
        cycle_state["entry_price"] = None
        state["entry_reference_price"] = None
        state["last_exit_signature"] = None
        cycle_state["entry_price"] = None
        state["net_long_loss_balance"] = 0.0
        state["net_short_loss_balance"] = 0.0
        state["processed_pnl_exec_ids"] = set()
        state["processed_pnl_exec_ids_order"] = []
        self._write_cycle_state(cycle_state)
        return cycle_state

    def _cycle_state_last_fill_price(self, fills: dict) -> float | None:
        if not fills:
            return None
        try:
            last_index = max(int(key) for key in fills.keys())
        except ValueError:
            return None
        entry = fills.get(str(last_index)) or {}
        price = entry.get("price")
        return float(price) if price is not None else None

    def _fill_persistence_key(self, fill_event: FillEvent) -> str:
        if fill_event.exec_id:
            return fill_event.exec_id
        return f"{fill_event.client_order_id or ''}|{fill_event.purpose}|{fill_event.exec_price}|{fill_event.exec_qty}"

    def _all_cycle_purposes(self) -> list[str]:
        purposes: list[str] = []
        for cycle_index in range(1, self.config.max_cycles + 1):
            purposes.append(self._cycle_purpose("long", cycle_index))
            purposes.append(self._cycle_purpose("short", cycle_index))
            purposes.append(self._short_tp_pair_purpose(cycle_index))
        return purposes

    def _exit_purposes(self) -> list[str]:
        return [
            self.LONG_TP_EXIT_PURPOSE,
            self.LONG_SL_EXIT_PURPOSE,
            self.SHORT_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
        ]

    def on_order_update(
        self,
        payload,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []