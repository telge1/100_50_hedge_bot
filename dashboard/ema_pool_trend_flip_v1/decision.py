"""Entry flip/align/no-trade decision. EMA and pool must agree; stoch alone never flips."""

from __future__ import annotations

from typing import Any

from .schema import (
    DECISION_ALIGNED,
    DECISION_FLIPPED,
    DECISION_NO_TRADE,
    REASON_NO_SL,
    REASON_TREND,
)


def decide(
    *,
    original_direction: str,
    unique_up: bool,
    unique_down: bool,
    bullish_pool: bool,
    bearish_pool: bool,
    protection: dict[str, Any] | None,
) -> dict[str, Any]:
    orig = original_direction.upper().strip()
    ema_up = unique_up and not unique_down
    ema_down = unique_down and not unique_up
    ctx_long = ema_up and bullish_pool
    ctx_short = ema_down and bearish_pool

    if orig == "SHORT" and ctx_long:
        executed = "LONG"
        decision = DECISION_FLIPPED
        reason = "STOCH_SHORT_FLIPPED_TO_EMA_POOL_LONG"
    elif orig == "LONG" and ctx_short:
        executed = "SHORT"
        decision = DECISION_FLIPPED
        reason = "STOCH_LONG_FLIPPED_TO_EMA_POOL_SHORT"
    elif orig == "LONG" and ctx_long:
        executed = "LONG"
        decision = DECISION_ALIGNED
        reason = "ALIGNED_LONG"
    elif orig == "SHORT" and ctx_short:
        executed = "SHORT"
        decision = DECISION_ALIGNED
        reason = "ALIGNED_SHORT"
    else:
        return {
            "decision": DECISION_NO_TRADE,
            "executed_direction": None,
            "entry_reason": REASON_TREND,
            "no_trade_reason": REASON_TREND,
        }

    if protection is None:
        return {
            "decision": DECISION_NO_TRADE,
            "executed_direction": executed,
            "entry_reason": REASON_NO_SL,
            "no_trade_reason": REASON_NO_SL,
            "intended_direction": executed,
        }
    return {
        "decision": decision,
        "executed_direction": executed,
        "entry_reason": reason,
        "no_trade_reason": None,
    }


def filter_variant_decision(row: dict[str, Any]) -> dict[str, Any]:
    """EMA_POOL_DIRECTION_FILTER_V1: block countertrend, never flip."""
    out = dict(row)
    if row.get("decision") == DECISION_FLIPPED:
        out["decision"] = "BLOCKED"
        out["executed_direction"] = None
        out["entry_reason"] = "COUNTERTREND_BLOCKED"
        out["no_trade_reason"] = "COUNTERTREND_BLOCKED"
        out["variant"] = "EMA_POOL_DIRECTION_FILTER_V1"
        out["outcome"] = None
        out["gross_pnl_pct"] = None
        out["fees_pct"] = None
        out["net_pnl_pct"] = None
    else:
        out["variant"] = "EMA_POOL_DIRECTION_FILTER_V1"
    return out
