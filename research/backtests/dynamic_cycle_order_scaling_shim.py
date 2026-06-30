"""Backtest-only shim: apply dynamic cycle order scaling without touching live strategy code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from .dynamic_cycle_order_scaling import (
    DynamicCycleOrderScalingConfig,
    build_dynamic_cycle_debug_metadata,
    compute_scaled_target_profit_usdt,
    get_cycle_scaling_params,
    scale_cycle_qty,
    scaling_applies,
    symbol_rules_from_runtime,
)
from .purpose_utils import preserve_bot_purpose

_CYCLE_INDEX_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$", re.I)
_DCOS_AUDIT_KEY = "_dcos_backtest_audit_events"


@dataclass
class _ScalingSession:
    cycle_index: int
    purpose: str
    params: Any
    long_add_distance_before: float | None = None
    target_profit_usdt_before: float | None = None
    qty_before: float | None = None
    qty_after: float | None = None


def _append_dcos_audit_event(runtime_state: RuntimeState, event: dict[str, Any]) -> None:
    state = runtime_state.strategy_state
    events = state.setdefault(_DCOS_AUDIT_KEY, [])
    if isinstance(events, list):
        events.append(event)


def _active_order_purposes(strategy: Any, runtime_state: RuntimeState) -> list[str]:
    snapshot = runtime_state.last_snapshot
    purposes: list[str] = []
    if snapshot is not None:
        for order in getattr(snapshot, "active_orders", []) or []:
            purpose = preserve_bot_purpose(getattr(order, "purpose", "") or "")
            if purpose:
                purposes.append(purpose)
    for order in runtime_state.active_orders.values():
        purpose = preserve_bot_purpose(getattr(order, "purpose", "") or "")
        if purpose and purpose not in purposes:
            purposes.append(purpose)
    return purposes


def _leg_flags_snapshot(state: dict[str, Any]) -> dict[str, bool]:
    return {
        "cycle_long_add_filled": bool(state.get("cycle_long_add_filled")),
        "cycle_short_tp_filled": bool(state.get("cycle_short_tp_filled")),
        "long_add_rebuild_allowed": bool(state.get("long_add_rebuild_allowed", True)),
    }


def _second_leg_effectively_filled(strategy: Any, runtime_state: RuntimeState, cycle_index: int) -> bool:
    state = runtime_state.strategy_state
    if bool(state.get("cycle_short_tp_filled")):
        return True
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    second_leg_status = str(strategy._get_second_leg_status(entry) or "").upper()
    if second_leg_status in {"FILLED", "PROCESSED"}:
        return True
    fill_price_field = strategy._second_leg_fill_price_field()
    if float(entry.get(fill_price_field) or 0.0) > 0.0:
        return True
    second_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_second_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_second_leg_cycle_role()},
    )
    processed = {
        str(purpose or "").upper()
        for purpose in (state.get("processed_cycle_purposes") or [])
    }
    return second_leg_purpose.upper() in processed


def _sequencer_expects_next_cycle(
    strategy: Any,
    runtime_state: RuntimeState,
    cycle_index: int,
) -> bool:
    state = runtime_state.strategy_state
    next_required = str(state.get("next_required_purpose") or "").upper()
    if not next_required:
        return False
    next_cycle_la = strategy._normalize_cycle_purpose(
        strategy._get_first_leg_purpose(cycle_index + 1),
        {"cycle_index": cycle_index + 1, "cycle_role": strategy._get_first_leg_cycle_role()},
    ).upper()
    if next_required == next_cycle_la:
        return True
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    second_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_second_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_second_leg_cycle_role()},
    ).upper()
    if (
        next_required == second_leg_purpose
        and str(strategy._get_second_leg_status(entry) or "").upper() in {"FILLED", "PROCESSED"}
    ):
        return True
    return False


def _has_active_split_stage_orders(
    strategy: Any,
    runtime_state: RuntimeState,
    cycle_index: int,
) -> bool:
    snapshot = runtime_state.last_snapshot
    if snapshot is None:
        return False
    purpose = strategy._get_second_leg_purpose(cycle_index)
    for order in strategy._iter_active_orders_for_purpose(snapshot, runtime_state, purpose):
        metadata = getattr(order, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("normal_cycle_second_leg_split"):
            return True
    return False


def _sync_stale_normal_split_state(
    state: dict[str, Any],
    cycle_index: int,
) -> tuple[int, int]:
    cycle_key = str(cycle_index)
    stage_count_map = state.get("normal_cycle_second_leg_split_stage_count") or {}
    stage_count = int(stage_count_map.get(cycle_key) or 0)
    filled_map = state.setdefault("normal_cycle_second_leg_split_filled_stages", {})
    filled_stages = list(filled_map.get(cycle_key) or [])
    if stage_count > 0:
        filled_map[cycle_key] = list(range(stage_count))
    else:
        stage_count_map.pop(cycle_key, None)
        filled_map.pop(cycle_key, None)
        state["normal_cycle_second_leg_split_stage_count"] = stage_count_map
        state["normal_cycle_second_leg_split_filled_stages"] = filled_map
    return stage_count, len(filled_stages)


def _maybe_apply_stale_split_completion_fallback(
    strategy: Any,
    runtime_state: RuntimeState,
    cycle_index: int,
    trigger_purpose: str | None,
    *,
    config: DynamicCycleOrderScalingConfig,
) -> None:
    if cycle_index <= 0 or cycle_index < int(config.start_cycle_index):
        return

    state = runtime_state.strategy_state
    cycle_key = str(cycle_index)
    stage_count_map = state.get("normal_cycle_second_leg_split_stage_count") or {}
    if cycle_key not in stage_count_map:
        return

    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    if bool(entry.get("complete")):
        return
    if not bool(state.get("cycle_long_add_filled")):
        return
    if not _second_leg_effectively_filled(strategy, runtime_state, cycle_index):
        return
    if not _sequencer_expects_next_cycle(strategy, runtime_state, cycle_index):
        return
    if _has_active_split_stage_orders(strategy, runtime_state, cycle_index):
        return

    snapshot = runtime_state.last_snapshot
    if snapshot is None:
        return
    purpose = strategy._get_second_leg_purpose(cycle_index)
    missing_stages = strategy._missing_normal_split_stage_indices(
        state,
        runtime_state,
        snapshot,
        cycle_index=cycle_index,
        purpose=purpose,
        cycle_role=strategy._get_second_leg_cycle_role(),
    )
    split_complete, _ = strategy._is_normal_cycle_second_leg_split_complete(state, cycle_index)
    if not missing_stages and split_complete:
        return

    next_required_before = state.get("next_required_purpose")
    flags_before = _leg_flags_snapshot(state)
    active_before = _active_order_purposes(strategy, runtime_state)
    filled_stage_count_before = len(
        (state.get("normal_cycle_second_leg_split_filled_stages") or {}).get(cycle_key) or []
    )
    stage_count_before = int(stage_count_map.get(cycle_key) or 0)

    stage_count, filled_stage_count_after_sync = _sync_stale_normal_split_state(state, cycle_index)

    resolved_trigger = str(trigger_purpose or strategy._get_second_leg_purpose(cycle_index))
    cycle_state = strategy._ensure_cycle_state(runtime_state)
    counts_before = {
        "cycle_completed_count": int(state.get("cycle_completed_count") or 0),
        "cycle_pair_count": int(state.get("cycle_pair_count") or 0),
        "current_effective_cycle": int(state.get("current_effective_cycle") or 0),
        "current_short_cycle_index": int(state.get("current_short_cycle_index") or 0),
    }
    counts_after = {
        "cycle_completed_count": max(counts_before["cycle_completed_count"], cycle_index),
        "cycle_pair_count": max(counts_before["cycle_pair_count"], cycle_index),
        "current_effective_cycle": max(counts_before["current_effective_cycle"], cycle_index),
        "current_short_cycle_index": max(counts_before["current_short_cycle_index"], cycle_index),
    }
    followup_before = {
        "cycle_waiting_for_short_tp": strategy._get_second_leg_waiting(state, cycle_state),
        "short_tp_pending_cycle": strategy._get_second_leg_pending_cycle(state, cycle_state),
        "pending_short_cycle_index": int(state.get("pending_short_cycle_index") or 0),
        "long_add_pending": strategy._get_first_leg_pending(state, cycle_state),
        "pending_cycle_loss_usdt": float(state.get("pending_cycle_loss_usdt") or 0.0),
    }

    strategy._force_commit_short_reduce_completion_even_if_duplicate(
        runtime_state,
        cycle_index,
        followup_before=followup_before,
        counts_before=counts_before,
        counts_after=counts_after,
        trigger_purpose=resolved_trigger,
        reason="dcos_stale_split_completion_fix",
    )

    _append_dcos_audit_event(
        runtime_state,
        {
            "event": "dcos_stale_split_completion_fix_applied",
            "cycle_index": cycle_index,
            "split_stage_count": stage_count_before,
            "filled_stage_count_before": filled_stage_count_before,
            "filled_stage_count_after_sync": filled_stage_count_after_sync,
            "last_fill_purpose": (state.get("last_fill_info") or {}).get("purpose")
            or trigger_purpose,
            "next_required_purpose_before": next_required_before,
            "next_required_purpose_after": state.get("next_required_purpose"),
            "flags_before": flags_before,
            "flags_after": _leg_flags_snapshot(state),
            "active_order_purposes_before": active_before,
            "active_order_purposes_after": _active_order_purposes(strategy, runtime_state),
            "trigger_purpose": resolved_trigger,
            "missing_split_stage_indices": missing_stages,
        },
    )


def _install_stale_split_completion_shim(strategy: Any, config: DynamicCycleOrderScalingConfig) -> None:
    if getattr(strategy, "_backtest_dcos_stale_split_completion_shim_installed", False):
        return

    original_try_complete = strategy._try_complete_cycle_pair_after_confirmed_pnl

    def wrapped_try_complete(
        runtime_state: RuntimeState,
        cycle_index: int,
        trigger_purpose: str | None,
    ) -> Any:
        original_try_complete(runtime_state, cycle_index, trigger_purpose)
        active_config: DynamicCycleOrderScalingConfig | None = getattr(
            strategy,
            "_dynamic_cycle_order_scaling_config",
            None,
        )
        if active_config is None or not active_config.enabled:
            return None
        _maybe_apply_stale_split_completion_fallback(
            strategy,
            runtime_state,
            cycle_index,
            trigger_purpose,
            config=active_config,
        )
        return None

    strategy._try_complete_cycle_pair_after_confirmed_pnl = wrapped_try_complete
    strategy._backtest_dcos_stale_split_completion_shim_installed = True


def _cycle_index_from_purpose(purpose: str | None) -> int:
    match = _CYCLE_INDEX_RE.match(preserve_bot_purpose(purpose))
    if not match:
        return 0
    return int(match.group(1))


def _resolve_short_followup_cycle_index(strategy: Any, runtime_state: RuntimeState) -> int:
    state = runtime_state.strategy_state
    cycle_state = strategy._ensure_cycle_state(runtime_state)
    cycle_index = int(strategy._get_second_leg_pending_cycle(state, cycle_state) or 0)
    if cycle_index > 0:
        return cycle_index
    next_required = str(state.get("next_required_purpose") or "").upper()
    if not next_required:
        return 0
    seq_cycle_index, seq_field = strategy._extract_cycle_sequence_target(next_required, {})
    if seq_cycle_index > 0 and seq_field == strategy._get_second_leg_status_field():
        return seq_cycle_index
    return 0


def _merge_metadata(intent: StrategyIntent, metadata: dict[str, Any]) -> None:
    merged = dict(intent.metadata or {})
    merged.update({key: value for key, value in metadata.items() if value is not None})
    intent.metadata = merged


def _attach_metadata_to_intents(
    intents: list[StrategyIntent],
    *,
    config: DynamicCycleOrderScalingConfig,
    session: _ScalingSession | None,
) -> None:
    if session is None or not session.params:
        return
    for intent in intents:
        purpose = preserve_bot_purpose(intent.purpose)
        if purpose != session.purpose:
            continue
        metadata = build_dynamic_cycle_debug_metadata(
            config=config,
            cycle_index=session.cycle_index,
            purpose=purpose,
            params=session.params,
            planned_cycle_qty_before_scaling=session.qty_before,
            planned_cycle_qty_after_scaling=float(intent.qty),
            planned_long_add_distance_pct_before_scaling=session.long_add_distance_before,
            planned_long_add_distance_pct_after_scaling=session.params.long_add_distance_pct,
        )
        _merge_metadata(intent, metadata)


def install_dynamic_cycle_order_scaling(
    strategy: Any,
    config: DynamicCycleOrderScalingConfig | None,
) -> None:
    """Patch strategy helpers for backtest-only dynamic cycle scaling."""
    if config is None or not config.enabled:
        return
    if getattr(strategy, "_backtest_dynamic_cycle_order_scaling_installed", False):
        strategy._dynamic_cycle_order_scaling_config = config
        _install_stale_split_completion_shim(strategy, config)
        return

    original_build_long_add = strategy._build_cycle_long_add_intent
    original_build_short_follow_up = strategy._build_short_tp_follow_up
    original_fixed_long_qty = strategy._fixed_long_cycle_qty
    original_fixed_short_qty = strategy._fixed_short_cycle_qty

    strategy._dynamic_cycle_order_scaling_config = config
    strategy._dynamic_cycle_order_scaling_session = None
    strategy._backtest_dynamic_cycle_order_scaling_installed = True

    def wrapped_fixed_long_qty(
        initial_long_qty: float,
        current_open_long_qty: float,
        reference_price: float,
        runtime_state: RuntimeState | None = None,
    ) -> float:
        qty = original_fixed_long_qty(
            initial_long_qty,
            current_open_long_qty,
            reference_price,
            runtime_state,
        )
        session: _ScalingSession | None = getattr(strategy, "_dynamic_cycle_order_scaling_session", None)
        if session is None or runtime_state is None or qty <= 0:
            return qty
        if not scaling_applies(config, session.cycle_index):
            return qty
        rules = symbol_rules_from_runtime(runtime_state)
        session.qty_before = float(qty)
        scaled = scale_cycle_qty(
            qty,
            config,
            session.cycle_index,
            symbol_rules=rules,
        )
        session.qty_after = scaled
        return scaled

    def wrapped_fixed_short_qty(
        initial_short_qty: float,
        current_open_short_qty: float,
        reference_price: float,
        reduction_multiplier: float = 1.0,
        runtime_state: RuntimeState | None = None,
    ) -> float:
        qty = original_fixed_short_qty(
            initial_short_qty,
            current_open_short_qty,
            reference_price,
            reduction_multiplier,
            runtime_state,
        )
        session: _ScalingSession | None = getattr(strategy, "_dynamic_cycle_order_scaling_session", None)
        if session is None or runtime_state is None or qty <= 0:
            return qty
        if not scaling_applies(config, session.cycle_index):
            return qty
        rules = symbol_rules_from_runtime(runtime_state)
        session.qty_before = float(qty)
        scaled = scale_cycle_qty(
            qty,
            config,
            session.cycle_index,
            symbol_rules=rules,
        )
        session.qty_after = scaled
        return scaled

    def wrapped_build_long_add(
        snapshot: Any,
        runtime_state: RuntimeState,
        context: StrategyContext,
        *,
        cycle_index: int,
    ) -> StrategyIntent | None:
        session: _ScalingSession | None = None
        original_distance = float(strategy.config.long_fill_distance_pct)
        try:
            if scaling_applies(config, cycle_index):
                params = get_cycle_scaling_params(config, cycle_index)
                if params is not None:
                    session = _ScalingSession(
                        cycle_index=cycle_index,
                        purpose=strategy._get_first_leg_purpose(cycle_index),
                        params=params,
                        long_add_distance_before=original_distance,
                    )
                    strategy._dynamic_cycle_order_scaling_session = session
                    strategy.config.long_fill_distance_pct = params.long_add_distance_pct
                    strategy._fixed_long_cycle_qty = wrapped_fixed_long_qty
            intent = original_build_long_add(
                snapshot,
                runtime_state,
                context,
                cycle_index=cycle_index,
            )
            if intent is not None and session is not None:
                metadata = build_dynamic_cycle_debug_metadata(
                    config=config,
                    cycle_index=cycle_index,
                    purpose=preserve_bot_purpose(intent.purpose),
                    params=session.params,
                    planned_cycle_qty_before_scaling=session.qty_before,
                    planned_cycle_qty_after_scaling=float(intent.qty),
                    planned_long_add_distance_pct_before_scaling=session.long_add_distance_before,
                    planned_long_add_distance_pct_after_scaling=session.params.long_add_distance_pct,
                )
                _merge_metadata(intent, metadata)
            return intent
        finally:
            strategy.config.long_fill_distance_pct = original_distance
            strategy._fixed_long_cycle_qty = original_fixed_long_qty
            strategy._dynamic_cycle_order_scaling_session = None

    def wrapped_build_short_follow_up(
        snapshot: Any,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        cycle_index = _resolve_short_followup_cycle_index(strategy, runtime_state)
        session: _ScalingSession | None = None
        original_target_profit = float(strategy.config.target_profit_usdt or 0.0)
        try:
            if scaling_applies(config, cycle_index):
                params = get_cycle_scaling_params(config, cycle_index)
                if params is not None:
                    session = _ScalingSession(
                        cycle_index=cycle_index,
                        purpose=strategy._get_second_leg_purpose(cycle_index),
                        params=params,
                        target_profit_usdt_before=original_target_profit,
                    )
                    strategy._dynamic_cycle_order_scaling_session = session
                    strategy.config.target_profit_usdt = compute_scaled_target_profit_usdt(
                        original_target_profit,
                        params.target_profit_pct,
                    )
                    strategy._fixed_short_cycle_qty = wrapped_fixed_short_qty
            intents = original_build_short_follow_up(snapshot, runtime_state, context)
            if session is not None:
                _attach_metadata_to_intents(intents, config=config, session=session)
            return intents
        finally:
            strategy.config.target_profit_usdt = original_target_profit
            strategy._fixed_short_cycle_qty = original_fixed_short_qty
            strategy._dynamic_cycle_order_scaling_session = None

    strategy._build_cycle_long_add_intent = wrapped_build_long_add
    strategy._build_short_tp_follow_up = wrapped_build_short_follow_up
    _install_stale_split_completion_shim(strategy, config)
