"""Backtest-only shim: cap cycle short-reduce distance and carry uncovered loss to exits."""

from __future__ import annotations

import re
from typing import Any

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from .cycle_short_tp_relief import (
    CycleShortTpReliefConfig,
    ShortTpReliefComputation,
    compute_short_tp_relief,
    get_max_distance_pct,
    relief_applies,
)
from .purpose_utils import preserve_bot_purpose

_STATE_KEY = "_backtest_cycle_short_tp_relief"
_CYCLE_INDEX_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$", re.I)
_RELief_METADATA_KEYS = (
    "cycle_short_tp_relief_enabled",
    "normal_short_reduce_price",
    "capped_short_reduce_price",
    "required_profit",
    "covered_profit",
    "uncovered_loss",
    "cumulative_carry_loss",
    "exit_adjustment_pct",
    "max_short_reduce_distance_pct_from_long_fill",
    "short_tp_relief_cap_applied",
)


def _cycle_index_from_purpose(purpose: str | None) -> int:
    match = _CYCLE_INDEX_RE.match(preserve_bot_purpose(purpose))
    if not match:
        return 0
    return int(match.group(1))


def _ensure_relief_state(state: dict[str, Any]) -> dict[str, Any]:
    relief = state.setdefault(
        _STATE_KEY,
        {
            "carry_loss_by_trade_block": {},
            "applied_relief_keys_by_trade_block": {},
            "cycle_records": [],
            "cumulative_carry_loss": 0.0,
        },
    )
    relief.setdefault("carry_loss_by_trade_block", {})
    relief.setdefault("applied_relief_keys_by_trade_block", {})
    relief.setdefault("cycle_records", [])
    relief.setdefault("cumulative_carry_loss", 0.0)
    return relief


def _build_relief_applied_key(
    *,
    trade_block_id: str,
    cycle_index: int,
    purpose: str,
    capped_short_reduce_price: float,
    short_reduce_qty: float,
    order_id: str | None = None,
) -> str:
    normalized_purpose = preserve_bot_purpose(purpose)
    if order_id:
        return f"{trade_block_id}|order:{order_id}"
    return (
        f"{trade_block_id}|cycle:{cycle_index}|{normalized_purpose}|"
        f"cap:{float(capped_short_reduce_price):.8f}|qty:{float(short_reduce_qty):.8f}"
    )


def _relief_key_already_applied(state: dict[str, Any], *, trade_block_id: str, applied_key: str) -> bool:
    relief = state.get(_STATE_KEY) or {}
    applied_by_block = relief.get("applied_relief_keys_by_trade_block") or {}
    block_applied = applied_by_block.get(trade_block_id) or {}
    return bool(block_applied.get(applied_key))


def get_cumulative_carry_loss(state: dict[str, Any], *, trade_block_id: str | None = None) -> float:
    relief = state.get(_STATE_KEY) or {}
    if trade_block_id:
        by_block = relief.get("carry_loss_by_trade_block") or {}
        return float(by_block.get(trade_block_id) or 0.0)
    return float(relief.get("cumulative_carry_loss") or 0.0)


def _add_carry_loss(
    state: dict[str, Any],
    *,
    trade_block_id: str,
    uncovered_loss: float,
    record: dict[str, Any],
) -> float:
    relief = _ensure_relief_state(state)
    by_block: dict[str, float] = relief["carry_loss_by_trade_block"]
    previous = float(by_block.get(trade_block_id) or 0.0)
    updated = previous + float(uncovered_loss)
    by_block[trade_block_id] = updated
    relief["cumulative_carry_loss"] = float(
        sum(float(value or 0.0) for value in by_block.values())
    )
    relief["cycle_records"].append(record)
    return updated


