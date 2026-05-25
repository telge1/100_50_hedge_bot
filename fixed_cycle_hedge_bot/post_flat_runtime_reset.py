from __future__ import annotations

import copy
import logging
from typing import Any, Callable
from uuid import uuid4

from .base import StrategyContext
from .models import HedgeSnapshot, RuntimeState

TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}

RUNTIME_BLOCKING_FIELDS_TO_POP = [
    "final_pnl_context_missing_logged",
    "restart_delayed_pending_final_pnl_logged",
    "restart_requested_after_full_exit",
    "pending_dynamic_symbol",
    "next_dynamic_entry_allowed_at",
    "flat_waiting_order_cleanup_logged",
    "flat_waiting_final_pnl_logged",
    "flat_final_pnl_ready_logged",
    "final_exit_pnl_fetch_deferred_cleanup_started_at",
    "post_exit_cleanup_started_at",
    "post_exit_cleanup_verified_at",
    "post_exit_cleanup_verified_snapshot_updated_at",
    "emergency_exit_reason",
    "emergency_exit_signature",
]

RUNTIME_BLOCKING_FIELDS_TO_FALSE = [
    "fresh_restart_required",
    "external_restart_required",
    "post_exit_cleanup_required",
    "post_exit_cleanup_verified",
    "post_exit_cleanup_in_progress",
    "final_pnl_pending",
    "final_exit_pnl_fetch_deferred_cleanup",
    "dynamic_symbol_restart_required",
    "emergency_flat_required",
    "emergency_flat_in_progress",
    "initial_entry_submitted",
    "initial_entry_confirmed",
    "exit_locked",
    "full_exit_reset_in_progress",
    "refill_pending",
    "refill_in_progress",
    "final_exit_pnl_deferred_allows_fresh_entry",
]

RUNTIME_BLOCKING_FIELDS_TO_ZERO = [
    "final_exit_pnl_fetch_attempts",
    "final_exit_pnl_fetch_deferred_cleanup_attempts",
    "initial_entry_retry_count",
    "emergency_exit_attempts",
    "emergency_exit_verify_attempts",
]

RUNTIME_BLOCKING_FIELDS_TO_NONE = [
    "final_long_exit_order_context",
    "final_short_exit_order_context",
]

RUNTIME_BLOCKING_FIELDS_TO_EMPTY_DICT = [
    "refill_state",
]

RUNTIME_BLOCKING_FIELDS_TO_EMPTY_LIST = [
    "expected_exit_cancels",
]


def _status_is_terminal(status: object) -> bool:
    if status is None:
        return False
    return str(status).strip().upper() in TERMINAL_ORDER_STATUSES


def _count_nonterminal_snapshot_orders(snapshot: HedgeSnapshot) -> int:
    return sum(
        1
        for order in snapshot.active_orders
        if not _status_is_terminal(getattr(order, "status", None))
    )


def _default_audit_ledger() -> dict[str, Any]:
    return {
        "cycle_long_reduce_pnl": {},
        "cycle_short_tp_pnl": {},
        "cycle_pnl_entries": {},
        "final_long_exit_pnl": None,
        "final_short_exit_pnl": None,
        "total_realized_pnl": 0.0,
    }


def _archive_last_trade_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_trade_pnl_usdt": state.get("last_trade_pnl_usdt"),
        "last_trade_pnl_complete": state.get("last_trade_pnl_complete"),
        "last_trade_block_id": state.get("last_trade_block_id"),
        "trade_block_id": state.get("trade_block_id"),
        "last_trade_pnl_breakdown": state.get("last_trade_pnl_breakdown"),
        "last_trade_pnl_finalized_at": state.get("last_trade_pnl_finalized_at"),
        "last_trade_symbol": state.get("last_trade_symbol"),
    }


