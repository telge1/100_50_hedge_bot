"""StrategyIntent and exit-level diagnostics for backtests (Phase 8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from .purpose_utils import preserve_bot_purpose, purpose_log_fields
from .simulated_execution import evaluate_order_touch, normalize_trigger_direction, order_trigger_side
from .simulated_order_book import SyntheticCandle, VirtualOrder

INTENT_METADATA_EXCERPT_KEYS = (
    "cycle_index",
    "cycle_role",
    "trade_block_id",
    "reason",
    "order_role",
    "target_profit",
    "loss_to_recover",
    "pnl",
    "entry_price",
    "avg_price",
    "base_price",
    "distance_pct",
    "cancel_purpose",
    "replace_existing",
    "action",
    "intent_type",
)

TRIGGER_ORDER_TYPES = frozenset({"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"})


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata_excerpt(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    excerpt: dict[str, Any] = {}
    for key in INTENT_METADATA_EXCERPT_KEYS:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        excerpt[key] = value
    return excerpt


def build_intent_log_entry(
    intent: StrategyIntent,
    *,
    timestamp: datetime | None,
    candle_index: int | None,
    event_source: str,
    source_fill_purpose: str | None = None,
) -> dict[str, Any]:
    """Build a diagnostic record for one StrategyIntent."""
    metadata = dict(intent.metadata or {})
    purpose_fields = purpose_log_fields(intent.purpose, metadata)
    entry: dict[str, Any] = {
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
        "candle_index": candle_index,
        "event_source": event_source,
        "purpose": purpose_fields.get("purpose"),
        "purpose_original": purpose_fields.get("purpose_original"),
        "side": intent.side,
        "qty": float(intent.qty),
        "price": _safe_float(intent.price),
        "trigger_price": _safe_float(intent.trigger_price),
        "trigger_direction": intent.trigger_direction,
        "order_type": intent.order_type,
        "reduce_only": bool(intent.reduce_only),
        "raw_intent_class": type(intent).__name__,
        "metadata_excerpt": _metadata_excerpt(metadata),
    }
    if source_fill_purpose:
        entry["source_fill_purpose"] = preserve_bot_purpose(source_fill_purpose)

    action = metadata.get("action") or metadata.get("intent_type")
    if action is not None:
        entry["intent_type"] = action

    replace_existing = metadata.get("replace_existing")
    if replace_existing is not None:
        entry["replace_existing"] = replace_existing
    cancel_purpose = metadata.get("cancel_purpose")
    if cancel_purpose is not None:
        entry["cancel_purpose"] = preserve_bot_purpose(cancel_purpose)

    return entry


def _is_trigger_order(intent: StrategyIntent, order: VirtualOrder | None = None) -> bool:
    order_type = str(
        (order.order_type if order is not None else intent.order_type) or ""
    ).upper()
    if order_type in TRIGGER_ORDER_TYPES:
        return True
    if intent.trigger_price is not None or (order is not None and order.trigger_price is not None):
        return True
    return False


def build_intent_to_order_mapping(
    intent: StrategyIntent,
    order: VirtualOrder,
    *,
    intent_log_index: int | None = None,
) -> dict[str, Any]:
    """Map StrategyIntent fields to VirtualOrder and collect mapping warnings."""
    warnings: list[str] = []
    purpose = preserve_bot_purpose(intent.purpose)
    if not purpose:
        warnings.append("missing_purpose")
    if not str(intent.side or "").strip():
        warnings.append("missing_side")

    intent_price = _safe_float(intent.price)
    intent_trigger = _safe_float(intent.trigger_price)
    mapped_price = _safe_float(order.price)
    mapped_trigger = _safe_float(order.trigger_price)

    if intent_price is not None and mapped_price is not None and intent_price != mapped_price:
        warnings.append("price_mismatch")
    if intent_trigger is not None and mapped_trigger is not None and intent_trigger != mapped_trigger:
        warnings.append("trigger_mismatch")

    if _is_trigger_order(intent, order) and intent.trigger_direction is None and order.trigger_direction is None:
        warnings.append("missing_trigger_direction")

    mapping: dict[str, Any] = {
        "intent_log_index": intent_log_index,
        "intent_purpose": purpose or None,
        "intent_side": intent.side,
        "intent_price": intent_price,
        "intent_trigger_price": intent_trigger,
        "mapped_order_price": mapped_price,
        "mapped_trigger_price": mapped_trigger,
        "mapped_side": order.side,
    }
    if warnings:
        mapping["mapping_warning"] = "|".join(warnings)
    return mapping


def diagnose_exit_level(
    order: VirtualOrder,
    *,
    candles_after: Iterable[SyntheticCandle],
    created_candle_index: int | None = None,
    state: RuntimeState | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnose whether a resting exit order was ever touchable after creation."""
    candle_list = list(candles_after)
    trigger_price = _safe_float(order.trigger_price) or _safe_float(order.price)
    check_price = trigger_price

    max_high: float | None = None
    min_low: float | None = None
    first_touch_time: str | None = None
    was_touchable = False

    for candle in candle_list:
        high = float(candle.high if candle.high is not None else candle.close)
        low = float(candle.low if candle.low is not None else candle.close)
        max_high = high if max_high is None else max(max_high, high)
        min_low = low if min_low is None else min(min_low, low)
        touch = evaluate_order_touch(order, candle)
        if touch.touched:
            was_touchable = True
            if first_touch_time is None and candle.timestamp is not None:
                first_touch_time = candle.timestamp.isoformat()

    trigger_mode = normalize_trigger_direction(order.trigger_direction)
    if order.trigger_price is not None:
        trigger_check_side = f"trigger_{trigger_mode or 'unknown'}"
    else:
        trigger_check_side = order_trigger_side(order)
    distance_to_max_high_pct: float | None = None
    distance_to_min_low_pct: float | None = None
    if check_price is not None and check_price > 0:
        if max_high is not None:
            distance_to_max_high_pct = ((max_high - check_price) / check_price) * 100.0
        if min_low is not None:
            distance_to_min_low_pct = ((min_low - check_price) / check_price) * 100.0

    candles_waited = len(candle_list)
    order_age_candles = candles_waited
    if created_candle_index is not None and candle_list:
        last_index = getattr(candle_list[-1], "candle_index", None)
        if last_index is not None:
            order_age_candles = max(0, int(last_index) - int(created_candle_index))

    diagnostic: dict[str, Any] = {
        "order_id": order.order_id,
        "final_order_purpose": preserve_bot_purpose(order.purpose),
        "final_order_side": order.side,
        "final_order_price": _safe_float(order.price),
        "final_order_trigger_price": trigger_price,
        "trigger_direction": order.trigger_direction,
        "trigger_check_side": trigger_check_side,
        "created_candle_index": created_candle_index,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "candles_waited": candles_waited,
        "order_age_candles": order_age_candles,
        "was_touchable_after_created": was_touchable,
        "first_touch_time_after_created": first_touch_time,
        "max_high_after_created": max_high,
        "min_low_after_created": min_low,
        "distance_to_max_high_pct": distance_to_max_high_pct,
        "distance_to_min_low_pct": distance_to_min_low_pct,
    }
    if state is not None:
        strategy_state = state.strategy_state if isinstance(state, RuntimeState) else dict(state)
        excerpt_keys = ("active_cycle_index", "next_required_purpose", "initial_structure_built")
        diagnostic["strategy_state_excerpt"] = {
            key: strategy_state.get(key)
            for key in excerpt_keys
            if key in strategy_state
        }
    return diagnostic


