"""Shared frozen EDC strategy engine (XRP-canonical)."""

from .candidates import detect_candidates_for_scopes, evaluate_candidates_canonical
from .entry import entry_rule_id, next_signal_tf_open
from .market_data import load_strategy_market_data, prepare_tf_frames
from .outcomes import simulate_canonical_trade
from .semantics import (
    CANONICAL_STRATEGY_ID,
    DIFF_RESOLUTIONS,
    ENTRY_RULE,
    MULTICOIN_DETECTION_SCOPES,
    REQUIRE_FULL_HORIZON,
)

__all__ = [
    "CANONICAL_STRATEGY_ID",
    "DIFF_RESOLUTIONS",
    "ENTRY_RULE",
    "MULTICOIN_DETECTION_SCOPES",
    "REQUIRE_FULL_HORIZON",
    "detect_candidates_for_scopes",
    "entry_rule_id",
    "evaluate_candidates_canonical",
    "load_strategy_market_data",
    "next_signal_tf_open",
    "prepare_tf_frames",
    "simulate_canonical_trade",
]