def _register_carry_loss(
    state: dict[str, Any],
    *,
    trade_block_id: str,
    applied_key: str,
    uncovered_loss: float,
    record: dict[str, Any],
) -> tuple[float, bool]:
    """Add uncovered_loss once per applied_key; return (cumulative_carry_loss, newly_applied)."""
    relief = _ensure_relief_state(state)
    applied_by_block: dict[str, dict[str, bool]] = relief["applied_relief_keys_by_trade_block"]
    block_applied: dict[str, bool] = applied_by_block.setdefault(trade_block_id, {})
    cumulative_carry_loss = get_cumulative_carry_loss(state, trade_block_id=trade_block_id)
    if block_applied.get(applied_key):
        return cumulative_carry_loss, False

    block_applied[applied_key] = True
    cumulative_carry_loss = _add_carry_loss(
        state,
        trade_block_id=trade_block_id,
        uncovered_loss=uncovered_loss,
        record={**record, "applied_key": applied_key},
    )
    return cumulative_carry_loss, True


def _build_relief_metadata(
    config: CycleShortTpReliefConfig,
    computation: ShortTpReliefComputation,
    *,
    cumulative_carry_loss: float,
    exit_adjustment_pct: float | None = None,
) -> dict[str, Any]:
    return {
        "cycle_short_tp_relief_enabled": config.enabled,
        "normal_short_reduce_price": computation.normal_short_reduce_price,
        "capped_short_reduce_price": computation.capped_short_reduce_price,
        "required_profit": computation.required_profit,
        "covered_profit": computation.covered_profit,
        "uncovered_loss": computation.uncovered_loss,
        "cumulative_carry_loss": cumulative_carry_loss,
        "exit_adjustment_pct": exit_adjustment_pct,
        "max_short_reduce_distance_pct_from_long_fill": computation.max_distance_pct_from_long_fill,
        "short_tp_relief_cap_applied": computation.cap_applied,
    }


def _resolve_required_profit(metadata: dict[str, Any], strategy: Any) -> float:
    for key in ("required_net", "required_profit_to_cover_loss", "required_profit"):
        value = metadata.get(key)
        if value is not None:
            return max(float(value), 0.0)
    long_loss = max(float(metadata.get("long_loss_usdt") or 0.0), 0.0)
    target = float(
        metadata.get("target_profit_usdt")
        or getattr(strategy.config, "target_profit_usdt", 0.0)
        or 0.0
    )
    return max(long_loss + target, 0.0)