def compute_final_active_order_diagnostics(
    orders: list[VirtualOrder],
    *,
    all_candles: list[SyntheticCandle],
    order_created_candle_index: dict[str, int | None] | None = None,
    state: RuntimeState | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build exit diagnostics for each final active order."""
    diagnostics: list[dict[str, Any]] = []
    created_map = order_created_candle_index or {}
    for order in orders:
        created_index = created_map.get(order.order_id)
        if created_index is None:
            created_index = getattr(order, "created_candle_index", None)
        start_idx = int(created_index) if created_index is not None else 0
        candles_after = all_candles[start_idx + 1 :] if start_idx + 1 < len(all_candles) else []
        diagnostic = diagnose_exit_level(
            order,
            candles_after=candles_after,
            created_candle_index=created_index,
            state=state,
        )
        diagnostics.append(diagnostic)
    return diagnostics


def summarize_exit_diagnostics(diagnostics: list[dict[str, Any]]) -> str:
    """Compact one-line summary for CSV."""
    if not diagnostics:
        return ""
    parts: list[str] = []
    for item in diagnostics:
        purpose = item.get("final_order_purpose") or "UNKNOWN"
        trigger = item.get("final_order_trigger_price")
        touchable = item.get("was_touchable_after_created")
        max_high = item.get("max_high_after_created")
        parts.append(f"{purpose}@{trigger}|touch={touchable}|maxH={max_high}")
    return "; ".join(parts)


def last_intent_summary(intent_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract compact last-intent fields for CSV."""
    if not intent_log:
        return {
            "last_intent_purpose": "",
            "last_intent_trigger_price": "",
            "last_intent_price": "",
            "last_intent_source_fill_purpose": "",
        }
    last = intent_log[-1]
    return {
        "last_intent_purpose": last.get("purpose") or "",
        "last_intent_trigger_price": last.get("trigger_price") if last.get("trigger_price") is not None else "",
        "last_intent_price": last.get("price") if last.get("price") is not None else "",
        "last_intent_source_fill_purpose": last.get("source_fill_purpose") or "",
    }
