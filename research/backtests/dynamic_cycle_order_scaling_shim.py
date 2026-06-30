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


@dataclass
class _ScalingSession:
    cycle_index: int
    purpose: str
    params: Any
    long_add_distance_before: float | None = None
    target_profit_usdt_before: float | None = None
    qty_before: float | None = None
    qty_after: float | None = None


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