def _apply_relief_to_short_reduce_intents(
    strategy: Any,
    snapshot: Any,
    runtime_state: RuntimeState,
    intents: list[StrategyIntent],
    *,
    config: CycleShortTpReliefConfig,
) -> list[StrategyIntent]:
    if not config.enabled or not intents:
        return intents

    state = runtime_state.strategy_state
    trade_block_id = str(state.get("trade_block_id") or "")
    updated: list[StrategyIntent] = []

    for intent in intents:
        purpose = preserve_bot_purpose(intent.purpose)
        metadata = dict(intent.metadata or {})
        cycle_index = int(metadata.get("cycle_index") or _cycle_index_from_purpose(purpose))
        if not relief_applies(config, cycle_index) or "SHORT_REDUCE" not in purpose.upper():
            updated.append(intent)
            continue

        max_distance_pct = get_max_distance_pct(config, cycle_index)
        if max_distance_pct is None:
            updated.append(intent)
            continue

        long_fill_price = float(
            metadata.get("first_leg_fill_price")
            or metadata.get("long_fill_price")
            or strategy._get_first_leg_fill_price(runtime_state, cycle_index)
            or 0.0
        )
        normal_short_reduce_price = float(
            intent.trigger_price
            or metadata.get("trigger_price")
            or metadata.get("trigger_price_normalized")
            or metadata.get("raw_trigger_price")
            or 0.0
        )
        short_avg_price = float(
            metadata.get("short_entry_price")
            or getattr(snapshot, "short_avg", 0.0)
            or state.get("short_avg")
            or 0.0
        )
        short_reduce_qty = float(intent.qty or metadata.get("short_qty") or metadata.get("qty") or 0.0)
        required_profit = _resolve_required_profit(metadata, strategy)

        if (
            long_fill_price <= 0
            or normal_short_reduce_price <= 0
            or short_avg_price <= 0
            or short_reduce_qty <= 0
        ):
            updated.append(intent)
            continue

        computation = compute_short_tp_relief(
            cycle_index=cycle_index,
            long_fill_price=long_fill_price,
            normal_short_reduce_price=normal_short_reduce_price,
            short_avg_price=short_avg_price,
            short_reduce_qty=short_reduce_qty,
            required_profit=required_profit,
            max_distance_pct_from_long_fill=max_distance_pct,
        )

        cumulative_carry_loss = get_cumulative_carry_loss(state, trade_block_id=trade_block_id)
        newly_applied = False
        normalized_trigger = None
        if computation.cap_applied:
            normalized_trigger = strategy._normalize_price(
                computation.capped_short_reduce_price,
                runtime_state,
            )
            intent.trigger_price = normalized_trigger
            metadata["trigger_price"] = normalized_trigger
            metadata["raw_trigger_price"] = normalized_trigger

        if computation.cap_applied and computation.uncovered_loss > 0 and config.carry_uncovered_loss_to_exit:
            applied_key = _build_relief_applied_key(
                trade_block_id=trade_block_id,
                cycle_index=cycle_index,
                purpose=purpose,
                capped_short_reduce_price=float(normalized_trigger or computation.capped_short_reduce_price),
                short_reduce_qty=short_reduce_qty,
                order_id=str(metadata.get("order_id") or metadata.get("client_order_id") or "") or None,
            )
            record = {
                "cycle_index": cycle_index,
                "trade_block_id": trade_block_id,
                "purpose": purpose,
                "applied_key": applied_key,
                **_build_relief_metadata(config, computation, cumulative_carry_loss=cumulative_carry_loss),
            }
            cumulative_carry_loss, newly_applied = _register_carry_loss(
                state,
                trade_block_id=trade_block_id,
                applied_key=applied_key,
                uncovered_loss=computation.uncovered_loss,
                record=record,
            )

        metadata.update(
            _build_relief_metadata(
                config,
                computation,
                cumulative_carry_loss=cumulative_carry_loss,
            )
        )
        if computation.cap_applied and computation.uncovered_loss > 0 and config.carry_uncovered_loss_to_exit and not newly_applied:
            metadata["short_tp_relief_carry_already_applied"] = True
        intent.metadata = metadata
        updated.append(intent)

    return updated


def _attach_exit_relief_metadata(
    runtime_state: RuntimeState,
    *,
    config: CycleShortTpReliefConfig,
    exit_adjustment_pct: float | None,
) -> None:
    state = runtime_state.strategy_state
    trade_block_id = str(state.get("trade_block_id") or "")
    cumulative_carry_loss = get_cumulative_carry_loss(state, trade_block_id=trade_block_id)
    state["_last_exit_short_tp_relief"] = {
        "cycle_short_tp_relief_enabled": config.enabled,
        "cumulative_carry_loss": cumulative_carry_loss,
        "exit_adjustment_pct": exit_adjustment_pct,
        "carry_uncovered_loss_to_exit": config.carry_uncovered_loss_to_exit,
    }


