from __future__ import annotations

import json
import time
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

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
    net_realized_pnl_target: float = 0.0

    hard_stop_cycle: int = 8
    hard_stop_pct: float = 1.0
    max_cycles: int = 10

    leverage_long: float = 3.0
    leverage_short: float = 3.0

    use_reduce_only: bool = True

    rest_poll_after_fill_ms: int = 250
    ws_enabled: bool = True
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
    LONG_EXIT_PURPOSE = "LONG_TP_EXIT"
    SHORT_EXIT_PURPOSE = "SHORT_SL_EXIT"
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

        self._ensure_cycle_state(runtime_state)

        context.audit.log_event(
            "fixed_cycle_start",
            strategy=self.name,
            config=asdict(self.config),
            snapshot=snapshot,
        )

        if snapshot.long_qty > 0 and snapshot.short_qty > 0:
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

        if snapshot.long_qty > 0 or snapshot.short_qty > 0:
            state["initial_entry_confirmed"] = True

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
        self._advance_cycle_from_fill(fill_event, runtime_state)

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

        long_qty = self._normalize_qty(self.config.base_notional_usdt / resolved_price)
        short_qty = self._normalize_qty(
            (self.config.base_notional_usdt * self.config.hedge_ratio_short) / resolved_price
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
        price = self._normalize_price(resolved_price) if order_type == "Limit" else None

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
        exit_intents = self._build_exit_intents(
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
        long_reference_candidate = (
            long_fill_price
            or cycle_entry_price
            or reference_price
        )
        long_reference = long_reference_candidate
        # Downside long-add orders must always trigger below the live price.
        # If a stored long fill sits above the current market after restart/rebuild,
        # fall back to the live reference only for this downside path.
        if reference_price > 0 and long_reference > reference_price:
            long_reference = reference_price
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
            if long_add_pending and not snapshot.has_open_purpose(purpose):
                state["long_add_pending"] = False
                cycle_state["long_add_pending"] = False
                long_add_pending = False
            if waiting_for_short_tp:
                context.audit.log_event(
                    "fixed_cycle_downside_skip",
                    strategy=self.name,
                    skip_reason="waiting_for_short_tp",
                    cycle_number=long_cycle_number,
                    purpose=purpose,
                    short_tp_pending_cycle=short_tp_pending_cycle,
                )
            elif snapshot.has_open_purpose(purpose) or long_add_pending:
                state["long_add_pending"] = True
                cycle_state["long_add_pending"] = True
                context.audit.log_event(
                    "fixed_cycle_downside_skip",
                    strategy=self.name,
                    skip_reason="long_order_already_open",
                    cycle_number=long_cycle_number,
                    purpose=purpose,
                )
            else:
                long_qty = self._fixed_long_cycle_qty(
                    initial_long_qty,
                    snapshot.long_qty,
                    reference_price,
                )
                raw_trigger_price = long_reference * (1 - long_distance_pct)
                trigger_price = self._normalize_price(raw_trigger_price)
                if reference_price > 0 and trigger_price >= reference_price:
                    guarded_trigger = max(reference_price - self.config.price_tick_size, self.config.price_tick_size)
                    trigger_price = self._normalize_price(guarded_trigger)
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
                            order_type="Limit",
                            price=trigger_price + self.config.price_tick_size,
                            reduce_only=True,
                            trigger_price=trigger_price,
                            trigger_direction=2,
                            trigger_by="LastPrice",
                            order_filter="StopOrder",
                            metadata={
                                "cycle_index": long_cycle_number,
                                "cycle_role": "long_reduce",
                                "replace_open_purpose": purpose,
                                "entry_reference_price": entry_reference_price,
                            },
                        )
                    )
                    state["long_add_pending"] = True
                    cycle_state["long_add_pending"] = True
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
        if snapshot.has_open_purpose(purpose):
            return []

        long_fill = (cycle_state.get("long_fills") or {}).get(str(cycle_index)) or {}
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

        short_qty = self._fixed_short_cycle_qty(
            float(state.get("initial_short_qty") or 0.0),
            snapshot.short_qty,
            long_fill_price,
        )
        short_reduce_reference = long_fill_price
        short_distance_pct_config = self.config.short_fill_distance_pct
        short_distance_pct = self._clamp_pct_fraction(self._pct(short_distance_pct_config))
        raw_trigger_price = short_reduce_reference * (1 - short_distance_pct)
        raw_trigger_price = max(raw_trigger_price, self.config.price_tick_size)
        trigger_price = self._normalize_price(raw_trigger_price)
        price = self._normalize_price(
            max(trigger_price - self.config.price_tick_size, self.config.price_tick_size)
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

        context.audit.log_event(
            "fixed_cycle_short_cycle_planned",
            strategy=self.name,
            cycle_index=cycle_index,
            side="short",
            purpose=purpose,
            entry_reference_price=float(state.get("entry_reference_price") or 0.0),
            distance_pct=short_distance_pct_config,
            distance_pct_used=short_distance_pct,
            short_reduce_reference=short_reduce_reference,
            trigger_formula="short_reduce_reference * (1 - distance_pct)",
            trigger_price_raw=raw_trigger_price,
            trigger_price_normalized=trigger_price,
            qty_formula="current_short_qty * reduction_pct_per_fill",
            qty_raw=snapshot.short_qty * self._pct(self.config.reduction_pct_per_fill),
            qty_normalized=short_qty,
            order_type="Limit",
            reduce_only=True,
        )
        return [
            StrategyIntent(
                side="short",
                qty=short_qty,
                purpose=purpose,
                order_type="Limit",
                price=price,
                reduce_only=True,
                trigger_price=trigger_price,
                trigger_direction=2,
                trigger_by="LastPrice",
                position_idx=2,
                metadata={
                    "cycle_index": cycle_index,
                    "cycle_role": "short_reduce",
                    "replace_open_purpose": purpose,
                    "entry_reference_price": float(state.get("entry_reference_price") or 0.0),
                    "long_fill_price": long_fill_price,
                    "short_reduce_reference": short_reduce_reference,
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

        short_sl_price = tp_price
        signature = {
            "basket_tp_price": tp_price,
            "basket_break_even_price": break_even_price,
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

        long_metadata = dict(metadata_base)
        long_metadata["replace_open_purpose"] = [self.LONG_EXIT_PURPOSE]
        intents.append(
            StrategyIntent(
                side="long",
                qty=snapshot.long_qty,
                purpose=self.LONG_EXIT_PURPOSE,
                price=tp_price,
                order_type="Limit",
                reduce_only=True,
                trigger_price=tp_price,
                trigger_direction=1,
                trigger_by="LastPrice",
                position_idx=1,
                metadata=long_metadata,
            )
        )

        short_metadata = dict(metadata_base)
        short_metadata["replace_open_purpose"] = [self.SHORT_EXIT_PURPOSE]
        intents.append(
            StrategyIntent(
                side="short",
                qty=snapshot.short_qty,
                purpose=self.SHORT_EXIT_PURPOSE,
                price=short_sl_price,
                order_type="Limit",
                reduce_only=True,
                trigger_price=short_sl_price,
                trigger_direction=1,
                trigger_by="LastPrice",
                position_idx=2,
                metadata=short_metadata,
            )
        )

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
        realized_long_loss = max(-snapshot.realized_long_pnl_total, 0.0)
        realized_short_loss = max(-snapshot.realized_short_pnl_total, 0.0)
        loss_compensation = realized_long_loss + realized_short_loss
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
                "loss_compensation": loss_compensation,
                "denominator": denominator,
            },
        )

        break_even_price = self._normalize_price(max(break_even_price, self.config.price_tick_size))

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
            + components["loss_recovery"]
            + components["goal_profit"]
            + components["buffer"]
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
        loss_recovery = self._loss_recovery_price_component(snapshot)
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

    def _loss_recovery_price_component(self, snapshot: HedgeSnapshot | None) -> float:
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
        realized_long_loss = max(-snapshot.realized_long_pnl_total, 0.0)
        realized_short_loss = max(-snapshot.realized_short_pnl_total, 0.0)
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

    def _advance_cycle_from_fill(self, fill_event: FillEvent, runtime_state: RuntimeState) -> None:
        cycle_index = int(fill_event.metadata.get("cycle_index") or 0)
        if cycle_index <= 0:
            return

        state = runtime_state.strategy_state
        cycle_state = self._ensure_cycle_state(runtime_state)
        cycle_state["trade_active"] = True
        cycle_state["symbol"] = self.config.symbol
        processed = set(cycle_state.get("processed_fill_ids") or [])
        fill_key = self._fill_persistence_key(fill_event)
        if fill_key in processed:
            return

        if "_LONG_" in fill_event.purpose:
            state["current_long_cycle_index"] = max(int(state.get("current_long_cycle_index") or 0), cycle_index)
            state["cycle_waiting_for_short_tp"] = True
            state["pending_long_cycle_index"] = cycle_index
            state["short_tp_pending_cycle"] = cycle_index
            state["long_add_pending"] = False

        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

        if "_LONG_" in fill_event.purpose:
            long_index = int(state.get("current_long_cycle_index") or 0)
            cycle_state["long_cycle_index"] = long_index
            cycle_state["cycle_waiting_for_short_tp"] = True
            cycle_state["pending_long_cycle_index"] = cycle_index
            cycle_state["short_tp_pending_cycle"] = cycle_index
            cycle_state["long_add_pending"] = False
            fills = cycle_state.setdefault("long_fills", {})
            fills[str(cycle_index)] = {"price": fill_event.exec_price, "qty": fill_event.exec_qty}
            if float(cycle_state.get("entry_price") or 0.0) <= 0:
                cycle_state["entry_price"] = fill_event.exec_price

        if "_SHORT_" in fill_event.purpose:
            state["current_short_cycle_index"] = max(int(state.get("current_short_cycle_index") or 0), cycle_index)
            short_index = int(state.get("current_short_cycle_index") or 0)
            cycle_state["short_cycle_index"] = short_index
            fills = cycle_state.setdefault("short_fills", {})
            fills[str(cycle_index)] = {"price": fill_event.exec_price, "qty": fill_event.exec_qty}
            if float(cycle_state.get("entry_price") or 0.0) <= 0:
                cycle_state["entry_price"] = fill_event.exec_price
            if (
                state.get("cycle_waiting_for_short_tp")
                and int(state.get("pending_long_cycle_index") or 0) == cycle_index
            ):
                state["cycle_completed_count"] = int(state.get("cycle_completed_count") or 0) + 1
                state["cycle_waiting_for_short_tp"] = False
                state["pending_long_cycle_index"] = 0
                state["short_tp_pending_cycle"] = 0
                cycle_state["cycle_waiting_for_short_tp"] = False
                cycle_state["pending_long_cycle_index"] = 0
                cycle_state["short_tp_pending_cycle"] = 0
        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

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

    def _fixed_long_cycle_qty(
        self,
        initial_long_qty: float,
        current_open_long_qty: float,
        reference_price: float,
    ) -> float:
        raw_qty = current_open_long_qty * self._pct(self.config.reduction_pct_per_fill)
        normalized = self._normalize_qty(raw_qty)
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
    ) -> float:
        raw_qty = current_open_short_qty * self._pct(self.config.reduction_pct_per_fill)
        normalized = self._normalize_qty(min(raw_qty, current_open_short_qty))
        if normalized <= 0:
            return 0.0
        if reference_price <= 0:
            return 0.0
        if normalized * reference_price < self.config.min_notional_usdt:
            return 0.0
        return normalized

    def _normalize_qty(self, qty: float) -> float:
        if qty <= 0:
            return 0.0
        stepped = int(qty / self.config.qty_step) * self.config.qty_step
        return round(max(stepped, 0.0), 12) if stepped >= self.config.min_order_qty else 0.0

    def _normalize_price(self, price: float) -> float:
        if price <= 0:
            return 0.0
        tick = self.config.price_tick_size
        return round(round(price / tick) * tick, 12)

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
            max(fill_event.exec_price * multiplier, self.config.price_tick_size)
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

    @classmethod
    @staticmethod
    def _safe_float(value: Any, default: float | None) -> float | None:
        if value in (None, ""):
            return default
        try:
            return float(value)
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
        path = self._cycle_state_file_path()
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(cycle_state), encoding="utf-8")
        tmp_path.replace(path)

    def _ensure_cycle_state(self, runtime_state: RuntimeState) -> dict:
        state = runtime_state.strategy_state
        cycle_state = state.get("cycle_state")
        if not cycle_state:
            cycle_state = self._load_cycle_state()
        if cycle_state.get("symbol") != self.config.symbol:
            cycle_state = self._default_cycle_state()
            self._write_cycle_state(cycle_state)
        state["cycle_state"] = cycle_state
        state.setdefault("current_long_cycle_index", int(cycle_state.get("long_cycle_index") or 0))
        state.setdefault("current_short_cycle_index", int(cycle_state.get("short_cycle_index") or 0))
        state.setdefault("cycle_waiting_for_short_tp", bool(cycle_state.get("cycle_waiting_for_short_tp")))
        state.setdefault("pending_long_cycle_index", int(cycle_state.get("pending_long_cycle_index") or 0))
        state.setdefault("short_tp_pending_cycle", int(cycle_state.get("short_tp_pending_cycle") or 0))
        state.setdefault("long_add_pending", bool(cycle_state.get("long_add_pending")))
        return cycle_state

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
        return purposes

    def _exit_purposes(self) -> list[str]:
        return [self.LONG_EXIT_PURPOSE, self.SHORT_EXIT_PURPOSE]

    def on_order_update(
        self,
        payload,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []