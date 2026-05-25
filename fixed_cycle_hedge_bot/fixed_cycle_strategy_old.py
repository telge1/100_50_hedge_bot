from __future__ import annotations

import json
import math
import time
import logging
import subprocess
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .base import HedgeStrategy, StrategyContext
from .models import CalculationTrace, FillEvent, HedgeSnapshot, ManagedOrder, RuntimeState, StrategyIntent
from .hedge_exit_math import calculate_hedge_exit_price
from .trailing_fallback import (
    ShortTpFallbackState,
    reset_short_tp_fallback,
    update_short_tp_fallback,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONFIG_PATH = Path("fixed_cycle_hedge_bot/config/fixed_cycle_config.json")
PER_BOT_CONFIG_ROOT = PROJECT_ROOT / "live_bots"


def _is_per_bot_config(path_obj: Path) -> bool:
    try:
        relative = path_obj.relative_to(PER_BOT_CONFIG_ROOT)
    except ValueError:
        return False
    parts = relative.parts
    return len(parts) >= 4 and parts[-2] == "config" and parts[-1] == "fixed_cycle_config.json"
PNL_VALIDATION_THRESHOLD_USDT = 0.01
POST_EXIT_CLEANUP_MAX_ATTEMPTS = 5
CONFIRMED_CLOSED_PNL_RETRY_INITIAL_DELAY_MS = 2000
CONFIRMED_CLOSED_PNL_RETRY_INTERVAL_MS = 2000
CONFIRMED_CLOSED_PNL_WARNING_MS = 30_000
CONFIRMED_CLOSED_PNL_FINAL_WARNING_MS = 60_000


def _current_time_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class FixedCycleHedgeConfig:
    symbol: str = "BTCUSDT"
    category: str = "linear"
    dynamic_symbol_enabled: bool = False
    best_coin_file: str = "logs/best_coin.json"
    best_coin_max_age_minutes: int = 45

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
    order_fee_rate_pct: float = 0.055
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
    # Number of ticks between SHORT_SL_EXIT and LONG_TP_EXIT in the final basket exit.
    # 0.0 = both final exits use the same trigger price.
    # 1.0 = LONG_TP_EXIT is kept one tick above SHORT_SL_EXIT.
    final_exit_tick_offset: float = 0.0
    trailing_stop_dist: float = 0.003
    short_tp_fallback_activation_drop_pct: float = 0.001
    short_tp_fallback_stop_offset_pct: float = 0.0025
    short_tp_min_threshold_pct_after_long_reduce: float = 0.006
    fallback_stale_seconds: float = 8.0
    qty_step: float = 0.001
    min_order_qty: float = 0.001
    min_notional_usdt: float = 5.0
    dynamic_symbol_hold_minutes: int = 10

    @classmethod
    def from_json_file(cls, path: str | Path | None, *, enforce_expected_path: bool = True) -> "FixedCycleHedgeConfig":
        if not path:
            return cls()
        path_obj = Path(path).resolve()
        expected_path = EXPECTED_CONFIG_PATH.resolve()
        if enforce_expected_path:
            if not (
                path_obj == expected_path or _is_per_bot_config(path_obj)
            ):
                error_message = (
                    f"Invalid config path: {path_obj}\n"
                    f"Expected: {expected_path} or live_bots/*/*/config/fixed_cycle_config.json"
                )
                raise ValueError(error_message)

        payload = json.loads(path_obj.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Fixed-cycle config must be a JSON object")
        return cls(**payload)


logger = logging.getLogger(__name__)
calc_audit_logger = logging.getLogger("fixed_cycle_calc_audit")
calc_audit_handler: logging.Handler | None = None
calc_audit_log_path = Path("logs") / "fixed_cycle_calc_audit.log"
calc_audit_logger.setLevel(logging.INFO)
calc_audit_logger.propagate = False

confirmed_order_pnl_history_path = Path("logs") / "confirmed_order_pnl_history.jsonl"
cycle_state_file_path_override: Path | None = None
default_bot_name = "long_bot_1"


def configure_calc_audit_log_file(path: str | Path | None) -> None:
    global calc_audit_log_path, calc_audit_handler
    target_path = Path(path) if path else Path("logs") / "fixed_cycle_calc_audit.log"
    calc_audit_log_path = target_path
    if calc_audit_handler:
        calc_audit_logger.removeHandler(calc_audit_handler)
        calc_audit_handler.close()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(target_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    calc_audit_logger.addHandler(handler)
    calc_audit_handler = handler


def configure_confirmed_order_pnl_history_file(path: str | Path | None) -> None:
    global confirmed_order_pnl_history_path
    confirmed_order_pnl_history_path = Path(path) if path else Path("logs") / "confirmed_order_pnl_history.jsonl"


def configure_cycle_state_file(path: str | Path | None) -> None:
    global cycle_state_file_path_override
    cycle_state_file_path_override = Path(path).expanduser().resolve() if path else None


def set_default_bot_name(name: str | None) -> None:
    global default_bot_name
    if name:
        default_bot_name = name


configure_calc_audit_log_file(calc_audit_log_path)


def _emit_analyzer_event(logger: logging.Logger, event: str, payload: dict[str, Any]) -> None:
    data = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    logger.info(json.dumps(data))


def _log_event(event: str, payload: dict[str, Any]) -> None:
    logger.info("%s %s", event, payload)


def _log_warning_event(event: str, payload: dict[str, Any]) -> None:
    logger.warning("%s %s", event, payload)


def _safe_audit_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _safe_audit_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_audit_value(val) for val in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return str(value)


def _audit_calc(event: str, data: dict[str, Any], *, level: int = logging.INFO) -> None:
    payload = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat()}
    payload["timestamp_ms"] = int(time.time() * 1000)
    payload.update({key: _safe_audit_value(value) for key, value in data.items()})
    try:
        calc_audit_logger.log(level, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        logger.warning(
            "calc_audit_logging_failed",
            extra={
                "event": event,
                "error": str(exc),
                "data": {key: str(val) for key, val in data.items()},
            },
        )


class FixedCycleHedgeStrategy(HedgeStrategy):
    name = "fixed_cycle"

    STATE_INIT = "INIT"
    STATE_OPENING_HEDGE = "OPENING_HEDGE"
    STATE_PREPLACING_DOWNSIDE_ORDERS = "PREPLACING_DOWNSIDE_ORDERS"
    STATE_RUNNING = "RUNNING"
    STATE_RECONCILING_AFTER_FILL = "RECONCILING_AFTER_FILL"
    STATE_RESETTING_EXITS = "RESETTING_EXITS"
    STATE_REFILL_PENDING = "REFILL_PENDING"
    STATE_HARD_STOP_MODE = "HARD_STOP_MODE"
    STATE_EXITED = "EXITED"
    STATE_ERROR = "ERROR"
    EMERGENCY_EXIT_STATUSES = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DEACTIVATED"}
    EMERGENCY_REJECT_REASON = "EC_NOIMMEDIATEQTYTOFILL"
    EMERGENCY_EXIT_MAX_RETRIES = 3
    EMERGENCY_FLAT_LONG_PURPOSE = "EMERGENCY_FLAT_LONG"
    EMERGENCY_FLAT_SHORT_PURPOSE = "EMERGENCY_FLAT_SHORT"

    LONG_ENTRY_PURPOSE = "INITIAL_LONG_ENTRY"
    SHORT_ENTRY_PURPOSE = "INITIAL_SHORT_ENTRY"
    LONG_TP_EXIT_PURPOSE = "LONG_TP_EXIT"
    LONG_SL_EXIT_PURPOSE = "LONG_SL_EXIT"
    SHORT_TP_EXIT_PURPOSE = "SHORT_TP_EXIT"
    SHORT_SL_EXIT_PURPOSE = "SHORT_SL_EXIT"
    LONG_TP_EXIT_RECOVERY_PURPOSE = "LONG_TP_EXIT_RECOVERY"
    SHORT_SL_EXIT_RECOVERY_PURPOSE = "SHORT_SL_EXIT_RECOVERY"
    SHORT_HARD_STOP_PURPOSE = "SHORT_HARD_STOP_EXIT"

    def __init__(self, config: FixedCycleHedgeConfig | None = None) -> None:
        self.config = config or FixedCycleHedgeConfig()
        logger = logging.getLogger(__name__)
        config_fields = [field.name for field in fields(self.config)]
        long_attr = hasattr(self.config, "long_exit_reduce_only")
        short_attr = hasattr(self.config, "short_exit_reduce_only")
        logger.debug(
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
        self.realized_long_loss_total = 0.0

    def _get_short_tp_fallback_state(self, runtime_state: RuntimeState) -> ShortTpFallbackState:
        return ShortTpFallbackState.from_dict(runtime_state.strategy_state.get("short_tp_fallback_state"))

    def _store_short_tp_fallback_state(
        self, runtime_state: RuntimeState, fallback_state: ShortTpFallbackState
    ) -> None:
        runtime_state.strategy_state["short_tp_fallback_state"] = fallback_state.to_dict()

    def _clear_short_tp_fallback_order_context(self, runtime_state: RuntimeState) -> None:
        runtime_state.strategy_state.pop("short_tp_fallback_order_context", None)

    def _register_short_tp_fallback_order(self, runtime_state: RuntimeState) -> None:
        fallback_state = self._get_short_tp_fallback_state(runtime_state)
        context = runtime_state.strategy_state.get("short_tp_fallback_order_context") or {}
        client_order_id = fallback_state.client_order_id
        if not client_order_id:
            return
        managed_order = ManagedOrder(
            client_order_id=client_order_id,
            side="short",
            qty=float(fallback_state.qty or 0.0),
            purpose=str(context.get("purpose") or "SHORT_TP_FALLBACK"),
            price=float(fallback_state.original_trigger_price or 0.0),
            order_type="Market",
            reduce_only=True,
            exchange_order_id=fallback_state.exchange_order_id,
            status="OPEN",
            remaining_qty=float(fallback_state.qty or 0.0),
            metadata={
                "cycle_index": int(context.get("cycle_index") or 0),
                "cycle_role": "short_reduce",
                "short_tp_fallback": True,
            },
        )
        runtime_state.active_orders[client_order_id] = managed_order
        if fallback_state.exchange_order_id:
            runtime_state.exchange_to_client_id[fallback_state.exchange_order_id] = client_order_id

    def _force_fresh_start_reset(self, runtime_state: RuntimeState) -> None:
        state = runtime_state.strategy_state
        preserved_last_trade = self._preserve_last_trade_pnl_fields(state)
        state["startup_flat_reset_applied"] = False
        state["full_exit_reset_in_progress"] = False
        state["block_exit_rebuild_until_pnl_ready"] = False
        state["force_exit_rebuild"] = False
        state["long_add_locked"] = False
        state["long_add_pending"] = False
        state["cycle_waiting_for_short_tp"] = False
        state["short_tp_pending_cycle"] = 0
        state["exit_locked"] = False
        state["exit_rebuild_allowed"] = True
        state["trailing_active"] = None
        state["refill_pending"] = False
        state["refill_in_progress"] = False
        state["refill_long_filled"] = False
        state["refill_short_filled"] = False
        state["force_short_tp_rebuild"] = False
        state["cycle_long_add_filled"] = False
        state["cycle_short_tp_filled"] = False
        state["cycle_pair_count"] = 0
        state["cycle_completed_count"] = 0
        state["current_long_cycle_index"] = 0
        state["current_short_cycle_index"] = 0
        state["current_effective_cycle"] = 0
        state["pending_long_cycle_index"] = 0
        state["pending_short_cycle_index"] = 0
        state["last_exit_signature"] = None
        state["last_fill_info"] = {}
        state["latest_break_even_price"] = 0.0
        state["latest_tp_price"] = 0.0
        state["last_short_tp_trigger_price"] = 0.0
        state["last_expected_short_tp_net"] = 0.0
        state["last_short_tp_qty"] = 0.0
        state["short_tp_fallback_state"] = None
        state["short_tp_fallback_order_context"] = None
        state["pending_cycle_loss_usdt"] = 0.0
        state["short_exit_recovery_submitted"] = False
        state["long_exit_recovery_submitted"] = False
        state["exit_recovery_marker"] = False
        state["pending_loss_updated_in_fill"] = False
        state["pending_loss_exit_old_signature"] = None
        state["pending_loss_exit_rebuild_reason"] = None
        state["initial_entry_confirmed"] = False
        state["initial_entry_submitted"] = False
        state["current_trade_pnl_state_reset_for_entry"] = False
        state["entry_reference_price"] = 0.0
        state["initial_long_qty"] = 0.0
        state["initial_short_qty"] = 0.0
        state.pop("flat_waiting_order_cleanup_logged", None)
        state.pop("flat_waiting_final_pnl_logged", None)
        state.pop("flat_final_pnl_ready_logged", None)
        state.pop("final_pnl_context_missing_logged", None)
        self._restore_last_trade_pnl_fields(state, preserved_last_trade)

    def _clear_startup_zero_state_residuals(self, runtime_state: RuntimeState) -> None:
        state = runtime_state.strategy_state
        keys_to_pop = [
            "final_trade_pnl_audited",
            "final_long_exit_audited",
            "final_short_exit_audited",
            "final_long_exit_order_context",
            "final_short_exit_order_context",
            "final_exit_closed_pnl_signatures",
            "audit_processed_exit_fill_ids",
            "audit_completed_cycle_indices",
            "processed_pnl_exec_ids",
            "processed_pnl_exec_ids_order",
            "trade_block_id",
            "last_trade_block_id",
            "post_exit_cleanup_required",
            "post_exit_cleanup_verified",
            "post_exit_cleanup_in_progress",
            "post_exit_cleanup_attempts",
            "post_exit_cleanup_started_at",
            "post_exit_cleanup_verified_at",
            "post_exit_cleanup_verified_snapshot_updated_at",
            "restart_delayed_pending_final_pnl_logged",
            "short_exit_recovery_submitted",
            "long_exit_recovery_submitted",
            "exit_recovery_marker",
        ]
        for key in keys_to_pop:
            state.pop(key, None)
        for key in list(state.keys()):
            if key.startswith("last_trade_"):
                state.pop(key, None)
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "cycle_pnl_entries": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        state["startup_flat_reset_applied"] = True

    def prepare_for_clean_startup(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> bool:
        if snapshot.long_qty > 0 or snapshot.short_qty > 0 or snapshot.active_orders:
            return False
        state = runtime_state.strategy_state
        cycle_state = state.get("cycle_state") or {}
        stale_runtime_orders = [
            order.purpose or order.client_order_id
            for order in runtime_state.active_orders.values()
            if order.status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
        ]
        stale_details = {
            "initial_entry_confirmed": bool(state.get("initial_entry_confirmed")),
            "initial_entry_submitted": bool(state.get("initial_entry_submitted")),
            "cycle_completed_count": int(state.get("cycle_completed_count") or 0),
            "cycle_waiting_for_short_tp": bool(state.get("cycle_waiting_for_short_tp")),
            "short_tp_pending_cycle": int(state.get("short_tp_pending_cycle") or 0),
            "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
            "cycle_state_trade_active": bool(cycle_state.get("trade_active")),
            "cycle_state_long_add_pending": bool(cycle_state.get("long_add_pending")),
            "cycle_state_waiting_for_short_tp": bool(cycle_state.get("cycle_waiting_for_short_tp")),
            "cycle_state_short_tp_pending_cycle": int(cycle_state.get("short_tp_pending_cycle") or 0),
            "stale_runtime_orders": stale_runtime_orders,
        }
        logger.info("fixed_cycle_startup_zero_state_reset_started %s", stale_details)

        runtime_state.active_orders.clear()
        runtime_state.exchange_to_client_id.clear()
        runtime_state.temporary_pnl_by_order.clear()
        runtime_state.confirmed_pnl_applied.clear()
        runtime_state.processed_fill_cumulative.clear()
        runtime_state.realized_long_pnl_total = 0.0
        runtime_state.realized_short_pnl_total = 0.0

        self._reset_cycle_state(runtime_state)
        self._force_fresh_start_reset(runtime_state)
        self._clear_startup_zero_state_residuals(runtime_state)
        logger.info(
            "fixed_cycle_startup_zero_state_reset_cleared_old_exit_manifest %s",
            {
                "symbol": self.config.symbol,
                "final_long_exit_order_context_present": bool(state.get("final_long_exit_order_context")),
                "final_short_exit_order_context_present": bool(state.get("final_short_exit_order_context")),
                "final_long_exit_audited": bool(state.get("final_long_exit_audited")),
                "final_short_exit_audited": bool(state.get("final_short_exit_audited")),
                "final_exit_closed_pnl_signatures": state.get("final_exit_closed_pnl_signatures"),
                "trade_block_id": state.get("trade_block_id"),
                "last_trade_block_id": state.get("last_trade_block_id"),
            },
        )
        logger.info(
            "fixed_cycle_startup_zero_state_reset_cleared_old_intents %s",
            {
                "symbol": self.config.symbol,
                "active_order_count": len(runtime_state.active_orders),
                "stale_runtime_orders": stale_runtime_orders,
                "cycle_state": state.get("cycle_state"),
                "initial_entry_confirmed": bool(state.get("initial_entry_confirmed")),
                "initial_entry_submitted": bool(state.get("initial_entry_submitted")),
            },
        )
        logger.info(
            "fixed_cycle_restart_state_cleanup_complete %s",
            {
                "symbol": self.config.symbol,
                "long_add_locked": state.get("long_add_locked"),
                "long_add_pending": state.get("long_add_pending"),
                "cycle_waiting_for_short_tp": state.get("cycle_waiting_for_short_tp"),
                "short_tp_pending_cycle": state.get("short_tp_pending_cycle"),
                "pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
                "exit_locked": state.get("exit_locked"),
                "exit_rebuild_allowed": state.get("exit_rebuild_allowed"),
                "trailing_active": state.get("trailing_active"),
                "refill_pending": state.get("refill_pending"),
                "refill_in_progress": state.get("refill_in_progress"),
                "cycle_completed_count": state.get("cycle_completed_count"),
                "cycle_pair_count": state.get("cycle_pair_count"),
                "current_long_cycle_index": state.get("current_long_cycle_index"),
                "current_short_cycle_index": state.get("current_short_cycle_index"),
                "current_effective_cycle": state.get("current_effective_cycle"),
                "cycle_state": state.get("cycle_state"),
            },
        )

        state["bot_state"] = self.STATE_INIT
        state["cycle_completed_count"] = 0
        state["cycle_pair_count"] = 0
        state["cycle_long_add_filled"] = False
        state["cycle_short_tp_filled"] = False
        state["pending_long_cycle_index"] = 0
        state["pending_short_cycle_index"] = 0
        state["current_long_cycle_index"] = 0
        state["current_short_cycle_index"] = 0
        state["current_effective_cycle"] = 0
        state["long_add_pending"] = False
        state["refill_pending"] = False
        state["refill_in_progress"] = False
        state["refill_long_filled"] = False
        state["refill_short_filled"] = False
        state["refill_state"] = {}
        state["initial_entry_retry_count"] = 0
        state["initial_total_notional_usdt"] = 0.0
        state["last_structure_refresh_ms"] = 0
        state["open_long_qty"] = 0.0
        state["open_short_qty"] = 0.0
        state["long_avg"] = 0.0
        state["short_avg"] = 0.0
        state["realized_pnl_total"] = 0.0
        state["audit_processed_exit_fill_ids"] = []
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "cycle_pnl_entries": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        state["dynamic_entry_hold_initialized"] = False
        state.pop("next_dynamic_entry_allowed_at", None)
        state["fresh_restart_required"] = False

        logger.info(
            "fixed_cycle_clean_startup_reset_complete %s",
            {
                "symbol": self.config.symbol,
                "cycle_completed_count": state.get("cycle_completed_count"),
                "cycle_waiting_for_short_tp": state.get("cycle_waiting_for_short_tp"),
                "short_tp_pending_cycle": state.get("short_tp_pending_cycle"),
                "pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
                "active_order_count": len(runtime_state.active_orders),
                "startup_flat_reset_applied": bool(state.get("startup_flat_reset_applied")),
            },
        )
        return True

    def _log_short_tp_fallback_event(
        self,
        event_name: str,
        fallback_state: ShortTpFallbackState,
        current_price: float,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "original_trigger_price": fallback_state.original_trigger_price,
            "activation_price": fallback_state.activation_price,
            "trailing_distance": fallback_state.trailing_distance,
            "qty": fallback_state.qty,
            "current_price": current_price,
            "lowest_price": fallback_state.lowest_price,
            "submitted": fallback_state.submitted,
            "submit_failed": fallback_state.submit_failed,
            "position_idx": fallback_state.position_idx,
        }
        if extra:
            payload.update(extra)
        log_method = logger.info
        if event_name == "SHORT_TP_FALLBACK_ACTIVE":
            log_method = logger.debug
        elif event_name == "SHORT_TP_FALLBACK_SUBMIT_FAILED":
            log_method = logger.warning
        log_method("%s %s", event_name, payload)

    def _maybe_run_short_tp_fallback(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> None:
        fallback_state = self._get_short_tp_fallback_state(runtime_state)
        if not fallback_state.active:
            return
        current_price = float(snapshot.current_price or 0.0)
        if current_price <= 0:
            return
        now_ms = int(time.time() * 1000)
        start_ms = fallback_state.started_at_ms or now_ms
        age_seconds = (now_ms - start_ms) / 1000
        order_manager = context.order_manager

        def _reset_fallback(*, cancel_existing_order: bool = True) -> None:
            cancel_success = True
            if cancel_existing_order and fallback_state.exchange_order_id and order_manager:
                try:
                    cancel_success = bool(
                        order_manager.cancel_order(
                            fallback_state.exchange_order_id,
                            symbol=self.config.symbol,
                            category=self.config.category,
                        )
                    )
                except Exception as exc:
                    cancel_success = False
                    logger.warning(
                        "SHORT_TP_FALLBACK_CANCEL_FAILED %s",
                        {
                            "exchange_order_id": fallback_state.exchange_order_id,
                            "error": str(exc),
                        },
                    )
            if not cancel_success:
                return
            reset_short_tp_fallback(fallback_state)
            self._store_short_tp_fallback_state(runtime_state, fallback_state)
            self._clear_short_tp_fallback_order_context(runtime_state)
            has_short_tp = any(
                "SHORT_TP" in str(getattr(order, "purpose", "") or "")
                and getattr(order, "status", None) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
                for order in runtime_state.active_orders.values()
            ) or any(
                "SHORT_TP" in str(getattr(order, "purpose", "") or "")
                and getattr(order, "status", None) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
                for order in snapshot.active_orders
            )
            if not has_short_tp:
                runtime_state.strategy_state["force_short_tp_rebuild"] = True

        if not fallback_state.submitted:
            log_extra = {
                "original_trigger_price": fallback_state.original_trigger_price,
                "activation_price": fallback_state.activation_price,
                "lowest_price": fallback_state.lowest_price,
                "current_price": current_price,
                "age_seconds": round(age_seconds, 2),
                "submitted": fallback_state.submitted,
            }
            if fallback_state.original_trigger_price > 0 and current_price >= (
                fallback_state.original_trigger_price
            ):
                self._log_short_tp_fallback_event(
                    "SHORT_TP_FALLBACK_RETRY_NORMAL_TP", fallback_state, current_price, extra=log_extra
                )
                _reset_fallback()
                return
            if age_seconds >= float(self.config.fallback_stale_seconds or 0.0):
                self._log_short_tp_fallback_event(
                    "SHORT_TP_FALLBACK_STALE_RESET", fallback_state, current_price, extra=log_extra
                )
                _reset_fallback()
                return
        if fallback_state.submitted and snapshot.short_qty <= 0:
            self._log_short_tp_fallback_event("SHORT_TP_FALLBACK_FILLED", fallback_state, current_price)
            _reset_fallback()
            return
        if snapshot.short_qty <= 0:
            self._log_short_tp_fallback_event("SHORT_TP_FALLBACK_ABORTED", fallback_state, current_price)
            _reset_fallback()
            return
        self._log_short_tp_fallback_event("SHORT_TP_FALLBACK_ACTIVE", fallback_state, current_price)
        if fallback_state.submitted and not fallback_state.submit_failed:
            if fallback_state.exchange_order_id:
                return
        if (
            fallback_state.last_submit_attempt_ms
            and now_ms - int(fallback_state.last_submit_attempt_ms) < 1000
        ):
            return
        fallback_state.last_submit_attempt_ms = now_ms
        submitted, response = update_short_tp_fallback(
            fallback_state,
            order_manager=context.order_manager,
            symbol=self.config.symbol,
            category=self.config.category,
            current_price=current_price,
            activation_drop_pct=self.config.short_tp_fallback_activation_drop_pct,
            stop_offset_pct=self.config.short_tp_fallback_stop_offset_pct,
        )
        self._store_short_tp_fallback_state(runtime_state, fallback_state)
        if submitted:
            self._register_short_tp_fallback_order(runtime_state)
            self._log_short_tp_fallback_event(
                "SHORT_TP_FALLBACK_SUBMITTED",
                fallback_state,
                current_price,
                extra={"response": response},
            )
        elif fallback_state.submit_failed:
            self._log_short_tp_fallback_event(
                "SHORT_TP_FALLBACK_SUBMIT_FAILED",
                fallback_state,
                current_price,
                extra={"response": response},
            )
        elif response:
            self._log_short_tp_fallback_event(
                "SHORT_TP_FALLBACK_ACTIVE",
                fallback_state,
                current_price,
                extra={"response": response},
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
        state.setdefault("cycle_pair_count", 0)
        state.setdefault("cycle_long_add_filled", False)
        state.setdefault("cycle_short_tp_filled", False)
        state.setdefault("refill_pending", False)
        state.setdefault("refill_in_progress", False)
        state.setdefault("refill_long_filled", False)
        state.setdefault("refill_short_filled", False)
        state.setdefault("refill_state", {})
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
        state.setdefault("pending_cycle_loss_usdt", 0.0)
        state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        state.setdefault("audit_processed_exit_fill_ids", [])
        state.setdefault("block_exit_rebuild_until_pnl_ready", False)
        state.setdefault("force_exit_rebuild", False)
        state.setdefault("pending_loss_updated_in_fill", False)
        state.setdefault("pending_loss_exit_old_signature", None)
        state.setdefault("pending_loss_exit_rebuild_reason", None)
        state.setdefault("realized_long_loss_total", 0.0)
        self.realized_long_loss_total = float(state.get("realized_long_loss_total") or 0.0)
        state.setdefault("processed_pnl_exec_ids", [])
        state.setdefault("processed_pnl_exec_ids_order", [])
        state["recovery_marker_emitted"] = False
        state["block_closed_marker_emitted"] = False
        state["exit_armed_marker_emitted"] = False
        state.setdefault("exit_rebuild_allowed", True)
        state.setdefault("exit_rebuild_allowed", True)
        state.setdefault("fresh_restart_required", False)
        self._retry_pending_cycle_closed_pnl_fills(runtime_state, context)
        if state.get("fresh_restart_required"):
            if state.get("full_exit_reset_in_progress"):
                return []
            if snapshot.long_qty > 0 or snapshot.short_qty > 0:
                return []
            if self._block_flat_restart_until_final_pnl(
                snapshot, runtime_state, context, reason="on_start_fresh_restart_required"
            ):
                return []
            if not self._dynamic_symbol_entry_gate_allows_entry(runtime_state, context, "fresh_restart"):
                return []
            best_coin = self._load_best_coin_symbol_from_file(
                self.config.best_coin_file or "logs/best_coin.json"
            )
            desired_symbol = (
                best_coin["symbol"].upper() if best_coin and best_coin.get("symbol") else None
            )
            current_config_symbol = str(self.config.symbol or "").upper()
            snapshot_symbol = str(snapshot.symbol or "").upper()
            restart_requested = bool(state.get("restart_requested_after_full_exit"))
            if (
                desired_symbol
                and snapshot_symbol
                and desired_symbol != current_config_symbol
                and desired_symbol != snapshot_symbol
                and not restart_requested
            ):
                active_snapshot_order_purposes = [
                    getattr(order, "purpose", None) or getattr(order, "client_order_id", None)
                    for order in snapshot.active_orders
                    if getattr(order, "status", None) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
                ]
                active_runtime_order_purposes = [
                    getattr(order, "purpose", None) or getattr(order, "client_order_id", None)
                    for order in runtime_state.active_orders.values()
                    if getattr(order, "status", None) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
                ]
                self._emit_final_trade_pnl_if_complete_or_fetch(
                    runtime_state, context, "fresh_restart_gate_retry"
                )
                final_pnl_ready = self._final_pnl_ready_for_restart(runtime_state)
                if not final_pnl_ready:
                    if not state.get("restart_delayed_pending_final_pnl_logged"):
                        _log_event(
                            "fixed_cycle_restart_delayed_pending_final_pnl",
                            {
                                "symbol": self.config.symbol,
                                "desired_symbol": desired_symbol,
                                "final_trade_pnl_audited": state.get(
                                    "final_trade_pnl_audited"
                                ),
                                "final_long_exit_audited": state.get(
                                    "final_long_exit_audited"
                                ),
                                "final_short_exit_audited": state.get(
                                    "final_short_exit_audited"
                                ),
                                "last_trade_pnl_complete": state.get(
                                    "last_trade_pnl_complete"
                                ),
                                "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                                "trade_block_id": state.get("trade_block_id"),
                                "last_trade_block_id": state.get("last_trade_block_id"),
                                "pnl_ready_for_current_trade": final_pnl_ready,
                                "reason": "fresh_restart_pending_final_pnl",
                            },
                        )
                        state["restart_delayed_pending_final_pnl_logged"] = True
                    return []
                state.pop("restart_delayed_pending_final_pnl_logged", None)
                _log_event(
                    "fixed_cycle_restart_allowed_after_final_pnl",
                    {
                        "symbol": self.config.symbol,
                        "desired_symbol": desired_symbol,
                        "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                        "last_trade_pnl_finalized_at": state.get(
                            "last_trade_pnl_finalized_at"
                        ),
                        "trade_block_id": state.get("trade_block_id"),
                        "last_trade_block_id": state.get("last_trade_block_id"),
                        "pnl_ready_for_current_trade": final_pnl_ready,
                    },
                )
                if self._trigger_restart_script_after_full_exit(
                    snapshot,
                    runtime_state,
                    context,
                    desired_symbol,
                    active_snapshot_order_purposes,
                    active_runtime_order_purposes,
                    reason="full_exit_flat_symbol_changed",
                ):
                    return []
            cleanup_snapshot = self._ensure_post_exit_cleanup_ready_for_fresh_restart(
                snapshot,
                runtime_state,
                context,
                reason="on_start_fresh_restart_required",
            )
            if cleanup_snapshot is None:
                return []
            snapshot = cleanup_snapshot
            self._maybe_update_symbol_from_best_coin(runtime_state, context, "fresh_restart")
            state["full_exit_reset_in_progress"] = True
            try:
                logger.info("fixed_cycle_full_exit_reset_start", {})
                self._reset_cycle_state(runtime_state)
                self._force_fresh_start_reset(runtime_state)
                runtime_state.realized_long_pnl_total = 0.0
                runtime_state.realized_short_pnl_total = 0.0
                intents = self._build_entry_intents(snapshot, runtime_state, context)
                if intents:
                    state["fresh_restart_required"] = False
                    state["dynamic_entry_hold_initialized"] = False
                    state.pop("next_dynamic_entry_allowed_at", None)
                return intents
            finally:
                state["full_exit_reset_in_progress"] = False

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
        if not self._dynamic_symbol_entry_gate_allows_entry(runtime_state, context, "startup"):
            return []
        self._maybe_update_symbol_from_best_coin(runtime_state, context, "startup")
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
            state["dynamic_entry_hold_initialized"] = False
            state.pop("next_dynamic_entry_allowed_at", None)
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
        self._retry_pending_cycle_closed_pnl_fills(runtime_state, context)
        self._maybe_run_short_tp_fallback(snapshot, runtime_state, context)
        expected_cancel_intents = self._check_expected_cancel_replacement_timeouts(
            snapshot,
            runtime_state,
            context,
        )
        if expected_cancel_intents is not None:
            return expected_cancel_intents
        emergency_intents = self._maybe_handle_emergency_exit_tick(snapshot, runtime_state, context)
        if emergency_intents is not None:
            return emergency_intents
        if state.pop("force_short_tp_rebuild", False):
            return self._rebuild_structure(snapshot, runtime_state, context, reason="short_tp_fallback_reset")
        if state.get("fresh_restart_required"):
            if snapshot.long_qty > 0 or snapshot.short_qty > 0:
                return []
            if self._block_flat_restart_until_final_pnl(
                snapshot, runtime_state, context, reason="on_tick_fresh_restart_required"
            ):
                return []
            self._emit_final_trade_pnl_if_complete_or_fetch(
                runtime_state, context, "fresh_restart_required_flat_gate"
            )
            if not self._final_pnl_ready_for_restart(runtime_state):
                if not state.get("restart_delayed_pending_final_pnl_logged"):
                    _log_event(
                        "fixed_cycle_restart_delayed_pending_final_pnl",
                        {
                            "symbol": self.config.symbol,
                            "desired_symbol": self.config.symbol,
                            "final_trade_pnl_audited": state.get("final_trade_pnl_audited"),
                            "final_long_exit_audited": state.get("final_long_exit_audited"),
                            "final_short_exit_audited": state.get("final_short_exit_audited"),
                            "last_trade_pnl_complete": state.get("last_trade_pnl_complete"),
                            "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                            "trade_block_id": state.get("trade_block_id"),
                            "last_trade_block_id": state.get("last_trade_block_id"),
                            "pnl_ready_for_current_trade": False,
                            "reason": "fresh_restart_required_flat_gate",
                        },
                    )
                    state["restart_delayed_pending_final_pnl_logged"] = True
                return []
            state.pop("restart_delayed_pending_final_pnl_logged", None)

            cleanup_snapshot = self._ensure_post_exit_cleanup_ready_for_fresh_restart(
                snapshot,
                runtime_state,
                context,
                reason="on_tick_fresh_restart_required",
            )
            if cleanup_snapshot is None:
                return []
            snapshot = cleanup_snapshot

            logger.info("fixed_cycle_full_exit_reset_start", {"source": "on_tick_fresh_restart_required"})
            state["full_exit_reset_in_progress"] = True
            try:
                self._reset_cycle_state(runtime_state)
                self._force_fresh_start_reset(runtime_state)
                runtime_state.realized_long_pnl_total = 0.0
                runtime_state.realized_short_pnl_total = 0.0

                if not self._dynamic_symbol_entry_gate_allows_entry(
                    runtime_state, context, "fresh_restart_tick"
                ):
                    return []

                self._maybe_update_symbol_from_best_coin(runtime_state, context, "fresh_restart_tick")
                intents = self._build_entry_intents(snapshot, runtime_state, context)
                if intents:
                    state["fresh_restart_required"] = False
                    state["dynamic_entry_hold_initialized"] = False
                    state.pop("next_dynamic_entry_allowed_at", None)
                    return intents
                return []
            finally:
                state["full_exit_reset_in_progress"] = False

        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            if self._block_flat_restart_until_final_pnl(
                snapshot, runtime_state, context, reason="on_tick_flat_before_entry"
            ):
                return []

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

            if not self._dynamic_symbol_entry_gate_allows_entry(runtime_state, context, "tick_retry"):
                return []
            self._maybe_update_symbol_from_best_coin(runtime_state, context, "tick_retry")

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
                state["dynamic_entry_hold_initialized"] = False
                state.pop("next_dynamic_entry_allowed_at", None)
            return intents

        if state.get("bot_state") == self.STATE_EXITED:
            return []

        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            if self._block_flat_restart_until_final_pnl(
                snapshot, runtime_state, context, reason="on_tick_flat_exit_state"
            ):
                return []
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
        metadata = fill_event.metadata or {}
        purpose = fill_event.purpose or ""
        try:
            self._audit_exit_pnl_summary(fill_event, runtime_state, context)
        except Exception as exc:  # pragma: no cover - audit must never block
            logger.warning(
                "exit_pnl_audit_failed_non_blocking",
                {
                    "error": str(exc),
                    "purpose": fill_event.purpose,
                    "client_order_id": fill_event.client_order_id,
                    "exchange_order_id": fill_event.exchange_order_id,
                    "exec_id": fill_event.exec_id,
                },
            )
        fill_type, cycle_index = self._classify_exit_fill_for_audit(fill_event)
        cycle_order_already_confirmed = False
        if fill_type == "cycle_short_tp":
            cycle_order_already_confirmed = self._cycle_order_confirmed(
                runtime_state,
                fill_event.exchange_order_id,
                fill_event.purpose,
            )
            if cycle_order_already_confirmed:
                _log_event(
                    "fixed_cycle_cycle_closed_pnl_already_confirmed_for_order",
                    {
                        "cycle_index": cycle_index,
                        "purpose": fill_event.purpose,
                        "client_order_id": fill_event.client_order_id,
                        "exchange_order_id": fill_event.exchange_order_id,
                        "exec_id": fill_event.exec_id,
                    },
                )
                self._remove_pending_cycle_closed_pnl_for_order(
                    runtime_state,
                    exchange_order_id=fill_event.exchange_order_id,
                    purpose=fill_event.purpose,
                )
            if (
                not cycle_order_already_confirmed
                and not self._has_confirmed_cycle_closed_pnl(fill_event, fill_type)
            ):
                event_name = "fixed_cycle_short_reduce_pnl_waiting_for_closed_pnl"
                self._queue_pending_cycle_closed_pnl_fill(
                    fill_event,
                    runtime_state,
                    fill_type=fill_type,
                    cycle_index=cycle_index,
                )
                _log_event(
                    event_name,
                    {
                        "cycle_index": cycle_index,
                        "purpose": fill_event.purpose,
                        "client_order_id": fill_event.client_order_id,
                        "exchange_order_id": fill_event.exchange_order_id,
                        "exec_id": fill_event.exec_id,
                        "runtime_calculated_pnl": (fill_event.metadata or {}).get("runtime_calculated_pnl"),
                        "exec_pnl": (fill_event.metadata or {}).get("exec_pnl"),
                        "reason": "queue_closed_pnl_retry_without_blocking_cycle_advance",
                    },
                )
                context.audit.log_event(
                    event_name,
                    strategy=self.name,
                    cycle_index=cycle_index,
                    purpose=fill_event.purpose,
                    client_order_id=fill_event.client_order_id,
                    exchange_order_id=fill_event.exchange_order_id,
                    exec_id=fill_event.exec_id,
                )
        if (
            isinstance(purpose, str)
            and "SHORT" in purpose
            and metadata.get("cycle_role") == "short_reduce"
        ):
            expected = float(state.get("last_expected_short_tp_net") or 0.0)
            actual, actual_source = self._extract_realized_fill_pnl(fill_event, fill_type)
            delta = actual - expected
            delta_pct = (delta / expected) if expected > 0 else 0.0
            filled_price = float(fill_event.exec_price or 0.0)
            filled_qty = float(fill_event.exec_qty or 0.0)
            expected_qty = float(state.get("last_short_tp_qty") or 0.0)
            logger.debug(
                "short_tp_pnl_validation %s",
                {
                    "expected_profit_usdt": expected,
                    "actual_profit_usdt": actual,
                    "actual_source": actual_source,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "trigger_price": state.get("last_short_tp_trigger_price"),
                    "filled_price": filled_price,
                    "qty": filled_qty,
                    "cycle_index": metadata.get("cycle_index"),
                },
            )
            _audit_calc(
                "short_tp_fill_validation",
                {
                    "cycle_index": metadata.get("cycle_index"),
                    "purpose": purpose,
                    "expected_profit_usdt": expected,
                    "actual_profit_usdt": actual,
                    "actual_source": actual_source,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "expected_trigger_price": state.get("last_short_tp_trigger_price"),
                    "filled_price": filled_price,
                    "expected_qty": expected_qty,
                    "filled_qty": filled_qty,
                    "confirmed_pnl": getattr(fill_event, "confirmed_pnl", None),
                    "closed_pnl": metadata.get("closed_pnl"),
                    "exec_price": fill_event.exec_price,
                    "fee": metadata.get("fee"),
                    "order_id": fill_event.client_order_id,
                    "metadata": metadata,
                },
            )
            if abs(delta) > PNL_VALIDATION_THRESHOLD_USDT:
                logger.warning(
                    "short_tp_pnl_mismatch",
                    {
                        "expected": expected,
                        "actual": actual,
                        "delta": delta,
                        "delta_pct": delta_pct,
                        "threshold": PNL_VALIDATION_THRESHOLD_USDT,
                        "cycle_index": metadata.get("cycle_index"),
                        "trigger_price": state.get("last_short_tp_trigger_price"),
                        "filled_price": filled_price,
                        "qty": filled_qty,
                    },
                )
                _audit_calc(
                    "short_tp_pnl_mismatch",
                    {
                        "expected": expected,
                        "actual": actual,
                        "delta": delta,
                        "delta_pct": delta_pct,
                        "threshold": PNL_VALIDATION_THRESHOLD_USDT,
                        "cycle_index": metadata.get("cycle_index"),
                        "trigger_price": state.get("last_short_tp_trigger_price"),
                        "filled_price": filled_price,
                        "qty": filled_qty,
                    },
                    level=logging.WARNING,
                )
        if fill_type == "cycle_long_reduce":
            cycle_order_already_confirmed = self._cycle_order_confirmed(
                runtime_state,
                fill_event.exchange_order_id,
                fill_event.purpose,
            )
            if cycle_order_already_confirmed:
                _log_event(
                    "fixed_cycle_cycle_closed_pnl_already_confirmed_for_order",
                    {
                        "cycle_index": cycle_index,
                        "purpose": fill_event.purpose,
                        "client_order_id": fill_event.client_order_id,
                        "exchange_order_id": fill_event.exchange_order_id,
                        "exec_id": fill_event.exec_id,
                    },
                )
                self._remove_pending_cycle_closed_pnl_for_order(
                    runtime_state,
                    exchange_order_id=fill_event.exchange_order_id,
                    purpose=fill_event.purpose,
                )
            elif self._has_confirmed_cycle_closed_pnl(fill_event, fill_type):
                try:
                    self._audit_exit_pnl_summary(fill_event, runtime_state, context)
                except Exception as exc:  # pragma: no cover - audit must never block
                    logger.warning(
                        "exit_pnl_reaudit_failed_non_blocking",
                        {
                            "error": str(exc),
                            "purpose": fill_event.purpose,
                            "client_order_id": fill_event.client_order_id,
                            "exchange_order_id": fill_event.exchange_order_id,
                            "exec_id": fill_event.exec_id,
                        },
                    )
            elif not cycle_order_already_confirmed:
                event_name = "fixed_cycle_long_reduce_pnl_waiting_for_closed_pnl"
                self._queue_pending_cycle_closed_pnl_fill(
                    fill_event,
                    runtime_state,
                    fill_type=fill_type,
                    cycle_index=cycle_index,
                )
                _log_event(
                    event_name,
                    {
                        "cycle_index": cycle_index,
                        "purpose": fill_event.purpose,
                        "client_order_id": fill_event.client_order_id,
                        "exchange_order_id": fill_event.exchange_order_id,
                        "exec_id": fill_event.exec_id,
                        "runtime_calculated_pnl": (fill_event.metadata or {}).get("runtime_calculated_pnl"),
                        "exec_pnl": (fill_event.metadata or {}).get("exec_pnl"),
                        "reason": "waiting_for_long_reduce_bybit_closed_pnl",
                    },
                )
                context.audit.log_event(
                    event_name,
                    strategy=self.name,
                    cycle_index=cycle_index,
                    purpose=fill_event.purpose,
                    client_order_id=fill_event.client_order_id,
                    exchange_order_id=fill_event.exchange_order_id,
                    exec_id=fill_event.exec_id,
                )
        self._advance_cycle_from_fill(fill_event, runtime_state, context)
        if fill_event.metadata.get("short_tp_fallback") and fill_event.status == "FILLED":
            fallback_state = self._get_short_tp_fallback_state(runtime_state)
            self._log_short_tp_fallback_event(
                "SHORT_TP_FALLBACK_FILLED",
                fallback_state,
                float(snapshot.current_price or fill_event.exec_price or 0.0),
                extra={
                    "client_order_id": fill_event.client_order_id,
                    "exchange_order_id": fill_event.exchange_order_id,
                },
            )
            reset_short_tp_fallback(fallback_state)
            self._store_short_tp_fallback_state(runtime_state, fallback_state)
            self._clear_short_tp_fallback_order_context(runtime_state)

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

        if state.get("refill_pending") and not state.get("refill_state"):
            refill_intents = self._build_entry_intents(refreshed_snapshot, runtime_state, context)
            if refill_intents:
                if not state.get("refill_state"):
                    state["refill_state"] = {"REQUESTED": True}
                return refill_intents
        fast_intents = self._fast_path_second_order(fill_event, refreshed_snapshot, runtime_state, context)
        old_exit_trigger_prices = self._collect_exit_trigger_prices_from_snapshot(refreshed_snapshot)
        self._log_realized_state(
            tag="fixed_cycle_fill_state",
            snapshot=refreshed_snapshot,
            runtime_state=runtime_state,
            stage="fill",
            reason=fill_event.purpose,
            old_exit_trigger_prices=old_exit_trigger_prices,
            new_exit_trigger_prices={},
            basket_tp_price=float(state.get("latest_tp_price") or 0.0),
        )

        return fast_intents + self._rebuild_structure(refreshed_snapshot, runtime_state, context, reason="fill_reconcile")

    def _extract_realized_fill_pnl(
        self,
        fill_event: FillEvent,
        fill_type: str,
    ) -> tuple[float, str]:
        metadata = fill_event.metadata or {}

        def _first_usable(*candidates: tuple[str, Any]) -> tuple[float, str]:
            for source, value in candidates:
                if value is None:
                    continue
                try:
                    return float(value), source
                except (TypeError, ValueError):
                    continue
            return 0.0, ""

        if fill_type == "cycle_long_reduce":
            return _first_usable(
                ("confirmed_pnl", getattr(fill_event, "confirmed_pnl", None)),
                ("closed_pnl", metadata.get("closed_pnl")),
                ("confirmed_closed_pnl", metadata.get("confirmed_closed_pnl")),
                ("long_closed_pnl", metadata.get("long_closed_pnl")),
                ("long_reduce_closed_pnl", metadata.get("long_reduce_closed_pnl")),
                ("provisional_runtime_calculated_pnl", metadata.get("runtime_calculated_pnl")),
                ("provisional_exec_pnl", metadata.get("exec_pnl")),
            )
        if fill_type == "cycle_short_tp":
            return _first_usable(
                ("confirmed_pnl", getattr(fill_event, "confirmed_pnl", None)),
                ("short_closed_pnl", metadata.get("short_closed_pnl")),
                ("short_reduce_closed_pnl", metadata.get("short_reduce_closed_pnl")),
                ("closed_pnl", metadata.get("closed_pnl")),
                ("confirmed_closed_pnl", metadata.get("confirmed_closed_pnl")),
                ("provisional_runtime_calculated_pnl", metadata.get("runtime_calculated_pnl")),
                ("provisional_exec_pnl", metadata.get("exec_pnl")),
            )
        if fill_type in {"final_long_exit", "final_short_exit"}:
            return _first_usable(
                ("confirmed_pnl", getattr(fill_event, "confirmed_pnl", None)),
                ("closed_pnl", metadata.get("closed_pnl")),
                ("confirmed_closed_pnl", metadata.get("confirmed_closed_pnl")),
            )
        return _first_usable(
            ("confirmed_pnl", getattr(fill_event, "confirmed_pnl", None)),
            ("closed_pnl", metadata.get("closed_pnl")),
            ("confirmed_closed_pnl", metadata.get("confirmed_closed_pnl")),
            ("exec_pnl", metadata.get("exec_pnl")),
            ("runtime_calculated_pnl", metadata.get("runtime_calculated_pnl")),
        )

    def _merge_cycle_closed_pnl_metadata(
        self,
        fill_event: FillEvent,
        *,
        cycle_index: int,
        cycle_role: str,
        confirmed_closed_pnl: float | None,
        closed_qty: float | None = None,
        closed_avg_price: float | None = None,
        confirmed_pnl_updated_time: int | None = None,
        pnl_source: str | None = None,
    ) -> bool:
        metadata = dict(fill_event.metadata or {})
        updated = False

        metadata["cycle_index"] = int(metadata.get("cycle_index") or cycle_index or 0)
        metadata.setdefault("cycle_role", cycle_role or "long_reduce")

        def _assign(key: str, value: Any) -> None:
            nonlocal updated
            if value is None:
                return
            if metadata.get(key) != value:
                metadata[key] = value
                updated = True

        _assign("closed_pnl", confirmed_closed_pnl)
        _assign("confirmed_closed_pnl", confirmed_closed_pnl)
        if cycle_role == "long_reduce":
            _assign("long_closed_pnl", confirmed_closed_pnl)
            _assign("long_reduce_closed_pnl", confirmed_closed_pnl)
        elif cycle_role == "short_reduce":
            _assign("short_closed_pnl", confirmed_closed_pnl)
            _assign("short_reduce_closed_pnl", confirmed_closed_pnl)

        _assign("confirmed_closed_qty", closed_qty)
        _assign("confirmed_closed_avg_price", closed_avg_price)
        _assign("confirmed_closed_pnl_updated_time", confirmed_pnl_updated_time)
        if pnl_source:
            _assign("closed_pnl_source", pnl_source)
            _assign("pnl_source", pnl_source)

        if confirmed_closed_pnl is not None:
            if getattr(fill_event, "confirmed_pnl", None) != confirmed_closed_pnl:
                fill_event.confirmed_pnl = confirmed_closed_pnl
                updated = True

        if updated:
            fill_event.metadata = metadata
        return updated

    def _hydrate_cycle_fill_metadata_from_state(
        self,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        *,
        fill_type: str,
        cycle_index: int,
        ledger: dict[str, Any],
    ) -> bool:
        if fill_type not in {"cycle_long_reduce", "cycle_short_tp"} or cycle_index <= 0:
            return False

        if self._has_confirmed_cycle_closed_pnl(fill_event, fill_type):
            return False

        cycle_order_confirmed = self._cycle_order_confirmed(
            runtime_state,
            fill_event.exchange_order_id,
            fill_event.purpose,
        )
        if not cycle_order_confirmed:
            return False

        cycle_key = str(cycle_index)
        entries_key = "cycle_long_reduce_pnl" if fill_type == "cycle_long_reduce" else "cycle_short_tp_pnl"
        cycle_state = self._ensure_cycle_state(runtime_state)
        fill_map_key = "long_fills" if fill_type == "cycle_long_reduce" else "short_fills"
        fill_map = (cycle_state.get(fill_map_key) or {}).get(cycle_key) or {}
        confirmed_value = self._safe_float(fill_map.get("confirmed_closed_pnl"), None)
        if confirmed_value is None:
            confirmed_value = self._safe_float(ledger.get(entries_key, {}).get(cycle_key), None)

        if confirmed_value is None:
            return False

        closed_qty = self._safe_float(fill_map.get("confirmed_closed_qty"), None) if fill_map else None
        closed_avg_price = self._safe_float(fill_map.get("confirmed_closed_avg_price"), None) if fill_map else None
        updated_time = self._safe_int(fill_map.get("confirmed_closed_pnl_updated_time"), None) if fill_map else None
        source = (
            fill_map.get("pnl_source")
            or fill_map.get("closed_pnl_source")
            or "confirmed_order"
        )

        metadata_updated = self._merge_cycle_closed_pnl_metadata(
            fill_event,
            cycle_index=cycle_index,
            cycle_role="long_reduce" if fill_type == "cycle_long_reduce" else "short_reduce",
            confirmed_closed_pnl=float(confirmed_value),
            closed_qty=closed_qty,
            closed_avg_price=closed_avg_price,
            confirmed_pnl_updated_time=updated_time,
            pnl_source=source,
        )

        _log_event(
            "fixed_cycle_cycle_pnl_already_confirmed_dedupe_skip",
            {
                "cycle_index": cycle_index,
                "fill_type": fill_type,
                "exchange_order_id": fill_event.exchange_order_id,
                "client_order_id": fill_event.client_order_id,
                "exec_id": fill_event.exec_id,
                "confirmed_pnl": float(confirmed_value),
                "source": source,
                "metadata_updated": metadata_updated,
                "dedupe_key": f"{fill_event.exchange_order_id or ''}:{fill_event.purpose or ''}",
            },
        )
        return True

    def _classify_exit_fill_for_audit(self, fill_event: FillEvent) -> tuple[str, int]:
        metadata = fill_event.metadata or {}
        cycle_role = (metadata.get("cycle_role") or "").lower()
        try:
            cycle_index = int(metadata.get("cycle_index") or 0)
        except (TypeError, ValueError):
            cycle_index = 0
        if cycle_role == "long_reduce":
            return "cycle_long_reduce", cycle_index
        if cycle_role == "short_reduce":
            return "cycle_short_tp", cycle_index
        purpose = (fill_event.purpose or "").upper()
        if (
            purpose in {self.LONG_TP_EXIT_PURPOSE, self.LONG_SL_EXIT_PURPOSE}
            or "LONG_TP" in purpose
            or "LONG_SL" in purpose
        ):
            return "final_long_exit", 0
        if (
            purpose
            in {
                self.SHORT_TP_EXIT_PURPOSE,
                self.SHORT_SL_EXIT_PURPOSE,
                self.SHORT_HARD_STOP_PURPOSE,
            }
            or "SHORT_TP" in purpose
            or "SHORT_SL" in purpose
            or "SHORT_HARD_STOP" in purpose
        ):
            return "final_short_exit", 0
        return "ignore", 0

    @staticmethod
    def _is_confirmed_cycle_pnl_source(source: str) -> bool:
        return source in {
            "confirmed_pnl",
            "closed_pnl",
            "confirmed_closed_pnl",
            "long_closed_pnl",
            "long_reduce_closed_pnl",
            "short_closed_pnl",
            "short_reduce_closed_pnl",
        }

    def _has_confirmed_cycle_closed_pnl(self, fill_event: FillEvent, fill_type: str) -> bool:
        metadata = fill_event.metadata or {}
        if getattr(fill_event, "confirmed_pnl", None) is not None:
            return True
        confirmed_keys = (
            "closed_pnl",
            "confirmed_closed_pnl",
            "short_closed_pnl",
            "short_reduce_closed_pnl",
            "long_closed_pnl",
            "long_reduce_closed_pnl",
        )
        return any(metadata.get(key) is not None for key in confirmed_keys)

    def _cycle_has_confirmed_pair_pnl(self, runtime_state: RuntimeState, cycle_index: int) -> bool:
        if cycle_index <= 0:
            return False
        ledger = (runtime_state.strategy_state.get("audit_pnl_ledger") or {})
        cycle_key = str(cycle_index)
        long_entries = ledger.get("cycle_long_reduce_pnl") or {}
        short_entries = ledger.get("cycle_short_tp_pnl") or {}
        return cycle_key in long_entries and cycle_key in short_entries

    def _queue_pending_cycle_closed_pnl_fill(
        self,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        *,
        fill_type: str,
        cycle_index: int,
    ) -> None:
        state = runtime_state.strategy_state
        queue = state.setdefault("pending_cycle_closed_pnl_fills", [])
        fill_dict = fill_event.to_dict()
        fill_symbol = self._active_trade_symbol(runtime_state.last_snapshot, runtime_state, fill_event=fill_event)
        fill_dict["symbol"] = fill_symbol
        fill_metadata = dict(fill_dict.get("metadata") or {})
        fill_metadata.setdefault("symbol", fill_symbol)
        fill_dict["metadata"] = fill_metadata
        cycle_queue_key: str | None = None
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"}:
            queue_key_parts = []
            if fill_event.exchange_order_id:
                queue_key_parts.append(str(fill_event.exchange_order_id))
            elif fill_event.client_order_id:
                queue_key_parts.append(str(fill_event.client_order_id))
            if fill_event.purpose:
                queue_key_parts.append(str(fill_event.purpose))
            if queue_key_parts:
                cycle_queue_key = ":".join(queue_key_parts)
        queue_key = (
            cycle_queue_key
            or str(fill_event.exec_id or "").strip()
            or str(fill_event.exchange_order_id or "").strip()
            or str(fill_event.client_order_id or "").strip()
            or f"{fill_event.purpose}:{fill_event.exec_price}:{fill_event.exec_qty}"
        )
        now = _current_time_ms()
        for entry in queue:
            if str(entry.get("queue_key") or "") == queue_key:
                existing_fill = dict(entry.get("fill") or {})
                existing_fill.update(fill_dict)
                entry["fill"] = existing_fill
                entry.setdefault("first_seen_ts", entry.get("first_seen_ts", now))
                entry["last_try_ts"] = now
                entry["cycle_index"] = cycle_index
                entry["fill_type"] = fill_type
                return
        queue.append(
            {
                "queue_key": queue_key,
                "fill_type": fill_type,
                "cycle_index": cycle_index,
                "fill": fill_dict,
                "first_seen_ts": now,
                "next_attempt_ts": now + CONFIRMED_CLOSED_PNL_RETRY_INITIAL_DELAY_MS,
                "attempt_count": 0,
            }
        )
        if len(queue) > 50:
            del queue[:-50]

    def _cycle_pending_queue_key(
        self,
        *,
        exchange_order_id: str | None,
        client_order_id: str | None,
        purpose: str | None,
    ) -> str | None:
        parts: list[str] = []
        if exchange_order_id:
            parts.append(str(exchange_order_id))
        elif client_order_id:
            parts.append(str(client_order_id))
        if purpose:
            parts.append(str(purpose))
        return ":".join(parts) if parts else None

    def _queue_key_from_payload(self, payload: dict[str, Any]) -> str | None:
        fill = payload.get("fill") or {}
        return self._cycle_pending_queue_key(
            exchange_order_id=str(fill.get("exchange_order_id") or ""),
            client_order_id=str(fill.get("client_order_id") or ""),
            purpose=str(fill.get("purpose") or ""),
        )

    def _cycle_order_confirmed(self, runtime_state: RuntimeState, exchange_order_id: str | None, purpose: str | None) -> bool:
        if not exchange_order_id or not purpose:
            return False
        dedupe_key = f"{exchange_order_id}:{purpose}"
        processed = runtime_state.strategy_state.get("processed_confirmed_order_keys") or []
        return dedupe_key in processed

    def _remove_pending_cycle_closed_pnl_for_order(
        self,
        runtime_state: RuntimeState,
        *,
        exchange_order_id: str | None,
        purpose: str | None,
    ) -> None:
        key = self._cycle_pending_queue_key(
            exchange_order_id=exchange_order_id,
            client_order_id=None,
            purpose=purpose,
        )
        if not key:
            return
        queue = runtime_state.strategy_state.get("pending_cycle_closed_pnl_fills") or []
        filtered = [
            entry
            for entry in queue
            if str(entry.get("queue_key") or "") != key
        ]
        if len(filtered) != len(queue):
            runtime_state.strategy_state["pending_cycle_closed_pnl_fills"] = filtered

    def _pending_cycle_fill_from_payload(self, payload: dict[str, Any]) -> FillEvent:
        fill = dict(payload.get("fill") or {})
        occurred_at_raw = fill.get("occurred_at")
        occurred_at = (
            datetime.fromisoformat(occurred_at_raw)
            if isinstance(occurred_at_raw, str) and occurred_at_raw
            else datetime.now(timezone.utc)
        )
        event = FillEvent(
            exchange_order_id=str(fill.get("exchange_order_id") or ""),
            client_order_id=fill.get("client_order_id"),
            side=str(fill.get("side") or ""),
            purpose=str(fill.get("purpose") or ""),
            exec_qty=float(fill.get("exec_qty") or 0.0),
            exec_price=float(fill.get("exec_price") or 0.0),
            order_type=str(fill.get("order_type") or ""),
            reduce_only=bool(fill.get("reduce_only")),
            status=str(fill.get("status") or ""),
            cumulative_qty=fill.get("cumulative_qty"),
            incremental_qty=fill.get("incremental_qty"),
            exec_id=fill.get("exec_id"),
            metadata=dict(fill.get("metadata") or {}),
            occurred_at=occurred_at,
        )
        payload_symbol = str(fill.get("symbol") or "").strip()
        if payload_symbol and not event.metadata.get("symbol"):
            event.metadata["symbol"] = payload_symbol
        confirmed_pnl = (fill.get("metadata") or {}).get("confirmed_closed_pnl")
        if confirmed_pnl is not None:
            event.confirmed_pnl = confirmed_pnl
        return event

    def _retry_pending_cycle_closed_pnl_fills(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> None:
        state = runtime_state.strategy_state
        pending = list(state.get("pending_cycle_closed_pnl_fills") or [])
        if not pending:
            return
        remaining: list[dict[str, Any]] = []
        resolved_keys: set[str] = set()
        now = _current_time_ms()
        for payload in pending:
            next_attempt = payload.get("next_attempt_ts", payload.get("first_seen_ts", now))
            if now < next_attempt:
                remaining.append(payload)
                continue
            first_seen = payload.get("first_seen_ts", now)
            attempt_count = payload.get("attempt_count", 0) + 1
            payload["attempt_count"] = attempt_count
            payload["last_try_ts"] = now
            payload["next_attempt_ts"] = now + CONFIRMED_CLOSED_PNL_RETRY_INTERVAL_MS
            age_ms = now - first_seen
            log_metadata = {
                "queue_key": payload.get("queue_key"),
                "exchange_order_id": payload.get("fill", {}).get("exchange_order_id"),
                "client_order_id": payload.get("fill", {}).get("client_order_id"),
                "purpose": payload.get("fill", {}).get("purpose"),
                "cycle_index": payload.get("cycle_index"),
                "attempt_count": attempt_count,
                "age_seconds": round(age_ms / 1000, 1),
            }
            if age_ms >= CONFIRMED_CLOSED_PNL_FINAL_WARNING_MS and not payload.get("final_warning_logged"):
                warning_meta = dict(log_metadata)
                warning_meta["delay_level"] = "final"
                _log_event("fixed_cycle_closed_pnl_wait_exceeded_expected_delay", warning_meta)
                payload["final_warning_logged"] = True
            elif age_ms >= CONFIRMED_CLOSED_PNL_WARNING_MS and not payload.get("warning_logged"):
                warning_meta = dict(log_metadata)
                warning_meta["delay_level"] = "warning"
                _log_event("fixed_cycle_closed_pnl_wait_exceeded_expected_delay", warning_meta)
                payload["warning_logged"] = True
            _log_event("fixed_cycle_closed_pnl_retry_attempt", log_metadata)
            fill_event = self._pending_cycle_fill_from_payload(payload)
            fill_symbol = self._active_trade_symbol(
                runtime_state.last_snapshot,
                runtime_state,
                fill_event=fill_event,
                payload=payload,
            )
            if fill_symbol and fill_symbol != str(self.config.symbol or "").upper():
                _log_event(
                    "fixed_cycle_closed_pnl_retry_uses_fill_symbol",
                    {
                        **log_metadata,
                        "fill_symbol": fill_symbol,
                        "config_symbol": str(self.config.symbol or "").upper(),
                    },
                )
            try:
                self._audit_exit_pnl_summary(fill_event, runtime_state, context)
            except Exception:
                remaining.append(payload)
                continue
            fill_type = str(payload.get("fill_type") or "")
            if fill_type not in {"cycle_short_tp", "cycle_long_reduce"}:
                continue
            if not self._has_confirmed_cycle_closed_pnl(fill_event, fill_type):
                updated_payload = dict(payload)
                updated_payload["fill"] = fill_event.to_dict()
                remaining.append(updated_payload)
                continue
            exchange_order_id = fill_event.exchange_order_id
            purpose = fill_event.purpose
            self._remove_pending_cycle_closed_pnl_for_order(
                runtime_state,
                exchange_order_id=exchange_order_id,
                purpose=purpose,
            )
            resolved_key = self._cycle_pending_queue_key(
                exchange_order_id=exchange_order_id,
                client_order_id=fill_event.client_order_id,
                purpose=purpose,
            )
            if resolved_key:
                resolved_keys.add(resolved_key)
                self._try_complete_cycle_pair_after_confirmed_pnl(
                    runtime_state,
                    int(payload.get("cycle_index") or 0),
                    purpose,
                )
        filtered_remaining = []
        for entry in remaining:
            queue_key = self._queue_key_from_payload(entry)
            if queue_key and queue_key in resolved_keys:
                continue
            filtered_remaining.append(entry)
        state["pending_cycle_closed_pnl_fills"] = filtered_remaining

    def _recompute_cycle_pnl_ledger_totals(self, ledger: dict[str, Any]) -> None:
        cycle_long_reduce_totals: dict[str, float] = {}
        cycle_short_tp_totals: dict[str, float] = {}
        for entry_key, entry in (ledger.get("cycle_pnl_entries") or {}).items():
            try:
                fill_type, cycle_key, _ = str(entry_key).split(":", 2)
            except ValueError:
                continue
            pnl = float((entry or {}).get("pnl") or 0.0)
            if fill_type == "cycle_long_reduce":
                cycle_long_reduce_totals[cycle_key] = cycle_long_reduce_totals.get(cycle_key, 0.0) + pnl
            elif fill_type == "cycle_short_tp":
                cycle_short_tp_totals[cycle_key] = cycle_short_tp_totals.get(cycle_key, 0.0) + pnl
        ledger["cycle_long_reduce_pnl"] = cycle_long_reduce_totals
        ledger["cycle_short_tp_pnl"] = cycle_short_tp_totals

    def _record_cycle_pnl_entry(
        self,
        ledger: dict[str, Any],
        *,
        fill_type: str,
        cycle_index: int,
        fill_event: FillEvent,
        pnl: float,
        pnl_source: str,
    ) -> None:
        cycle_entries = ledger.setdefault("cycle_pnl_entries", {})
        order_key = (
            str(fill_event.exchange_order_id or "").strip()
            or str(fill_event.client_order_id or "").strip()
            or str(fill_event.exec_id or "").strip()
            or f"{fill_event.purpose}:{fill_event.exec_price}:{fill_event.exec_qty}"
        )
        entry_key = f"{fill_type}:{cycle_index}:{order_key}"
        is_confirmed = self._is_confirmed_cycle_pnl_source(pnl_source)
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"} and not is_confirmed:
            _log_event(
                "fixed_cycle_unconfirmed_cycle_pnl_not_recorded",
                {
                    "fill_type": fill_type,
                    "cycle_index": cycle_index,
                    "purpose": fill_event.purpose,
                    "client_order_id": fill_event.client_order_id,
                    "exchange_order_id": fill_event.exchange_order_id,
                    "exec_id": fill_event.exec_id,
                    "pnl": pnl,
                    "pnl_source": pnl_source,
                },
            )
            return
        existing = cycle_entries.get(entry_key)
        if existing:
            existing_source = str(existing.get("source") or "")
            existing_confirmed = bool(existing.get("is_confirmed"))
            if existing_source == pnl_source and abs(float(existing.get("pnl") or 0.0) - pnl) <= 1e-12:
                return
            if existing_confirmed and not is_confirmed:
                return
            if is_confirmed and not existing_confirmed:
                cycle_entries[entry_key] = {
                    "pnl": pnl,
                    "source": pnl_source,
                    "is_confirmed": True,
                }
                self._recompute_cycle_pnl_ledger_totals(ledger)
                return
            if not existing_confirmed and not is_confirmed:
                cycle_entries[entry_key] = {
                    "pnl": float(existing.get("pnl") or 0.0) + pnl,
                    "source": pnl_source,
                    "is_confirmed": False,
                }
                self._recompute_cycle_pnl_ledger_totals(ledger)
                return
            return
        cycle_entries[entry_key] = {
            "pnl": pnl,
            "source": pnl_source,
            "is_confirmed": is_confirmed,
        }
        self._recompute_cycle_pnl_ledger_totals(ledger)

    def _ensure_confirmed_cycle_pnl_ledger_entry(
        self,
        runtime_state: RuntimeState,
        *,
        fill_type: str,
        cycle_index: int,
        fill_event: FillEvent,
        pnl: float,
        pnl_source: str,
    ) -> None:
        if fill_type not in {"cycle_long_reduce", "cycle_short_tp"}:
            return
        if cycle_index <= 0:
            return
        if not self._is_confirmed_cycle_pnl_source(pnl_source):
            return
        ledger = runtime_state.strategy_state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        self._record_cycle_pnl_entry(
            ledger,
            fill_type=fill_type,
            cycle_index=cycle_index,
            fill_event=fill_event,
            pnl=pnl,
            pnl_source=pnl_source,
        )

    def _audit_exit_pnl_summary(
        self,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> None:
        status = str(fill_event.status or "").upper()
        if status not in {"FILLED", "PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILLED"}:
            return
        state = runtime_state.strategy_state
        processed = state.setdefault("audit_processed_exit_fill_ids", [])
        metadata = fill_event.metadata or {}
        confirmed_pnl_value = getattr(fill_event, "confirmed_pnl", None)
        confirmed_marker_value = confirmed_pnl_value
        if confirmed_marker_value is None:
            confirmed_marker_value = metadata.get("confirmed_closed_pnl")

        fill_type, cycle_index = self._classify_exit_fill_for_audit(fill_event)
        if fill_type == "ignore":
            return

        if (
            fill_type in {"cycle_long_reduce", "cycle_short_tp"}
            and fill_event.exec_id
        ):
            confirmed_marker_str = (
                str(confirmed_marker_value)
                if confirmed_marker_value is not None
                else "pending"
            )
            fill_id = f"{fill_event.exec_id}|{fill_event.exchange_order_id or ''}|{fill_event.purpose or ''}|{confirmed_marker_str}"
        elif fill_event.exec_id:
            fill_id = str(fill_event.exec_id)
        elif fill_event.exchange_order_id:
            fill_id = f"{fill_event.exchange_order_id}|{fill_event.exec_price}|{fill_event.exec_qty}|{confirmed_marker_value}"
        elif fill_event.client_order_id:
            fill_id = str(fill_event.client_order_id)
        else:
            fill_id = f"{fill_event.purpose or ''}|{fill_event.exec_price or 0.0}|{fill_event.exec_qty or 0.0}|{confirmed_marker_value or 0.0}"
        if fill_id in processed:
            return
        refresh_success = True
        if fill_type == "cycle_short_tp":
            refresh_success = self._refresh_short_reduce_closed_pnl(
                fill_event=fill_event,
                runtime_state=runtime_state,
                context=context,
                cycle_index=cycle_index,
            )
        if fill_type == "cycle_long_reduce":
            cycle_state = self._ensure_cycle_state(runtime_state)
            long_fills = cycle_state.get("long_fills", {})
            long_fill = dict(long_fills.get(str(cycle_index)) or {})
            refresh_success = self._refresh_long_fill_closed_pnl(
                cycle_index=cycle_index,
                long_fill=long_fill,
                runtime_state=runtime_state,
                context=context,
                occurred_at_ms=int(fill_event.occurred_at.timestamp() * 1000)
                if getattr(fill_event, "occurred_at", None)
                else None,
                exec_id=fill_event.exec_id,
                fill_event=fill_event,
            )
        if refresh_success or fill_type not in {"cycle_short_tp", "cycle_long_reduce"}:
            processed.append(fill_id)
            if len(processed) > 500:
                del processed[:-500]
        ledger = state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        ledger.setdefault("cycle_pnl_entries", {})
        self._hydrate_cycle_fill_metadata_from_state(
            fill_event,
            runtime_state,
            fill_type=fill_type,
            cycle_index=cycle_index,
            ledger=ledger,
        )
        metadata = fill_event.metadata or {}
        pnl, pnl_source = self._extract_realized_fill_pnl(fill_event, fill_type)
        snapshot = runtime_state.last_snapshot
        metadata_keys = sorted(metadata.keys())
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"} and self._is_confirmed_cycle_pnl_source(pnl_source):
            _log_event(
                "fixed_cycle_cycle_pnl_confirmed_seen_after_refresh",
                {
                    "cycle_index": cycle_index,
                    "fill_type": fill_type,
                    "exchange_order_id": fill_event.exchange_order_id,
                    "client_order_id": fill_event.client_order_id,
                    "exec_id": fill_event.exec_id,
                    "pnl": pnl,
                    "pnl_source": pnl_source,
                    "metadata_keys": metadata_keys,
                    "confirmed_closed_pnl": metadata.get("confirmed_closed_pnl"),
                    "closed_pnl": metadata.get("closed_pnl"),
                },
            )
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"} and pnl_source in {
            "provisional_runtime_calculated_pnl",
            "provisional_exec_pnl",
        }:
            _log_event(
                "fixed_cycle_cycle_pnl_using_provisional_runtime_pnl",
                {
                    "cycle_index": cycle_index,
                    "fill_type": fill_type,
                    "exchange_order_id": fill_event.exchange_order_id,
                    "client_order_id": fill_event.client_order_id,
                    "exec_id": fill_event.exec_id,
                    "pnl": pnl,
                    "pnl_source": pnl_source,
                    "runtime_calculated_pnl": metadata.get("runtime_calculated_pnl"),
                    "exec_pnl": metadata.get("exec_pnl"),
                    "metadata_keys": metadata_keys,
                },
            )
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"}:
            self._ensure_confirmed_cycle_pnl_ledger_entry(
                runtime_state,
                fill_type=fill_type,
                cycle_index=cycle_index,
                fill_event=fill_event,
                pnl=pnl,
                pnl_source=pnl_source,
            )
        if fill_type in {"cycle_long_reduce", "cycle_short_tp"} and abs(pnl) <= 1e-12:
            _log_warning_event(
                "fixed_cycle_cycle_pnl_missing_confirmed_closed_pnl",
                {
                    "purpose": fill_event.purpose,
                    "fill_type": fill_type,
                    "cycle_index": cycle_index,
                    "client_order_id": fill_event.client_order_id,
                    "exchange_order_id": fill_event.exchange_order_id,
                    "exec_id": fill_event.exec_id,
                    "exec_qty": fill_event.exec_qty,
                    "exec_price": fill_event.exec_price,
                    "reduce_only": fill_event.reduce_only,
                    "metadata_keys": sorted(metadata.keys()),
                    "runtime_calculated_pnl": metadata.get("runtime_calculated_pnl"),
                    "exec_pnl": metadata.get("exec_pnl"),
                    "confirmed_closed_pnl": metadata.get("confirmed_closed_pnl"),
                    "closed_pnl": metadata.get("closed_pnl"),
                    "pnl_source": pnl_source,
                    "reason": "cycle fill has no usable pnl value",
                },
            )
        occurred_at_iso = (
            fill_event.occurred_at.isoformat() if getattr(fill_event, "occurred_at", None) else None
        )
        if fill_type == "final_long_exit":
            state["final_long_exit_order_context"] = {
                "symbol": str((snapshot.symbol if snapshot else None) or self.config.symbol or ""),
                "side": "long",
                "purpose": str(fill_event.purpose or ""),
                "exchange_order_id": str(fill_event.exchange_order_id or ""),
                "client_order_id": str(fill_event.client_order_id or ""),
                "exec_id": str(fill_event.exec_id or ""),
                "exec_price": float(fill_event.exec_price or 0.0),
                "exec_qty": float(fill_event.exec_qty or 0.0),
                "trigger_price": float(self._safe_float(metadata.get("trigger_price"), 0.0) or 0.0),
                "basket_tp_price": float(self._safe_float(metadata.get("basket_tp_price"), 0.0) or 0.0),
                "basket_break_even_price": float(
                    self._safe_float(metadata.get("basket_break_even_price"), 0.0) or 0.0
                ),
                "occurred_at": occurred_at_iso,
                "source": "final_long_exit",
            }
        elif fill_type == "final_short_exit":
            state["final_short_exit_order_context"] = {
                "symbol": str((snapshot.symbol if snapshot else None) or self.config.symbol or ""),
                "side": "short",
                "purpose": str(fill_event.purpose or ""),
                "exchange_order_id": str(fill_event.exchange_order_id or ""),
                "client_order_id": str(fill_event.client_order_id or ""),
                "exec_id": str(fill_event.exec_id or ""),
                "exec_price": float(fill_event.exec_price or 0.0),
                "exec_qty": float(fill_event.exec_qty or 0.0),
                "trigger_price": float(self._safe_float(metadata.get("trigger_price"), 0.0) or 0.0),
                "basket_tp_price": float(self._safe_float(metadata.get("basket_tp_price"), 0.0) or 0.0),
                "basket_break_even_price": float(
                    self._safe_float(metadata.get("basket_break_even_price"), 0.0) or 0.0
                ),
                "occurred_at": occurred_at_iso,
                "source": "final_short_exit",
            }

        cycle_long_total = sum(
            float(value or 0.0) for value in ledger["cycle_long_reduce_pnl"].values()
        )
        cycle_short_total = sum(
            float(value or 0.0) for value in ledger["cycle_short_tp_pnl"].values()
        )
        cycle_net = cycle_long_total + cycle_short_total

        raw_final_long_exit_pnl = ledger.get("final_long_exit_pnl")
        raw_final_short_exit_pnl = ledger.get("final_short_exit_pnl")
        final_long_exit_pnl = (
            float(raw_final_long_exit_pnl) if raw_final_long_exit_pnl is not None else None
        )
        final_short_exit_pnl = (
            float(raw_final_short_exit_pnl) if raw_final_short_exit_pnl is not None else None
        )
        final_exit_net = sum(
            value
            for value in (
                final_long_exit_pnl,
                final_short_exit_pnl,
            )
            if value is not None
        )

        total_net = cycle_net + final_exit_net
        ledger["total_realized_pnl"] = total_net

        all_cycle_keys = sorted(
            set(ledger["cycle_long_reduce_pnl"].keys()) | set(ledger["cycle_short_tp_pnl"].keys()),
            key=lambda key: int(key) if str(key).isdigit() else key,
        )
        cycle_breakdown: list[dict[str, Any]] = []
        for key in all_cycle_keys:
            try:
                cycle_idx = int(key)
            except ValueError:
                cycle_idx = key
            long_reduce_pnl = float(ledger["cycle_long_reduce_pnl"].get(key, 0.0))
            short_tp_pnl = float(ledger["cycle_short_tp_pnl"].get(key, 0.0))
            cycle_breakdown.append(
                {
                    "cycle_index": cycle_idx,
                    "long_add_loss_or_profit": long_reduce_pnl,
                    "short_tp_profit_or_loss": short_tp_pnl,
                    "cycle_net_pnl": long_reduce_pnl + short_tp_pnl,
                }
            )
        expected = None
        delta = None
        delta_pct = None
        if fill_type == "cycle_short_tp":
            expected = float(state.get("last_expected_short_tp_net") or 0.0)
            delta = pnl - expected
            delta_pct = (delta / expected) if expected > 0 else 0.0
        expected_vs_actual = {
            "expected_short_tp_net": expected if fill_type == "cycle_short_tp" else None,
            "actual_fill_pnl": pnl,
            "actual_fill_pnl_source": pnl_source,
            "delta": delta if fill_type == "cycle_short_tp" else None,
            "delta_pct": delta_pct if fill_type == "cycle_short_tp" else None,
            "fill_type": fill_type,
        }
        completed = state.setdefault("audit_completed_cycle_indices", [])
        for entry in cycle_breakdown:
            cycle_key = str(entry["cycle_index"])
            cycle_confirmed = (
                cycle_key in (ledger["cycle_long_reduce_pnl"] or {})
                and cycle_key in (ledger["cycle_short_tp_pnl"] or {})
            )
            if (
                cycle_confirmed
                and cycle_key not in completed
            ):
                completed.append(cycle_key)
                _audit_calc(
                    "cycle_completed",
                    {
                        "cycle_index": entry["cycle_index"],
                        "long_add_loss_or_profit": entry["long_add_loss_or_profit"],
                        "short_tp_profit_or_loss": entry["short_tp_profit_or_loss"],
                        "cycle_net_pnl": entry["cycle_net_pnl"],
                        "total_realized_pnl": total_net,
                        "latest_fill_purpose": fill_event.purpose,
                        "latest_fill_status": fill_event.status,
                    },
                )

        def _fmt(value: float | None) -> str:
            return "pending" if value is None else f"{value:+.4f}"

        latest_fill_info = {
            "purpose": fill_event.purpose,
            "fill_type": fill_type,
            "cycle_index": cycle_index,
            "pnl_this_fill": pnl,
            "pnl_source": pnl_source,
            "exec_price": float(fill_event.exec_price or 0.0),
            "qty": float(fill_event.exec_qty or 0.0),
            "client_order_id": fill_event.client_order_id,
            "exchange_order_id": fill_event.exchange_order_id,
            "metadata": fill_event.metadata or {},
            "exec_id": fill_event.exec_id,
            "confirmed_pnl": confirmed_pnl_value,
            "closed_pnl": (fill_event.metadata or {}).get("closed_pnl"),
            "fill_status": fill_event.status,
            "expected_short_tp_net": expected if fill_type == "cycle_short_tp" else None,
            "expected_actual_delta": delta if fill_type == "cycle_short_tp" else None,
            "expected_trigger_price": (
                float(state.get("last_short_tp_trigger_price") or 0.0)
                if fill_type == "cycle_short_tp"
                else None
            ),
            "filled_price": float(fill_event.exec_price or 0.0),
            "trigger_vs_fill_delta": (
                float(fill_event.exec_price or 0.0)
                - float(state.get("last_short_tp_trigger_price") or 0.0)
                if fill_type == "cycle_short_tp"
                and float(state.get("last_short_tp_trigger_price") or 0.0) > 0
                else None
            ),
        }

        summary_lines: list[str] = []
        for entry in cycle_breakdown:
            idx = entry.get("cycle_index")
            summary_lines.append(f"Cycle {idx}:")
            summary_lines.append(f"LONG_ADD_{idx} Verlust/Profit: {_fmt(entry['long_add_loss_or_profit'])}")
            summary_lines.append(f"SHORT_TP_{idx} Profit/Loss: {_fmt(entry['short_tp_profit_or_loss'])}")
            summary_lines.append(f"Cycle Net: {_fmt(entry['cycle_net_pnl'])}")
            cycle_key = str(idx)
            status = (
                "COMPLETED"
                if (
                    cycle_key in (ledger["cycle_long_reduce_pnl"] or {})
                    and cycle_key in (ledger["cycle_short_tp_pnl"] or {})
                )
                else "OPEN / WAITING"
            )
            summary_lines.append(f"Status: {status}")
            summary_lines.append("")
        summary_lines.append("Final exits:")
        summary_lines.append(f"LONG_EXIT Profit/Loss: {_fmt(final_long_exit_pnl)}")
        summary_lines.append(f"SHORT_EXIT Profit/Loss: {_fmt(final_short_exit_pnl)}")
        summary_lines.append(f"Final Exit Net: {_fmt(final_exit_net)}")
        summary_lines.append("")
        summary_lines.append("Total:")
        summary_lines.append(f"Cycle Net Total: {_fmt(cycle_net)}")
        summary_lines.append(f"Final Exit Net: {_fmt(final_exit_net)}")
        summary_lines.append(f"BOT TOTAL REALIZED PNL: {_fmt(total_net)}")
        if fill_type == "cycle_short_tp":
            summary_lines.append("")
            summary_lines.append("Expected vs Actual:")
            summary_lines.append(f"Expected SHORT_TP Net: {_fmt(expected or 0.0)}")
            summary_lines.append(f"Actual Fill PnL: {_fmt(pnl)}")
            summary_lines.append(f"Delta: {_fmt(delta or 0.0)}")
        summary_text = "\n".join(summary_lines)

        payload = {
            "latest_fill": latest_fill_info,
            "cycle_breakdown": cycle_breakdown,
            "totals": {
                "cycle_long_reduce_total": cycle_long_total,
                "cycle_short_tp_total": cycle_short_total,
                "cycle_net_pnl": cycle_net,
                "final_long_exit_pnl": final_long_exit_pnl,
                "final_short_exit_pnl": final_short_exit_pnl,
                "final_exit_net_pnl": final_exit_net,
                "total_realized_pnl": total_net,
            },
            "summary_text": summary_text,
            "expected_vs_actual": expected_vs_actual,
            "pnl_source": pnl_source,
        }
        _audit_calc("exit_pnl_summary", payload)

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
        if state.get("emergency_flat_required"):
            _log_event(
                "fixed_cycle_fresh_entry_blocked_emergency_flat",
                {
                    "symbol": self.config.symbol,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                    "emergency_attempts": state.get("emergency_exit_attempts"),
                },
            )
            return []
        if self._post_exit_cleanup_pending(runtime_state):
            if not self._attempt_post_exit_cleanup(snapshot, runtime_state, context):
                return []
        if (
            not state.get("refill_pending")
            and (snapshot.long_qty > 0 or snapshot.short_qty > 0)
        ):
            _log_warning_event(
                "fixed_cycle_initial_entry_blocked_open_position",
                {
                    "reason": "build_entry_intents_open_position_guard",
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                    "long_avg": snapshot.long_avg,
                    "short_avg": snapshot.short_avg,
                    "current_price": snapshot.current_price,
                    "bot_state": state.get("bot_state"),
                    "initial_entry_confirmed": bool(state.get("initial_entry_confirmed")),
                    "initial_entry_submitted": bool(state.get("initial_entry_submitted")),
                    "fresh_restart_required": bool(state.get("fresh_restart_required")),
                },
            )
            return []
        final_pnl_ready = self._final_pnl_ready_for_restart(runtime_state)
        if not final_pnl_ready and state.get("current_trade_pnl_state_reset_for_entry"):
            final_pnl_ready = (
                bool(state.get("final_trade_pnl_audited"))
                and bool(state.get("last_trade_pnl_complete"))
                and state.get("last_trade_pnl_usdt") is not None
            )
        if not state.get("refill_pending"):
            active_snapshot_order_purposes, active_runtime_order_purposes = (
                self._collect_active_strategy_order_purposes(snapshot, runtime_state)
            )
            if active_snapshot_order_purposes or active_runtime_order_purposes:
                _log_event(
                    "fixed_cycle_fresh_entry_blocked_active_strategy_orders",
                    {
                        "symbol": self.config.symbol,
                        "reason": "active_strategy_orders",
                        "active_snapshot_order_purposes": active_snapshot_order_purposes,
                        "active_runtime_order_purposes": active_runtime_order_purposes,
                        "long_qty": snapshot.long_qty,
                        "short_qty": snapshot.short_qty,
                        "last_trade_pnl_complete": bool(state.get("last_trade_pnl_complete")),
                        "final_trade_pnl_audited": bool(state.get("final_trade_pnl_audited")),
                        "current_trade_pnl_state_reset_for_entry": bool(
                            state.get("current_trade_pnl_state_reset_for_entry")
                        ),
                    },
                )
                return []
        if (
            not state.get("refill_pending")
            and snapshot.long_qty <= 0
            and snapshot.short_qty <= 0
        ):
            if self._previous_trade_evidence_present(runtime_state):
                ledger = state.get("audit_pnl_ledger") or {}
                final_long_exit_pnl = ledger.get("final_long_exit_pnl")
                final_short_exit_pnl = ledger.get("final_short_exit_pnl")
                if not final_pnl_ready:
                    _log_event(
                        "fixed_cycle_fresh_entry_blocked_pending_final_exit_settlement",
                        {
                            "symbol": self.config.symbol,
                            "reason": "final_pnl_pending",
                            "final_long_exit_pnl": final_long_exit_pnl,
                            "final_short_exit_pnl": final_short_exit_pnl,
                            "final_trade_pnl_audited": bool(state.get("final_trade_pnl_audited")),
                            "last_trade_pnl_complete": bool(state.get("last_trade_pnl_complete")),
                            "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                            "trade_block_id": state.get("trade_block_id"),
                            "last_trade_block_id": state.get("last_trade_block_id"),
                            "pnl_ready_for_current_trade": False,
                        },
                    )
                    return []
                snapshot_purposes, runtime_purposes = self._collect_active_final_exit_purposes(
                    snapshot, runtime_state
                )
                if snapshot_purposes or runtime_purposes:
                    _log_event(
                        "fixed_cycle_fresh_entry_blocked_pending_final_exit_settlement",
                        {
                            "symbol": self.config.symbol,
                            "reason": "final_exit_orders_pending",
                            "pending_snapshot_final_exit_purposes": snapshot_purposes,
                            "pending_runtime_final_exit_purposes": runtime_purposes,
                            "final_long_exit_pnl": final_long_exit_pnl,
                            "final_short_exit_pnl": final_short_exit_pnl,
                            "trade_block_id": state.get("trade_block_id"),
                            "last_trade_block_id": state.get("last_trade_block_id"),
                            "pnl_ready_for_current_trade": True,
                        },
                    )
                    return []
            if not state.get("current_trade_pnl_state_reset_for_entry"):
                self._reset_current_trade_pnl_state(
                    runtime_state,
                    reason="before_initial_entry",
                )
                state["current_trade_pnl_state_reset_for_entry"] = True

        refill_required = bool(
            state.get("refill_pending") or state.get("bot_state") == self.STATE_REFILL_PENDING
        )
        if refill_required:
            gate_details = self._reconcile_refill_gate_state(snapshot, runtime_state)
            context.audit.log_event(
                "fixed_cycle_refill_required_after_cycle_pair",
                strategy=self.name,
                cycle_completed_count=state.get("cycle_completed_count"),
                cycle_pair_count=state.get("cycle_pair_count"),
                refill_pending=bool(state.get("refill_pending")),
                bot_state=state.get("bot_state"),
                active_refill_orders_count=gate_details["active_refill_orders_count"],
                stale_detected=gate_details["stale_detected"],
            )
            if gate_details["active_refill_orders_count"] > 0:
                context.audit.log_event(
                    "fixed_cycle_refill_block_details",
                    strategy=self.name,
                    **gate_details,
                )
                return []
            current_price = float(snapshot.current_price or 0.0)
            initial_long_qty = float(state.get("initial_long_qty") or 0.0)
            initial_short_qty = float(state.get("initial_short_qty") or 0.0)

            current_long_qty = float(snapshot.long_qty or 0.0)
            current_short_qty = float(snapshot.short_qty or 0.0)

            missing_long_qty = max(initial_long_qty - current_long_qty, 0.0)
            missing_short_qty = max(initial_short_qty - current_short_qty, 0.0)
            refill_long_qty = (
                self._normalize_qty(missing_long_qty, runtime_state)
                if missing_long_qty > 0
                else 0.0
            )
            refill_short_qty = (
                self._normalize_qty(missing_short_qty, runtime_state)
                if missing_short_qty > 0
                else 0.0
            )

            refill_payload = {
                "current_price": current_price,
                "initial_long_qty": initial_long_qty,
                "initial_short_qty": initial_short_qty,
                "current_long_qty": current_long_qty,
                "current_short_qty": current_short_qty,
                "missing_long_qty": missing_long_qty,
                "missing_short_qty": missing_short_qty,
                "refill_long_qty": refill_long_qty,
                "refill_short_qty": refill_short_qty,
                "cycle_completed_count": state.get("cycle_completed_count"),
                "cycle_pair_count": state.get("cycle_pair_count"),
                "trigger_purpose": state.get("refill_trigger_purpose"),
            }
            _log_event("fixed_cycle_refill_qty_calculated", refill_payload)

            if current_price <= 0:
                _log_warning_event(
                    "fixed_cycle_refill_submit_failed",
                    {**refill_payload, "reason": "current_price_missing"},
                )
                self._clear_refill_gate_state(runtime_state, preserve_pending=True)
                return []

            if missing_long_qty <= 0 and missing_short_qty <= 0:
                state["refill_long_filled"] = True
                state["refill_short_filled"] = True
                _log_event("fixed_cycle_refill_no_qty_needed", refill_payload)
                self._complete_refill(runtime_state, context)
                return []

            intents: list[StrategyIntent] = []
            expected_purposes: list[str] = []
            state["refill_long_filled"] = missing_long_qty <= 0
            state["refill_short_filled"] = missing_short_qty <= 0

            if refill_long_qty > 0:
                expected_purposes.append("REFILL_LONG")
                intents.append(
                    StrategyIntent(
                        side="long",
                        qty=refill_long_qty,
                        price=None,
                        purpose="REFILL_LONG",
                        order_type="Market",
                        reduce_only=False,
                        position_idx=1,
                        metadata={"entry_role": "refill_long"},
                    )
                )

            if refill_short_qty > 0:
                expected_purposes.append("REFILL_SHORT")
                intents.append(
                    StrategyIntent(
                        side="short",
                        qty=refill_short_qty,
                        price=None,
                        purpose="REFILL_SHORT",
                        order_type="Market",
                        reduce_only=False,
                        position_idx=2,
                        metadata={"entry_role": "refill_short"},
                    )
                )

            if not intents:
                _log_warning_event(
                    "fixed_cycle_refill_submit_failed",
                    {**refill_payload, "reason": "normalized_qty_zero_or_below_min"},
                )
                self._clear_refill_gate_state(runtime_state, preserve_pending=True)
                if state.get("refill_long_filled") and state.get("refill_short_filled"):
                    self._complete_refill(runtime_state, context)
                return []

            state["refill_in_progress"] = True
            state["refill_state"] = {
                "REQUESTED": True,
                "expected_purposes": expected_purposes,
                "created_at_ms": _current_time_ms(),
            }
            _log_event(
                "fixed_cycle_refill_intents_created",
                {
                    **refill_payload,
                    "expected_purposes": expected_purposes,
                    "intent_count": len(intents),
                    "intents": [
                        {
                            "purpose": intent.purpose,
                            "qty": intent.qty,
                            "side": intent.side,
                            "position_idx": intent.position_idx,
                        }
                        for intent in intents
                    ],
                },
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

    def _get_realized_net_pnl_total(
        self, runtime_state: RuntimeState | None, snapshot: HedgeSnapshot | None = None
    ) -> float:
        total = 0.0
        if runtime_state:
            total = float(
                (runtime_state.realized_long_pnl_total or 0.0)
                + (runtime_state.realized_short_pnl_total or 0.0)
            )
        if abs(total) <= 1e-12 and snapshot:
            total = float(snapshot.realized_pnl_total or 0.0)
        return total

    @staticmethod
    def _final_pnl_ready_for_restart(runtime_state: RuntimeState) -> bool:
        state = runtime_state.strategy_state
        current_trade_block_id = state.get("trade_block_id")
        last_trade_block_id = state.get("last_trade_block_id")
        return (
            bool(state.get("final_trade_pnl_audited"))
            and bool(state.get("last_trade_pnl_complete"))
            and state.get("last_trade_pnl_usdt") is not None
            and bool(current_trade_block_id)
            and last_trade_block_id == current_trade_block_id
        )

    @staticmethod
    def _is_active_final_exit_order(order: Any) -> bool:
        purpose = str(getattr(order, "purpose", "") or "").upper()
        status = str(getattr(order, "status", "") or "").upper()
        return (
            purpose
            in {
                FixedCycleHedgeStrategy.LONG_TP_EXIT_PURPOSE,
                FixedCycleHedgeStrategy.LONG_TP_EXIT_RECOVERY_PURPOSE,
                FixedCycleHedgeStrategy.LONG_SL_EXIT_PURPOSE,
                FixedCycleHedgeStrategy.SHORT_TP_EXIT_PURPOSE,
                FixedCycleHedgeStrategy.SHORT_SL_EXIT_PURPOSE,
                FixedCycleHedgeStrategy.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                FixedCycleHedgeStrategy.SHORT_HARD_STOP_PURPOSE,
            }
            and status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
        )

    def _collect_active_final_exit_purposes(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> tuple[list[str], list[str]]:
        snapshot_purposes: list[str] = []
        runtime_purposes: list[str] = []
        for order in snapshot.active_orders:
            if self._is_active_final_exit_order(order):
                snapshot_purposes.append(
                    str(getattr(order, "purpose", "") or getattr(order, "client_order_id", "") or "")
                )
        for order in runtime_state.active_orders.values():
            if self._is_active_final_exit_order(order):
                runtime_purposes.append(
                    str(getattr(order, "purpose", "") or getattr(order, "client_order_id", "") or "")
                )
        return snapshot_purposes, runtime_purposes

    def _has_active_final_exit_orders(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> bool:
        snapshot_purposes, runtime_purposes = self._collect_active_final_exit_purposes(
            snapshot, runtime_state
        )
        return bool(snapshot_purposes or runtime_purposes)

    def _previous_trade_evidence_present(self, runtime_state: RuntimeState) -> bool:
        state = runtime_state.strategy_state
        audit_pnl_ledger = state.get("audit_pnl_ledger") or {}
        cycle_long_reduce_total = sum(
            float(value or 0.0)
            for value in (audit_pnl_ledger.get("cycle_long_reduce_pnl") or {}).values()
        )
        cycle_short_tp_total = sum(
            float(value or 0.0)
            for value in (audit_pnl_ledger.get("cycle_short_tp_pnl") or {}).values()
        )
        return any(
            [
                bool(state.get("initial_entry_confirmed")),
                bool(state.get("final_long_exit_order_context")),
                bool(state.get("final_short_exit_order_context")),
                float(audit_pnl_ledger.get("final_long_exit_pnl") or 0.0) != 0.0,
                float(audit_pnl_ledger.get("final_short_exit_pnl") or 0.0) != 0.0,
                cycle_long_reduce_total != 0.0,
                cycle_short_tp_total != 0.0,
                bool(state.get("final_long_exit_audited")),
                bool(state.get("final_short_exit_audited")),
                bool(state.get("exit_locked")),
                bool(state.get("fresh_restart_required")),
                bool(state.get("last_trade_block_id")),
                bool(state.get("trade_block_id")),
            ]
        )

    @staticmethod
    def _is_terminal_order_status(status: Any) -> bool:
        return str(status or "").upper() in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "DEACTIVATED"}

    def _is_strategy_order_purpose(self, purpose: Any) -> bool:
        text = str(purpose or "").upper()
        return (
            text
            in {
                self.LONG_ENTRY_PURPOSE,
                self.SHORT_ENTRY_PURPOSE,
                self.LONG_TP_EXIT_PURPOSE,
                self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                self.LONG_SL_EXIT_PURPOSE,
                self.SHORT_TP_EXIT_PURPOSE,
                self.SHORT_SL_EXIT_PURPOSE,
                self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                self.SHORT_HARD_STOP_PURPOSE,
                "REFILL_LONG",
                "REFILL_SHORT",
                "SHORT_TP_FALLBACK",
            }
            or text.startswith("CYCLE_")
        )

    def _collect_active_refill_order_purposes(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState,
    ) -> list[str]:
        purposes = {"REFILL_LONG", "REFILL_SHORT"}
        active: list[str] = []
        seen: set[str] = set()

        def _append(purpose: Any, status: Any) -> None:
            purpose_text = str(purpose or "").upper()
            if purpose_text not in purposes:
                return
            if self._is_terminal_order_status(status):
                return
            if purpose_text in seen:
                return
            seen.add(purpose_text)
            active.append(purpose_text)

        for order in runtime_state.active_orders.values():
            _append(getattr(order, "purpose", None), getattr(order, "status", None))
        if snapshot is not None:
            for order in snapshot.active_orders:
                _append(getattr(order, "purpose", None), getattr(order, "status", None))
        return active

    @staticmethod
    def _collect_refill_requested_flags(refill_state: dict[str, Any]) -> list[str]:
        flags: list[str] = []
        if refill_state.get("REQUESTED"):
            flags.append("REQUESTED")
        for purpose in ("REFILL_LONG", "REFILL_SHORT"):
            if refill_state.get(purpose):
                flags.append(purpose)
        return flags

    def _clear_refill_gate_state(
        self,
        runtime_state: RuntimeState,
        *,
        preserve_pending: bool,
    ) -> None:
        state = runtime_state.strategy_state
        state["refill_in_progress"] = False
        state["refill_state"] = {}
        if not preserve_pending:
            state["refill_pending"] = False

    def _reconcile_refill_gate_state(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState,
        *,
        preserve_pending: bool = True,
    ) -> dict[str, Any]:
        state = runtime_state.strategy_state
        refill_state = state.get("refill_state") or {}
        active_refill_purposes = self._collect_active_refill_order_purposes(snapshot, runtime_state)
        requested_flags = self._collect_refill_requested_flags(refill_state)
        active_refill_orders_count = len(active_refill_purposes)
        stale_detected = bool(
            active_refill_orders_count == 0
            and (
                state.get("refill_in_progress")
                or requested_flags
            )
        )
        details = {
            "cycle_completed_count": state.get("cycle_completed_count"),
            "cycle_pair_count": state.get("cycle_pair_count"),
            "refill_in_progress": bool(state.get("refill_in_progress")),
            "requested_flags": requested_flags,
            "active_refill_purposes": active_refill_purposes,
            "active_refill_orders_count": active_refill_orders_count,
            "stale_detected": stale_detected,
            "bot_state": state.get("bot_state"),
            "refill_pending": bool(state.get("refill_pending")),
        }
        if stale_detected:
            _log_warning_event("fixed_cycle_refill_stale_state_reset", details)
            self._clear_refill_gate_state(runtime_state, preserve_pending=preserve_pending)
            details["refill_in_progress"] = False
            details["requested_flags"] = []
            details["active_refill_purposes"] = []
            details["active_refill_orders_count"] = 0
        return details

    def _maybe_reset_stale_refill_state(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState,
    ) -> bool:
        details = self._reconcile_refill_gate_state(snapshot, runtime_state)
        return bool(details.get("stale_detected"))

    def _is_final_exit_only_order(self, order: Any) -> bool:
        purpose = str(getattr(order, "purpose", "") or "").upper()
        reduce_only = bool(getattr(order, "reduce_only", False))
        final_exit_purposes = {
            self.LONG_TP_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.LONG_SL_EXIT_PURPOSE,
            self.SHORT_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
            self.SHORT_HARD_STOP_PURPOSE,
            "FINAL_LONG_EXIT",
            "FINAL_SHORT_EXIT",
        }
        if purpose in final_exit_purposes:
            return True
        return bool(
            reduce_only
            and "EXIT" in purpose
            and not purpose.startswith("CYCLE_")
            and not purpose.startswith("INITIAL_")
            and not purpose.startswith("REFILL_")
        )

    def _only_final_exit_orders_remain(self, active_orders: list[Any]) -> bool:
        return bool(active_orders) and all(self._is_final_exit_only_order(order) for order in active_orders)

    def _is_unsettled_strategy_order(self, order: Any) -> bool:
        purpose = getattr(order, "purpose", None)
        if not self._is_strategy_order_purpose(purpose):
            return False
        remaining_qty = float(getattr(order, "remaining_qty", 0.0) or 0.0)
        if remaining_qty > 1e-9:
            return True
        return not self._is_terminal_order_status(getattr(order, "status", None))

    @staticmethod
    def _strategy_order_summary(order: Any) -> dict[str, Any]:
        return {
            "client_order_id": getattr(order, "client_order_id", None),
            "exchange_order_id": getattr(order, "exchange_order_id", None),
            "purpose": getattr(order, "purpose", None),
            "status": getattr(order, "status", None),
            "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
            "remaining_qty": float(getattr(order, "remaining_qty", 0.0) or 0.0),
            "qty": float(getattr(order, "qty", 0.0) or 0.0),
        }

    def _collect_unsettled_strategy_orders(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        unsettled_runtime_orders = [
            self._strategy_order_summary(order)
            for order in runtime_state.active_orders.values()
            if self._is_unsettled_strategy_order(order)
        ]
        unsettled_snapshot_orders = [
            self._strategy_order_summary(order)
            for order in snapshot.active_orders
            if self._is_unsettled_strategy_order(order)
        ]
        return unsettled_runtime_orders, unsettled_snapshot_orders

    def _force_cleanup_flat_final_exit_orders(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        active_snapshot_orders = [
            order for order in snapshot.active_orders if self._is_unsettled_strategy_order(order)
        ]
        active_runtime_orders = [
            order for order in runtime_state.active_orders.values() if self._is_unsettled_strategy_order(order)
        ]
        remaining_orders = [*active_snapshot_orders, *active_runtime_orders]
        if not self._only_final_exit_orders_remain(remaining_orders):
            return False

        symbol = context.symbol or self.config.symbol
        category = context.category or self.config.category
        canceled_exchange_order_ids: list[str] = []
        cancel_errors: list[dict[str, Any]] = []
        seen_exchange_order_ids: set[str] = set()
        for order in remaining_orders:
            exchange_order_id = str(getattr(order, "exchange_order_id", "") or "")
            status = str(getattr(order, "status", "") or "").upper()
            if (
                exchange_order_id
                and exchange_order_id not in seen_exchange_order_ids
                and not self._is_terminal_order_status(status)
                and context.order_manager
            ):
                seen_exchange_order_ids.add(exchange_order_id)
                try:
                    context.order_manager.cancel_order(
                        exchange_order_id,
                        symbol=symbol,
                        category=category,
                    )
                    canceled_exchange_order_ids.append(exchange_order_id)
                except Exception as exc:
                    cancel_errors.append(
                        {
                            "exchange_order_id": exchange_order_id,
                            "error": str(exc),
                        }
                    )

        removed_orders: list[dict[str, Any]] = []
        for client_order_id, managed_order in list(runtime_state.active_orders.items()):
            if not self._is_unsettled_strategy_order(managed_order):
                continue
            if not self._is_final_exit_only_order(managed_order):
                continue
            removed_orders.append(self._strategy_order_summary(managed_order))
            runtime_state.active_orders.pop(client_order_id, None)
            if managed_order.exchange_order_id:
                runtime_state.exchange_to_client_id.pop(managed_order.exchange_order_id, None)

        state["exit_locked"] = False
        state["exit_rebuild_allowed"] = False
        state["force_exit_rebuild"] = False
        state["long_exit_filled"] = False
        state["short_exit_filled"] = False
        state["short_exit_recovery_submitted"] = False
        state["long_exit_recovery_submitted"] = False
        state["exit_recovery_marker"] = False
        state.pop("flat_waiting_order_cleanup_logged", None)
        _log_event(
            "fixed_cycle_flat_final_exit_order_cleanup_forced",
            {
                "reason": reason,
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "canceled_exchange_order_ids": canceled_exchange_order_ids,
                "cancel_errors": cancel_errors,
                "removed_orders": removed_orders,
                "remaining_snapshot_orders": [
                    self._strategy_order_summary(order) for order in active_snapshot_orders
                ],
            },
        )
        return True

    def _collect_active_strategy_order_purposes(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> tuple[list[str], list[str]]:
        active_snapshot_order_purposes: list[str] = []
        active_runtime_order_purposes: list[str] = []
        for order in snapshot.active_orders:
            if not self._is_unsettled_strategy_order(order):
                continue
            purpose = getattr(order, "purpose", None)
            active_snapshot_order_purposes.append(
                str(purpose or getattr(order, "client_order_id", None) or "")
            )
        for order in runtime_state.active_orders.values():
            if not self._is_unsettled_strategy_order(order):
                continue
            purpose = getattr(order, "purpose", None)
            active_runtime_order_purposes.append(
                str(purpose or getattr(order, "client_order_id", None) or "")
            )
        return active_snapshot_order_purposes, active_runtime_order_purposes

    def _has_active_strategy_orders(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> bool:
        active_snapshot_order_purposes, active_runtime_order_purposes = (
            self._collect_active_strategy_order_purposes(snapshot, runtime_state)
        )
        return bool(active_snapshot_order_purposes or active_runtime_order_purposes)

    def _release_exit_lock_if_unprotected_open_position(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        if snapshot.long_qty <= 0 or snapshot.short_qty <= 0:
            return False
        snapshot_purposes, runtime_purposes = self._collect_active_final_exit_purposes(
            snapshot, runtime_state
        )
        if snapshot_purposes or runtime_purposes:
            state.pop("unprotected_open_position_first_seen_ms", None)
            return False
        previous_exit_locked = bool(state.get("exit_locked"))
        previous_exit_rebuild_allowed = bool(state.get("exit_rebuild_allowed", True))
        previous_force = bool(state.get("force_exit_rebuild"))
        state["exit_locked"] = False
        state["exit_rebuild_allowed"] = True
        state["force_exit_rebuild"] = True
        state["long_exit_filled"] = False
        state["short_exit_filled"] = False
        state["short_exit_recovery_submitted"] = False
        state["long_exit_recovery_submitted"] = False
        state["exit_recovery_marker"] = False
        now_ms = int(time.time() * 1000)
        state.setdefault("unprotected_open_position_first_seen_ms", now_ms)
        should_log = (
            previous_exit_locked
            or bool(state.get("last_exit_signature"))
            or bool(state.get("exit_armed_marker_emitted"))
        )
        if should_log:
            _log_warning_event(
                "fixed_cycle_exit_lock_released_missing_exit_orders",
                {
                    "reason": reason,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                    "long_avg": snapshot.long_avg,
                    "short_avg": snapshot.short_avg,
                    "current_price": snapshot.current_price,
                    "active_snapshot_final_exit_purposes": snapshot_purposes,
                    "active_runtime_final_exit_purposes": runtime_purposes,
                    "previous_exit_locked": previous_exit_locked,
                    "previous_exit_rebuild_allowed": previous_exit_rebuild_allowed,
                    "previous_force_exit_rebuild": previous_force,
                    "long_exit_filled_before": bool(state.get("long_exit_filled")),
                    "short_exit_filled_before": bool(state.get("short_exit_filled")),
                    "realized_long_pnl_total": snapshot.realized_long_pnl_total,
                    "realized_short_pnl_total": snapshot.realized_short_pnl_total,
                    "realized_cycle_net": snapshot.realized_pnl_total,
                    "current_effective_cycle": int(state.get("current_effective_cycle") or 0),
                    "last_exit_signature_is_none": state.get("last_exit_signature") is None,
                },
            )
        return True

    def _block_flat_restart_until_final_pnl(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> bool:
        if snapshot.long_qty > 0 or snapshot.short_qty > 0:
            return False
        state = runtime_state.strategy_state
        final_long_context_present = bool(state.get("final_long_exit_order_context"))
        final_short_context_present = bool(state.get("final_short_exit_order_context"))
        audit_pnl_ledger = state.get("audit_pnl_ledger") or {}
        active_snapshot_order_purposes, active_runtime_order_purposes = (
            self._collect_active_strategy_order_purposes(snapshot, runtime_state)
        )
        trade_evidence_present = (
            self._previous_trade_evidence_present(runtime_state)
            or any(
                [
                    float(runtime_state.realized_long_pnl_total or 0.0) != 0.0,
                    float(runtime_state.realized_short_pnl_total or 0.0) != 0.0,
                ]
            )
        )
        if active_snapshot_order_purposes or active_runtime_order_purposes:
            if (
                bool(state.get("fresh_restart_required"))
                and self._final_pnl_ready_for_restart(runtime_state)
                and self._force_cleanup_flat_final_exit_orders(
                    snapshot,
                    runtime_state,
                    context,
                    reason=reason,
                )
            ):
                active_snapshot_order_purposes = []
                active_runtime_order_purposes = []
            else:
                if trade_evidence_present:
                    self._emit_final_trade_pnl_if_complete_or_fetch(runtime_state, context, reason)
                state.pop("flat_waiting_final_pnl_logged", None)
                state.pop("flat_final_pnl_ready_logged", None)
                if not state.get("flat_waiting_order_cleanup_logged"):
                    pnl_ready_for_current_trade = self._final_pnl_ready_for_restart(runtime_state)
                    _log_event(
                        "fixed_cycle_flat_waiting_for_order_cleanup",
                        {
                            "reason": reason,
                            "long_qty": snapshot.long_qty,
                            "short_qty": snapshot.short_qty,
                            "active_snapshot_order_purposes": active_snapshot_order_purposes,
                            "active_runtime_order_purposes": active_runtime_order_purposes,
                            "final_trade_pnl_audited": state.get("final_trade_pnl_audited"),
                            "final_long_exit_audited": state.get("final_long_exit_audited"),
                            "final_short_exit_audited": state.get("final_short_exit_audited"),
                            "last_trade_pnl_complete": state.get("last_trade_pnl_complete"),
                            "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                            "final_long_exit_order_context_present": final_long_context_present,
                            "final_short_exit_order_context_present": final_short_context_present,
                            "trade_block_id": state.get("trade_block_id"),
                            "last_trade_block_id": state.get("last_trade_block_id"),
                            "pnl_ready_for_current_trade": pnl_ready_for_current_trade,
                        },
                    )
                    state["flat_waiting_order_cleanup_logged"] = True
                state["bot_state"] = self.STATE_EXITED
                state["fresh_restart_required"] = True
                return True
        state.pop("flat_waiting_order_cleanup_logged", None)
        if not trade_evidence_present:
            state.pop("flat_waiting_final_pnl_logged", None)
            state.pop("flat_final_pnl_ready_logged", None)
            state.pop("final_pnl_context_missing_logged", None)
            return False

        self._emit_final_trade_pnl_if_complete_or_fetch(runtime_state, context, reason)
        if not self._final_pnl_ready_for_restart(runtime_state):
            if (
                (
                    final_long_context_present
                    or final_short_context_present
                    or bool(state.get("final_long_exit_audited"))
                    or bool(state.get("final_short_exit_audited"))
                    or float(audit_pnl_ledger.get("final_long_exit_pnl") or 0.0) != 0.0
                    or float(audit_pnl_ledger.get("final_short_exit_pnl") or 0.0) != 0.0
                    or bool(state.get("last_trade_block_id"))
                )
                and (not final_long_context_present or not final_short_context_present)
                and not state.get("final_pnl_context_missing_logged")
            ):
                _log_warning_event(
                    "fixed_cycle_final_pnl_context_missing",
                    {
                        "reason": reason,
                        "final_long_exit_order_context_present": final_long_context_present,
                        "final_short_exit_order_context_present": final_short_context_present,
                        "active_snapshot_order_purposes": active_snapshot_order_purposes,
                        "active_runtime_order_purposes": active_runtime_order_purposes,
                        "last_fill_info": state.get("last_fill_info"),
                        "audit_pnl_ledger": audit_pnl_ledger,
                        "trade_block_id": state.get("trade_block_id"),
                        "last_trade_block_id": state.get("last_trade_block_id"),
                        "pnl_ready_for_current_trade": self._final_pnl_ready_for_restart(runtime_state),
                    },
                )
                state["final_pnl_context_missing_logged"] = True
            state.pop("flat_final_pnl_ready_logged", None)
            if not state.get("flat_waiting_final_pnl_logged"):
                pnl_ready_for_current_trade = self._final_pnl_ready_for_restart(runtime_state)
                _log_event(
                    "fixed_cycle_flat_waiting_for_final_pnl",
                    {
                        "reason": reason,
                        "final_trade_pnl_audited": state.get("final_trade_pnl_audited"),
                        "final_long_exit_audited": state.get("final_long_exit_audited"),
                        "final_short_exit_audited": state.get("final_short_exit_audited"),
                        "last_trade_pnl_complete": state.get("last_trade_pnl_complete"),
                        "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                        "long_qty": snapshot.long_qty,
                        "short_qty": snapshot.short_qty,
                        "active_snapshot_order_purposes": active_snapshot_order_purposes,
                        "active_runtime_order_purposes": active_runtime_order_purposes,
                        "final_long_exit_order_context_present": final_long_context_present,
                        "final_short_exit_order_context_present": final_short_context_present,
                        "trade_block_id": state.get("trade_block_id"),
                        "last_trade_block_id": state.get("last_trade_block_id"),
                        "pnl_ready_for_current_trade": pnl_ready_for_current_trade,
                    },
                )
                state["flat_waiting_final_pnl_logged"] = True
            state["bot_state"] = self.STATE_EXITED
            state["fresh_restart_required"] = True
            return True

        state.pop("flat_waiting_final_pnl_logged", None)
        state.pop("final_pnl_context_missing_logged", None)
        if not state.get("flat_final_pnl_ready_logged"):
            pnl_ready_for_current_trade = self._final_pnl_ready_for_restart(runtime_state)
            _log_event(
                "fixed_cycle_flat_final_pnl_ready_for_restart",
                {
                    "reason": reason,
                    "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                    "last_trade_pnl_finalized_at": state.get("last_trade_pnl_finalized_at"),
                    "trade_block_id": state.get("trade_block_id"),
                    "last_trade_block_id": state.get("last_trade_block_id"),
                    "pnl_ready_for_current_trade": pnl_ready_for_current_trade,
                },
            )
            state["flat_final_pnl_ready_logged"] = True
        return False

    def _required_remaining_profit(
        self, runtime_state: RuntimeState | None, snapshot: HedgeSnapshot | None = None
    ) -> float:
        required_target = float(self.config.target_profit_usdt or 0.0)
        net_pnl = self._get_realized_net_pnl_total(runtime_state, snapshot)
        return max(required_target - net_pnl, 0.0)

    def _required_remaining_profit(
        self, runtime_state: RuntimeState | None, snapshot: HedgeSnapshot | None = None
    ) -> float:
        required_target = float(self.config.target_profit_usdt or 0.0)
        net_pnl = self._get_realized_net_pnl_total(runtime_state, snapshot)
        return max(required_target - net_pnl, 0.0)

    def _collect_exit_trigger_prices_from_snapshot(
        self, snapshot: HedgeSnapshot | None
    ) -> dict[str, float]:
        prices: dict[str, float] = {}
        if not snapshot:
            return prices
        exit_purposes = set(self._exit_purposes())
        for order in snapshot.active_orders:
            if not order or order.purpose not in exit_purposes:
                continue
            trigger = getattr(order, "trigger_price", None) or getattr(order, "price", None)
            if trigger is None:
                continue
            prices[order.purpose] = float(trigger)
        return prices

    def _collect_exit_trigger_prices_from_intents(
        self, intents: list[StrategyIntent]
    ) -> dict[str, float]:
        prices: dict[str, float] = {}
        exit_purposes = set(self._exit_purposes())
        for intent in intents:
            if not intent or intent.purpose not in exit_purposes:
                continue
            trigger = intent.trigger_price or intent.price
            if trigger is None:
                continue
            prices[intent.purpose] = float(trigger)
        return prices

    def _log_realized_state(
        self,
        *,
        tag: str,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState,
        stage: str,
        reason: str | None,
        old_exit_trigger_prices: dict[str, float],
        new_exit_trigger_prices: dict[str, float],
        basket_tp_price: float | None = None,
    ) -> None:
        state = runtime_state.strategy_state
        fill_info = state.get("last_fill_info") or {}
        long_pnl = float(runtime_state.realized_long_pnl_total or 0.0)
        short_pnl = float(runtime_state.realized_short_pnl_total or 0.0)
        net_pnl = self._get_realized_net_pnl_total(runtime_state, snapshot)
        pending_loss = float(state.get("pending_cycle_loss_usdt") or 0.0)
        target_profit = float(self.config.target_profit_usdt or 0.0)
        required_remaining_profit = pending_loss + target_profit
        long_qty = float(snapshot.long_qty if snapshot else state.get("open_long_qty") or 0.0)
        short_qty = float(snapshot.short_qty if snapshot else state.get("open_short_qty") or 0.0)
        payload = {
            "stage": stage,
            "reason": reason,
            "fill_purpose": fill_info.get("fill_purpose"),
            "confirmed_closed_pnl": fill_info.get("confirmed_closed_pnl"),
            "realized_long_pnl_total": long_pnl,
            "realized_short_pnl_total": short_pnl,
            "realized_net_pnl_total": net_pnl,
            "target_profit_usdt": float(self.config.target_profit_usdt or 0.0),
            "required_remaining_profit": required_remaining_profit,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "net_qty": long_qty - short_qty,
            "old_exit_trigger_prices": old_exit_trigger_prices or {},
            "new_exit_trigger_prices": new_exit_trigger_prices or {},
            "basket_tp_price": basket_tp_price,
        }
        logger.debug("%s %s", tag, payload)

    def _emit_throttled_strategy_audit(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext,
        event_name: str,
        payload: dict[str, Any],
        *,
        signature_key: str,
        interval_ms: int = 120000,
    ) -> None:
        state = runtime_state.strategy_state
        now_ms = int(time.time() * 1000)
        try:
            signature = json.dumps(payload, sort_keys=True, default=str)
        except Exception:
            signature = repr(payload)
        last_sig_key = f"last_{signature_key}_signature"
        last_ms_key = f"last_{signature_key}_logged_ms"
        last_sig = state.get(last_sig_key)
        last_ms = int(state.get(last_ms_key) or 0)
        if signature != last_sig or now_ms - last_ms >= interval_ms:
            context.audit.log_event(event_name, strategy=self.name, **payload)
            state[last_sig_key] = signature
            state[last_ms_key] = now_ms

    def _pending_second_pair_short_reduce_exit_defer_payload(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> dict[str, Any] | None:
        state = runtime_state.strategy_state
        if state.get("refill_pending") or state.get("refill_in_progress"):
            return None
        if int(state.get("cycle_pair_count") or 0) != 1:
            return None

        protected_cycle = 2
        symbol = str(snapshot.symbol or self.config.symbol or "")
        cycle_pair_count = int(state.get("cycle_pair_count") or 0)
        cycle_completed_count = int(state.get("cycle_completed_count") or 0)
        current_effective_cycle = int(state.get("current_effective_cycle") or 0)
        current_long_cycle_index = int(state.get("current_long_cycle_index") or 0)
        current_short_cycle_index = int(state.get("current_short_cycle_index") or 0)
        pending_long_cycle_index = int(state.get("pending_long_cycle_index") or 0)
        pending_short_cycle_index = int(state.get("pending_short_cycle_index") or 0)
        short_tp_pending_cycle = int(state.get("short_tp_pending_cycle") or 0)
        cycle_waiting_for_short_tp = bool(state.get("cycle_waiting_for_short_tp"))
        cycle_long_add_filled = bool(state.get("cycle_long_add_filled"))
        cycle_short_tp_filled = bool(state.get("cycle_short_tp_filled"))
        pending_cycle_loss_usdt = float(state.get("pending_cycle_loss_usdt") or 0.0)
        short_reduce_purpose = self._cycle_purpose("short", protected_cycle)
        active_order_purposes = sorted(
            {
                str(getattr(order, "purpose", "") or "")
                for order in [
                    *snapshot.active_orders,
                    *runtime_state.active_orders.values(),
                ]
                if str(getattr(order, "purpose", "") or "")
                and not self._is_terminal_order_status(getattr(order, "status", None))
            }
        )

        def _has_active_short_reduce_order() -> bool:
            def _matches(order: Any) -> bool:
                if getattr(order, "purpose", None) != short_reduce_purpose:
                    return False
                if self._is_terminal_order_status(getattr(order, "status", None)):
                    return False
                remaining_qty = float(getattr(order, "remaining_qty", 0.0) or 0.0)
                filled_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
                qty = float(getattr(order, "qty", 0.0) or 0.0)
                return remaining_qty > 1e-9 or qty <= 0.0 or filled_qty < qty

            return any(_matches(order) for order in snapshot.active_orders) or any(
                _matches(order) for order in runtime_state.active_orders.values()
            )

        has_active_short_reduce_order = _has_active_short_reduce_order()
        cycle_two_active = any(
            value == protected_cycle
            for value in (
                current_long_cycle_index,
                pending_long_cycle_index,
                short_tp_pending_cycle,
            )
        ) or has_active_short_reduce_order
        if not cycle_two_active or current_short_cycle_index >= protected_cycle:
            return None

        trigger_ready = cycle_long_add_filled or pending_cycle_loss_usdt > 0.0
        short_reduce_pending_or_buildable = has_active_short_reduce_order or (
            cycle_waiting_for_short_tp and short_tp_pending_cycle == protected_cycle
        )
        if not trigger_ready or not short_reduce_pending_or_buildable:
            return None

        return {
            "symbol": symbol,
            "cycle_pair_count": cycle_pair_count,
            "cycle_completed_count": cycle_completed_count,
            "protected_cycle": protected_cycle,
            "current_effective_cycle": current_effective_cycle,
            "current_long_cycle_index": current_long_cycle_index,
            "current_short_cycle_index": current_short_cycle_index,
            "pending_long_cycle_index": pending_long_cycle_index,
            "pending_short_cycle_index": pending_short_cycle_index,
            "short_tp_pending_cycle": short_tp_pending_cycle,
            "cycle_waiting_for_short_tp": cycle_waiting_for_short_tp,
            "cycle_long_add_filled": cycle_long_add_filled,
            "cycle_short_tp_filled": cycle_short_tp_filled,
            "pending_cycle_loss_usdt": pending_cycle_loss_usdt,
            "short_reduce_purpose": short_reduce_purpose,
            "short_reduce_open": has_active_short_reduce_order,
            "short_reduce_should_follow_up_build": (
                not has_active_short_reduce_order
                and cycle_waiting_for_short_tp
                and short_tp_pending_cycle == protected_cycle
            ),
            "refill_pending": bool(state.get("refill_pending")),
            "refill_in_progress": bool(state.get("refill_in_progress")),
            "active_order_purposes": active_order_purposes,
            "bot_state": state.get("bot_state"),
        }

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
        unsettled_runtime_orders, unsettled_snapshot_orders = self._collect_unsettled_strategy_orders(
            snapshot, runtime_state
        )
        active_order_purposes = [
            order.purpose
            for order in snapshot.active_orders
            if order.purpose and self._is_unsettled_strategy_order(order)
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
            self._emit_throttled_strategy_audit(
                runtime_state,
                context,
                "fixed_cycle_structure_skip",
                {
                    "skip_reason": "initial_entry_order_still_open",
                    "open_initial_orders": open_initial_orders,
                },
                signature_key="structure_skip",
                interval_ms=120000,
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
                and not self._is_terminal_order_status(getattr(order, "status", None))
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
            if not self._dynamic_symbol_entry_gate_allows_entry(
                runtime_state, context, "structure_initial_entry"
            ):
                return []
            self._maybe_update_symbol_from_best_coin(runtime_state, context, "structure_initial_entry")
            intents = self._build_entry_intents(snapshot, runtime_state, context)
            if intents:
                state["dynamic_entry_hold_initialized"] = False
                state.pop("next_dynamic_entry_allowed_at", None)
            return intents

        if (
            snapshot.long_qty <= 0
            and snapshot.short_qty <= 0
            and not snapshot.active_orders
            and not unsettled_snapshot_orders
            and not unsettled_runtime_orders
            and not state.get("full_exit_reset_in_progress")
        ):
            self._maybe_start_dynamic_symbol_hold_after_flat(snapshot, runtime_state, context, reason)
            state["fresh_restart_required"] = True
            logger.info(
                "fixed_cycle_full_exit_detected",
                {
                    "strategy": self.name,
                    "fresh_restart_required": True,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                },
            )
        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            if unsettled_runtime_orders or unsettled_snapshot_orders:
                context.audit.log_event(
                    "fixed_cycle_flat_waiting_unsettled_strategy_orders",
                    strategy=self.name,
                    reason=reason,
                    long_qty=snapshot.long_qty,
                    short_qty=snapshot.short_qty,
                    unsettled_runtime_orders=unsettled_runtime_orders,
                    unsettled_snapshot_orders=unsettled_snapshot_orders,
                )
                return []
            self._cancel_all_pending_orders(context, snapshot, runtime_state)
            state["bot_state"] = self.STATE_EXITED
            context.audit.log_event("fixed_cycle_exited", strategy=self.name, reason=reason, snapshot=snapshot)
            return []

        hard_stop_active = current_cycle >= self.config.hard_stop_cycle
        if hard_stop_active and context.cancel_open_orders_by_purpose:
            context.cancel_open_orders_by_purpose(self._all_cycle_purposes())

        state["bot_state"] = self.STATE_HARD_STOP_MODE if hard_stop_active else self.STATE_RESETTING_EXITS
        refill_required = bool(
            state.get("refill_pending") or state.get("bot_state") == self.STATE_REFILL_PENDING
        )

        downside_intents: list[StrategyIntent] = []
        if not hard_stop_active:
            downside_intents = self._build_downside_cycle_intents(snapshot, runtime_state, context)
        pending_loss_updated = bool(state.pop("pending_loss_updated_in_fill", False))
        pending_loss_reason = state.pop("pending_loss_exit_rebuild_reason", None)
        pending_loss_old_signature = state.get("pending_loss_exit_old_signature")
        force_exit_rebuild = bool(state.pop("force_exit_rebuild", False))
        if pending_loss_updated:
            logger.info(
                "pending_loss_exit_rebuild_forced",
                {
                    "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
                    "reason": pending_loss_reason,
                    "old_last_exit_signature": pending_loss_old_signature,
                    "force_exit_rebuild": force_exit_rebuild,
                },
            )
        logger.debug(
            "fixed_cycle_pre_break_even_state",
            {
                "reason": reason,
                "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
                "force_exit_rebuild": force_exit_rebuild,
                "pending_loss_updated_in_fill": pending_loss_updated,
                "long_qty": snapshot.long_qty,
                "short_qty": snapshot.short_qty,
                "long_avg": snapshot.long_avg,
                "short_avg": snapshot.short_avg,
                "denominator": snapshot.long_qty - snapshot.short_qty,
                "realized_long_pnl_total": snapshot.realized_long_pnl_total,
                "realized_short_pnl_total": snapshot.realized_short_pnl_total,
                "realized_net_pnl_total": float(snapshot.realized_long_pnl_total or 0.0)
                + float(snapshot.realized_short_pnl_total or 0.0),
            },
        )
        break_even_price, break_even_traces = self._calculate_break_even(snapshot, runtime_state)
        tp_price = self._calculate_tp_price(break_even_price, snapshot, runtime_state)
        logger.debug(
            "fixed_cycle_post_tp_state",
            {
                "reason": reason,
                "break_even_price": break_even_price,
                "tp_price": tp_price,
                "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
                "latest_break_even_price": break_even_price,
                "latest_tp_price": tp_price,
                "force_exit_rebuild": force_exit_rebuild,
            },
        )
        state["latest_break_even_price"] = break_even_price
        state["latest_tp_price"] = tp_price

        exit_intents: list[StrategyIntent] = []
        if not refill_required:
            pending_second_pair_short_reduce = self._pending_second_pair_short_reduce_exit_defer_payload(
                snapshot, runtime_state
            )
            if pending_second_pair_short_reduce is not None:
                _log_event(
                    "fixed_cycle_exit_deferred_pending_second_pair_short_reduce",
                    {
                        "reason": reason,
                        "tp_price": tp_price,
                        "break_even_price": break_even_price,
                        "current_price": snapshot.current_price,
                        **pending_second_pair_short_reduce,
                    },
                )
            else:
                exit_intents = self._build_exit_intents(
                    snapshot,
                    runtime_state,
                    current_cycle,
                    break_even_price,
                    tp_price,
                    hard_stop_active,
                    context,
                    force_exit_rebuild=force_exit_rebuild,
                    pending_loss_old_signature=pending_loss_old_signature,
                )
        intents = downside_intents + exit_intents
        if pending_loss_old_signature is not None:
            state.pop("pending_loss_exit_old_signature", None)

        if hard_stop_active:
            state["bot_state"] = self.STATE_HARD_STOP_MODE
        elif refill_required:
            state["bot_state"] = self.STATE_REFILL_PENDING
        else:
            state["bot_state"] = self.STATE_RUNNING

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
        old_exit_trigger_prices = self._collect_exit_trigger_prices_from_snapshot(snapshot)
        new_exit_trigger_prices = self._collect_exit_trigger_prices_from_intents(exit_intents)
        self._log_realized_state(
            tag="fixed_cycle_rebuild_state",
            snapshot=snapshot,
            runtime_state=runtime_state,
            stage="rebuild",
            reason=reason,
            old_exit_trigger_prices=old_exit_trigger_prices,
            new_exit_trigger_prices=new_exit_trigger_prices,
            basket_tp_price=tp_price,
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
        refill_required = bool(
            state.get("refill_pending") or state.get("bot_state") == self.STATE_REFILL_PENDING
        )
        gate_details = self._reconcile_refill_gate_state(snapshot, runtime_state)
        if refill_required:
            context.audit.log_event(
                "fixed_cycle_refill_required_after_cycle_pair",
                strategy=self.name,
                cycle_completed_count=state.get("cycle_completed_count"),
                cycle_pair_count=state.get("cycle_pair_count"),
                refill_pending=bool(state.get("refill_pending")),
                bot_state=state.get("bot_state"),
                active_refill_orders_count=gate_details["active_refill_orders_count"],
                stale_detected=gate_details["stale_detected"],
            )
            if gate_details["active_refill_orders_count"] > 0:
                context.audit.log_event(
                    "fixed_cycle_refill_block",
                    strategy=self.name,
                    cycle_completed_count=state.get("cycle_completed_count"),
                    cycle_pair_count=state.get("cycle_pair_count"),
                    reason="refill_in_progress",
                )
                context.audit.log_event(
                    "fixed_cycle_refill_block_details",
                    strategy=self.name,
                    **gate_details,
                )
                return []
            return self._build_entry_intents(snapshot, runtime_state, context)
        if gate_details["active_refill_orders_count"] > 0:
            context.audit.log_event(
                "fixed_cycle_refill_block",
                strategy=self.name,
                cycle_completed_count=state.get("cycle_completed_count"),
                cycle_pair_count=state.get("cycle_pair_count"),
                reason="refill_in_progress",
            )
            context.audit.log_event(
                "fixed_cycle_refill_block_details",
                strategy=self.name,
                **gate_details,
            )
            return []

        if state.get("trailing_active"):
            return intents
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
        purpose = self._cycle_purpose("short", cycle_index) if cycle_index > 0 else None
        long_purpose = self._cycle_purpose("long", cycle_index) if cycle_index > 0 else None

        def _emit_short_tp_follow_up_skip(reason: str, **extra: Any) -> None:
            runtime_active_orders = list(runtime_state.active_orders.values())
            context.audit.log_event(
                "fixed_cycle_short_tp_follow_up_skip",
                strategy=self.name,
                reason=reason,
                symbol=extra.pop("symbol", snapshot.symbol or self.config.symbol),
                cycle_index=cycle_index,
                purpose=purpose,
                long_purpose=long_purpose,
                cycle_waiting_for_short_tp=bool(state.get("cycle_waiting_for_short_tp")),
                short_tp_pending_cycle=int(state.get("short_tp_pending_cycle") or 0),
                current_long_cycle_index=int(state.get("current_long_cycle_index") or 0),
                current_short_cycle_index=int(state.get("current_short_cycle_index") or 0),
                cycle_long_add_filled=bool(state.get("cycle_long_add_filled")),
                cycle_short_tp_filled=bool(state.get("cycle_short_tp_filled")),
                long_add_pending=bool(state.get("long_add_pending")),
                pending_cycle_loss_usdt=float(state.get("pending_cycle_loss_usdt") or 0.0),
                pending_loss_updated_in_fill=bool(state.get("pending_loss_updated_in_fill")),
                force_exit_rebuild=bool(state.get("force_exit_rebuild")),
                force_short_tp_rebuild=bool(state.get("force_short_tp_rebuild")),
                refill_pending=bool(state.get("refill_pending")),
                refill_in_progress=bool(state.get("refill_in_progress")),
                active_order_purposes=[
                    order.purpose for order in runtime_active_orders if getattr(order, "purpose", None)
                ],
                runtime_active_order_ids=list(runtime_state.active_orders.keys()),
                snapshot_active_order_purposes=[
                    order.purpose for order in snapshot.active_orders if getattr(order, "purpose", None)
                ],
                **extra,
            )

        fallback_state = self._get_short_tp_fallback_state(runtime_state)
        if fallback_state.active:
            current_price = float(snapshot.current_price or 0.0)

            if current_price >= fallback_state.original_trigger_price:
                logger.info("Fallback cancel → price recovered above trigger")
                reset_short_tp_fallback(fallback_state)
                self._store_short_tp_fallback_state(runtime_state, fallback_state)
                self._clear_short_tp_fallback_order_context(runtime_state)
            else:
                _emit_short_tp_follow_up_skip(
                    "fallback_state_active_blocking_short_tp_build",
                    fallback_active=True,
                    fallback_original_trigger_price=float(
                        fallback_state.original_trigger_price or 0.0
                    ),
                    current_price=current_price,
                )
                return []
        if cycle_index <= 0:
            _emit_short_tp_follow_up_skip("cycle_index_non_positive")
            return []
        if not state.get("cycle_waiting_for_short_tp"):
            _emit_short_tp_follow_up_skip("cycle_waiting_for_short_tp_false")
            return []

        pending_before = float(state.get("pending_cycle_loss_usdt") or 0.0)

        current_short_cycle_index = int(state.get("current_short_cycle_index") or 0)
        cycle_state_short_index = int(cycle_state.get("short_cycle_index") or 0)

        if max(current_short_cycle_index, cycle_state_short_index) >= cycle_index:
            _emit_short_tp_follow_up_skip(
                "short_cycle_already_filled",
                cycle_state_short_index=cycle_state_short_index,
            )
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="short_cycle_already_filled",
                cycle_number=cycle_index,
                purpose=purpose,
                current_short_cycle_index=current_short_cycle_index,
                cycle_state_short_index=cycle_state_short_index,
            )
            return []

        def _has_pending_order(purpose_name: str) -> bool:
            for order in snapshot.active_orders:
                if order.purpose != purpose_name:
                    continue
                status = order.status
                if status in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}:
                    continue
                if float(order.remaining_qty or 0.0) <= 0:
                    continue
                return True
            return False

        if _has_pending_order(long_purpose):
            _emit_short_tp_follow_up_skip(
                "long_cycle_order_still_open",
                blocking_purpose=long_purpose,
            )
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
            _emit_short_tp_follow_up_skip(
                "short_tp_order_already_active",
                blocking_purpose=purpose,
            )
            return []

        long_fill = (cycle_state.get("long_fills") or {}).get(str(cycle_index)) or {}
        self._seed_long_fill_closed_pnl_fields(long_fill)
        active_trade_symbol = self._active_trade_symbol(snapshot, runtime_state, payload=long_fill)
        long_fill_price = float(long_fill.get("price") or 0.0)
        if long_fill_price <= 0:
            _emit_short_tp_follow_up_skip(
                "missing_long_fill_price_for_short_tp",
                symbol=active_trade_symbol or snapshot.symbol or self.config.symbol,
                long_fill_price=long_fill_price,
            )
            context.audit.log_event(
                "fixed_cycle_downside_skip",
                strategy=self.name,
                skip_reason="missing_long_fill_price_for_short_tp",
                cycle_number=cycle_index,
                purpose=purpose,
            )
            return []

        closed_pnl_ready = bool(long_fill.get("closed_pnl_ready"))
        if not closed_pnl_ready:
            closed_pnl_ready = self._refresh_long_fill_closed_pnl(
                cycle_index=cycle_index,
                long_fill=long_fill,
                runtime_state=runtime_state,
                context=context,
            )
            self._write_cycle_state(cycle_state)
            if not closed_pnl_ready:
                pending_meta = {
                    "order_id": long_fill.get("order_id"),
                    "cycle_index": cycle_index,
                    "symbol": active_trade_symbol,
                    "closed_pnl": long_fill.get("closed_pnl"),
                    "closed_qty": long_fill.get("closed_qty"),
                    "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
                }
                logger.info(
                    "fixed_cycle_long_closed_pnl_pending_but_short_followup_allowed %s",
                    pending_meta,
                )
                context.audit.log_event(
                    "fixed_cycle_long_closed_pnl_pending_but_short_followup_allowed",
                    strategy=self.name,
                    cycle_number=cycle_index,
                    purpose=purpose,
                    order_id=long_fill.get("order_id"),
                    pending_cycle_loss_usdt=float(state.get("pending_cycle_loss_usdt") or 0.0),
                )
        config_symbol = str(self.config.symbol or "").upper()
        snapshot_symbol = str(snapshot.symbol or "").upper()
        if active_trade_symbol and config_symbol and active_trade_symbol != config_symbol:
            _emit_short_tp_follow_up_skip(
                "symbol_mismatch_order_blocked",
                symbol=active_trade_symbol,
                config_symbol=config_symbol,
                snapshot_symbol=snapshot_symbol,
                fill_symbol=str(long_fill.get("symbol") or "").upper(),
                exchange_order_id=long_fill.get("order_id"),
                client_order_id=long_fill.get("client_order_id"),
            )
            _log_warning_event(
                "fixed_cycle_symbol_mismatch_order_blocked",
                {
                    "config_symbol": config_symbol,
                    "fill_symbol": str(long_fill.get("symbol") or "").upper(),
                    "snapshot_symbol": snapshot_symbol,
                    "active_trade_symbol": active_trade_symbol,
                    "purpose": purpose,
                    "exchange_order_id": long_fill.get("order_id"),
                    "client_order_id": long_fill.get("client_order_id"),
                    "cycle_index": cycle_index,
                },
            )
            return []
        state["block_exit_rebuild_until_pnl_ready"] = False

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
        provisional_closed_pnl = self._safe_float(long_fill.get("provisional_exec_pnl_total"), None)
        if provisional_closed_pnl is None:
            provisional_closed_pnl = self._safe_float(
                long_fill.get("provisional_runtime_pnl_total"), None
            )
        short_followup_pnl = confirmed_closed_pnl
        short_followup_pnl_source = "confirmed_closed_pnl"
        if short_followup_pnl is None and provisional_closed_pnl is not None:
            short_followup_pnl = provisional_closed_pnl
            short_followup_pnl_source = "provisional_runtime_pnl"
        elif short_followup_pnl is None:
            short_followup_pnl = 0.0
            short_followup_pnl_source = "missing_assumed_zero"
        short_followup_qty = confirmed_closed_qty
        if short_followup_qty is None:
            short_followup_qty = self._safe_float(
                long_fill.get("total_qty") or long_fill.get("qty"), None
            )
        logger.info(
            "short_tp_build_proceed",
            extra={
                "order_id": long_fill.get("order_id"),
                "cycle_index": cycle_index,
                "symbol": active_trade_symbol,
                "decision": "proceed",
                "closed_pnl": confirmed_closed_pnl,
                "closed_qty": confirmed_closed_qty,
                "short_followup_pnl": short_followup_pnl,
                "short_followup_pnl_source": short_followup_pnl_source,
                "short_followup_qty": short_followup_qty,
                "closed_pnl_ready": closed_pnl_ready,
            },
        )
        short_entry_price = float(snapshot.short_avg or state.get("short_avg") or 0.0)
        short_reduce_reference = short_entry_price
        target_profit_usdt = float(self.config.target_profit_usdt or 0.0)
        fee_rate = 0.00055
        if (
            short_qty <= 0
            or long_reduce_qty <= 0
            or (short_followup_qty is not None and short_followup_qty <= 0)
            or short_entry_price <= 0
            or fee_rate >= 1.0
        ):
            _emit_short_tp_follow_up_skip(
                "short_tp_invalid_initial_inputs",
                symbol=active_trade_symbol,
                long_fill_price=long_fill_price,
                long_reduce_qty=long_reduce_qty,
                confirmed_closed_pnl=confirmed_closed_pnl,
                confirmed_closed_qty=confirmed_closed_qty,
                short_followup_pnl=short_followup_pnl,
                short_followup_pnl_source=short_followup_pnl_source,
                short_followup_qty=short_followup_qty,
                short_entry_price=short_entry_price,
                short_qty=short_qty,
                fee_rate=fee_rate,
            )
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
                short_followup_pnl=short_followup_pnl,
                short_followup_pnl_source=short_followup_pnl_source,
                short_followup_qty=short_followup_qty,
                short_entry_price=short_entry_price,
                short_qty=short_qty,
                fee_rate=fee_rate,
            )
            return []

        short_entry_price = float(short_entry_price)
        short_qty = float(short_qty)
        fee_rate = float(fee_rate)

        long_loss_usdt = max(-float(short_followup_pnl or 0.0), 0.0)
        required_net = max(long_loss_usdt + target_profit_usdt, 0.0)
        required_remaining_profit = required_net
        required_price_move = required_net / short_qty if short_qty > 0 else 0.0
        if (
            long_loss_usdt > 0
            and required_net <= float(target_profit_usdt or 0.0)
        ):
            logger.warning(
                "short_tp_required_net_suspicious",
                extra={
                    "long_loss_usdt": long_loss_usdt,
                    "target_profit_usdt": target_profit_usdt,
                    "required_net": required_net,
                    "realized_net_pnl_total": self._get_realized_net_pnl_total(
                        runtime_state, snapshot
                    ),
                    "confirmed_closed_pnl": confirmed_closed_pnl,
                    "short_followup_pnl": short_followup_pnl,
                    "short_followup_pnl_source": short_followup_pnl_source,
                    "short_qty": short_qty,
                    "short_entry_price": short_entry_price,
                },
            )

        if short_qty <= 0:
            _emit_short_tp_follow_up_skip(
                "short_tp_invalid_short_qty_non_positive",
                symbol=active_trade_symbol,
                short_entry_price=short_entry_price,
                short_qty=short_qty,
            )
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
            _emit_short_tp_follow_up_skip(
                "short_tp_invalid_tp_price_non_positive",
                symbol=active_trade_symbol,
                tp_price=tp_price,
            )
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
        long_avg = float(snapshot.long_avg or 0.0)
        min_threshold_pct = float(self.config.short_tp_min_threshold_pct_after_long_reduce or 0.0)
        original_trigger_price_raw = raw_trigger_price
        safe_short_tp_price = None
        short_tp_guard_applied = False

        if (
            raw_trigger_price is not None
            and raw_trigger_price > 0
            and long_avg > 0
            and min_threshold_pct > 0
        ):
            safe_short_tp_price = long_avg * (1 - min_threshold_pct)

            if raw_trigger_price > safe_short_tp_price:
                raw_trigger_price = safe_short_tp_price
                short_tp_guard_applied = True
                logger.info(
                    "short_tp_min_threshold_guard_applied %s",
                    {
                        "cycle_index": cycle_index,
                        "long_avg": long_avg,
                        "short_avg": float(snapshot.short_avg or 0.0),
                        "current_price": float(snapshot.current_price or 0.0),
                        "original_trigger_price_raw": original_trigger_price_raw,
                        "safe_short_tp_price": safe_short_tp_price,
                        "final_trigger_price_raw": raw_trigger_price,
                        "min_threshold_pct": min_threshold_pct,
                        "short_qty": short_qty,
                        "reason": "short_tp_after_long_reduce_too_close_to_long_avg",
                    },
                )

        trigger_price = self._normalize_price(raw_trigger_price, runtime_state)
        state["last_expected_short_tp_net"] = required_net
        state["last_short_tp_trigger_price"] = trigger_price
        state["last_short_tp_qty"] = short_qty
        realized_net_pnl_total = self._get_realized_net_pnl_total(runtime_state, snapshot)
        logger.debug(
            "short_tp_loss_recovery",
            extra={
                "realized_net_pnl_total": realized_net_pnl_total,
                "required_remaining_profit": required_remaining_profit,
                "required_price_move": required_price_move,
                "tp_price": tp_price,
            },
        )
        current_price = float(snapshot.current_price or 0.0)
        use_market_fallback = current_price > 0 and current_price <= trigger_price
        required_price_move = short_entry_price - trigger_price
        required_short_gross = short_qty * required_price_move
        price = self._normalize_price(
            max(trigger_price - self.config.price_tick_size, self.config.price_tick_size),
            runtime_state,
        )
        if short_qty <= 0 or trigger_price <= 0:
            _emit_short_tp_follow_up_skip(
                "short_tp_invalid_final_trigger_inputs",
                symbol=active_trade_symbol,
                long_fill_price=long_fill_price,
                short_qty=short_qty,
                trigger_price=trigger_price,
                final_limit_price=price,
            )
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
                "symbol": active_trade_symbol,
                "decision": "trigger_run",
                "closed_pnl": confirmed_closed_pnl,
                "closed_qty": confirmed_closed_qty,
                "long_loss_usdt": long_loss_usdt,
                "required_net_profit": required_net,
                "trigger_price": trigger_price,
                "short_qty": short_qty,
                "short_tp_min_threshold_pct_after_long_reduce": min_threshold_pct,
                "short_tp_guard_long_avg": long_avg,
                "short_tp_guard_original_trigger_price_raw": original_trigger_price_raw,
                "short_tp_guard_safe_short_tp_price": safe_short_tp_price,
                "short_tp_guard_applied": short_tp_guard_applied,
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
            short_tp_min_threshold_pct_after_long_reduce=min_threshold_pct,
            short_tp_guard_long_avg=long_avg,
            short_tp_guard_original_trigger_price_raw=original_trigger_price_raw,
            short_tp_guard_safe_short_tp_price=safe_short_tp_price,
            short_tp_guard_applied=short_tp_guard_applied,
            order_type="Limit",
            reduce_only=True,
        )
        pending_after = float(state.get("pending_cycle_loss_usdt") or 0.0)
        _audit_calc(
            "short_tp_build_calc",
            {
                "cycle_index": cycle_index,
                "symbol": active_trade_symbol,
                "long_fill_purpose": long_purpose,
                "long_loss_usdt": long_loss_usdt,
                "confirmed_closed_pnl": confirmed_closed_pnl,
                "short_followup_pnl": short_followup_pnl,
                "short_followup_pnl_source": short_followup_pnl_source,
                "target_profit_usdt": target_profit_usdt,
                "required_net": required_net,
                "short_entry_price": short_entry_price,
                "short_qty": short_qty,
                "fee_rate": fee_rate,
                "trigger_price": trigger_price,
                "raw_trigger_price": raw_trigger_price,
                "short_tp_min_threshold_pct_after_long_reduce": min_threshold_pct,
                "short_tp_guard_long_avg": long_avg,
                "short_tp_guard_original_trigger_price_raw": original_trigger_price_raw,
                "short_tp_guard_safe_short_tp_price": safe_short_tp_price,
                "short_tp_guard_applied": short_tp_guard_applied,
                "break_even_price": float(state.get("latest_break_even_price") or 0.0),
                "expected_short_tp_net": required_net,
                "pending_cycle_loss_usdt_before": pending_before,
                "pending_cycle_loss_usdt_after": pending_after,
                "order_purpose": purpose,
                "cycle_role": "short_reduce",
                "required_price_move": required_price_move,
                "required_short_gross": required_short_gross,
            },
        )
        metadata = {
            "cycle_index": cycle_index,
            "cycle_role": "short_reduce",
            "symbol": active_trade_symbol,
            "replace_open_purpose": purpose,
            "entry_reference_price": float(state.get("entry_reference_price") or 0.0),
            "long_fill_price": long_fill_price,
            "long_reduce_qty": long_reduce_qty,
            "source_long_reduce_confirmed_pnl": confirmed_closed_pnl,
            "source_long_reduce_confirmed_qty": confirmed_closed_qty,
            "source_long_reduce_confirmed_avg_price": confirmed_closed_avg_price,
            "source_long_reduce_confirmed_cost": confirmed_closed_cost,
            "source_long_reduce_confirmed_pnl_updated_time": long_fill.get("closed_pnl_updated_time"),
            "short_entry_price": short_entry_price,
            "short_reduce_reference": short_reduce_reference,
            "fee_rate": fee_rate,
            "required_price_move": required_price_move,
        }
        if use_market_fallback:
            metadata.update(
                {
                    "market_fallback": True,
                    "fallback_reason": "short_tp_trigger_already_crossed",
                    "original_trigger_price": trigger_price,
                    "current_price": current_price,
                }
            )
            context.audit.log_event(
                "fixed_cycle_short_cycle_market_fallback_planned",
                strategy=self.name,
                cycle_index=cycle_index,
                purpose=purpose,
                symbol=active_trade_symbol,
                trigger_price=trigger_price,
                current_price=current_price,
            )
        return [
            StrategyIntent(
                side="short",
                qty=short_qty,
                purpose=purpose,
                order_type="Market",
                reduce_only=True,
                trigger_price=None if use_market_fallback else trigger_price,
                trigger_direction=None if use_market_fallback else 2,
                trigger_by=None if use_market_fallback else "LastPrice",
                close_on_trigger=None if use_market_fallback else True,
                position_idx=2,
                metadata=metadata,
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
        *,
        force_exit_rebuild: bool = False,
        pending_loss_old_signature: Any = None,
    ) -> list[StrategyIntent]:
        intents: list[StrategyIntent] = []
        state = runtime_state.strategy_state
        self._ensure_cycle_state(runtime_state)
        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            logger.debug(
                "fixed_cycle_exit_build_skipped_flat_snapshot %s",
                {
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                    "current_cycle": current_cycle,
                    "force_exit_rebuild": force_exit_rebuild,
                },
            )
            return intents
        if state.get("block_exit_rebuild_until_pnl_ready"):
            return intents

        if self._release_exit_lock_if_unprotected_open_position(
            snapshot,
            runtime_state,
            context,
            reason="build_exit_intents_before_exit_locked_skip",
        ):
            force_exit_rebuild = True

        logger.debug(
            "fixed_cycle_exit_lock_check",
            {
                "exit_rebuild_allowed": state.get("exit_rebuild_allowed", True),
                "force_exit_rebuild": force_exit_rebuild,
                "last_exit_signature_is_none": state.get("last_exit_signature") is None,
                "tp_price": tp_price,
                "break_even_price": break_even_price,
            },
        )
        if not state.get("exit_rebuild_allowed", True) and not force_exit_rebuild:
            active_order_purposes = [getattr(order, "purpose", None) for order in snapshot.active_orders]
            context.audit.log_event(
                "fixed_cycle_exit_skip",
                strategy=self.name,
                skip_reason="exit_locked",
                long_qty=snapshot.long_qty,
                short_qty=snapshot.short_qty,
                long_exit_filled=bool(state.get("long_exit_filled")),
                short_exit_filled=bool(state.get("short_exit_filled")),
                current_cycle=int(state.get("current_effective_cycle") or 0),
                active_order_purposes=active_order_purposes,
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

        current_price = snapshot.current_price
        symbol, rules, source = self._resolve_instrument_rules(runtime_state)
        tick_decimal = rules["tick_size"] if rules and rules.get("tick_size", Decimal("0")) > 0 else Decimal(
            str(self.config.price_tick_size)
        )
        tick_size = float(tick_decimal)
        final_exit_tick_offset_ticks = max(float(self.config.final_exit_tick_offset or 0.0), 0.0)
        final_exit_tick_offset_price = tick_size * final_exit_tick_offset_ticks
        long_tp_price = tp_price
        short_sl_price = max(tp_price - final_exit_tick_offset_price, tick_size)
        logger.debug(
            "exit_tick_size %s",
            {
                "symbol": symbol,
                "tick_size": str(tick_decimal),
                "source": source,
                "price_tick_config": self.config.price_tick_size,
            },
        )
        market_exit_mode = current_price >= tp_price
        long_tp_valid = False
        short_sl_valid = False
        if not market_exit_mode:
            if current_price > 0:
                min_short_trigger = current_price + tick_size
                min_long_trigger = current_price + (2 * tick_size)
                logger.debug(
                    "exit_trigger_clamp %s",
                    {
                        "symbol": symbol,
                        "tp_price": tp_price,
                        "min_short_trigger": min_short_trigger,
                        "min_long_trigger": min_long_trigger,
                        "current_price": current_price,
                    },
                )
                short_sl_price = max(short_sl_price, min_short_trigger)
                long_tp_price = max(
                    long_tp_price,
                    short_sl_price + final_exit_tick_offset_price,
                    min_long_trigger,
                )
                logger.debug(
                    "exit_trigger_result %s",
                    {
                        "symbol": symbol,
                        "long_tp_price": long_tp_price,
                        "short_sl_price": short_sl_price,
                        "tp_price": tp_price,
                        "current_price": current_price,
                    },
                )
        if final_exit_tick_offset_ticks <= 0:
            same_exit_price = max(long_tp_price, short_sl_price)
            long_tp_price = same_exit_price
            short_sl_price = same_exit_price
        _log_event(
            "fixed_cycle_final_exit_tick_offset_config_applied",
            {
                "base_exit_price": tp_price,
                "long_tp_trigger_price": long_tp_price,
                "short_sl_trigger_price": short_sl_price,
                "price_tick_size": float(tick_size),
                "final_exit_tick_offset_ticks": final_exit_tick_offset_ticks,
                "final_exit_tick_offset_price": final_exit_tick_offset_price,
                "same_price_mode": final_exit_tick_offset_ticks <= 0,
                "long_minus_short_ticks": (
                    round((long_tp_price - short_sl_price) / tick_size)
                    if tick_size > 0
                    else None
                ),
                "long_purpose": self.LONG_TP_EXIT_PURPOSE,
                "short_purpose": self.SHORT_SL_EXIT_PURPOSE,
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

        old_signature = pending_loss_old_signature
        exit_rebuild_allowed = state.get("exit_rebuild_allowed", True)
        if (
            state.get("last_exit_rebuild_skip_signature")
            and state.get("last_exit_rebuild_skip_signature") != signature
        ):
            state["last_exit_rebuild_skip_signature"] = None
        if (
            old_signature == signature
            and not force_exit_rebuild
            and not exit_rebuild_allowed
        ):
            last_skip_sig = state.get("last_exit_rebuild_skip_signature")
            if last_skip_sig != signature:
                state["last_exit_rebuild_skip_signature"] = signature
                pending_loss = float(state.get("pending_cycle_loss_usdt") or 0.0)
                _audit_calc(
                    "exit_rebuild_skipped_duplicate_signature",
                    {
                        "signature": signature,
                        "pending_loss_exit_old_signature": old_signature,
                        "last_exit_signature": state.get("last_exit_signature"),
                        "force_exit_rebuild": force_exit_rebuild,
                        "exit_rebuild_allowed": exit_rebuild_allowed,
                        "pending_cycle_loss_usdt": pending_loss,
                    },
                )
            return []

        if context.cancel_open_orders_by_purpose:
            context.cancel_open_orders_by_purpose(self._exit_purposes())

        cycle_idx = int(state.get("current_effective_cycle") or 0)
        metadata_base = {
            "basket_break_even_price": break_even_price,
            "basket_tp_price": tp_price,
            "exit_mode": "basket_exit",
        }

        def build_metadata(purpose: str, exit_type: str) -> dict[str, Any]:
            metadata = dict(metadata_base)
            metadata["replace_open_purpose"] = [purpose]
            metadata["exit_type"] = exit_type
            metadata["cycle_index"] = cycle_idx
            return metadata

        if market_exit_mode:
            logger.info(
                "basket_market_exit_immediate",
                {
                    "symbol": symbol,
                    "current_price": current_price,
                    "tp_price": tp_price,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                },
            )
            if snapshot.long_qty > 0:
                intents.append(
                    StrategyIntent(
                        side="long",
                        qty=snapshot.long_qty,
                        purpose=self.LONG_TP_EXIT_PURPOSE,
                        order_type="Market",
                        reduce_only=True,
                        trigger_price=None,
                        trigger_direction=None,
                        trigger_by=None,
                        close_on_trigger=True,
                        position_idx=1,
                        metadata=build_metadata(
                            self.LONG_TP_EXIT_PURPOSE, "basket_market_long_exit"
                        ),
                    )
                )
            if snapshot.short_qty > 0:
                intents.append(
                    StrategyIntent(
                        side="short",
                        qty=snapshot.short_qty,
                        purpose=self.SHORT_SL_EXIT_PURPOSE,
                        order_type="Market",
                        reduce_only=True,
                        trigger_price=None,
                        trigger_direction=None,
                        trigger_by=None,
                        close_on_trigger=True,
                        position_idx=2,
                        metadata=build_metadata(
                            self.SHORT_SL_EXIT_PURPOSE, "basket_market_short_exit"
                        ),
                    )
                )
        else:
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
                _audit_calc(
                    "exit_order_plan_calc",
                    {
                        "purpose": self.LONG_TP_EXIT_PURPOSE,
                        "side": "long",
                        "qty": snapshot.long_qty,
                        "trigger_price": long_tp_price,
                        "expected_profit_or_loss": (long_tp_price - break_even_price) * snapshot.long_qty,
                        "cycle_index": cycle_idx,
                        "cycle_role": "long_exit",
                        "break_even_price": break_even_price,
                    },
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
                _audit_calc(
                    "exit_order_plan_calc",
                    {
                        "purpose": self.SHORT_SL_EXIT_PURPOSE,
                        "side": "short",
                        "qty": snapshot.short_qty,
                        "trigger_price": short_sl_price,
                        "expected_profit_or_loss": (short_sl_price - break_even_price) * snapshot.short_qty,
                        "cycle_index": cycle_idx,
                        "cycle_role": "short_exit",
                        "break_even_price": break_even_price,
                    },
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
            pending_cycle_loss_usdt=float(state.get("pending_cycle_loss_usdt") or 0.0),
            force_exit_rebuild=force_exit_rebuild,
            exit_rebuild_allowed=state.get("exit_rebuild_allowed", True),
            last_exit_signature_is_none=state.get("last_exit_signature") is None,
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

        long_qty = snapshot.long_qty
        short_qty = snapshot.short_qty
        long_avg = snapshot.long_avg
        short_avg = snapshot.short_avg
        denominator = long_qty - short_qty
        if abs(denominator) <= 1e-9:
            base_break_even = long_avg
        else:
            base_break_even = (
                (long_avg * long_qty) - (short_avg * short_qty)
            ) / denominator
        break_even_price = base_break_even
        logger.debug(
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
                "base_break_even": base_break_even,
                "final_break_even_price": break_even_price,
                "denominator": denominator,
            },
        )

        break_even_price = self._normalize_price(
            max(break_even_price, self.config.price_tick_size), runtime_state
        )

        traces = [
            CalculationTrace(
                name="break_even_price",
                formula="(long_avg * long_qty - short_avg * short_qty) / (long_qty - short_qty)",
                inputs={
                    "long_avg": snapshot.long_avg,
                    "short_avg": snapshot.short_avg,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                    "denominator": denominator,
                },
                result={"break_even_price": break_even_price},
                details={"base_break_even": base_break_even},
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
        state = runtime_state.strategy_state if runtime_state else {}
        snapshot_long_avg = snapshot.long_avg if snapshot else float(state.get("long_avg") or 0.0)
        snapshot_short_avg = snapshot.short_avg if snapshot else float(state.get("short_avg") or 0.0)
        snapshot_long_qty = snapshot.long_qty if snapshot else float(state.get("open_long_qty") or 0.0)
        snapshot_short_qty = snapshot.short_qty if snapshot else float(state.get("open_short_qty") or 0.0)
        realized_long_pnl = float((runtime_state.realized_long_pnl_total if runtime_state else 0.0) or 0.0)
        realized_short_pnl = float((runtime_state.realized_short_pnl_total if runtime_state else 0.0) or 0.0)
        realized_cycle_net = realized_long_pnl + realized_short_pnl
        components = calculate_hedge_exit_price(
            long_avg=snapshot_long_avg,
            long_qty=snapshot_long_qty,
            short_avg=snapshot_short_avg,
            short_qty=snapshot_short_qty,
            tp_profit_target_pct=float(self.config.tp_profit_target_pct or 0.0),
            tp_buffer_pct=float(self.config.tp_buffer_pct or 0.0),
            realized_cycle_net=realized_cycle_net,
        )
        fee_rate = max(float(self.config.order_fee_rate_pct or 0.0), 0.0) / 100.0
        long_notional = snapshot_long_avg * snapshot_long_qty
        short_notional = snapshot_short_avg * snapshot_short_qty
        entry_fee_usdt = fee_rate * (long_notional + short_notional)
        close_fee_denominator = components.net_qty - fee_rate * (snapshot_long_qty + snapshot_short_qty)
        fee_adjusted_exit_price = components.exit_price
        if fee_rate > 0 and close_fee_denominator > 1e-12:
            fee_adjusted_exit_price = (
                long_notional
                - short_notional
                + components.required_profit_usdt
                + entry_fee_usdt
            ) / close_fee_denominator
        tp_price = self._normalize_price(fee_adjusted_exit_price, runtime_state)
        long_profit_at_exit = (tp_price - snapshot_long_avg) * snapshot_long_qty
        short_loss_at_exit = (tp_price - snapshot_short_avg) * snapshot_short_qty
        open_hedge_net_at_exit = long_profit_at_exit - short_loss_at_exit
        close_fee_usdt = fee_rate * tp_price * (snapshot_long_qty + snapshot_short_qty)
        total_fee_adjustment_usdt = entry_fee_usdt + close_fee_usdt
        open_hedge_net_after_fees = open_hedge_net_at_exit - total_fee_adjustment_usdt
        expected_total_net_after_exit = open_hedge_net_after_fees + components.realized_cycle_net
        target_total_profit_usdt = components.target_profit_usdt + components.buffer_usdt
        target_delta_usdt = expected_total_net_after_exit - target_total_profit_usdt
        logger.debug(
            "fixed_cycle_tp_components %s",
            {
                "break_even_price": break_even_price,
                "fee_rate": fee_rate,
                "order_fee_rate_pct": float(self.config.order_fee_rate_pct or 0.0),
                "profit_basis_usdt": components.profit_basis_usdt,
                "target_profit_usdt": components.target_profit_usdt,
                "buffer_usdt": components.buffer_usdt,
                "realized_cycle_net": components.realized_cycle_net,
                "required_profit_usdt": components.required_profit_usdt,
                "net_qty": components.net_qty,
                "entry_fee_usdt": entry_fee_usdt,
                "close_fee_usdt": close_fee_usdt,
                "total_fee_adjustment_usdt": total_fee_adjustment_usdt,
                "fee_adjusted_exit_price": fee_adjusted_exit_price,
                "raw_exit_price_without_fees": components.exit_price,
                "long_profit_at_exit": long_profit_at_exit,
                "short_loss_at_exit": short_loss_at_exit,
                "open_hedge_net_at_exit": open_hedge_net_at_exit,
                "open_hedge_net_after_fees": open_hedge_net_after_fees,
                "expected_total_net_after_exit": expected_total_net_after_exit,
                "target_total_profit_usdt": target_total_profit_usdt,
                "target_delta_usdt": target_delta_usdt,
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
        pending_cycle_loss_usdt = float(
            (runtime_state.strategy_state.get("pending_cycle_loss_usdt") if runtime_state else 0.0) or 0.0
        )
        pending_loss_price_component = 0.0
        if snapshot:
            net_qty = float(snapshot.long_qty or 0.0) - float(snapshot.short_qty or 0.0)
            if pending_cycle_loss_usdt > 0 and abs(net_qty) > 1e-9:
                pending_loss_price_component = pending_cycle_loss_usdt / net_qty
        goal_profit = reference_price * self._pct(self.config.tp_profit_target_pct)
        buffer = reference_price * self._pct(self.config.tp_buffer_pct)
        return {
            "reference_price": reference_price,
            "loss_recovery": loss_recovery,
            "goal_profit": goal_profit,
            "buffer": buffer,
            "pending_cycle_loss_usdt": pending_cycle_loss_usdt,
            "pending_loss_price_component": pending_loss_price_component,
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
        return 0.0

    def _get_realized_long_loss_total(
        self, runtime_state: RuntimeState | None
    ) -> float:
        stored_total = float(getattr(self, "realized_long_loss_total", 0.0) or 0.0)
        if runtime_state:
            stored_total = float(runtime_state.strategy_state.get("realized_long_loss_total") or stored_total)
        return stored_total

    def _add_realized_long_loss(self, runtime_state: RuntimeState, loss_usdt: float) -> None:
        if loss_usdt <= 0:
            return
        state = runtime_state.strategy_state
        total = float(state.get("realized_long_loss_total") or 0.0) + loss_usdt
        state["realized_long_loss_total"] = total
        self.realized_long_loss_total = total

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

    def _try_complete_cycle_pair_after_confirmed_pnl(
        self,
        runtime_state: RuntimeState,
        cycle_index: int,
        trigger_purpose: str | None,
    ) -> None:
        if cycle_index <= 0:
            return
        state = runtime_state.strategy_state
        if not (state.get("cycle_long_add_filled") and state.get("cycle_short_tp_filled")):
            return
        cycle_state = self._ensure_cycle_state(runtime_state)
        if not self._cycle_has_confirmed_pair_pnl(runtime_state, cycle_index):
            ledger = state.get("audit_pnl_ledger") or {}
            _log_event(
                "fixed_cycle_complete_blocked_missing_confirmed_pair_pnl",
                {
                    "cycle_index": cycle_index,
                    "long_flag": state.get("cycle_long_add_filled"),
                    "short_flag": state.get("cycle_short_tp_filled"),
                    "ledger_cycle_long_reduce_keys": list((ledger.get("cycle_long_reduce_pnl") or {}).keys()),
                    "ledger_cycle_short_tp_keys": list((ledger.get("cycle_short_tp_pnl") or {}).keys()),
                    "cycle_long_reduce_entry": (ledger.get("cycle_long_reduce_pnl") or {}).get(str(cycle_index)),
                    "cycle_short_tp_entry": (ledger.get("cycle_short_tp_pnl") or {}).get(str(cycle_index)),
                },
            )
            return
        state["cycle_completed_count"] = int(state.get("cycle_completed_count") or 0) + 1
        state["cycle_pair_count"] = int(state.get("cycle_pair_count") or 0) + 1
        logger.info(
            "[CYCLE-COMPLETE] cycle_count=%s",
            state.get("cycle_completed_count"),
        )
        _log_event(
            "fixed_cycle_cycle_pair_completed_after_confirmed_pnl_retry",
            {
                "cycle_index": cycle_index,
                "trigger_purpose": trigger_purpose,
                "cycle_completed_count": state.get("cycle_completed_count"),
                "cycle_pair_count": state.get("cycle_pair_count"),
            },
        )
        state["cycle_long_add_filled"] = False
        state["cycle_short_tp_filled"] = False
        state["cycle_waiting_for_short_tp"] = False
        state["pending_long_cycle_index"] = 0
        state["short_tp_pending_cycle"] = 0
        cycle_state["cycle_waiting_for_short_tp"] = False
        cycle_state["pending_long_cycle_index"] = 0
        cycle_state["short_tp_pending_cycle"] = 0

        if state["cycle_completed_count"] >= 2:
            logger.info("[REFILL-TRIGGER] switching to STATE_REFILL_PENDING")
            _log_event(
                "fixed_cycle_refill_triggered",
                {
                    "cycle_completed_count": state.get("cycle_completed_count"),
                    "cycle_pair_count": state.get("cycle_pair_count"),
                    "initial_long_qty": state.get("initial_long_qty"),
                    "initial_short_qty": state.get("initial_short_qty"),
                    "open_long_qty": state.get("open_long_qty"),
                    "open_short_qty": state.get("open_short_qty"),
                    "trigger_purpose": trigger_purpose or state.get("refill_trigger_purpose"),
                },
            )
            state["bot_state"] = self.STATE_REFILL_PENDING
            state["refill_pending"] = True
            state["refill_in_progress"] = False
            state["refill_long_filled"] = False
            state["refill_short_filled"] = False
            state["long_add_pending"] = False
            state["long_add_rebuild_allowed"] = True
            state["refill_state"] = {}
            state["cycle_pair_count"] = int(state.get("cycle_pair_count") or 0) or state.get("cycle_completed_count") or 0
            state["refill_trigger_cycle_completed_count"] = state.get("cycle_completed_count")
            state["refill_trigger_purpose"] = trigger_purpose or state.get("refill_trigger_purpose")
            return


    def _complete_refill(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
    ) -> None:
        state = runtime_state.strategy_state
        cycle_state = self._ensure_cycle_state(runtime_state)
        short_tp_fallback_state_before = state.get("short_tp_fallback_state")
        short_tp_fallback_order_context_present_before = bool(
            state.get("short_tp_fallback_order_context")
        )
        state["cycle_completed_count"] = 0
        state["cycle_pair_count"] = 0
        state["current_long_cycle_index"] = 0
        state["current_short_cycle_index"] = 0
        state["current_effective_cycle"] = 0
        state["cycle_long_add_filled"] = False
        state["cycle_short_tp_filled"] = False
        state["cycle_waiting_for_short_tp"] = False
        state["long_add_pending"] = False
        state["pending_long_cycle_index"] = 0
        state["pending_short_cycle_index"] = 0
        state["short_tp_pending_cycle"] = 0
        state["pending_cycle_loss_usdt"] = 0.0
        state["last_short_tp_trigger_price"] = 0.0
        state["last_expected_short_tp_net"] = 0.0
        state["last_short_tp_qty"] = 0.0
        state["force_short_tp_rebuild"] = False
        state["short_tp_fallback_state"] = None
        state["short_tp_fallback_order_context"] = None
        state["pending_loss_updated_in_fill"] = False
        state["pending_loss_exit_old_signature"] = None
        state["pending_loss_exit_rebuild_reason"] = None
        state["long_add_rebuild_allowed"] = True
        state["exit_rebuild_allowed"] = True
        state["refill_pending"] = False
        state["refill_in_progress"] = False
        state["refill_long_filled"] = False
        state["refill_short_filled"] = False
        state["refill_state"] = {}
        cycle_state["long_add_pending"] = False
        cycle_state["cycle_waiting_for_short_tp"] = False
        cycle_state["pending_long_cycle_index"] = 0
        cycle_state["short_tp_pending_cycle"] = 0
        cycle_state["long_fills"] = {}
        cycle_state["short_fills"] = {}
        state["bot_state"] = self.STATE_RUNNING
        self._write_cycle_state(cycle_state)
        logger.info(
            "cycle_state_reset_after_refill %s",
            {
                "cycle_completed_count": state.get("cycle_completed_count"),
                "cycle_pair_count": state.get("cycle_pair_count"),
                "current_long_cycle_index": state.get("current_long_cycle_index"),
                "current_short_cycle_index": state.get("current_short_cycle_index"),
            },
        )
        if context is not None:
            context.audit.log_event(
                "fixed_cycle_refill_short_tp_fallback_state_cleared",
                strategy=self.name,
                symbol=self.config.symbol,
                cycle_pair_count=state.get("cycle_pair_count"),
                cycle_completed_count=state.get("cycle_completed_count"),
                short_tp_fallback_state_before=short_tp_fallback_state_before,
                short_tp_fallback_order_context_present_before=short_tp_fallback_order_context_present_before,
                pending_cycle_loss_usdt=state.get("pending_cycle_loss_usdt"),
                bot_state=state.get("bot_state"),
            )
            context.audit.log_event(
                "refill_completed",
                strategy=self.name,
                cycle_completed_count=state.get("cycle_completed_count"),
                cycle_pair_count=state.get("cycle_pair_count"),
            )
        self._reset_exit_state_for_new_structure(runtime_state, "refill_completed")

    def _advance_cycle_from_fill(
        self,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: StrategyContext | None = None,
    ) -> None:
        purpose = fill_event.purpose or ""
        exit_purposes = {
            self.LONG_TP_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        }
        is_exit_fill = fill_event.status == "FILLED" and purpose in exit_purposes

        state = runtime_state.strategy_state
        fill_info = {
            "fill_purpose": fill_event.purpose,
            "confirmed_closed_pnl": None,
        }
        cycle_state = self._ensure_cycle_state(runtime_state)
        snapshot = runtime_state.last_snapshot
        order_fully_completed = (
            fill_event.status == "FILLED"
        )
        cycle_state["trade_active"] = True
        cycle_state["symbol"] = self._active_trade_symbol(snapshot, runtime_state, fill_event=fill_event)
        processed = set(cycle_state.get("processed_fill_ids") or [])
        fill_key = self._fill_persistence_key(fill_event)
        if fill_key in processed and not is_exit_fill:
            return

        if purpose in {"REFILL_LONG", "REFILL_SHORT"}:
            refill_state = state.setdefault("refill_state", {})
            _log_event(
                "fixed_cycle_refill_order_fill_received",
                {
                    "purpose": purpose,
                    "status": fill_event.status,
                    "exec_qty": fill_event.exec_qty,
                    "exec_price": fill_event.exec_price,
                    "expected_purposes": list(refill_state.get("expected_purposes") or []),
                    "refill_long_filled": bool(state.get("refill_long_filled")),
                    "refill_short_filled": bool(state.get("refill_short_filled")),
                },
            )
            if order_fully_completed:
                refill_state[purpose] = True
                if purpose == "REFILL_LONG":
                    state["refill_long_filled"] = True
                elif purpose == "REFILL_SHORT":
                    state["refill_short_filled"] = True

            if state.get("refill_long_filled") and state.get("refill_short_filled"):
                self._complete_refill(runtime_state, context)
                state["refill_state"] = {}

            processed.add(fill_key)
            cycle_state["processed_fill_ids"] = list(processed)
            self._write_cycle_state(cycle_state)
            return

        cycle_index = int(fill_event.metadata.get("cycle_index") or 0)
        if cycle_index <= 0 and not is_exit_fill:
            return
        fill_type, _ = self._classify_exit_fill_for_audit(fill_event)

        if "_LONG_" in fill_event.purpose and "LONG_ADD" in fill_event.purpose:
            state["long_add_pending"] = fill_event.status != "FILLED"
            cycle_state["long_add_pending"] = state["long_add_pending"]
            state["cycle_long_add_filled"] = True
            logger.info(
                "[CYCLE-LONG] purpose=%s status=%s long_flag=%s cycle_count=%s",
                fill_event.purpose,
                fill_event.status,
                state.get("cycle_long_add_filled"),
                state.get("cycle_completed_count"),
            )
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
            exec_pnl_increment = self._safe_float((fill_event.metadata or {}).get("exec_pnl"), None)
            runtime_pnl_increment = self._safe_float(
                (fill_event.metadata or {}).get("runtime_calculated_pnl"), None
            )
            fills[str(cycle_index)] = {
                **entry,
                "price": fill_event.exec_price,
                "qty": fill_event.exec_qty,
                "total_qty": total_qty,
                "weighted_price_sum": weighted_price_sum,
                "avg_price": avg_price,
                "client_order_id": fill_event.client_order_id,
                "exec_id": fill_event.exec_id,
                "purpose": fill_event.purpose,
                "symbol": self._active_trade_symbol(snapshot, runtime_state, fill_event=fill_event),
                "confirmed_pnl_applied": bool(entry.get("confirmed_pnl_applied", False)),
                "provisional_exec_pnl_total": float(entry.get("provisional_exec_pnl_total") or 0.0)
                + (float(exec_pnl_increment) if exec_pnl_increment is not None else 0.0),
                "provisional_runtime_pnl_total": float(
                    entry.get("provisional_runtime_pnl_total") or 0.0
                )
                + (float(runtime_pnl_increment) if runtime_pnl_increment is not None else 0.0),
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
                        fill_event=fill_event,
                    )
                    if long_fill.get("confirmed_closed_pnl") is not None:
                        self._cleanup_order_pnl(runtime_state, long_fill.get("client_order_id"))
            confirmed_closed_pnl = long_fill.get("confirmed_closed_pnl")
            if confirmed_closed_pnl is not None:
                metadata = dict(fill_event.metadata or {})
                metadata["long_reduce_closed_pnl"] = confirmed_closed_pnl
                metadata["long_closed_pnl"] = confirmed_closed_pnl
                metadata["closed_pnl"] = confirmed_closed_pnl
                metadata["confirmed_closed_pnl"] = confirmed_closed_pnl
                fill_event.metadata = metadata
                fill_event.confirmed_pnl = confirmed_closed_pnl
                long_add_loss_usdt = max(-float(confirmed_closed_pnl), 0.0)
                long_fill["last_long_add_loss_usdt"] = long_add_loss_usdt
                self._add_realized_long_loss(runtime_state, long_add_loss_usdt)
                _audit_calc(
                    "long_fill_loss_calc",
                    {
                        "purpose": fill_event.purpose,
                        "cycle_index": cycle_index,
                        "confirmed_closed_pnl": confirmed_closed_pnl,
                        "derived_long_loss_usdt": long_add_loss_usdt,
                        "qty": fill_event.exec_qty,
                        "exec_price": fill_event.exec_price,
                    },
                )
                fill_info["confirmed_closed_pnl"] = confirmed_closed_pnl
            if float(cycle_state.get("entry_price") or 0.0) <= 0:
                cycle_state["entry_price"] = fill_event.exec_price

        if "_SHORT_" in fill_event.purpose:
            if "SHORT_TP" in fill_event.purpose or "SHORT_REDUCE" in fill_event.purpose:
                state["cycle_short_tp_filled"] = True
                logger.info(
                    "[CYCLE-SHORT] purpose=%s status=%s short_flag=%s cycle_count=%s",
                    fill_event.purpose,
                    fill_event.status,
                    state.get("cycle_short_tp_filled"),
                    state.get("cycle_completed_count"),
                )
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
            logger.info(
                "[CYCLE-CHECK] long_flag=%s short_flag=%s",
                state.get("cycle_long_add_filled"),
                state.get("cycle_short_tp_filled"),
            )
            self._try_complete_cycle_pair_after_confirmed_pnl(
                runtime_state, cycle_index, fill_event.purpose
            )
        if fill_event.purpose in {
            self.LONG_TP_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
        } and order_fully_completed:
            state["long_exit_filled"] = True
            self._maybe_finalize_exit_after_leg_fill(runtime_state, context, fill_event.purpose)
        if (
            fill_event.purpose == self.SHORT_TP_EXIT_PURPOSE
            and order_fully_completed
        ):
            state["short_exit_filled"] = True
            self._maybe_finalize_exit_after_leg_fill(runtime_state, context, "SHORT_TP_EXIT")
            state["exit_rebuild_allowed"] = True
        if fill_event.purpose in {
            self.SHORT_SL_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        } and order_fully_completed:
            state["short_exit_filled"] = True
            self._maybe_finalize_exit_after_leg_fill(runtime_state, context, fill_event.purpose)
        state["current_effective_cycle"] = max(
            int(state.get("current_short_cycle_index") or 0),
            int(state.get("cycle_completed_count") or 0),
        )

        exit_purposes = {
            self.LONG_TP_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        }
        exit_orders_open = (
            snapshot.has_open_purpose(self.LONG_TP_EXIT_PURPOSE)
            or snapshot.has_open_purpose(self.LONG_TP_EXIT_RECOVERY_PURPOSE)
            or snapshot.has_open_purpose(self.SHORT_SL_EXIT_PURPOSE)
            or snapshot.has_open_purpose(self.SHORT_SL_EXIT_RECOVERY_PURPOSE)
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
            runtime_state.realized_long_pnl_total = 0.0
            runtime_state.realized_short_pnl_total = 0.0
            logger.info(
                "fixed_cycle_full_exit_runtime_pnl_reset %s",
                {
                    "symbol": self.config.symbol,
                    "realized_long_pnl_total": runtime_state.realized_long_pnl_total,
                    "realized_short_pnl_total": runtime_state.realized_short_pnl_total,
                    "fresh_restart_required": True,
                    "pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
                },
            )
            state["fresh_restart_required"] = True
            state["initial_entry_confirmed"] = False
            state["initial_entry_submitted"] = False
            state["initial_entry_retry_count"] = 0
            state["entry_reference_price"] = 0.0
            state["initial_long_qty"] = 0.0
            state["initial_short_qty"] = 0.0
            state["initial_total_notional_usdt"] = 0.0
            state["last_exit_signature"] = None
            state["net_long_loss_balance"] = 0.0
            state["net_short_loss_balance"] = 0.0
            state["pending_cycle_loss_usdt"] = 0.0

        processed.add(fill_key)
        cycle_state["processed_fill_ids"] = list(processed)
        self._write_cycle_state(cycle_state)
        state["last_fill_info"] = fill_info

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
            not self._is_terminal_order_status(order.status) and order.purpose in entry_purposes
            for order in runtime_state.active_orders.values()
        )

    def _finalize_initial_entry_orders_from_snapshot(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> None:
        state = runtime_state.strategy_state
        initial_long_qty = float(state.get("initial_long_qty") or 0.0)
        initial_short_qty = float(state.get("initial_short_qty") or 0.0)
        if initial_long_qty <= 0 or initial_short_qty <= 0:
            return
        long_confirmed = float(snapshot.long_qty) >= initial_long_qty - 1e-9
        short_confirmed = float(snapshot.short_qty) >= initial_short_qty - 1e-9
        if not (long_confirmed and short_confirmed):
            return
        updated_orders: list[str] = []
        for order in runtime_state.active_orders.values():
            if order.purpose == self.LONG_ENTRY_PURPOSE and long_confirmed:
                if self._is_terminal_order_status(order.status):
                    continue
                order.status = "FILLED"
                order.filled_qty = order.qty
                order.remaining_qty = 0.0
                updated_orders.append(order.client_order_id)
            if order.purpose == self.SHORT_ENTRY_PURPOSE and short_confirmed:
                if self._is_terminal_order_status(order.status):
                    continue
                order.status = "FILLED"
                order.filled_qty = order.qty
                order.remaining_qty = 0.0
                updated_orders.append(order.client_order_id)
        if updated_orders:
            _log_event(
                "fixed_cycle_initial_entry_orders_marked_filled",
                {
                    "symbol": self.config.symbol,
                    "initial_long_qty": initial_long_qty,
                    "initial_short_qty": initial_short_qty,
                    "snapshot_long_qty": snapshot.long_qty,
                    "snapshot_short_qty": snapshot.short_qty,
                    "updated_order_ids": updated_orders,
                },
            )

    def _update_initial_entry_confirmation(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
    ) -> bool:
        state = runtime_state.strategy_state
        self._finalize_initial_entry_orders_from_snapshot(snapshot, runtime_state)
        has_open_initial_orders = self._has_open_initial_entry_orders(snapshot, runtime_state)
        confirmed = (
            snapshot.long_qty > 0
            and snapshot.short_qty > 0
            and not has_open_initial_orders
        )
        previously_confirmed = bool(state.get("initial_entry_confirmed"))
        state["initial_entry_confirmed"] = confirmed
        if confirmed:
            state["initial_entry_submitted"] = True
            if not previously_confirmed:
                self._reset_exit_state_for_new_structure(runtime_state, "initial_entry_confirmed")
            if state.get("startup_flat_reset_applied"):
                state["startup_flat_reset_applied"] = False
                _log_event(
                    "fixed_cycle_startup_flat_reset_guard_lifted_after_initial_entry",
                    {
                        "symbol": self.config.symbol,
                        "long_qty": snapshot.long_qty,
                        "short_qty": snapshot.short_qty,
                        "long_avg": snapshot.long_avg,
                        "short_avg": snapshot.short_avg,
                    },
                )
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
        logger.debug(
            "normalize_qty %s",
            {
                "symbol": symbol,
                "input_qty": qty,
                "qty_step_used": str(qty_step),
                "rounded_qty": rounded_float,
                "source": source,
            },
        )
        logger.debug(
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
        logger.debug(
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
        logger.debug(
            "normalize_price_debug %s",
            {"symbol": symbol, "has_rules": source == "instrument_rules"},
        )
        return normalized

    def _resolve_instrument_rules(
        self, runtime_state: RuntimeState | None, *, symbol_override: str | None = None
    ) -> tuple[str, dict[str, Decimal] | None, str]:
        symbol = str(symbol_override or self.config.symbol or "").upper()
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

    def _active_trade_symbol(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState | None,
        *,
        fill_event: FillEvent | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        state = runtime_state.strategy_state if runtime_state is not None else {}
        cycle_state = (state.get("cycle_state") or {}) if isinstance(state, dict) else {}
        fill_metadata = dict(getattr(fill_event, "metadata", {}) or {})
        payload_data = payload or {}
        payload_fill = payload_data.get("fill") or {}
        payload_metadata = payload_fill.get("metadata") or {}
        candidates = [
            fill_metadata.get("symbol"),
            payload_data.get("symbol"),
            payload_fill.get("symbol"),
            payload_metadata.get("symbol"),
            getattr(snapshot, "symbol", None),
            getattr(getattr(runtime_state, "last_snapshot", None), "symbol", None),
            cycle_state.get("symbol"),
            state.get("last_trade_symbol"),
            self.config.symbol,
        ]
        for candidate in candidates:
            text = str(candidate or "").strip().upper()
            if text:
                return text
        return ""

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
            self._emit_throttled_strategy_audit(
                runtime_state,
                context,
                "fixed_cycle_fast_path_skip",
                {
                    "skip_reason": "initial_entry_order_still_open",
                    "open_initial_orders": open_initial_orders,
                },
                signature_key="fast_path_skip",
                interval_ms=120000,
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

    def _make_closed_pnl_signature(self, row: dict[str, Any]) -> str:
        return "|".join([
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            str(row.get("closedSize") or row.get("qty") or ""),
            str(row.get("avgEntryPrice") or ""),
            str(row.get("avgExitPrice") or ""),
            str(row.get("closedPnl") or ""),
            str(row.get("createdTime") or ""),
            str(row.get("updatedTime") or ""),
        ])

    def _expected_bybit_closed_pnl_side(self, long_fill: dict[str, Any]) -> str | None:
        purpose = (long_fill.get("purpose") or "").upper()
        if "LONG" in purpose:
            return "Sell"
        if "SHORT" in purpose:
            return "Buy"
        role = (long_fill.get("cycle_role") or "").lower()
        if role == "long_reduce":
            return "Sell"
        if role == "short_reduce":
            return "Buy"
        return None

    @staticmethod
    def _is_close(a: float | None, b: float | None, tol: float) -> bool:
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= tol
        except Exception:
            return False

    def _select_closed_pnl_match(
        self,
        rows: list[dict[str, Any]],
        *,
        expected_symbol: str,
        expected_side: str | None,
        expected_qty: float,
        expected_fill_price: float,
        start_time_ms: int | None,
        end_time_ms: int | None,
        processed_signatures: set[str],
        qty_tolerance: float,
        price_tolerance: float,
    ) -> tuple[dict[str, Any] | None, str | None, int | None]:
        best = None
        best_score = -1
        best_sig = None
        for row in rows:
            if str(row.get("symbol") or "").upper() != expected_symbol.upper():
                continue
            sig = self._make_closed_pnl_signature(row)
            if sig in processed_signatures:
                continue
            row_side = row.get("side")
            if expected_side and row_side != expected_side:
                continue
            row_qty = row.get("closedSize") or row.get("qty")
            row_price = row.get("avgExitPrice")
            score = 0
            if self._is_close(row_qty, expected_qty, qty_tolerance):
                score += 40
            if self._is_close(row_price, expected_fill_price, price_tolerance):
                score += 40
            row_time = row.get("updatedTime") or row.get("createdTime")
            try:
                row_time_ms = int(row_time) if row_time is not None else None
            except Exception:
                row_time_ms = None
            if row_time_ms and start_time_ms is not None and end_time_ms is not None and start_time_ms <= row_time_ms <= end_time_ms:
                score += 20
            if score > best_score:
                best = row
                best_score = score
                best_sig = sig
        if best_score >= 80:
            return best, best_sig, best_score
        return None, None, None

    def _make_closed_pnl_signature(self, row: dict[str, Any]) -> str:
        parts = [
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            str(row.get("closedSize") or row.get("qty") or ""),
            str(row.get("avgEntryPrice") or ""),
            str(row.get("avgExitPrice") or ""),
            str(row.get("closedPnl") or ""),
            str(row.get("createdTime") or ""),
            str(row.get("updatedTime") or ""),
        ]
        return "|".join(parts)

    def _expected_bybit_closed_pnl_side(self, long_fill: dict[str, Any]) -> str | None:
        purpose = (long_fill.get("purpose") or "").upper()
        if "LONG" in purpose:
            return "Sell"
        if "SHORT" in purpose:
            return "Buy"
        role = (long_fill.get("cycle_role") or "").lower()
        if role == "long_reduce":
            return "Sell"
        if role == "short_reduce":
            return "Buy"
        return None

    @staticmethod
    def _is_close(a: float | None, b: float | None, tol: float) -> bool:
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= tol
        except Exception:
            return False

    def _select_closed_pnl_match(
        self,
        rows: list[dict[str, Any]],
        *,
        expected_symbol: str,
        expected_side: str | None,
        expected_qty: float,
        expected_fill_price: float,
        start_time_ms: int | None,
        end_time_ms: int | None,
        processed_signatures: set[str],
        qty_tolerance: float,
        price_tolerance: float,
    ) -> tuple[dict[str, Any] | None, str | None, int | None]:
        best_row: dict[str, Any] | None = None
        best_score = -1
        best_sig: str | None = None
        for row in rows:
            if str(row.get("symbol") or "").upper() != expected_symbol.upper():
                continue
            sig = self._make_closed_pnl_signature(row)
            if sig in processed_signatures:
                continue
            if expected_side and row.get("side") != expected_side:
                continue
            row_qty = row.get("closedSize") or row.get("qty")
            row_price = row.get("avgExitPrice")
            score = 0
            if self._is_close(row_qty, expected_qty, qty_tolerance):
                score += 40
            if self._is_close(row_price, expected_fill_price, price_tolerance):
                score += 40
            row_time = row.get("updatedTime") or row.get("createdTime")
            try:
                row_time_ms = int(row_time) if row_time is not None else None
            except Exception:
                row_time_ms = None
            if (
                row_time_ms
                and start_time_ms is not None
                and end_time_ms is not None
                and start_time_ms <= row_time_ms <= end_time_ms
            ):
                score += 20
            if score > best_score:
                best_row = row
                best_score = score
                best_sig = sig
        if best_score >= 80:
            return best_row, best_sig, best_score
        return None, None, None

    def _make_closed_pnl_signature(self, row: dict[str, Any]) -> str:
        parts = [
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            str(row.get("closedSize") or row.get("qty") or ""),
            str(row.get("avgEntryPrice") or ""),
            str(row.get("avgExitPrice") or ""),
            str(row.get("closedPnl") or ""),
            str(row.get("createdTime") or ""),
            str(row.get("updatedTime") or ""),
        ]
        return "|".join(parts)

    def _expected_bybit_closed_pnl_side(self, long_fill: dict[str, Any]) -> str | None:
        purpose = (long_fill.get("purpose") or "").upper()
        if "LONG" in purpose:
            return "Sell"
        if "SHORT" in purpose:
            return "Buy"
        cycle_role = (long_fill.get("cycle_role") or "").lower()
        if cycle_role == "long_reduce":
            return "Sell"
        if cycle_role == "short_reduce":
            return "Buy"
        return None

    @staticmethod
    def _is_close(a: float | None, b: float | None, tol: float) -> bool:
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= tol
        except Exception:
            return False

    def _select_closed_pnl_match(
        self,
        rows: list[dict[str, Any]],
        *,
        expected_symbol: str,
        expected_side: str | None,
        expected_qty: float,
        expected_fill_price: float,
        start_time_ms: int | None,
        end_time_ms: int | None,
        processed_signatures: set[str],
        qty_tolerance: float,
        price_tolerance: float,
    ) -> tuple[dict[str, Any] | None, str | None, int | None]:
        best: dict[str, Any] | None = None
        best_score = -1
        best_sig: str | None = None
        for row in rows:
            row_symbol = str(row.get("symbol") or "")
            if row_symbol.upper() != expected_symbol.upper():
                logger.info(
                    "closed_pnl_robust_candidate_rejected",
                    extra={
                        "reason": "symbol_mismatch",
                        "expected_symbol": expected_symbol,
                        "row_symbol": row_symbol,
                        "row_order_id": row.get("orderId"),
                        "row_qty": row.get("closedSize") or row.get("qty"),
                        "row_avg_exit": row.get("avgExitPrice"),
                        "row_closed_pnl": row.get("closedPnl"),
                    },
                )
                continue
            sig = self._make_closed_pnl_signature(row)
            if sig in processed_signatures:
                logger.debug(
                    "closed_pnl_signature_skipped %s",
                    {
                        "signature": sig,
                        "reason": "already_processed",
                        "row_order_id": row.get("orderId"),
                    },
                )
                continue
            row_qty = row.get("closedSize") or row.get("qty")
            row_price = row.get("avgExitPrice")
            row_side = str(row.get("side") or "").strip().lower()
            expected_side_norm = str(expected_side or "").strip().lower()
            if expected_side_norm and row_side != expected_side_norm:
                logger.info(
                    "closed_pnl_robust_candidate_rejected",
                    extra={
                        "reason": "side_mismatch",
                        "expected_side": expected_side,
                        "row_side": row.get("side"),
                        "row_order_id": row.get("orderId"),
                        "row_symbol": row_symbol,
                        "row_qty": row_qty,
                        "row_avg_exit": row_price,
                        "row_closed_pnl": row.get("closedPnl"),
                        "signature": sig,
                    },
                )
                continue
            score = 0
            if self._is_close(row_qty, expected_qty, qty_tolerance):
                score += 40
            if self._is_close(row_price, expected_fill_price, price_tolerance):
                score += 40
            row_time = row.get("updatedTime") or row.get("createdTime")
            try:
                row_time_int = int(row_time) if row_time is not None else None
            except Exception:
                row_time_int = None
            if (
                row_time_int
                and start_time_ms is not None
                and end_time_ms is not None
                and start_time_ms <= row_time_int <= end_time_ms
            ):
                score += 20
            if score > best_score:
                best = row
                best_score = score
                best_sig = sig
                logger.info(
                    "closed_pnl_robust_candidate",
                    extra={
                        "expected_symbol": expected_symbol,
                        "row_order_id": row.get("orderId"),
                        "signature": sig,
                        "score": score,
                        "row_side": row_side,
                        "row_qty": row_qty,
                        "row_avg_exit": row_price,
                        "expected_qty": expected_qty,
                        "expected_fill_price": expected_fill_price,
                        "qty_tolerance": qty_tolerance,
                        "price_tolerance": price_tolerance,
                        "start_time_ms": start_time_ms,
                        "end_time_ms": end_time_ms,
                        "row_time_ms": row_time_int,
                    },
                )
        if best_score >= 80:
            return best, best_sig, best_score
        return None, None, None

    def _refresh_long_fill_closed_pnl(
        self,
        *,
        cycle_index: int,
        long_fill: dict,
        runtime_state: RuntimeState,
        context: StrategyContext,
        occurred_at_ms: int | None = None,
        exec_id: str | None = None,
        fill_event: FillEvent | None = None,
    ) -> bool:
        self._seed_long_fill_closed_pnl_fields(long_fill)
        order_id = str(long_fill.get("order_id") or "").strip()
        fetcher = getattr(context.order_manager, "fetch_closed_pnl", None) if context.order_manager else None
        if not order_id or not callable(fetcher):
            return False
        fetch_symbol = self._active_trade_symbol(
            runtime_state.last_snapshot,
            runtime_state,
            fill_event=fill_event,
            payload=long_fill,
        )

        logger.debug(
            "closed_pnl_fetch_started",
            extra={
                "order_id": order_id,
                "cycle_index": cycle_index,
                "symbol": fetch_symbol,
                "decision": "fetch",
                "closed_pnl": long_fill.get("closed_pnl"),
                "closed_qty": long_fill.get("closed_qty"),
            },
        )
        start_time_ms = max(occurred_at_ms - 300_000, 0) if occurred_at_ms is not None else None
        end_time_ms = (
            (occurred_at_ms + 15 * 60 * 1000) if occurred_at_ms is not None else None
        )
        start_time_iso = (
            datetime.fromtimestamp(start_time_ms / 1000, tz=timezone.utc).isoformat()
            if start_time_ms is not None
            else None
        )
        end_time_iso = (
            datetime.fromtimestamp(end_time_ms / 1000, tz=timezone.utc).isoformat()
            if end_time_ms is not None
            else None
        )
        expected_side = long_fill.get("side") or "long"
        expected_qty = long_fill.get("qty") or long_fill.get("total_qty") or 0.0
        expected_fill_price = long_fill.get("price") or 0.0
        if fetch_symbol and fetch_symbol != str(self.config.symbol or "").upper():
            _log_event(
                "fixed_cycle_closed_pnl_retry_uses_fill_symbol",
                {
                    "expected_order_id": order_id,
                    "cycle_index": cycle_index,
                    "fill_symbol": fetch_symbol,
                    "config_symbol": str(self.config.symbol or "").upper(),
                    "purpose": long_fill.get("purpose"),
                },
            )
        logger.info(
            "closed_pnl_fetch_window",
            extra={
                "expected_order_id": order_id,
                "expected_symbol": fetch_symbol,
                "expected_side": expected_side,
                "expected_qty": expected_qty,
                "expected_fill_price": expected_fill_price,
                "cycle_index": cycle_index,
                "occurred_at_ms": occurred_at_ms,
                "start_time_ms": start_time_ms,
                "end_time_ms": end_time_ms,
                "start_time_iso_utc": start_time_iso,
                "end_time_iso_utc": end_time_iso,
                "source": "strict_order_id_match_current",
            },
        )
        rows = fetcher(
            fetch_symbol,
            self.config.category,
            limit=100,
            start_time_ms=start_time_ms,
        )
        rows_preview = []
        if rows:
            for row in rows[:10]:
                created_time = row.get("createdTime")
                updated_time = row.get("updatedTime")
                try:
                    created_time = int(created_time) if created_time is not None else None
                except Exception:
                    created_time = None
                try:
                    updated_time = int(updated_time) if updated_time is not None else None
                except Exception:
                    updated_time = None
                rows_preview.append(
                    {
                        "orderId": row.get("orderId"),
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "closedSize": row.get("closedSize") or row.get("qty"),
                        "avgEntryPrice": row.get("avgEntryPrice"),
                        "avgExitPrice": row.get("avgExitPrice"),
                        "closedPnl": row.get("closedPnl"),
                        "createdTime": created_time,
                        "updatedTime": updated_time,
                        "createdTime_iso_utc": datetime.fromtimestamp(
                            created_time / 1000, tz=timezone.utc
                        ).isoformat()
                        if created_time
                        else None,
                        "updatedTime_iso_utc": datetime.fromtimestamp(
                            updated_time / 1000, tz=timezone.utc
                        ).isoformat()
                        if updated_time
                        else None,
                    }
                )
        logger.info(
            "closed_pnl_rows_received",
            extra={
                "expected_order_id": order_id,
                "rows_count": len(rows or []),
                "rows_preview": rows_preview,
            },
        )
        if not rows:
            logger.debug(
                "closed_pnl_not_yet_available",
                extra={
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": fetch_symbol,
                    "decision": "deferred",
                    "closed_pnl": None,
                    "closed_qty": None,
                },
            )
            logger.debug(
                "closed_pnl_no_match_debug %s",
                {
                    "expected_order_id": order_id,
                    "expected_symbol": fetch_symbol,
                    "expected_qty": expected_qty,
                    "expected_fill_price": expected_fill_price,
                    "rows_count": 0,
                    "row_order_ids": [],
                    "row_symbols": [],
                    "row_sizes": [],
                    "row_exit_prices": [],
                    "row_closed_pnls": [],
                    "reason": "strict_order_id_symbol_match_failed",
                },
            )
            return False

        cycle_state = runtime_state.strategy_state.setdefault("cycle_state", {})
        processed_signatures = set(cycle_state.get("processed_closed_pnl_signatures") or [])
        symbol, rules, _ = self._resolve_instrument_rules(
            runtime_state,
            symbol_override=fetch_symbol,
        )
        expected_symbol = symbol
        expected_order_id = order_id
        expected_qty = float(
            long_fill.get("total_qty")
            or long_fill.get("qty")
            or long_fill.get("closed_qty")
            or 0.0
        )
        expected_fill_price = float(
            long_fill.get("avg_fill_price")
            or long_fill.get("price")
            or long_fill.get("fill_price")
            or 0.0
        )
        expected_side = self._expected_bybit_closed_pnl_side(long_fill)
        fallback_rules = rules or {}
        qty_step = float(fallback_rules.get("qty_step") or Decimal("0.0001"))
        tick_size_candidate = fallback_rules.get("tick_size")
        tick_size = float(
            tick_size_candidate
            if tick_size_candidate
            else Decimal(str(self.config.price_tick_size or 0.0001))
        )
        qty_tolerance = max(qty_step, expected_qty * 0.001)
        price_tolerance = max(tick_size * 2, expected_fill_price * 0.0005)

        matched = None
        matched_sig = None
        matched_score = None
        match_source = None
        for row in rows:
            if (
                str(row.get("orderId") or "").strip() == expected_order_id
                and str(row.get("symbol") or "").upper() == expected_symbol.upper()
            ):
                matched = row
                matched_sig = self._make_closed_pnl_signature(row)
                match_source = "strict_order_id"
                break
        if not matched:
            logger.info(
                "closed_pnl_strict_match_failed",
                extra={
                    "expected_order_id": expected_order_id,
                    "expected_symbol": expected_symbol,
                    "expected_qty": expected_qty,
                    "expected_fill_price": expected_fill_price,
                },
            )
            matched, matched_sig, matched_score = self._select_closed_pnl_match(
                rows,
                expected_symbol=expected_symbol,
                expected_side=expected_side,
                expected_qty=expected_qty,
                expected_fill_price=expected_fill_price,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                processed_signatures=processed_signatures,
                qty_tolerance=qty_tolerance,
                price_tolerance=price_tolerance,
            )
            if matched and matched_sig:
                match_source = "robust_signature_match"
                logger.debug(
                    "closed_pnl_robust_match_selected %s",
                    {
                        "expected_order_id": expected_order_id,
                        "expected_symbol": expected_symbol,
                        "signature": matched_sig,
                        "score": matched_score,
                    },
                )
        if not matched:
            row_order_ids = [row.get("orderId") for row in rows]
            row_symbols = [row.get("symbol") for row in rows]
            row_sizes = [row.get("closedSize") or row.get("qty") for row in rows]
            row_exit_prices = [row.get("avgExitPrice") for row in rows]
            row_closed_pnls = [row.get("closedPnl") for row in rows]
            logger.debug(
                "closed_pnl_no_match_debug %s",
                {
                    "expected_order_id": expected_order_id,
                    "expected_symbol": expected_symbol,
                    "expected_qty": expected_qty,
                    "expected_fill_price": expected_fill_price,
                    "rows_count": len(rows),
                    "row_order_ids": row_order_ids,
                    "row_symbols": row_symbols,
                    "row_sizes": row_sizes,
                    "row_exit_prices": row_exit_prices,
                    "row_closed_pnls": row_closed_pnls,
                    "reason": "strict_order_id_symbol_match_failed",
                },
            )
            return False

        if not matched_sig:
            matched_sig = self._make_closed_pnl_signature(matched)
        if matched_sig in processed_signatures:
            logger.debug(
                "closed_pnl_signature_skipped %s",
                {
                    "signature": matched_sig,
                    "reason": "already_processed",
                    "match_source": match_source,
                },
            )
            return False
        processed_signatures.add(matched_sig)
        cycle_state["processed_closed_pnl_signatures"] = list(processed_signatures)[-200:]

        if matched.get("closedPnl") is None:
            _log_event(
                "fixed_cycle_closed_pnl_order_found_but_pnl_missing",
                {
                    "order_id": order_id,
                    "cycle_index": cycle_index,
                    "symbol": fetch_symbol,
                    "match_source": match_source,
                    "reason": "closed_pnl_missing",
                    "row_order_id": matched.get("orderId"),
                },
            )
        matched_pnl = float(matched.get("closedPnl") or 0)
        matched_qty = float(matched.get("closedSize") or matched.get("qty") or 0)
        logger.debug(
            "closed_pnl_row_found",
            extra={
                "order_id": order_id,
                "cycle_index": cycle_index,
                "symbol": fetch_symbol,
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
                "symbol": fetch_symbol,
                "decision": "matched",
                "closed_pnl": matched_pnl,
                "closed_qty": matched_qty,
            },
        )

        long_fill["order_id"] = order_id
        long_fill["closed_pnl"] = matched_pnl
        long_fill["closed_qty"] = matched_qty
        long_fill["closed_avg_price"] = self._safe_float(matched.get("avgExitPrice"), None)
        long_fill["closed_cost"] = self._safe_float(matched.get("cumExitValue"), None)
        long_fill["closed_pnl_ready"] = True
        long_fill["closed_pnl_updated_time"] = self._safe_int(
            matched.get("updatedTime") or matched.get("createdTime")
        )
        long_fill["fill_count"] = self._safe_int(matched.get("fillCount"))
        long_fill["confirmed_closed_pnl"] = matched_pnl
        long_fill["confirmed_closed_qty"] = matched_qty
        long_fill["closed_pnl_signature"] = matched_sig
        long_fill["closed_pnl_source"] = match_source or "strict_order_id"
        closed_qty = self._safe_float(matched.get("closedSize") or matched.get("qty"), None)
        closed_avg_price = self._safe_float(matched.get("avgExitPrice") or matched.get("orderPrice"), None)
        closed_cost = self._safe_float(matched.get("cumExitValue"), None)
        if closed_cost is None and closed_qty is not None and closed_avg_price is not None:
            closed_cost = closed_qty * closed_avg_price

        long_fill["order_id"] = order_id
        long_fill["closed_pnl"] = self._safe_float(matched.get("closedPnl"), None)
        long_fill["closed_qty"] = closed_qty
        long_fill["closed_avg_price"] = closed_avg_price
        long_fill["closed_cost"] = closed_cost
        long_fill["closed_pnl_ready"] = long_fill["closed_pnl"] is not None
        long_fill["closed_pnl_updated_time"] = self._safe_int(matched.get("updatedTime") or matched.get("createdTime"))
        long_fill["fill_count"] = self._safe_int(matched.get("fillCount"))
        long_fill["confirmed_closed_pnl"] = long_fill["closed_pnl"]
        long_fill["confirmed_closed_qty"] = closed_qty
        long_fill["confirmed_closed_avg_price"] = closed_avg_price
        long_fill["confirmed_closed_pnl_updated_time"] = long_fill["closed_pnl_updated_time"]
        long_fill["confirmed_pnl_applied"] = long_fill.get("confirmed_pnl_applied", False)
        long_fill["closed_pnl_signature"] = matched_sig
        long_fill["closed_pnl_source"] = match_source or "strict_order_id"
        long_fill["pnl_source"] = long_fill["closed_pnl_source"]
        long_fills = cycle_state.setdefault("long_fills", {})
        long_fills[str(cycle_index)] = long_fill
        self._apply_confirmed_realized_pnl(
            runtime_state=runtime_state,
            client_order_id=long_fill.get("client_order_id"),
            confirmed_pnl=long_fill["confirmed_closed_pnl"],
            side="long",
            exec_id=exec_id or long_fill.get("exec_id"),
            purpose=self._cycle_purpose("long", cycle_index),
            position_idx=1,
            cycle_role="long_reduce",
        )
        if fill_event is not None:
            metadata_applied = self._merge_cycle_closed_pnl_metadata(
                fill_event,
                cycle_index=cycle_index,
                cycle_role="long_reduce",
                confirmed_closed_pnl=long_fill["confirmed_closed_pnl"],
                closed_qty=long_fill["confirmed_closed_qty"],
                closed_avg_price=long_fill["confirmed_closed_avg_price"],
                confirmed_pnl_updated_time=long_fill["confirmed_closed_pnl_updated_time"],
                pnl_source=long_fill["pnl_source"],
            )
            if metadata_applied:
                _log_event(
                    "fixed_cycle_cycle_pnl_refreshed_metadata_applied",
                    {
                        "cycle_index": cycle_index,
                        "client_order_id": long_fill.get("client_order_id"),
                        "exchange_order_id": order_id,
                        "exec_id": exec_id,
                        "confirmed_closed_pnl": long_fill["confirmed_closed_pnl"],
                        "confirmed_closed_qty": long_fill["confirmed_closed_qty"],
                        "confirmed_closed_avg_price": long_fill["confirmed_closed_avg_price"],
                        "pnl_source": long_fill["pnl_source"],
                        "match_source": match_source or "strict_order_id",
                    },
                )
        self._write_confirmed_order_pnl_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": fetch_symbol,
                "exchange_order_id": order_id,
                "client_order_id": long_fill.get("client_order_id"),
                "purpose": (
                    fill_event.purpose
                    if fill_event is not None and fill_event.purpose
                    else self._cycle_purpose("long", cycle_index)
                ),
                "closed_pnl": long_fill.get("confirmed_closed_pnl"),
                "trade_block_id": runtime_state.strategy_state.get("trade_block_id"),
                "cycle_index": cycle_index,
                "pnl_scope": "cycle",
            },
            runtime_state=runtime_state,
        )
        return bool(long_fill["closed_pnl_ready"])

    def _write_confirmed_order_pnl_history(
        self,
        payload: dict[str, Any],
        runtime_state: RuntimeState | None = None,
    ) -> None:
        record = {
            "timestamp": payload.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "symbol": payload.get("symbol") or self.config.symbol,
            "exchange_order_id": payload.get("exchange_order_id"),
            "client_order_id": payload.get("client_order_id"),
            "purpose": payload.get("purpose"),
            "closed_pnl": payload.get("closed_pnl"),
            "source": "bot_confirmed_pnl",
            "trade_block_id": payload.get("trade_block_id"),
            "cycle_index": payload.get("cycle_index"),
            "pnl_scope": payload.get("pnl_scope"),
        }
        exchange_order_id = str(record.get("exchange_order_id") or "").strip()
        purpose = str(record.get("purpose") or "").strip()
        closed_pnl = record.get("closed_pnl")
        if not exchange_order_id or not purpose or closed_pnl is None:
            _log_warning_event(
                "confirmed_order_pnl_missing_required_fields",
                {
                    "exchange_order_id": record.get("exchange_order_id"),
                    "purpose": record.get("purpose"),
                    "closed_pnl": record.get("closed_pnl"),
                    "client_order_id": record.get("client_order_id"),
                    "trade_block_id": record.get("trade_block_id"),
                    "pnl_scope": record.get("pnl_scope"),
                },
            )
            return
        try:
            record["closed_pnl"] = float(closed_pnl)
        except (TypeError, ValueError):
            _log_warning_event(
                "confirmed_order_pnl_missing_required_fields",
                {
                    "exchange_order_id": exchange_order_id,
                    "purpose": purpose,
                    "closed_pnl": closed_pnl,
                    "reason": "closed_pnl_not_numeric",
                },
            )
            return
        record["dedupe_key"] = f"{exchange_order_id}:{purpose}"
        dedupe_key = record["dedupe_key"]
        state = runtime_state.strategy_state if runtime_state is not None else None
        written_keys = set(str(value) for value in ((state or {}).get("processed_confirmed_order_keys") or []))
        if dedupe_key in written_keys:
            _log_event(
                "confirmed_order_pnl_skipped_duplicate",
                {
                    "dedupe_key": dedupe_key,
                    "exchange_order_id": exchange_order_id,
                    "purpose": purpose,
                    "pnl_scope": record.get("pnl_scope"),
                    "duplicate_source": "strategy_state",
                },
            )
            return
        record["bot_name"] = default_bot_name
        path = confirmed_order_pnl_history_path
        try:
            existing_keys: set[str] = set()
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line in lines[-500:]:
                    if not line.strip():
                        continue
                    try:
                        existing_payload = json.loads(line)
                    except Exception:
                        continue
                    existing_key = str(existing_payload.get("dedupe_key") or "").strip()
                    if existing_key:
                        existing_keys.add(existing_key)
            if dedupe_key in existing_keys:
                if state is not None:
                    written_keys.add(dedupe_key)
                    state["processed_confirmed_order_keys"] = sorted(written_keys)[-2000:]
                _log_event(
                    "confirmed_order_pnl_skipped_duplicate",
                    {
                        "dedupe_key": dedupe_key,
                        "exchange_order_id": exchange_order_id,
                        "purpose": purpose,
                        "pnl_scope": record.get("pnl_scope"),
                        "duplicate_source": "history_file",
                    },
                )
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_safe_audit_value(record), ensure_ascii=False) + "\n")
            if state is not None:
                written_keys.add(dedupe_key)
                state["processed_confirmed_order_keys"] = sorted(written_keys)[-2000:]
            _log_event(
                "confirmed_order_pnl_written",
                {
                    "dedupe_key": dedupe_key,
                    "exchange_order_id": exchange_order_id,
                    "client_order_id": record.get("client_order_id"),
                    "purpose": purpose,
                    "closed_pnl": record.get("closed_pnl"),
                    "trade_block_id": record.get("trade_block_id"),
                    "cycle_index": record.get("cycle_index"),
                    "pnl_scope": record.get("pnl_scope"),
                    "path": str(path),
                },
            )
        except Exception as exc:
            _log_warning_event(
                "confirmed_order_pnl_write_failed",
                {
                    "dedupe_key": dedupe_key,
                    "exchange_order_id": exchange_order_id,
                    "client_order_id": record.get("client_order_id"),
                    "purpose": purpose,
                    "pnl_scope": record.get("pnl_scope"),
                    "error": str(exc),
                    "path": str(path),
                },
            )

    def _apply_confirmed_realized_pnl(
        self,
        runtime_state: RuntimeState,
        client_order_id: str | None,
        confirmed_pnl: float | None,
        side: str,
        exec_id: str | None = None,
        purpose: str | None = None,
        position_idx: int | None = None,
        cycle_role: str | None = None,
        short_tp_fallback: bool = False,
    ) -> None:
        if not client_order_id or confirmed_pnl is None:
            return
        if not exec_id and not client_order_id:
            return
        state = runtime_state.strategy_state
        processed_value = state.setdefault("processed_pnl_exec_ids", [])
        processed = set(processed_value)
        order = state.setdefault("processed_pnl_exec_ids_order", [])
        exec_key = exec_id or client_order_id
        if exec_key in processed:
            return
        applied = runtime_state.confirmed_pnl_applied
        if client_order_id in applied:
            return
        purpose_value = str(purpose or client_order_id or "")
        purpose_upper = purpose_value.upper()
        cycle_role_value = str(cycle_role or "").lower()
        is_relevant_cycle_repair_fill = (
            ("CYCLE_" in purpose_upper and "LONG_ADD" in purpose_upper)
            or ("CYCLE_" in purpose_upper and "SHORT_REDUCE" in purpose_upper)
            or ("TRAILING_SHORT_REDUCE" in purpose_upper)
            or (cycle_role_value in {"long_reduce", "short_reduce"})
            or bool(short_tp_fallback)
        )
        is_ignored_fill = any(
            token in purpose_upper
            for token in (
                "INITIAL_LONG_ENTRY",
                "INITIAL_SHORT_ENTRY",
                "LONG_TP_EXIT",
                "SHORT_SL_EXIT",
                "REFILL_LONG",
                "REFILL_SHORT",
            )
        )
        previous_pending = float(state.get("pending_cycle_loss_usdt") or 0.0)
        if is_relevant_cycle_repair_fill and not is_ignored_fill:
            if confirmed_pnl < 0:
                new_pending = previous_pending + abs(confirmed_pnl)
                update_reason = "loss_added"
            elif confirmed_pnl > 0:
                new_pending = max(previous_pending - confirmed_pnl, 0.0)
                update_reason = "profit_deducted"
            else:
                new_pending = previous_pending
                update_reason = "zero_pnl_no_change"
            state["pending_cycle_loss_usdt"] = new_pending
            if new_pending != previous_pending:
                state["exit_rebuild_allowed"] = True
                state["pending_loss_exit_old_signature"] = state.get("last_exit_signature")
                state["pending_loss_exit_rebuild_reason"] = update_reason
                state["last_exit_signature"] = None
                state["force_exit_rebuild"] = True
                state["pending_loss_updated_in_fill"] = True
                _audit_calc(
                    "pending_cycle_loss_update_calc",
                    {
                        "before": previous_pending,
                        "added_loss": new_pending - previous_pending,
                        "after": new_pending,
                        "reason": update_reason,
                        "cycle_index": int(state.get("short_tp_pending_cycle") or 0),
                    },
                )
        else:
            update_reason = "ignored_non_cycle_fill"
        logger.info(
            "pending_cycle_loss_updated %s",
            {
                "purpose": purpose,
                "side": side,
                "position_idx": position_idx,
                "cycle_role": cycle_role,
                "exec_id": exec_id,
                "client_order_id": client_order_id,
                "confirmed_pnl": confirmed_pnl,
                "previous_pending_cycle_loss_usdt": previous_pending,
                "new_pending_cycle_loss_usdt": state.get("pending_cycle_loss_usdt"),
                "update_reason": update_reason,
            },
        )
        logger.info(
            "pending_cycle_loss_exit_rebuild_state",
            {
                "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
                "exit_rebuild_allowed": state.get("exit_rebuild_allowed", True),
                "force_exit_rebuild": bool(state.get("force_exit_rebuild")),
                "pending_loss_updated_in_fill": bool(state.get("pending_loss_updated_in_fill")),
                "last_exit_signature": state.get("last_exit_signature"),
                "update_reason": update_reason,
                "confirmed_pnl": confirmed_pnl,
                "purpose": purpose,
                "exec_id": exec_id,
                "client_order_id": client_order_id,
            },
        )
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
        state["processed_pnl_exec_ids"] = list(processed)
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
        return cycle_state_file_path_override or (Path(__file__).resolve().parent / "state.json")

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

    def _cancel_all_pending_orders(
        self,
        context: StrategyContext,
        snapshot: HedgeSnapshot | None = None,
        runtime_state: RuntimeState | None = None,
    ) -> None:
        canceler = context.cancel_open_orders_by_purpose
        if not canceler:
            return
        cycle_purposes = self._all_cycle_purposes()
        exit_purposes = self._exit_purposes()
        unsettled_orders: list[dict[str, Any]] = []
        if runtime_state is not None:
            unsettled_orders.extend(
                self._strategy_order_summary(order)
                for order in runtime_state.active_orders.values()
                if self._is_unsettled_strategy_order(order) and getattr(order, "purpose", None) in exit_purposes
            )
        if snapshot is not None:
            unsettled_orders.extend(
                self._strategy_order_summary(order)
                for order in snapshot.active_orders
                if self._is_unsettled_strategy_order(order) and getattr(order, "purpose", None) in exit_purposes
            )
        skipped_exit_purposes = sorted(
            {
                str(order.get("purpose") or "")
                for order in unsettled_orders
                if str(order.get("purpose") or "")
            }
        )
        if skipped_exit_purposes:
            context.audit.log_event(
                "fixed_cycle_cancel_pending_skipped_unsettled_final_exit",
                strategy=self.name,
                skipped_exit_purposes=skipped_exit_purposes,
                unsettled_orders=unsettled_orders,
            )
        canceler(cycle_purposes + [purpose for purpose in exit_purposes if purpose not in skipped_exit_purposes])

    def _maybe_finalize_exit_after_leg_fill(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        reason: str,
    ) -> None:
        state = runtime_state.strategy_state
        snapshot = runtime_state.last_snapshot
        long_qty = float(snapshot.long_qty) if snapshot else float(state.get("open_long_qty") or 0.0)
        short_qty = float(snapshot.short_qty) if snapshot else float(state.get("open_short_qty") or 0.0)
        long_done = bool(state.get("long_exit_filled")) or long_qty <= 0
        short_done = bool(state.get("short_exit_filled")) or short_qty <= 0
        logger.info(
            "exit_leg_filled",
            {
                "purpose": reason,
                "long_exit_filled": long_done,
                "short_exit_filled": short_done,
                "long_qty": long_qty,
                "short_qty": short_qty,
            },
        )
        active_order_purposes = sorted(
            {
                str(getattr(order, "purpose", "") or "")
                for order in runtime_state.active_orders.values()
                if not self._is_terminal_order_status(getattr(order, "status", None))
            }
            | {
                str(getattr(order, "purpose", "") or "")
                for order in (snapshot.active_orders if snapshot else [])
                if not self._is_terminal_order_status(getattr(order, "status", None))
            }
        )
        missing_long = reason in {self.SHORT_SL_EXIT_PURPOSE, self.SHORT_SL_EXIT_RECOVERY_PURPOSE} and long_qty > 0 and self.LONG_TP_EXIT_PURPOSE not in active_order_purposes and self.LONG_TP_EXIT_RECOVERY_PURPOSE not in active_order_purposes
        missing_short = reason in {self.LONG_TP_EXIT_PURPOSE, self.LONG_TP_EXIT_RECOVERY_PURPOSE, self.LONG_SL_EXIT_PURPOSE} and short_qty > 0 and self.SHORT_SL_EXIT_PURPOSE not in active_order_purposes and self.SHORT_SL_EXIT_RECOVERY_PURPOSE not in active_order_purposes
        if missing_long:
            logger.warning(
                "final_exit_missing_opposite_long_exit %s",
                {
                    "filled_purpose": reason,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "active_order_purposes": active_order_purposes,
                },
            )
        if missing_short:
            logger.warning(
                "final_exit_missing_opposite_short_exit %s",
                {
                    "filled_purpose": reason,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "active_order_purposes": active_order_purposes,
                },
            )
        missing_triggered = False
        if missing_long or missing_short:
            missing_triggered = self._trigger_emergency_flat_for_remaining_positions(snapshot, runtime_state, context, "final_exit_missing_opposite_exit")
        if long_done and short_done:
            unsettled_runtime_orders, unsettled_snapshot_orders = (
                self._collect_unsettled_strategy_orders(snapshot, runtime_state)
                if snapshot
                else ([], [])
            )
            unsettled_final_exit_orders = [
                order
                for order in unsettled_runtime_orders + unsettled_snapshot_orders
                if str(order.get("purpose") or "").upper()
                in {
                    self.LONG_TP_EXIT_PURPOSE,
                    self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                    self.LONG_SL_EXIT_PURPOSE,
                    self.SHORT_TP_EXIT_PURPOSE,
                    self.SHORT_SL_EXIT_PURPOSE,
                    self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                    self.SHORT_HARD_STOP_PURPOSE,
                }
            ]
            if unsettled_final_exit_orders:
                logger.warning(
                    "final_exit_cleanup_delayed_unsettled_final_orders %s",
                    {
                        "reason": reason,
                        "long_qty": long_qty,
                        "short_qty": short_qty,
                        "unsettled_final_exit_orders": unsettled_final_exit_orders,
                    },
                )
                if context:
                    context.audit.log_event(
                        "final_exit_cleanup_delayed_unsettled_final_orders",
                        strategy=self.name,
                        reason=reason,
                        long_qty=long_qty,
                        short_qty=short_qty,
                        unsettled_final_exit_orders=unsettled_final_exit_orders,
                    )
                self._emit_final_trade_pnl_if_complete_or_fetch(runtime_state, context, reason)
                return
            if not state.get("exit_locked"):
                state["exit_locked"] = True
                state["exit_rebuild_allowed"] = False
                logger.info(
                    "final_exit_completed_both_legs",
                    {
                        "reason": reason,
                        "long_qty": long_qty,
                        "short_qty": short_qty,
                    },
                )
                if context:
                    self._cancel_all_orders_after_exit(runtime_state, context)
                    self._ensure_post_exit_cleanup_required(
                        runtime_state, reason="final_exit_completed_both_legs"
                    )
                    logger.info(
                        "exit_cancel_all_after_both_legs_only",
                        {
                            "reason": reason,
                            "long_qty": long_qty,
                            "short_qty": short_qty,
                        },
                    )
            self._emit_final_trade_pnl_if_complete_or_fetch(runtime_state, context, reason)
            return
        waiting_side = "short" if not short_done else "long"
        if not missing_triggered:
            logger.info(
                "final_exit_waiting_other_leg",
                {
                    "waiting_for": waiting_side,
                    "reason": reason,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                },
            )

    def _ensure_final_exit_pnl_from_exchange(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        snapshot = runtime_state.last_snapshot
        long_qty = float(snapshot.long_qty) if snapshot else float(state.get("open_long_qty") or 0.0)
        short_qty = float(snapshot.short_qty) if snapshot else float(state.get("open_short_qty") or 0.0)
        if long_qty > 0 or short_qty > 0:
            return bool(state.get("final_long_exit_audited") and state.get("final_short_exit_audited"))
        fetcher = getattr(context.order_manager, "fetch_closed_pnl", None) if context and context.order_manager else None
        if not callable(fetcher):
            return bool(state.get("final_long_exit_audited") and state.get("final_short_exit_audited"))

        ledger = state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        processed_signatures = set(state.get("final_exit_closed_pnl_signatures") or [])
        _, rules, _ = self._resolve_instrument_rules(runtime_state)
        qty_step = float(rules.get("qty_step") or Decimal("0.0001")) if rules else 0.0001
        tick_size = (
            float(rules.get("tick_size"))
            if rules and rules.get("tick_size")
            else float(self.config.price_tick_size or 0.0001)
        )

        def _parse_occurred_at_ms(raw: Any) -> int | None:
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)

        def _fetch_for_side(
            *,
            state_key: str,
            ledger_key: str,
            audited_key: str,
            expected_side: str,
        ) -> None:
            if state.get(audited_key):
                return
            order_context = state.get(state_key) or {}
            _log_event(
                "fixed_cycle_final_exit_pnl_fetch_started",
                {
                    "reason": reason,
                    "state_key": state_key,
                    "symbol": order_context.get("symbol") or (snapshot.symbol if snapshot else self.config.symbol),
                    "exchange_order_id": order_context.get("exchange_order_id"),
                    "client_order_id": order_context.get("client_order_id"),
                },
            )
            if not order_context:
                _log_event(
                    "fixed_cycle_final_exit_pnl_fetch_missing",
                    {"reason": reason, "state_key": state_key, "missing": "order_context"},
                )
                return
            expected_symbol = str(order_context.get("symbol") or (snapshot.symbol if snapshot else self.config.symbol) or "")
            expected_order_id = str(order_context.get("exchange_order_id") or "").strip()
            expected_client_order_id = str(order_context.get("client_order_id") or "").strip()
            expected_qty = float(self._safe_float(order_context.get("exec_qty"), 0.0) or 0.0)
            expected_fill_price = float(self._safe_float(order_context.get("exec_price"), 0.0) or 0.0)
            occurred_at_ms = _parse_occurred_at_ms(order_context.get("occurred_at"))
            start_time_ms = max(occurred_at_ms - 300_000, 0) if occurred_at_ms is not None else None
            end_time_ms = occurred_at_ms + 900_000 if occurred_at_ms is not None else None
            rows = fetcher(
                expected_symbol or self.config.symbol,
                self.config.category,
                limit=100,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
            ) or []
            matched = None
            matched_sig = None
            match_source = None
            for row in rows:
                row_order_id = str(row.get("orderId") or "").strip()
                row_link_id = str(row.get("orderLinkId") or "").strip()
                if expected_order_id and row_order_id == expected_order_id:
                    matched = row
                    matched_sig = self._make_closed_pnl_signature(row)
                    match_source = "orderId"
                    break
                if expected_client_order_id and row_link_id == expected_client_order_id:
                    matched = row
                    matched_sig = self._make_closed_pnl_signature(row)
                    match_source = "orderLinkId"
                    break
            if not matched:
                qty_tolerance = max(qty_step, expected_qty * 0.001, 1e-9)
                price_tolerance = max(tick_size * 2, expected_fill_price * 0.0005, 1e-9)
                matched, matched_sig, _ = self._select_closed_pnl_match(
                    rows,
                    expected_symbol=expected_symbol or self.config.symbol,
                    expected_side=expected_side,
                    expected_qty=expected_qty,
                    expected_fill_price=expected_fill_price,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                    processed_signatures=processed_signatures,
                    qty_tolerance=qty_tolerance,
                    price_tolerance=price_tolerance,
                )
                if matched:
                    match_source = "fallback_symbol_qty_price_side"
            closed_pnl = self._safe_float((matched or {}).get("closedPnl"), None)
            if matched and closed_pnl is not None:
                ledger[ledger_key] = float(closed_pnl)
                state[audited_key] = True
                if matched_sig:
                    processed_signatures.add(matched_sig)
                    state["final_exit_closed_pnl_signatures"] = list(processed_signatures)
                _log_event(
                    "fixed_cycle_final_exit_pnl_fetch_matched",
                    {
                        "reason": reason,
                        "state_key": state_key,
                        "ledger_key": ledger_key,
                        "match_source": match_source,
                        "exchange_order_id": expected_order_id,
                        "client_order_id": expected_client_order_id,
                        "matched_order_id": matched.get("orderId"),
                        "matched_order_link_id": matched.get("orderLinkId"),
                        "closed_pnl": float(closed_pnl),
                    },
                )
                self._write_confirmed_order_pnl_history(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "symbol": expected_symbol or self.config.symbol,
                        "exchange_order_id": expected_order_id or matched.get("orderId"),
                        "client_order_id": expected_client_order_id or matched.get("orderLinkId"),
                        "purpose": (
                            "LONG_TP_EXIT"
                            if state_key == "final_long_exit_order_context"
                            else "SHORT_SL_EXIT"
                        ),
                        "closed_pnl": float(closed_pnl),
                        "trade_block_id": runtime_state.strategy_state.get("trade_block_id"),
                        "cycle_index": None,
                        "pnl_scope": "final_exit",
                    },
                    runtime_state=runtime_state,
                )
                return
            _log_event(
                "fixed_cycle_final_exit_pnl_fetch_missing",
                {
                    "reason": reason,
                    "state_key": state_key,
                    "exchange_order_id": expected_order_id,
                    "client_order_id": expected_client_order_id,
                    "rows_count": len(rows),
                },
            )

        _fetch_for_side(
            state_key="final_long_exit_order_context",
            ledger_key="final_long_exit_pnl",
            audited_key="final_long_exit_audited",
            expected_side="Sell",
        )
        _fetch_for_side(
            state_key="final_short_exit_order_context",
            ledger_key="final_short_exit_pnl",
            audited_key="final_short_exit_audited",
            expected_side="Buy",
        )
        return bool(state.get("final_long_exit_audited") and state.get("final_short_exit_audited"))

    def _side_realized_by_cycle_pnl(
        self,
        runtime_state: RuntimeState,
        *,
        side: str,
    ) -> tuple[bool, float, str]:
        state = runtime_state.strategy_state
        snapshot = runtime_state.last_snapshot
        ledger = state.get("audit_pnl_ledger") or {}
        if side == "long":
            remaining_qty = (
                float(snapshot.long_qty)
                if snapshot is not None
                else float(state.get("open_long_qty") or 0.0)
            )
            cycle_total = sum(
                float(value or 0.0)
                for value in (ledger.get("cycle_long_reduce_pnl") or {}).values()
            )
            runtime_realized_total = float(runtime_state.realized_long_pnl_total or 0.0)
            confirmed_source = "cycle_long_reduce_confirmed_pnl"
            fallback_source = "runtime_realized_long_pnl_fallback"
        elif side == "short":
            remaining_qty = (
                float(snapshot.short_qty)
                if snapshot is not None
                else float(state.get("open_short_qty") or 0.0)
            )
            cycle_total = sum(
                float(value or 0.0)
                for value in (ledger.get("cycle_short_tp_pnl") or {}).values()
            )
            runtime_realized_total = float(runtime_state.realized_short_pnl_total or 0.0)
            confirmed_source = "cycle_short_tp_confirmed_pnl"
            fallback_source = "runtime_realized_short_pnl_fallback"
        else:
            raise ValueError(f"unsupported side for cycle pnl audit: {side}")

        if remaining_qty > 0:
            return False, 0.0, ""
        if abs(cycle_total) > 1e-12:
            return True, cycle_total, confirmed_source
        if abs(runtime_realized_total) > 1e-12:
            return True, runtime_realized_total, fallback_source
        return False, 0.0, ""

    def _emit_final_trade_pnl_if_complete_or_fetch(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        current_trade_block_id = state.get("trade_block_id")
        last_trade_block_id = state.get("last_trade_block_id")
        if (
            state.get("final_trade_pnl_audited")
            and bool(state.get("last_trade_pnl_complete"))
            and state.get("last_trade_pnl_usdt") is not None
            and current_trade_block_id
            and last_trade_block_id == current_trade_block_id
        ):
            return True
        if (
            state.get("final_trade_pnl_audited")
            and bool(state.get("last_trade_pnl_complete"))
            and state.get("last_trade_pnl_usdt") is not None
            and current_trade_block_id
            and last_trade_block_id != current_trade_block_id
        ):
            _log_event(
                "fixed_cycle_stale_final_pnl_state_ignored",
                {
                    "reason": reason,
                    "trade_block_id": current_trade_block_id,
                    "last_trade_block_id": last_trade_block_id,
                    "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
                },
            )
        ledger = state.get("audit_pnl_ledger") or {}
        final_long_exit_pnl = ledger.get("final_long_exit_pnl")
        final_short_exit_pnl = ledger.get("final_short_exit_pnl")
        if final_long_exit_pnl is None or final_short_exit_pnl is None:
            self._ensure_final_exit_pnl_from_exchange(runtime_state, context, reason)
            ledger = state.get("audit_pnl_ledger") or {}
            final_long_exit_pnl = ledger.get("final_long_exit_pnl")
            final_short_exit_pnl = ledger.get("final_short_exit_pnl")
        if final_long_exit_pnl is None or final_short_exit_pnl is None:
            return False
        cycle_long_reduce_entries = dict(ledger.get("cycle_long_reduce_pnl") or {})
        cycle_short_tp_entries = dict(ledger.get("cycle_short_tp_pnl") or {})
        cycle_long_reduce_pnl = sum(float(value or 0.0) for value in cycle_long_reduce_entries.values())
        cycle_short_tp_pnl = sum(float(value or 0.0) for value in cycle_short_tp_entries.values())
        long_side_satisfied, long_side_pnl, long_side_source = self._side_realized_by_cycle_pnl(
            runtime_state,
            side="long",
        )
        short_side_satisfied, short_side_pnl, short_side_source = self._side_realized_by_cycle_pnl(
            runtime_state,
            side="short",
        )
        cycle_net_pnl = cycle_long_reduce_pnl + cycle_short_tp_pnl
        final_exit_net_pnl = final_long_exit_pnl + final_short_exit_pnl
        total_trade_pnl = cycle_net_pnl + final_exit_net_pnl
        realized_long_pnl = float(runtime_state.realized_long_pnl_total or 0.0)
        realized_short_pnl = float(runtime_state.realized_short_pnl_total or 0.0)
        trade_block_id = state.get("trade_block_id") or str(uuid4())
        state["trade_block_id"] = trade_block_id
        source = "bybit_closed_pnl"
        finalized_at = datetime.now(timezone.utc).isoformat()
        breakdown = {
            "realized_long_pnl_total": realized_long_pnl,
            "realized_short_pnl_total": realized_short_pnl,
            "cycle_long_reduce_pnl": cycle_long_reduce_entries,
            "cycle_short_tp_pnl": cycle_short_tp_entries,
            "cycle_long_reduce_pnl_total": cycle_long_reduce_pnl,
            "cycle_short_tp_pnl_total": cycle_short_tp_pnl,
            "cycle_net_pnl": cycle_net_pnl,
            "final_long_exit_pnl": final_long_exit_pnl,
            "final_short_exit_pnl": final_short_exit_pnl,
            "final_exit_net_pnl": final_exit_net_pnl,
            "total_trade_pnl": total_trade_pnl,
            "source": source,
            "pnl_complete": True,
            "finalized_at": finalized_at,
            "long_side_satisfied": long_side_satisfied,
            "long_side_pnl": long_side_pnl,
            "long_side_source": long_side_source,
            "short_side_satisfied": short_side_satisfied,
            "short_side_pnl": short_side_pnl,
            "short_side_source": short_side_source,
        }
        if (
            abs(realized_long_pnl) > 1e-12 or abs(realized_short_pnl) > 1e-12
        ) and not cycle_long_reduce_entries and not cycle_short_tp_entries:
            _log_event(
                "fixed_cycle_runtime_realized_pnl_not_counted_as_cycle_pnl",
                {
                    "reason": reason,
                    "realized_long_pnl_total": realized_long_pnl,
                    "realized_short_pnl_total": realized_short_pnl,
                    "cycle_long_reduce_entries": cycle_long_reduce_entries,
                    "cycle_short_tp_entries": cycle_short_tp_entries,
                    "final_long_exit_pnl": final_long_exit_pnl,
                    "final_short_exit_pnl": final_short_exit_pnl,
                    "final_exit_net_pnl": final_exit_net_pnl,
                    "total_trade_pnl": total_trade_pnl,
                    "explanation": "runtime realized PnL is not counted as cycle PnL to avoid double counting confirmed final exits",
                },
            )
        _log_event(
            "fixed_cycle_final_exit_pnl_confirmed",
            {
                "reason": reason,
                "trade_block_id": trade_block_id,
                "final_long_exit_pnl": final_long_exit_pnl,
                "final_short_exit_pnl": final_short_exit_pnl,
                "final_long_exit_order_context_present": bool(state.get("final_long_exit_order_context")),
                "final_short_exit_order_context_present": bool(state.get("final_short_exit_order_context")),
                "cycle_net_pnl": cycle_net_pnl,
                "source": source,
            },
        )
        self._persist_last_trade_pnl_summary(
            runtime_state,
            total_trade_pnl=total_trade_pnl,
            breakdown=breakdown,
            source=source,
            pnl_complete=True,
            trade_block_id=trade_block_id,
            finalized_at=finalized_at,
        )
        payload_persist = {
            "symbol": self.config.symbol,
            "trade_block_id": trade_block_id,
            "total_trade_pnl": total_trade_pnl,
            "source": source,
            "pnl_complete": True,
            "finalized_at": finalized_at,
            "breakdown": breakdown,
        }
        _log_event("fixed_cycle_last_trade_pnl_persisted", payload_persist)
        payload_finalized = {
            "symbol": self.config.symbol,
            "trade_block_id": trade_block_id,
            "cycle_long_reduce_pnl_total": cycle_long_reduce_pnl,
            "cycle_short_tp_pnl_total": cycle_short_tp_pnl,
            "cycle_net_pnl": cycle_net_pnl,
            "final_long_exit_pnl": final_long_exit_pnl,
            "final_short_exit_pnl": final_short_exit_pnl,
            "final_exit_net_pnl": final_exit_net_pnl,
            "total_trade_pnl": total_trade_pnl,
            "realized_long_pnl_total": realized_long_pnl,
            "realized_short_pnl_total": realized_short_pnl,
            "source": source,
            "pnl_complete": True,
            "finalized_at": finalized_at,
            "reason": reason,
        }
        _log_event("fixed_cycle_trade_pnl_finalized", payload_finalized)
        state["final_trade_pnl_audited"] = True
        state["current_trade_pnl_state_reset_for_entry"] = False
        self._ensure_post_exit_cleanup_required(runtime_state, reason=reason)
        return True

    def _load_best_coin_symbol_from_file(self, path: str | Path) -> Optional[dict[str, Any]]:
        file_path = Path(path)
        if not file_path.exists():
            logger.debug("best_coin_file_missing", {"path": str(file_path)})
            return None
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8") or "{}")
        except Exception as exc:
            _log_event(
                "best_coin_file_invalid",
                {"path": str(file_path), "error": str(exc)},
            )
            return None
        if not isinstance(payload, dict):
            logger.info(
                "best_coin_file_invalid",
                {"path": str(file_path), "error": "payload not object"},
            )
            return None
        symbol = payload.get("symbol")
        score = payload.get("score")
        timestamp_raw = payload.get("timestamp")
        reason = payload.get("reason")
        if not symbol or not isinstance(symbol, str):
            logger.info(
                "best_coin_file_invalid",
                {"path": str(file_path), "error": "missing symbol"},
            )
            return None
        if not timestamp_raw or not isinstance(timestamp_raw, str):
            logger.info(
                "best_coin_file_invalid",
                {"path": str(file_path), "error": "missing timestamp"},
            )
            return None
        try:
            timestamp = datetime.fromisoformat(timestamp_raw)
        except ValueError as exc:
            logger.info(
                "best_coin_file_invalid",
                {"path": str(file_path), "error": str(exc)},
            )
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - timestamp).total_seconds() / 60
        max_age = float(self.config.best_coin_max_age_minutes or 0) or 0
        if max_age and age_minutes > max_age:
            _log_event(
                "best_coin_file_stale",
                {
                    "path": str(file_path),
                    "symbol": symbol,
                    "score": score,
                    "age_minutes": round(age_minutes, 1),
                },
            )
            return None
        _log_event(
            "best_coin_file_loaded",
            {
                "path": str(file_path),
                "symbol": symbol,
                "score": score,
                "timestamp": timestamp_raw,
                "reason": reason,
            },
        )
        return {
            "symbol": symbol,
            "score": score,
            "timestamp": timestamp,
            "age_minutes": age_minutes,
            "reason": reason,
        }

    def _trigger_restart_script_after_full_exit(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        desired_symbol: str,
        active_snapshot_order_purposes: list[str],
        active_runtime_order_purposes: list[str],
        *,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        if state.get("restart_requested_after_full_exit"):
            return False
        current_symbol = str(self.config.symbol or "").upper()
        snapshot_symbol = str(snapshot.symbol or "").upper()
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "restart_fixed_cycle.sh"
        payload = {
            "current_config_symbol": current_symbol,
            "snapshot_symbol": snapshot_symbol,
            "desired_symbol": desired_symbol,
            "reason": reason,
            "long_qty": snapshot.long_qty,
            "short_qty": snapshot.short_qty,
            "active_snapshot_order_purposes": active_snapshot_order_purposes,
            "active_runtime_order_purposes": active_runtime_order_purposes,
            "restart_script": str(script_path),
        }
        state["pending_dynamic_symbol"] = desired_symbol
        state["dynamic_symbol_restart_required"] = True
        state["restart_requested_after_full_exit"] = desired_symbol
        _log_event("dynamic_symbol_restart_script_requested", payload)
        if not script_path.exists():
            _log_warning_event(
                "dynamic_symbol_restart_script_missing",
                {**payload, "message": "restart script missing"},
            )
            return False
        try:
            subprocess.Popen([str(script_path)], cwd=str(script_path.parent.parent))
            _log_event(
                "dynamic_symbol_restart_script_spawned",
                {**payload, "message": "restart script spawn requested"},
            )
            return True
        except Exception as exc:
            _log_warning_event(
                "dynamic_symbol_restart_script_failed",
                {**payload, "error": str(exc)},
            )
            return False

    def _compute_next_dynamic_scan_ready_at(self, now: datetime) -> datetime:
        base = now.replace(second=0, microsecond=0)
        minute = base.minute
        if minute < 30:
            slot = base.replace(minute=30)
        else:
            slot = (base + timedelta(hours=1)).replace(minute=0)
        return slot + timedelta(minutes=3)

    def _maybe_start_dynamic_symbol_hold_after_flat(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        reason: str,
    ) -> None:
        state = runtime_state.strategy_state
        if not self.config.dynamic_symbol_enabled:
            return
        if state.get("dynamic_entry_hold_initialized"):
            return
        if snapshot.long_qty > 0 or snapshot.short_qty > 0:
            return
        if snapshot.active_orders:
            return
        active_runtime_orders = [
            order
            for order in runtime_state.active_orders.values()
            if order.status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
        ]
        if active_runtime_orders:
            return
        now = datetime.now(timezone.utc)
        next_ready = self._compute_next_dynamic_scan_ready_at(now)
        minutes_until_ready = (next_ready - now).total_seconds() / 60
        hold_minutes = float(self.config.dynamic_symbol_hold_minutes or 0) or 0
        state["last_trade_closed_at"] = now.isoformat()
        state["dynamic_entry_hold_initialized"] = True
        if minutes_until_ready <= hold_minutes:
            state["next_dynamic_entry_allowed_at"] = next_ready.isoformat()
            _log_event(
                "dynamic_entry_hold_started",
                {
                    "reason": reason,
                    "now": now.isoformat(),
                    "next_dynamic_entry_allowed_at": next_ready.isoformat(),
                    "minutes_until_ready": round(minutes_until_ready, 2),
                    "dynamic_symbol_hold_minutes": hold_minutes,
                },
            )
        else:
            state["next_dynamic_entry_allowed_at"] = now.isoformat()
            _log_event(
                "dynamic_entry_no_hold_needed",
                {
                    "reason": reason,
                    "now": now.isoformat(),
                    "next_dynamic_entry_allowed_at": now.isoformat(),
                    "next_dynamic_scan_ready_at": next_ready.isoformat(),
                    "minutes_until_ready": round(minutes_until_ready, 2),
                    "dynamic_symbol_hold_minutes": hold_minutes,
                },
            )

    def _dynamic_symbol_entry_gate_allows_entry(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext,
        reason: str,
    ) -> bool:
        if not self.config.dynamic_symbol_enabled:
            return True
        state = runtime_state.strategy_state
        allowed_at_raw = state.get("next_dynamic_entry_allowed_at")
        if not allowed_at_raw:
            return True
        try:
            allowed_at = datetime.fromisoformat(allowed_at_raw)
        except ValueError:
            _log_warning_event(
                "dynamic_entry_allowed_at_invalid",
                {"value": allowed_at_raw},
            )
            return True
        if allowed_at.tzinfo is None:
            allowed_at = allowed_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds_remaining = (allowed_at - now).total_seconds()
        if now < allowed_at:
            _log_event(
                "dynamic_entry_waiting_for_next_scan_result",
                {
                    "reason": reason,
                    "now": now.isoformat(),
                    "next_dynamic_entry_allowed_at": allowed_at.isoformat(),
                    "seconds_remaining": max(0.0, round(seconds_remaining, 2)),
                },
            )
            return False
        _log_event(
            "dynamic_entry_allowed_after_hold",
            {
                "reason": reason,
                "now": now.isoformat(),
                "next_dynamic_entry_allowed_at": allowed_at.isoformat(),
                "seconds_past_due": max(0.0, round(-seconds_remaining, 2)),
            },
        )
        return True

    def _maybe_update_symbol_from_best_coin(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        reason: str,
    ) -> None:
        if not self.config.dynamic_symbol_enabled:
            return
        best_coin = self._load_best_coin_symbol_from_file(
            self.config.best_coin_file or "logs/best_coin.json"
        )
        if not best_coin:
            _log_event(
                "dynamic_symbol_selection_skipped",
                {
                    "reason": reason,
                    "symbol": self.config.symbol,
                },
            )
            return
        desired_symbol = best_coin["symbol"].upper()
        current_symbol = str(self.config.symbol or "").upper()
        if desired_symbol == current_symbol:
            return
        state = runtime_state.strategy_state
        snapshot = runtime_state.last_snapshot
        cycle_state = state.get("cycle_state") or {}
        snapshot_active_order_count = len(getattr(snapshot, "active_orders", []) or [])
        active_orders = [
            order
            for order in runtime_state.active_orders.values()
            if not self._is_terminal_order_status(getattr(order, "status", None))
        ]
        pending_closed_pnl_count = len(state.get("pending_cycle_closed_pnl_fills") or [])
        long_qty = float(snapshot.long_qty or 0.0) if snapshot else float(state.get("open_long_qty") or 0.0)
        short_qty = float(snapshot.short_qty or 0.0) if snapshot else float(state.get("open_short_qty") or 0.0)
        trade_active = bool(state.get("trade_active"))
        cycle_trade_active = bool(cycle_state.get("trade_active"))
        final_exit_pending = bool(
            state.get("final_long_exit_order_context") or state.get("final_short_exit_order_context")
        )
        block_reasons: list[str] = []
        if long_qty > 0:
            block_reasons.append("long_qty_open")
        if short_qty > 0:
            block_reasons.append("short_qty_open")
        if trade_active:
            block_reasons.append("trade_active")
        if cycle_trade_active:
            block_reasons.append("cycle_trade_active")
        if active_orders:
            block_reasons.append("active_orders_present")
        if snapshot_active_order_count > 0:
            block_reasons.append("snapshot_active_orders_present")
        if pending_closed_pnl_count > 0:
            block_reasons.append("pending_closed_pnl_fills")
        if state.get("refill_pending"):
            block_reasons.append("refill_pending")
        if state.get("refill_in_progress"):
            block_reasons.append("refill_in_progress")
        if final_exit_pending:
            block_reasons.append("final_exit_context_present")
        if state.get("cycle_waiting_for_short_tp"):
            block_reasons.append("cycle_waiting_for_short_tp")
        if state.get("long_add_pending"):
            block_reasons.append("long_add_pending")
        if int(state.get("short_tp_pending_cycle") or 0) > 0:
            block_reasons.append("short_tp_pending_cycle")
        if block_reasons:
            _log_event(
                "fixed_cycle_dynamic_symbol_switch_blocked_active_trade",
                {
                    "current_symbol": current_symbol,
                    "desired_symbol": desired_symbol,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "active_order_count": len(active_orders),
                    "snapshot_active_order_count": snapshot_active_order_count,
                    "trade_active": trade_active,
                    "cycle_trade_active": cycle_trade_active,
                    "pending_closed_pnl_count": pending_closed_pnl_count,
                    "refill_pending": bool(state.get("refill_pending")),
                    "refill_in_progress": bool(state.get("refill_in_progress")),
                    "reason": ",".join(block_reasons),
                },
            )
            return
        _log_event(
            "fixed_cycle_dynamic_symbol_switch_allowed_flat",
            {
                "current_symbol": current_symbol,
                "desired_symbol": desired_symbol,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "active_order_count": len(active_orders),
                "snapshot_active_order_count": snapshot_active_order_count,
                "pending_closed_pnl_count": pending_closed_pnl_count,
                "reason": reason,
            },
        )
        self.config.symbol = desired_symbol
        _log_event(
            "dynamic_symbol_selected_for_fresh_entry",
            {
                "reason": reason,
                "new_symbol": desired_symbol,
                "score": best_coin.get("score"),
            },
        )

    def _reset_exit_state_for_new_structure(
        self,
        runtime_state: RuntimeState,
        reason: str,
    ) -> None:
        state = runtime_state.strategy_state
        snapshot = runtime_state.last_snapshot
        long_qty = float(snapshot.long_qty) if snapshot else float(state.get("open_long_qty") or 0.0)
        short_qty = float(snapshot.short_qty) if snapshot else float(state.get("open_short_qty") or 0.0)
        state["exit_locked"] = False
        state["long_exit_filled"] = False
        state["short_exit_filled"] = False
        state["short_exit_recovery_submitted"] = False
        state["long_exit_recovery_submitted"] = False
        state["exit_recovery_marker"] = False
        state["force_exit_rebuild"] = True
        logger.info(
            "exit_state_reset_for_new_structure",
            {
                "reason": reason,
                "long_qty": long_qty,
                "short_qty": short_qty,
            },
        )

    def _reset_current_trade_pnl_state(self, runtime_state: RuntimeState, *, reason: str) -> None:
        state = runtime_state.strategy_state
        previous_trade_block_id = state.get("trade_block_id")
        keys_to_reset_or_pop = [
            "final_trade_pnl_audited",
            "final_long_exit_audited",
            "final_short_exit_audited",
            "final_long_exit_order_context",
            "final_short_exit_order_context",
            "final_exit_closed_pnl_signatures",
            "audit_processed_exit_fill_ids",
            "audit_completed_cycle_indices",
            "processed_pnl_exec_ids",
            "processed_pnl_exec_ids_order",
            "flat_waiting_order_cleanup_logged",
            "flat_waiting_final_pnl_logged",
            "flat_final_pnl_ready_logged",
            "final_pnl_context_missing_logged",
            "restart_delayed_pending_final_pnl_logged",
        ]

        for key in keys_to_reset_or_pop:
            state.pop(key, None)

        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "cycle_pnl_entries": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        runtime_state.realized_long_pnl_total = 0.0
        runtime_state.realized_short_pnl_total = 0.0
        state["trade_block_id"] = str(uuid4())
        _log_event(
            "fixed_cycle_current_trade_pnl_state_reset",
            {
                "reason": reason,
                "previous_trade_block_id": previous_trade_block_id,
                "trade_block_id": state.get("trade_block_id"),
                "last_trade_block_id": state.get("last_trade_block_id"),
            },
        )

    def _cancel_all_orders_after_exit(
        self,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> None:
        order_manager = context.order_manager
        symbol = context.symbol or self.config.symbol
        category = context.category or self.config.category
        canceled = False
        if order_manager and symbol:
            try:
                canceled = order_manager.cancel_all_orders(symbol=symbol, category=category)
            except Exception as exc:
                logger.warning(
                    "exit_cancel_all_failed",
                    {
                        "symbol": symbol,
                        "category": category,
                        "error": str(exc),
                    },
                )
        self._purge_active_orders(runtime_state, self._all_cycle_purposes() + self._exit_purposes())
        _audit_calc(
            "exit_cancel_all",
            {
                "symbol": symbol,
                "category": category,
                "cancel_success": canceled,
            },
        )

    def _purge_active_orders(
        self,
        runtime_state: RuntimeState,
        purposes: list[str],
    ) -> None:
        removed = 0
        skipped = 0
        exit_purposes = set(self._exit_purposes())
        for client_id, order in list(runtime_state.active_orders.items()):
            if order.purpose not in purposes:
                continue
            if order.purpose in exit_purposes and self._is_unsettled_strategy_order(order):
                skipped += 1
                logger.info(
                    "purge_active_orders_skipped_unsettled_exit %s",
                    {
                        "client_order_id": client_id,
                        "exchange_order_id": order.exchange_order_id,
                        "purpose": order.purpose,
                        "status": order.status,
                        "filled_qty": order.filled_qty,
                        "remaining_qty": order.remaining_qty,
                    },
                )
                continue
            runtime_state.active_orders.pop(client_id, None)
            removed += 1
            if order.exchange_order_id:
                runtime_state.exchange_to_client_id.pop(order.exchange_order_id, None)
        logger.info(
            "purged_active_orders",
            {
                "removed": removed,
                "skipped": skipped,
                "purposes": purposes,
            },
        )

    def _is_fixed_cycle_exchange_order(self, order: dict[str, Any] | None) -> bool:
        if not order:
            return False
        for key in ("orderLinkId", "order_link_id", "clientOrderId", "client_order_id"):
            value = str(order.get(key) or "").strip()
            if value.startswith("fixed_cycle-"):
                return True
        return False

    def _refresh_short_reduce_closed_pnl(
        self,
        *,
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: StrategyContext,
        cycle_index: int,
    ) -> bool:
        metadata = fill_event.metadata or {}
        processed_key = "processed_closed_pnl_signatures"
        cycle_state = self._ensure_cycle_state(runtime_state)
        processed_signatures = set(cycle_state.get(processed_key) or [])

        order_id = str(
            (fill_event.exchange_order_id or fill_event.client_order_id or "").strip()
        )
        fetcher = getattr(context.order_manager, "fetch_closed_pnl", None) if context.order_manager else None
        if not order_id or not callable(fetcher):
            return False
        fetch_symbol = self._active_trade_symbol(runtime_state.last_snapshot, runtime_state, fill_event=fill_event)

        occurred_at_ms = (
            int(fill_event.occurred_at.timestamp() * 1000)
            if getattr(fill_event, "occurred_at", None)
            else None
        )
        start_time_ms = max(0, occurred_at_ms - 300_000) if occurred_at_ms is not None else None
        end_time_ms = (
            occurred_at_ms + 900_000 if occurred_at_ms is not None else None
        )
        if fetch_symbol and fetch_symbol != str(self.config.symbol or "").upper():
            _log_event(
                "fixed_cycle_closed_pnl_retry_uses_fill_symbol",
                {
                    "expected_order_id": order_id,
                    "cycle_index": cycle_index,
                    "fill_symbol": fetch_symbol,
                    "config_symbol": str(self.config.symbol or "").upper(),
                    "purpose": fill_event.purpose,
                },
            )
        rows = fetcher(
            fetch_symbol,
            self.config.category,
            limit=100,
            start_time_ms=start_time_ms,
        ) or []

        if not rows:
            return False

        symbol, rules, _ = self._resolve_instrument_rules(
            runtime_state,
            symbol_override=fetch_symbol,
        )
        expected_qty = float(fill_event.exec_qty or 0.0)
        expected_price = float(fill_event.exec_price or 0.0)
        expected_side = self._expected_bybit_closed_pnl_side(
            {
                "purpose": fill_event.purpose,
                "cycle_role": metadata.get("cycle_role", "short_reduce"),
            }
        )
        qty_step = float(rules.get("qty_step") or Decimal("0.0001")) if rules else float(Decimal("0.0001"))
        tick_size = float(
            rules.get("tick_size")
            if rules and rules.get("tick_size")
            else Decimal(str(self.config.price_tick_size or 0.0001))
        )
        qty_tolerance = max(qty_step, expected_qty * 0.001)
        price_tolerance = max(tick_size * 2, expected_price * 0.0005)

        matched = None
        matched_sig = None
        match_source = None
        for row in rows:
            if (
                str(row.get("orderId") or "").strip() == order_id
                and str(row.get("symbol") or "").upper() == symbol.upper()
            ):
                matched = row
                matched_sig = self._make_closed_pnl_signature(row)
                match_source = "strict_order_id"
                break
        if not matched:
            matched, matched_sig, score = self._select_closed_pnl_match(
                rows,
                expected_symbol=symbol,
                expected_side=expected_side,
                expected_qty=expected_qty,
                expected_fill_price=expected_price,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                processed_signatures=processed_signatures,
                qty_tolerance=qty_tolerance,
                price_tolerance=price_tolerance,
            )
            if matched and matched_sig:
                match_source = "robust_signature_match"
        if not matched:
            return False
        if matched_sig in processed_signatures:
            return False
        processed_signatures.add(matched_sig)
        cycle_state[processed_key] = list(processed_signatures)[-200:]

        closed_pnl = self._safe_float(matched.get("closedPnl"), None)
        if closed_pnl is None:
            return False
        metadata["short_reduce_closed_pnl"] = closed_pnl
        metadata["short_closed_pnl"] = closed_pnl
        metadata["closed_pnl"] = closed_pnl
        metadata["confirmed_closed_pnl"] = closed_pnl
        metadata["closed_pnl_updated_time"] = self._safe_int(
            matched.get("updatedTime") or matched.get("createdTime")
        )
        metadata["closed_pnl_source"] = match_source or "closed_pnl"
        metadata["cycle_index"] = int(metadata.get("cycle_index") or cycle_index)
        metadata.setdefault("cycle_role", "short_reduce")
        fill_event.metadata = metadata
        fill_event.confirmed_pnl = closed_pnl

        _log_event(
            "fixed_cycle_short_reduce_pnl_confirmed",
            {
                "symbol": fetch_symbol,
                "cycle_index": cycle_index,
                "order_id": order_id,
                "closed_pnl": closed_pnl,
                "match_source": match_source,
            },
        )
        self._write_confirmed_order_pnl_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": fetch_symbol,
                "exchange_order_id": matched.get("orderId") or fill_event.exchange_order_id or order_id,
                "client_order_id": fill_event.client_order_id,
                "purpose": fill_event.purpose,
                "closed_pnl": closed_pnl,
                "trade_block_id": runtime_state.strategy_state.get("trade_block_id"),
                "cycle_index": cycle_index,
                "pnl_scope": "cycle",
            },
            runtime_state=runtime_state,
        )
        _log_event(
            "closed_pnl_row_matched",
            {
                "order_id": order_id,
                "cycle_index": cycle_index,
                "symbol": fetch_symbol,
                "closed_pnl": closed_pnl,
                "match_source": match_source,
            },
        )
        return True

    def _ensure_post_exit_cleanup_required(
        self, runtime_state: RuntimeState, *, reason: str | None = None
    ) -> None:
        state = runtime_state.strategy_state
        now_iso = datetime.now(timezone.utc).isoformat()
        state["post_exit_cleanup_required"] = True
        state["post_exit_cleanup_verified"] = False
        state["post_exit_cleanup_in_progress"] = False
        state["post_exit_cleanup_attempts"] = 0
        state["post_exit_cleanup_started_at"] = now_iso
        state["post_exit_cleanup_verified_at"] = None
        _log_event(
            "fixed_cycle_post_exit_cleanup_required",
            {
                "symbol": self.config.symbol,
                "reason": reason or "final_exit",
                "trade_block_id": state.get("trade_block_id"),
                "last_trade_block_id": state.get("last_trade_block_id"),
            },
        )

    def _post_exit_cleanup_pending(self, runtime_state: RuntimeState) -> bool:
        state = runtime_state.strategy_state
        return bool(state.get("post_exit_cleanup_required") and not state.get("post_exit_cleanup_verified"))

    def _attempt_post_exit_cleanup(
        self, snapshot: HedgeSnapshot, runtime_state: RuntimeState, context: StrategyContext
    ) -> bool:
        state = runtime_state.strategy_state
        if not self._post_exit_cleanup_pending(runtime_state):
            return True
        state["post_exit_cleanup_attempts"] = int(state.get("post_exit_cleanup_attempts") or 0) + 1
        state["post_exit_cleanup_in_progress"] = True
        if not state.get("post_exit_cleanup_started_at"):
            state["post_exit_cleanup_started_at"] = datetime.now(timezone.utc).isoformat()

        symbol = context.symbol or self.config.symbol
        category = context.category or self.config.category
        cancel_success = False
        order_manager = context.order_manager

        if order_manager and symbol:
            try:
                cancel_success = order_manager.cancel_all_orders(symbol=symbol, category=category)
            except Exception as exc:
                logger.warning(
                    "post_exit_cleanup_cancel_failed",
                    {
                        "symbol": symbol,
                        "category": category,
                        "error": str(exc),
                    },
                )
        _log_event(
            "fixed_cycle_post_exit_cleanup_cancel_requested",
            {
                "symbol": symbol,
                "category": category,
                "attempt": state["post_exit_cleanup_attempts"],
                "cancel_success": cancel_success,
            },
        )

        open_orders: list[dict[str, Any]] = []
        fetch_failed = False
        if order_manager and symbol:
            try:
                open_orders = order_manager.fetch_open_orders(symbol=symbol, category=category) or []
            except Exception as exc:
                fetch_failed = True
                logger.warning(
                    "post_exit_cleanup_fetch_failed",
                    {
                        "symbol": symbol,
                        "category": category,
                        "error": str(exc),
                    },
                )
        else:
            fetch_failed = True
        if fetch_failed:
            _log_event(
                "fixed_cycle_post_exit_cleanup_failed",
                {
                    "symbol": symbol,
                    "category": category,
                    "attempt": state["post_exit_cleanup_attempts"],
                    "reason": "fetch_failed",
                },
            )

        remaining_fixed_cycle_orders = [
            order for order in open_orders if self._is_fixed_cycle_exchange_order(order)
        ]
        snapshot_purposes, runtime_purposes = self._collect_active_strategy_order_purposes(
            snapshot, runtime_state
        )
        long_qty = float(snapshot.long_qty or 0.0)
        short_qty = float(snapshot.short_qty or 0.0)

        clean_snapshot = (
            not snapshot_purposes
            and not runtime_purposes
            and long_qty == 0.0
            and short_qty == 0.0
        )
        clean_rest = not remaining_fixed_cycle_orders and clean_snapshot and not fetch_failed
        state["post_exit_cleanup_in_progress"] = False
        refreshed_snapshot = None
        if clean_rest and callable(context.refresh_snapshot):
            try:
                refreshed_snapshot = context.refresh_snapshot("post_exit_cleanup")
            except Exception as exc:
                logger.warning(
                    "post_exit_cleanup_snapshot_refresh_failed",
                    {
                        "symbol": symbol,
                        "category": category,
                        "error": str(exc),
                    },
                )
        snapshot_to_check = refreshed_snapshot or snapshot
        runtime_state.last_snapshot = snapshot_to_check
        snapshot_purposes, runtime_purposes = self._collect_active_strategy_order_purposes(
            snapshot_to_check, runtime_state
        )
        long_qty = float(snapshot_to_check.long_qty or 0.0)
        short_qty = float(snapshot_to_check.short_qty or 0.0)

        clean_snapshot = (
            not snapshot_purposes
            and not runtime_purposes
            and long_qty == 0.0
            and short_qty == 0.0
        )
        if clean_rest and clean_snapshot:
            now_iso = datetime.now(timezone.utc).isoformat()
            state["post_exit_cleanup_verified"] = True
            state["post_exit_cleanup_required"] = False
            state["post_exit_cleanup_verified_at"] = now_iso
            state["post_exit_cleanup_verified_snapshot_updated_at"] = (
                snapshot_to_check.updated_at.isoformat()
                if snapshot_to_check and snapshot_to_check.updated_at
                else now_iso
            )
            _log_event(
                "fixed_cycle_post_exit_cleanup_verified",
                {
                    "symbol": symbol,
                    "category": category,
                    "attempt": state["post_exit_cleanup_attempts"],
                },
            )
            return True

        wait_payload = {
            "symbol": symbol,
            "category": category,
            "attempt": state["post_exit_cleanup_attempts"],
            "remaining_exchange_orders": len(remaining_fixed_cycle_orders),
            "snapshot_orders": len(snapshot_purposes),
            "runtime_orders": len(runtime_purposes),
            "long_qty": long_qty,
            "short_qty": short_qty,
        }
        _log_event("fixed_cycle_post_exit_cleanup_waiting", wait_payload)
        if remaining_fixed_cycle_orders and state["post_exit_cleanup_attempts"] >= POST_EXIT_CLEANUP_MAX_ATTEMPTS:
            _log_event(
                "fixed_cycle_post_exit_cleanup_failed",
                {
                    "symbol": symbol,
                    "category": category,
                    "attempt": state["post_exit_cleanup_attempts"],
                    "remaining_exchange_orders": len(remaining_fixed_cycle_orders),
                },
            )
        return False

    def _ensure_post_exit_cleanup_ready_for_fresh_restart(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        reason: str,
    ) -> HedgeSnapshot | None:
        if not self._post_exit_cleanup_pending(runtime_state):
            return snapshot
        success = self._attempt_post_exit_cleanup(snapshot, runtime_state, context)
        if not success:
            _log_event(
                "fixed_cycle_fresh_restart_blocked_post_exit_cleanup_pending",
                {
                    "symbol": self.config.symbol,
                    "reason": reason,
                    "trade_block_id": runtime_state.strategy_state.get("trade_block_id"),
                    "last_trade_block_id": runtime_state.strategy_state.get("last_trade_block_id"),
                },
            )
            return None
        refreshed = runtime_state.last_snapshot or snapshot
        if (
            (refreshed.long_qty or 0.0) > 0
            or (refreshed.short_qty or 0.0) > 0
            or bool(refreshed.active_orders)
            or bool(runtime_state.active_orders)
        ):
            _log_event(
                "fixed_cycle_fresh_restart_blocked_post_exit_cleanup_not_clean",
                {
                    "symbol": self.config.symbol,
                    "reason": reason,
                    "snapshot_long_qty": refreshed.long_qty,
                    "snapshot_short_qty": refreshed.short_qty,
                    "snapshot_active_orders": len(refreshed.active_orders or ()),
                    "runtime_active_orders": len(runtime_state.active_orders),
                },
            )
            return None
        return refreshed

    def _reset_cycle_state(self, runtime_state: RuntimeState) -> dict:
        state = runtime_state.strategy_state
        preserved_last_trade = self._preserve_last_trade_pnl_fields(state)
        cycle_state = self._default_cycle_state()
        cycle_state["trade_active"] = False
        cycle_state["long_add_pending"] = False
        cycle_state["cycle_waiting_for_short_tp"] = False
        cycle_state["short_tp_pending_cycle"] = 0
        cycle_state["pending_loss_exit_old_signature"] = None
        cycle_state["pending_loss_exit_rebuild_reason"] = None
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
        state["fresh_restart_required"] = False
        state["entry_reference_price"] = None
        state["last_exit_signature"] = None
        state["current_effective_cycle"] = 0
        cycle_state["entry_price"] = None
        state["entry_reference_price"] = None
        state["last_exit_signature"] = None
        cycle_state["entry_price"] = None
        state["net_long_loss_balance"] = 0.0
        state["net_short_loss_balance"] = 0.0
        state["pending_cycle_loss_usdt"] = 0.0
        state["realized_long_loss_total"] = 0.0
        self.realized_long_loss_total = 0.0
        state["processed_pnl_exec_ids"] = []
        state["processed_pnl_exec_ids_order"] = []
        state["long_exit_filled"] = False
        state["short_exit_filled"] = False
        state["short_exit_recovery_submitted"] = False
        state["long_exit_recovery_submitted"] = False
        state["exit_recovery_marker"] = False
        state["exit_locked"] = False
        state.pop("flat_waiting_order_cleanup_logged", None)
        state.pop("flat_waiting_final_pnl_logged", None)
        state.pop("flat_final_pnl_ready_logged", None)
        state.pop("final_pnl_context_missing_logged", None)
        self._write_cycle_state(cycle_state)
        self._restore_last_trade_pnl_fields(state, preserved_last_trade)
        return cycle_state

    def _preserve_last_trade_pnl_fields(self, strategy_state: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in strategy_state.items() if key.startswith("last_trade_")}

    def _restore_last_trade_pnl_fields(self, strategy_state: dict[str, Any], preserved: dict[str, Any]) -> None:
        for key, value in preserved.items():
            strategy_state[key] = value

    def _persist_last_trade_pnl_summary(
        self,
        runtime_state: RuntimeState,
        *,
        total_trade_pnl: float,
        breakdown: dict[str, Any],
        source: str,
        pnl_complete: bool,
        trade_block_id: str,
        finalized_at: str,
    ) -> None:
        state = runtime_state.strategy_state
        state["last_trade_pnl_usdt"] = total_trade_pnl
        state["last_trade_pnl_finalized_at"] = finalized_at
        state["last_trade_symbol"] = self.config.symbol
        state["last_trade_block_id"] = trade_block_id
        state["last_trade_pnl_source"] = source
        state["last_trade_pnl_complete"] = pnl_complete
        state["last_trade_pnl_breakdown"] = breakdown
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
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.LONG_SL_EXIT_PURPOSE,
            self.SHORT_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        ]

    @staticmethod
    def _safe_recovery_qty(value: Decimal | float | str | int | None) -> float:
        if value in (None, ""):
            return 0.0
        try:
            return float(Decimal(str(value)))
        except Exception:
            return 0.0

    def _recover_purpose_from_order_update(self, payload: dict[str, Any]) -> str:
        candidates = [
            payload.get("orderLinkId"),
            payload.get("order_link_id"),
            payload.get("clientOrderId"),
            payload.get("client_order_id"),
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            upper = text.upper()
            if "REFILL_LONG" in upper:
                return "REFILL_LONG"
            if "REFILL_SHORT" in upper:
                return "REFILL_SHORT"
            if "SHORT_SL_EXIT_RECOVERY" in upper:
                return self.SHORT_SL_EXIT_RECOVERY_PURPOSE
            if "LONG_TP_EXIT_RECOVERY" in upper:
                return self.LONG_TP_EXIT_RECOVERY_PURPOSE
            if "SHORT_SL_EXIT" in upper:
                return self.SHORT_SL_EXIT_PURPOSE
            if "LONG_TP_EXIT" in upper:
                return self.LONG_TP_EXIT_PURPOSE
        return ""

    def _build_force_exit_recovery_intent(
        self,
        *,
        side: str,
        qty: Decimal | float | str,
        symbol: str,
        purpose: str,
        reason: str,
        source_order_id: str | None = None,
        source_order_link_id: str | None = None,
    ) -> StrategyIntent:
        runtime_side = "short" if str(side or "").strip().lower() == "short" else "long"
        position_idx = 2 if runtime_side == "short" else 1
        normalized_qty = self._safe_recovery_qty(qty)
        return StrategyIntent(
            side=runtime_side,
            qty=normalized_qty,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
            close_on_trigger=True,
            position_idx=position_idx,
            metadata={
                "recovery_force": True,
                "reason": reason,
                "symbol": symbol,
                "source_order_id": source_order_id,
                "source_order_link_id": source_order_link_id,
                "original_exit_cancelled": True,
                "position_idx": position_idx,
            },
        )

    def _emergency_exit_signature(self, payload: dict[str, Any]) -> str | None:
        for key in ("orderId", "orderLinkId", "order_link_id", "clientOrderId", "client_order_id"):
            candidate = str(payload.get(key) or "").strip()
            if candidate:
                return candidate.upper()
        return None

    def _emergency_exit_purposes(self) -> set[str]:
        return {
            self.LONG_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
            self.SHORT_HARD_STOP_PURPOSE,
            "FINAL_LONG_EXIT",
            "FINAL_SHORT_EXIT",
        }

    def _should_trigger_emergency_exit(
        self,
        state: dict[str, Any],
        purpose: str | None,
        status: str,
        reason_upper: str,
        payload: dict[str, Any],
    ) -> bool:
        if not purpose:
            return False
        if purpose not in self._emergency_exit_purposes():
            return False
        reason_matches = reason_upper == self.EMERGENCY_REJECT_REASON
        if status not in self.EMERGENCY_EXIT_STATUSES and not reason_matches:
            return False
        if state.get("emergency_flat_required"):
            existing_signature = state.get("emergency_exit_signature")
            current_signature = self._emergency_exit_signature(payload)
            if current_signature and current_signature == existing_signature:
                return False
        return True

    def _expected_exit_cancel_purposes(self) -> set[str]:
        return {
            self.LONG_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
            "FINAL_LONG_EXIT",
            "FINAL_SHORT_EXIT",
        }

    def _consume_expected_exit_cancel(
        self,
        runtime_state: RuntimeState,
        *,
        purpose: str | None,
        source_order_id: str | None,
        source_order_link_id: str | None,
        status: str,
    ) -> bool:
        if purpose not in self._expected_exit_cancel_purposes():
            return False
        state = runtime_state.strategy_state
        registry = state.get("expected_exit_cancels")
        if not isinstance(registry, list) or not registry:
            return False
        now = time.monotonic()
        matched = False
        new_registry: list[dict[str, Any]] = []
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            expires_at = float(entry.get("expires_at_monotonic") or 0.0)
            if expires_at and expires_at < now:
                continue
            same_exchange = bool(source_order_id) and entry.get("exchange_order_id") == source_order_id
            same_client = bool(source_order_link_id) and entry.get("client_order_id") == source_order_link_id
            if (
                not matched
                and entry.get("purpose") == purpose
                and (same_exchange or same_client)
            ):
                entry = dict(entry)
                entry["consumed"] = True
                entry["consumed_at_monotonic"] = now
                matched = True
                _log_event(
                    "fixed_cycle_emergency_exit_suppressed_expected_cancel",
                    {
                        "symbol": self.config.symbol,
                        "purpose": purpose,
                        "status": status,
                        "client_order_id": entry.get("client_order_id"),
                        "exchange_order_id": entry.get("exchange_order_id"),
                        "replacement_purpose": entry.get("replacement_purpose"),
                    },
                )
            new_registry.append(entry)
        state["expected_exit_cancels"] = new_registry
        return matched

    def _check_expected_cancel_replacement_timeouts(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent] | None:
        state = runtime_state.strategy_state
        registry = state.get("expected_exit_cancels")
        if not isinstance(registry, list) or not registry:
            return None
        now = time.monotonic()
        active_replacement_purposes = {
            str(getattr(order, "purpose", "") or "")
            for order in runtime_state.active_orders.values()
            if not self._is_terminal_order_status(getattr(order, "status", None))
        } | {
            str(getattr(order, "purpose", "") or "")
            for order in snapshot.active_orders
            if not self._is_terminal_order_status(getattr(order, "status", None))
        }
        new_registry: list[dict[str, Any]] = []
        timeout_triggered = False
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            expires_at = float(entry.get("expires_at_monotonic") or 0.0)
            replacement_purpose = str(entry.get("replacement_purpose") or "")
            if replacement_purpose and replacement_purpose in active_replacement_purposes:
                continue
            if entry.get("consumed") and expires_at and expires_at < now:
                _log_warning_event(
                    "fixed_cycle_expected_cancel_replacement_missing",
                    {
                        "symbol": self.config.symbol,
                        "purpose": entry.get("purpose"),
                        "client_order_id": entry.get("client_order_id"),
                        "exchange_order_id": entry.get("exchange_order_id"),
                        "replacement_purpose": replacement_purpose,
                    },
                )
                timeout_triggered = True
                continue
            if expires_at and expires_at < now and not entry.get("consumed"):
                continue
            new_registry.append(entry)
        state["expected_exit_cancels"] = new_registry
        if not timeout_triggered:
            return None
        if not self._trigger_emergency_flat_for_remaining_positions(
            snapshot,
            runtime_state,
            context,
            "expected_cancel_replacement_missing",
        ):
            if runtime_state.strategy_state.get("emergency_flat_required"):
                return None
            return []
        return self._maybe_handle_emergency_exit_tick(snapshot, runtime_state, context)

    def _resolve_emergency_snapshot(self, snapshot: HedgeSnapshot, context: StrategyContext | None) -> HedgeSnapshot:
        if snapshot and (
            (snapshot.long_qty or 0.0) > 0
            or (snapshot.short_qty or 0.0) > 0
            or (snapshot.active_orders or ())
        ):
            return snapshot
        if context and context.refresh_snapshot:
            try:
                return context.refresh_snapshot("emergency_exit")
            except Exception:
                pass
        return snapshot

    def _clear_emergency_state(self, runtime_state: RuntimeState) -> None:
        for key in (
            "emergency_flat_required",
            "emergency_exit_signature",
            "emergency_exit_reason",
            "emergency_exit_attempts",
            "emergency_exit_verify_attempts",
        ):
            runtime_state.strategy_state.pop(key, None)

    def _build_emergency_close_intents(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        *,
        reason: str,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        intents: list[StrategyIntent] = []
        long_qty = float(snapshot.long_qty or 0.0)
        short_qty = float(snapshot.short_qty or 0.0)
        if long_qty <= 0 and short_qty <= 0:
            return []
        trigger_ts = state.get("emergency_trigger_monotonic")
        latency_ms = (
            max(0, int((time.monotonic() - trigger_ts) * 1000))
            if trigger_ts and trigger_ts > 0
            else None
        )
        previous_attempts = int(state.get("emergency_exit_attempts") or 0)
        attempt = previous_attempts + 1
        state["emergency_exit_attempts"] = attempt
        normalized_long = self._safe_recovery_qty(long_qty)
        normalized_short = self._safe_recovery_qty(short_qty)
        for side, qty, purpose, position_idx in (
            ("long", normalized_long, self.EMERGENCY_FLAT_LONG_PURPOSE, 1),
            ("short", normalized_short, self.EMERGENCY_FLAT_SHORT_PURPOSE, 2),
        ):
            if qty <= 0:
                continue
            intent = StrategyIntent(
                side=side,
                qty=qty,
                purpose=purpose,
                order_type="Market",
                reduce_only=True,
                close_on_trigger=False,
                position_idx=position_idx,
                metadata={
                    "emergency_reason": reason,
                    "emergency_attempt": attempt,
                    "emergency_signature": state.get("emergency_exit_signature"),
                },
            )
            intents.append(intent)
            _log_event(
                "fixed_cycle_emergency_exit_close_submitted",
                {
                    "symbol": self.config.symbol,
                    "side": side,
                    "qty": qty,
                    "position_idx": position_idx,
                    "purpose": purpose,
                    "attempt": attempt,
                    "reason": reason,
                    **(
                        {"emergency_latency_ms": latency_ms}
                        if latency_ms is not None
                        else {}
                    ),
                },
            )
        return intents

    def _trigger_emergency_exit(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        payload: dict[str, Any],
        reason: str,
        *,
        allow_repeat: bool = False,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        snapshot = self._resolve_emergency_snapshot(snapshot, context)
        signature = self._emergency_exit_signature(payload) or state.get("emergency_exit_signature")
        if signature:
            state["emergency_exit_signature"] = signature
        self._record_emergency_trigger(runtime_state)
        state["emergency_exit_reason"] = reason
        state["emergency_flat_required"] = True
        state["emergency_exit_verify_attempts"] = 0
        guard_payload = {
            "symbol": self.config.symbol,
            "long_qty": snapshot.long_qty,
            "short_qty": snapshot.short_qty,
            "current_price": snapshot.current_price,
            "signature": signature,
            "reason": reason,
            "status": payload.get("orderStatus"),
        }
        _log_warning_event("fixed_cycle_emergency_exit_guard_triggered", guard_payload)
        _log_event(
            "fixed_cycle_emergency_exit_snapshot",
            {
                **guard_payload,
                "active_orders": len(snapshot.active_orders or ()),
            },
        )
        intents = self._build_emergency_close_intents(snapshot, runtime_state, reason=reason)
        if not intents:
            _log_event(
                "fixed_cycle_emergency_exit_completed",
                {
                    "symbol": self.config.symbol,
                    "reason": reason,
                    "long_qty": snapshot.long_qty,
                    "short_qty": snapshot.short_qty,
                },
            )
            self._clear_emergency_state(runtime_state)
        return intents

    def _maybe_handle_emergency_exit_tick(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent] | None:
        state = runtime_state.strategy_state
        if not state.get("emergency_flat_required"):
            return None
        long_qty = float(snapshot.long_qty or 0.0)
        short_qty = float(snapshot.short_qty or 0.0)
        active_orders = [
            order
            for order in runtime_state.active_orders.values()
            if getattr(order, "status", None) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
        ]
        emergency_orders = [
            order
            for order in active_orders
            if getattr(order, "purpose", None) in {self.EMERGENCY_FLAT_LONG_PURPOSE, self.EMERGENCY_FLAT_SHORT_PURPOSE}
        ]
        verify_attempts = int(state.get("emergency_exit_verify_attempts") or 0) + 1
        state["emergency_exit_verify_attempts"] = verify_attempts
        _log_event(
            "fixed_cycle_emergency_exit_verify_flat",
            {
                "symbol": self.config.symbol,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "active_orders": len(active_orders),
                "emergency_orders": len(emergency_orders),
                "attempt": verify_attempts,
                "reason": state.get("emergency_exit_reason"),
            },
        )
        if long_qty <= 0 and short_qty <= 0 and not active_orders:
            _log_event(
                "fixed_cycle_emergency_exit_completed",
                {
                    "symbol": self.config.symbol,
                    "attempts": state.get("emergency_exit_attempts"),
                    "reason": state.get("emergency_exit_reason"),
                },
            )
            self._clear_emergency_state(runtime_state)
            return []
        if emergency_orders:
            return []
        if verify_attempts <= self.EMERGENCY_EXIT_MAX_RETRIES:
            intents = self._build_emergency_close_intents(
                snapshot,
                runtime_state,
                reason=str(state.get("emergency_exit_reason") or ""),
            )
            if intents:
                return intents
            return []
        _log_warning_event(
            "fixed_cycle_emergency_exit_failed",
            {
                "symbol": self.config.symbol,
                "reason": state.get("emergency_exit_reason"),
                "long_qty": long_qty,
                "short_qty": short_qty,
                "verify_attempts": verify_attempts,
            },
        )
        return []

    def _record_emergency_trigger(self, runtime_state: RuntimeState) -> None:
        runtime_state.strategy_state["emergency_trigger_monotonic"] = time.monotonic()

    def _trigger_emergency_flat_for_remaining_positions(
        self,
        snapshot: HedgeSnapshot | None,
        runtime_state: RuntimeState,
        context: StrategyContext | None,
        reason: str,
    ) -> bool:
        state = runtime_state.strategy_state
        if state.get("emergency_flat_required"):
            return False
        emerg_snapshot = snapshot or runtime_state.last_snapshot
        long_qty = float(getattr(emerg_snapshot, "long_qty", 0.0) or 0.0)
        short_qty = float(getattr(emerg_snapshot, "short_qty", 0.0) or 0.0)
        if long_qty <= 0 and short_qty <= 0:
            return False
        self._record_emergency_trigger(runtime_state)
        guard_payload = {
            "symbol": self.config.symbol,
            "reason": reason,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "active_orders": len(getattr(emerg_snapshot, "active_orders", ()) or ()),
        }
        _log_warning_event("fixed_cycle_emergency_exit_guard_triggered_missing_or_incomplete_final_exit", guard_payload)
        _log_event(
            "fixed_cycle_emergency_exit_remaining_position_detected",
            {
                **guard_payload,
                "snapshot_source": getattr(emerg_snapshot, "source", None),
            },
        )
        state["emergency_flat_required"] = True
        state["emergency_exit_reason"] = reason
        state["emergency_exit_signature"] = f"missing-{reason}"
        state["emergency_exit_attempts"] = 0
        return True

    def on_order_update(
        self,
        payload,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        payload_data = payload if isinstance(payload, dict) else {}
        source_order_id = str(payload_data.get("orderId") or "").strip()
        purpose = self._recover_purpose_from_order_update(payload_data)
        if not purpose:
            purpose = str(payload_data.get("purpose") or "").strip().upper()
        if not purpose and source_order_id:
            for order in runtime_state.active_orders.values():
                if str(getattr(order, "exchange_order_id", "") or "").strip() == source_order_id:
                    purpose = str(getattr(order, "purpose", "") or "").strip().upper()
                    break
        status = str(payload_data.get("orderStatus") or "").upper()
        reject_reason = str(payload_data.get("rejectReason") or "").strip()
        reason_upper = reject_reason.upper()
        source_order_link_id = (
            str(payload_data.get("orderLinkId") or "").strip()
            or str(payload_data.get("order_link_id") or "").strip()
            or str(payload_data.get("clientOrderId") or "").strip()
            or str(payload_data.get("client_order_id") or "").strip()
            or str(payload_data.get("order_link_id") or "").strip()
            or ""
        )
        metadata_link = payload_data.get("metadata") or {}
        if not source_order_link_id:
            source_order_link_id = str(metadata_link.get("order_link_id") or "").strip()
        if not source_order_link_id and source_order_id:
            source_order_link_id = source_order_id
        state = runtime_state.strategy_state
        if (
            purpose in self._expected_exit_cancel_purposes()
            and status in self.EMERGENCY_EXIT_STATUSES
            and reason_upper != self.EMERGENCY_REJECT_REASON
            and self._consume_expected_exit_cancel(
                runtime_state,
                purpose=purpose,
                source_order_id=source_order_id or None,
                source_order_link_id=source_order_link_id or None,
                status=status,
            )
        ):
            return []
        if self._should_trigger_emergency_exit(
            state,
            purpose,
            status,
            reason_upper,
            payload_data,
        ):
            intents = self._trigger_emergency_exit(snapshot, runtime_state, context, payload_data, reason_upper or status)
            return intents
        if status not in {"CANCELED", "CANCELLED", "REJECTED", "DEACTIVATED", "EXPIRED"}:
            return []

        if purpose in {"REFILL_LONG", "REFILL_SHORT"}:
            self._reconcile_refill_gate_state(snapshot, runtime_state)
            return []
        if purpose not in {
            self.LONG_TP_EXIT_PURPOSE,
            self.SHORT_SL_EXIT_PURPOSE,
            self.LONG_TP_EXIT_RECOVERY_PURPOSE,
            self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        }:
            return []

        current_snapshot = snapshot or runtime_state.last_snapshot
        state = runtime_state.strategy_state
        long_qty = float(current_snapshot.long_qty or 0.0) if current_snapshot else float(state.get("open_long_qty") or 0.0)
        short_qty = float(current_snapshot.short_qty or 0.0) if current_snapshot else float(state.get("open_short_qty") or 0.0)

        if purpose in {self.SHORT_SL_EXIT_PURPOSE, self.SHORT_SL_EXIT_RECOVERY_PURPOSE}:
            if short_qty <= 0:
                _log_event(
                    "fixed_cycle_short_exit_force_market_recovery_not_needed",
                    {
                        "symbol": self.config.symbol,
                        "side": "short",
                        "qty": short_qty,
                        "purpose": self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                        "original_purpose": purpose,
                        "source_order_id": source_order_id,
                        "source_order_link_id": source_order_link_id,
                        "reason": status,
                        "exit_locked": bool(state.get("exit_locked")),
                        "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                    },
                )
                return []
            if state.get("short_exit_recovery_submitted"):
                _log_event(
                    "fixed_cycle_short_exit_force_market_recovery_skipped_duplicate",
                    {
                        "symbol": self.config.symbol,
                        "side": "short",
                        "qty": short_qty,
                        "purpose": self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                        "original_purpose": purpose,
                        "source_order_id": source_order_id,
                        "source_order_link_id": source_order_link_id,
                        "reason": status,
                        "exit_locked": bool(state.get("exit_locked")),
                        "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                    },
                )
                return []
            state["exit_rebuild_allowed"] = True
            state["exit_locked"] = False
            state["exit_recovery_marker"] = True
            state["short_exit_recovery_submitted"] = True
            intent = self._build_force_exit_recovery_intent(
                side="short",
                qty=short_qty,
                symbol=self.config.symbol,
                purpose=self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                reason=status,
                source_order_id=source_order_id,
                source_order_link_id=source_order_link_id,
            )
            _log_warning_event(
                "fixed_cycle_short_exit_force_market_recovery",
                {
                    "symbol": self.config.symbol,
                    "side": "short",
                    "qty": short_qty,
                    "purpose": self.SHORT_SL_EXIT_RECOVERY_PURPOSE,
                    "original_purpose": purpose,
                    "source_order_id": source_order_id,
                    "source_order_link_id": source_order_link_id,
                    "reason": status,
                    "exit_locked": bool(state.get("exit_locked")),
                    "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                },
            )
            return [intent]

        if purpose in {self.LONG_TP_EXIT_PURPOSE, self.LONG_TP_EXIT_RECOVERY_PURPOSE}:
            if long_qty <= 0:
                _log_event(
                    "fixed_cycle_long_exit_force_market_recovery_not_needed",
                    {
                        "symbol": self.config.symbol,
                        "side": "long",
                        "qty": long_qty,
                        "purpose": self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                        "original_purpose": purpose,
                        "source_order_id": source_order_id,
                        "source_order_link_id": source_order_link_id,
                        "reason": status,
                        "exit_locked": bool(state.get("exit_locked")),
                        "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                    },
                )
                return []
            if state.get("long_exit_recovery_submitted"):
                _log_event(
                    "fixed_cycle_long_exit_force_market_recovery_skipped_duplicate",
                    {
                        "symbol": self.config.symbol,
                        "side": "long",
                        "qty": long_qty,
                        "purpose": self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                        "original_purpose": purpose,
                        "source_order_id": source_order_id,
                        "source_order_link_id": source_order_link_id,
                        "reason": status,
                        "exit_locked": bool(state.get("exit_locked")),
                        "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                    },
                )
                return []
            state["exit_rebuild_allowed"] = True
            state["exit_locked"] = False
            state["exit_recovery_marker"] = True
            state["long_exit_recovery_submitted"] = True
            intent = self._build_force_exit_recovery_intent(
                side="long",
                qty=long_qty,
                symbol=self.config.symbol,
                purpose=self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                reason=status,
                source_order_id=source_order_id,
                source_order_link_id=source_order_link_id,
            )
            _log_warning_event(
                "fixed_cycle_long_exit_force_market_recovery",
                {
                    "symbol": self.config.symbol,
                    "side": "long",
                    "qty": long_qty,
                    "purpose": self.LONG_TP_EXIT_RECOVERY_PURPOSE,
                    "original_purpose": purpose,
                    "source_order_id": source_order_id,
                    "source_order_link_id": source_order_link_id,
                    "reason": status,
                    "exit_locked": bool(state.get("exit_locked")),
                    "exit_rebuild_allowed": bool(state.get("exit_rebuild_allowed")),
                },
            )
            return [intent]

        return []