def _compute_previous_trade_evidence(state: dict[str, Any]) -> bool:
    ledger = state.get("audit_pnl_ledger") or {}
    cycle_long_reduce_total = sum(
        float(value or 0.0) for value in (ledger.get("cycle_long_reduce_pnl") or {}).values()
    )
    cycle_short_tp_total = sum(
        float(value or 0.0) for value in (ledger.get("cycle_short_tp_pnl") or {}).values()
    )
    return any(
        [
            bool(state.get("initial_entry_confirmed")),
            bool(state.get("final_long_exit_order_context")),
            bool(state.get("final_short_exit_order_context")),
            float(ledger.get("final_long_exit_pnl") or 0.0) != 0.0,
            float(ledger.get("final_short_exit_pnl") or 0.0) != 0.0,
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


def _compute_pnl_ready(state: dict[str, Any]) -> bool:
    current_trade_block_id = state.get("trade_block_id")
    last_trade_block_id = state.get("last_trade_block_id")
    return (
        bool(state.get("final_trade_pnl_audited"))
        and bool(state.get("last_trade_pnl_complete"))
        and state.get("last_trade_pnl_usdt") is not None
        and bool(current_trade_block_id)
        and last_trade_block_id == current_trade_block_id
    )


def _log_reset_skipped(
    logger: logging.Logger,
    *,
    reason: str,
    snapshot: HedgeSnapshot,
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "reason": reason,
        "symbol": snapshot.symbol,
        "long_qty": float(snapshot.long_qty or 0.0),
        "short_qty": float(snapshot.short_qty or 0.0),
    }
    if extra:
        payload.update(extra)
    logger.info("fixed_cycle_flat_runtime_reset_skipped", payload)


def perform_verified_flat_runtime_reset(
    *,
    snapshot: HedgeSnapshot,
    runtime_state: RuntimeState,
    context: StrategyContext,
    reason: str,
    logger: logging.Logger,
    reset_cycle_state: Callable[[], object],
    force_fresh_start_reset: Callable[[], object],
    clear_startup_zero_state_residuals: Callable[[], object],
    state_init_value: str = "INIT",
    allow_unknown_final_pnl: bool = True,
    clear_audit_history: bool = False,
) -> bool:
    del clear_audit_history
    symbol = snapshot.symbol
    long_qty = float(snapshot.long_qty or 0.0)
    short_qty = float(snapshot.short_qty or 0.0)
    if long_qty > 0.0 or short_qty > 0.0:
        _log_reset_skipped(logger, reason="snapshot_not_flat", snapshot=snapshot)
        return False

    snapshot_active_orders = _count_nonterminal_snapshot_orders(snapshot)
    if snapshot_active_orders > 0:
        _log_reset_skipped(
            logger,
            reason="snapshot_active_orders_present",
            snapshot=snapshot,
            extra={"snapshot_active_orders": snapshot_active_orders},
        )
        return False

    state = runtime_state.strategy_state
    nonterminal_runtime_orders = [
        order
        for order in runtime_state.active_orders.values()
        if not _status_is_terminal(getattr(order, "status", None))
    ]
    runtime_active_orders = len(nonterminal_runtime_orders)
    active_order_purposes = list(state.get("active_order_purposes") or [])
    if runtime_active_orders or active_order_purposes:
        reason_code = (
            "runtime_active_orders_present"
            if runtime_active_orders
            else "active_order_purposes_present"
        )
        extra = {"runtime_active_orders": runtime_active_orders}
        if active_order_purposes:
            extra["active_order_purposes"] = active_order_purposes
        _log_reset_skipped(logger, reason=reason_code, snapshot=snapshot, extra=extra)
        logger.info(
            "fixed_cycle_flat_state_reset_blocked_not_safe",
            {
                "reason": reason,
                "symbol": symbol,
                "snapshot_active_orders": snapshot_active_orders,
                "active_order_purposes": active_order_purposes,
                "runtime_active_orders": runtime_active_orders,
            },
        )
        return False

    order_manager = getattr(context, "order_manager", None)
    if order_manager is None or not hasattr(order_manager, "fetch_open_orders"):
        _log_reset_skipped(
            logger,
            reason="open_order_check_failed",
            snapshot=snapshot,
            extra={"error": "fetch_open_orders_unavailable"},
        )
        return False

    try:
        open_orders = order_manager.fetch_open_orders(
            symbol=context.symbol,
            category=context.category,
        ) or []
    except Exception as exc:
        _log_reset_skipped(
            logger,
            reason="open_order_check_failed",
            snapshot=snapshot,
            extra={"error": str(exc)},
        )
        return False

    pending_exchange_orders = [
        order
        for order in open_orders
        if not _status_is_terminal(order.get("orderStatus") or order.get("status"))
    ]
    if pending_exchange_orders:
        _log_reset_skipped(
            logger,
            reason="exchange_orders_pending",
            snapshot=snapshot,
            extra={"pending_exchange_orders": len(pending_exchange_orders)},
        )
        return False

    final_pnl_pending_before = bool(state.get("final_pnl_pending"))
    final_context_missing_allowed = False
    if allow_unknown_final_pnl and (
        not state.get("final_long_exit_order_context")
        or not state.get("final_short_exit_order_context")
    ):
        final_context_missing_allowed = True
        logger.info(
            "fixed_cycle_final_pnl_missing_but_flat_reset_allowed",
            {
                "reason": reason,
                "symbol": symbol,
                "final_long_exit_order_context_present": bool(
                    state.get("final_long_exit_order_context")
                ),
                "final_short_exit_order_context_present": bool(
                    state.get("final_short_exit_order_context")
                ),
            },
        )

    started_payload = {
        "reason": reason,
        "symbol": symbol,
        "long_qty": long_qty,
        "short_qty": short_qty,
        "snapshot_active_orders": snapshot_active_orders,
        "runtime_active_orders": runtime_active_orders,
        "active_order_purposes": active_order_purposes,
    }
    logger.info("fixed_cycle_flat_state_reset_started", started_payload)

    archived_summary = _archive_last_trade_summary(state)
    state["archived_last_trade_summary"] = archived_summary
    ledger_snapshot = copy.deepcopy(state.get("audit_pnl_ledger") or _default_audit_ledger())
    state["archived_audit_pnl_ledger"] = ledger_snapshot

    cleared_active_orders_count = len(runtime_state.active_orders)
    cleared_exchange_map_count = len(runtime_state.exchange_to_client_id)

    runtime_state.active_orders.clear()
    runtime_state.exchange_to_client_id.clear()
    runtime_state.client_to_exchange_id.clear()
    runtime_state.temporary_pnl_by_order.clear()
    runtime_state.confirmed_pnl_applied.clear()
    runtime_state.processed_fill_cumulative.clear()
    runtime_state.realized_long_pnl_total = 0.0
    runtime_state.realized_short_pnl_total = 0.0

    reset_cycle_state()
    force_fresh_start_reset()
    clear_startup_zero_state_residuals()

    for key in RUNTIME_BLOCKING_FIELDS_TO_POP:
        state.pop(key, None)
    for key in RUNTIME_BLOCKING_FIELDS_TO_FALSE:
        state[key] = False
    for key in RUNTIME_BLOCKING_FIELDS_TO_ZERO:
        state[key] = 0
    for key in RUNTIME_BLOCKING_FIELDS_TO_NONE:
        state[key] = None
    for key in RUNTIME_BLOCKING_FIELDS_TO_EMPTY_DICT:
        state[key] = {}
    for key in RUNTIME_BLOCKING_FIELDS_TO_EMPTY_LIST:
        state[key] = []

    state.update(
        {
            "bot_state": state_init_value,
            "exit_rebuild_allowed": True,
            "force_exit_rebuild": False,
            "startup_flat_reset_applied": True,
            "verified_flat_runtime_reset_done": True,
            "final_pnl_pending": False,
            "final_exit_pnl_fetch_deferred_cleanup": False,
            "final_exit_pnl_fetch_attempts": 0,
            "final_exit_pnl_fetch_deferred_cleanup_attempts": 0,
            "final_exit_pnl_deferred_allows_fresh_entry": False,
            "expected_exit_cancels": [],
            "final_pnl_context_missing_logged": False,
            "flat_waiting_for_final_pnl": False,
            "final_exit_closed_pnl_signatures": [],
            "active_order_purposes": [],
            "last_fill_info": None,
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "final_long_exit_audited": False,
            "final_short_exit_audited": False,
            "final_trade_pnl_audited": False,
            "final_long_exit_order_context": None,
            "final_short_exit_order_context": None,
            "last_trade_pnl_usdt": None,
            "last_trade_pnl_complete": False,
            "last_trade_pnl_breakdown": None,
            "last_trade_pnl_finalized_at": None,
            "last_trade_symbol": None,
            "last_trade_block_id": None,
            "trade_block_id": None,
            "post_exit_cleanup_required": False,
            "post_exit_cleanup_in_progress": False,
            "post_exit_cleanup_verified": False,
            "post_exit_cleanup_attempts": 0,
            "post_exit_cleanup_started_at": None,
            "post_exit_cleanup_verified_at": None,
            "post_exit_cleanup_verified_snapshot_updated_at": None,
            "fresh_restart_required": False,
            "refill_pending": False,
            "refill_in_progress": False,
            "refill_long_filled": False,
            "refill_short_filled": False,
            "refill_state": {},
            "emergency_exit_reason": None,
            "emergency_exit_signature": None,
            "emergency_flat_required": False,
            "_in_emergency_final_exit_context": False,
            "trailing_active": None,
            "long_add_locked": False,
            "long_add_pending": False,
            "long_add_rebuild_allowed": True,
            "cycle_long_add_filled": False,
            "cycle_short_tp_filled": False,
            "cycle_completed_count": 0,
            "cycle_pair_count": 0,
            "pending_cycle_loss_usdt": 0.0,
            "pending_loss_exit_old_signature": None,
            "pending_loss_exit_rebuild_reason": None,
            "net_long_loss_balance": 0.0,
            "net_short_loss_balance": 0.0,
            "realized_long_loss_total": 0.0,
            "processed_pnl_exec_ids": [],
            "processed_pnl_exec_ids_order": [],
            "audit_processed_exit_fill_ids": [],
            "initial_entry_retry_count": 0,
            "initial_total_notional_usdt": 0.0,
            "last_structure_refresh_ms": 0,
            "open_long_qty": 0.0,
            "open_short_qty": 0.0,
            "long_avg": 0.0,
            "short_avg": 0.0,
            "entry_reference_price": None,
            "pending_long_cycle_index": 0,
            "pending_short_cycle_index": 0,
            "current_long_cycle_index": 0,
            "current_short_cycle_index": 0,
            "current_effective_cycle": 0,
            "cycle_waiting_for_short_tp": False,
            "short_tp_pending_cycle": 0,
            "block_exit_rebuild_until_pnl_ready": False,
            "exit_orders_submitted_once": False,
            "cycle_states": {},
            "processed_cycle_purposes": [],
            "pending_rest_fill_confirmations": {},
            "rest_fill_dispatch_keys": [],
        }
    )
    state["processed_fill_ids"] = []

    cycle_state = state.setdefault("cycle_state", {})
    cycle_state.update(
        {
            "trade_active": False,
            "symbol": symbol,
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
            "pending_loss_exit_old_signature": None,
            "pending_loss_exit_rebuild_reason": None,
        }
    )
    state["cycle_state"] = cycle_state
    state["cycle_states"] = {}
    state["processed_cycle_purposes"] = []
    state.pop("completed_cycle_purposes", None)
    state["cycle_completed_count"] = 0
    state["cycle_pair_count"] = 0
    state["cycle_waiting_for_short_tp"] = False
    state["short_tp_pending_cycle"] = 0

    state["audit_pnl_ledger"] = _default_audit_ledger()

    previous_trade_evidence_present_after = _compute_previous_trade_evidence(state)
    pnl_ready_for_current_trade_after = _compute_pnl_ready(state)
    active_snapshot_order_count_after = _count_nonterminal_snapshot_orders(snapshot)

    logger.info(
        "fixed_cycle_flat_state_reset_completed",
        {
            "reason": reason,
            "symbol": symbol,
            "final_long_exit_order_context_present_after": bool(
                state.get("final_long_exit_order_context")
            ),
            "final_short_exit_order_context_present_after": bool(
                state.get("final_short_exit_order_context")
            ),
            "final_long_exit_pnl_after": state.get("final_long_exit_pnl"),
            "final_short_exit_pnl_after": state.get("final_short_exit_pnl"),
            "final_trade_pnl_audited_after": bool(state.get("final_trade_pnl_audited")),
            "fresh_restart_required_after": bool(state.get("fresh_restart_required")),
            "post_exit_cleanup_required_after": bool(state.get("post_exit_cleanup_required")),
            "previous_trade_evidence_present_after": previous_trade_evidence_present_after,
            "pnl_ready_for_current_trade_after": pnl_ready_for_current_trade_after,
            "active_runtime_order_count_after": len(runtime_state.active_orders),
            "active_snapshot_order_count_after": active_snapshot_order_count_after,
        },
    )

    logger.info(
        "fixed_cycle_flat_runtime_reset_performed",
        {
            "reason": reason,
            "symbol": symbol,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "cleared_active_orders_count": cleared_active_orders_count,
            "cleared_exchange_map_count": cleared_exchange_map_count,
            "final_pnl_pending_before": final_pnl_pending_before,
            "final_context_missing_allowed": final_context_missing_allowed,
            "fresh_restart_required_after": bool(state.get("fresh_restart_required")),
            "initial_entry_submitted_after": bool(state.get("initial_entry_submitted")),
            "initial_entry_confirmed_after": bool(state.get("initial_entry_confirmed")),
        },
    )
    return True
