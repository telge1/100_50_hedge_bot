"""Shared models, ticks, safe math, field semantics."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import TICK_BY_SYMBOL
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.analysis import safe_div, safe_float


def tick_size(symbol: str) -> float:
    return float(TICK_BY_SYMBOL.get(symbol.upper(), 0.01))


def ticks_between(a: float, b: float, symbol: str) -> float | None:
    t = tick_size(symbol)
    if t <= 0:
        return None
    return abs(a - b) / t


def bps_between(a: float, b: float, ref: float | None = None) -> float | None:
    mid = ref if ref is not None else max(a, b)
    if mid is None or mid <= 0:
        return None
    return abs(a - b) / mid * 10000.0


# Bid wall attacked by aggressive sells; Ask wall by aggressive buys.
ATTACK_SIDE_BY_WALL = {"BID": "Sell", "ASK": "Buy"}
WALL_SIDE_BY_DIRECTION = {"LONG": "BID", "SHORT": "ASK"}  # V1 walls.py mapping


RESOLUTION_CLASSES = (
    "DEFENDED",
    "ABSORBED_REFILLED",
    "PULLED_BEFORE_CONTACT",
    "PULLED_ON_CONTACT",
    "CLEAN_BREAK_CONTINUATION",
    "BREAK_RECLAIM",
    "FLOW_DIED_NO_DEFENSE",
    "AMBIGUOUS",
    "DATA_UNAVAILABLE",
)


FIELD_SEMANTICS: list[dict[str, Any]] = [
    {
        "field": "attack_id",
        "semantic_role": "metadata",
        "feature_available_at": "episode_creation",
        "causal_cutoff_ms": None,
        "notes": "deterministic primary/secondary attack id",
    },
    {
        "field": "wall_notional_at_contact",
        "semantic_role": "causal_feature",
        "feature_available_at": "first_contact_at",
        "causal_cutoff_ms": 0,
        "notes": "visible wall size at first contact",
    },
    {
        "field": "depletion_ratio_5s",
        "semantic_role": "causal_feature",
        "feature_available_at": "first_contact_at+5s",
        "causal_cutoff_ms": 5000,
        "notes": "visible_size_removed / visible_size_at_contact",
    },
    {
        "field": "refill_ratio_5s",
        "semantic_role": "causal_feature",
        "feature_available_at": "first_contact_at+5s",
        "causal_cutoff_ms": 5000,
        "notes": "proxy; no order ids",
    },
    {
        "field": "resolution_class_60s",
        "semantic_role": "ex_post_label",
        "feature_available_at": "first_contact_at+60s",
        "causal_cutoff_ms": None,
        "notes": "uses future path; never as entry feature",
    },
    {
        "field": "fwd_return_bps_60s_at_cutoff_3s",
        "semantic_role": "outcome",
        "feature_available_at": "decision_cutoff+horizon",
        "causal_cutoff_ms": 3000,
        "notes": "outcome clock starts at causal decision cutoff",
    },
    {
        "field": "attribution_confidence",
        "semantic_role": "metadata",
        "feature_available_at": "post_contact",
        "causal_cutoff_ms": None,
        "notes": "HIGH/MEDIUM/LOW due to missing order ids / stream skew",
    },
]


def empty_proxy() -> dict[str, Any]:
    return {
        "visible_size_at_contact": None,
        "visible_size_removed": None,
        "traded_at_level_proxy": None,
        "depletion_ratio": None,
        "refill_ratio": None,
        "trade_to_display_ratio": None,
        "resilience_ratio": None,
        "price_response_per_notional": None,
        "pull_proxy": False,
        "absorption_proxy": False,
        "attribution_confidence": "LOW",
        "timing_alignment_ms": None,
        "unexplained_size_change": None,
        "proxy_limitations": "no_order_ids;book_trade_stream_skew_possible",
    }


__all__ = [
    "safe_div",
    "safe_float",
    "tick_size",
    "ticks_between",
    "bps_between",
    "ATTACK_SIDE_BY_WALL",
    "RESOLUTION_CLASSES",
    "FIELD_SEMANTICS",
    "empty_proxy",
]
