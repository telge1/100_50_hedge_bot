from __future__ import annotations

from typing import Any


def _coerce_trade_block_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _context_trade_block_id(context: object) -> str | None:
    if not isinstance(context, dict):
        return None
    return _coerce_trade_block_id(context.get("trade_block_id"))


def resolve_active_trade_block_id(state: dict[str, Any] | None) -> str | None:
    trade_block_id, _source = resolve_trade_block_id_with_source(state)
    return trade_block_id


def resolve_trade_block_id_with_source(
    state: dict[str, Any] | None,
    *,
    payload_trade_block_id: object = None,
) -> tuple[str | None, str]:
    explicit = _coerce_trade_block_id(payload_trade_block_id)
    if explicit:
        return explicit, "payload"

    if not state:
        return None, "missing"

    for key, source in (
        ("trade_block_id", "state_trade_block_id"),
        ("last_trade_block_id", "state_last_trade_block_id"),
    ):
        trade_block_id = _coerce_trade_block_id(state.get(key))
        if trade_block_id:
            return trade_block_id, source

    trading_stop_context = state.get("final_exit_trading_stop_context")
    trade_block_id = _context_trade_block_id(trading_stop_context)
    if trade_block_id:
        return trade_block_id, "final_exit_trading_stop_context"

    for context_key in (
        "final_long_exit_order_context",
        "final_short_exit_order_context",
    ):
        trade_block_id = _context_trade_block_id(state.get(context_key))
        if trade_block_id:
            return trade_block_id, context_key

    cycle_state = state.get("cycle_state")
    if isinstance(cycle_state, dict):
        trade_block_id = _coerce_trade_block_id(cycle_state.get("trade_block_id"))
        if trade_block_id:
            return trade_block_id, "cycle_state"

    return None, "missing"


def preserve_last_trade_block_id_before_clear(state: dict[str, Any]) -> None:
    current_trade_block_id = state.get("trade_block_id")
    if current_trade_block_id:
        state["last_trade_block_id"] = current_trade_block_id