def install_cycle_short_tp_relief(strategy: Any, config: CycleShortTpReliefConfig | None) -> None:
    """Patch strategy helpers for backtest-only short-TP distance relief."""
    if config is None or not config.enabled:
        return
    if getattr(strategy, "_backtest_cycle_short_tp_relief_installed", False):
        strategy._cycle_short_tp_relief_config = config
        return

    original_build_short_follow_up = strategy._build_short_tp_follow_up
    original_calculate_tp_projection = strategy._calculate_tp_projection
    original_build_exit_intents = strategy._build_exit_intents

    strategy._cycle_short_tp_relief_config = config
    strategy._backtest_cycle_short_tp_relief_installed = True

    def wrapped_build_short_follow_up(snapshot: Any, runtime_state: RuntimeState, context: Any) -> list[StrategyIntent]:
        intents = original_build_short_follow_up(snapshot, runtime_state, context)
        active_config: CycleShortTpReliefConfig | None = getattr(
            strategy, "_cycle_short_tp_relief_config", None
        )
        if active_config is None or not active_config.enabled:
            return intents
        return _apply_relief_to_short_reduce_intents(
            strategy,
            snapshot,
            runtime_state,
            intents,
            config=active_config,
        )

    def wrapped_calculate_tp_projection(
        break_even_price: float,
        snapshot: Any = None,
        runtime_state: RuntimeState | None = None,
    ) -> Any:
        active_config: CycleShortTpReliefConfig | None = getattr(
            strategy, "_cycle_short_tp_relief_config", None
        )
        if (
            active_config is None
            or not active_config.enabled
            or not active_config.carry_uncovered_loss_to_exit
            or runtime_state is None
        ):
            return original_calculate_tp_projection(break_even_price, snapshot, runtime_state)

        state = runtime_state.strategy_state
        trade_block_id = str(state.get("trade_block_id") or "")
        carry_loss = get_cumulative_carry_loss(state, trade_block_id=trade_block_id)
        pending_before = state.get("pending_cycle_loss_usdt")
        adjusted = False
        if carry_loss > 0:
            state["pending_cycle_loss_usdt"] = float(pending_before or 0.0) + carry_loss
            adjusted = True
        try:
            projection = original_calculate_tp_projection(
                break_even_price, snapshot, runtime_state
            )
        finally:
            if adjusted:
                state["pending_cycle_loss_usdt"] = pending_before

        if carry_loss > 0 and break_even_price > 0:
            exit_adjustment_pct = (
                (projection.tp_price - break_even_price) / break_even_price
            ) * 100.0
        else:
            exit_adjustment_pct = None
        _attach_exit_relief_metadata(
            runtime_state,
            config=active_config,
            exit_adjustment_pct=exit_adjustment_pct,
        )
        return projection

    def wrapped_build_exit_intents(*args: Any, **kwargs: Any) -> list[StrategyIntent]:
        intents = original_build_exit_intents(*args, **kwargs)
        active_config: CycleShortTpReliefConfig | None = getattr(
            strategy, "_cycle_short_tp_relief_config", None
        )
        if active_config is None or not active_config.enabled or not intents:
            return intents

        runtime_state = args[1] if len(args) > 1 else kwargs.get("runtime_state")
        if runtime_state is None:
            return intents

        state = runtime_state.strategy_state
        relief_meta = dict(state.get("_last_exit_short_tp_relief") or {})
        if not relief_meta:
            return intents

        trade_block_id = str(state.get("trade_block_id") or "")
        cumulative_carry_loss = get_cumulative_carry_loss(state, trade_block_id=trade_block_id)
        for intent in intents:
            purpose = preserve_bot_purpose(intent.purpose)
            if purpose not in {"LONG_TP_EXIT", "SHORT_SL_EXIT", "LONG_TP_EXIT_RECOVERY", "SHORT_SL_EXIT_RECOVERY"}:
                continue
            metadata = dict(intent.metadata or {})
            metadata.update(
                {
                    "cycle_short_tp_relief_enabled": active_config.enabled,
                    "cumulative_carry_loss": cumulative_carry_loss,
                    "exit_adjustment_pct": relief_meta.get("exit_adjustment_pct"),
                    "carry_uncovered_loss_to_exit": active_config.carry_uncovered_loss_to_exit,
                }
            )
            intent.metadata = metadata
        return intents

    strategy._build_short_tp_follow_up = wrapped_build_short_follow_up
    strategy._calculate_tp_projection = wrapped_calculate_tp_projection
    strategy._build_exit_intents = wrapped_build_exit_intents
