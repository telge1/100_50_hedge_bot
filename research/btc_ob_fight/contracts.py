"""Contracts, enums, and forbidden reason codes for Phase 0–1."""

from __future__ import annotations

from typing import Final

ANALYSIS_STATUS_FACTS_READY = "FACTS_READY_RULES_UNFROZEN"
ANALYSIS_STATUS_DATA_INSUFFICIENT = "DATA_INSUFFICIENT"

FORBIDDEN_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "BUYERS_CONTROL",
        "SELLERS_CONTROL",
        "BUYER_ABSORPTION",
        "SELLER_ABSORPTION",
        "BREAKOUT_ACCEPTED",
        "FAILED_ACCEPTANCE",
        "RECLAIM_CONFIRMED",
        "LONG_READY",
        "SHORT_READY",
        "NO_TRADE",
        "CONTROL_CONFIRMED",
        "BREAKOUT_CONFIRMED",
        "FAILED_BREAKOUT_CONFIRMED",
        "ABSORPTION",
        "LONG",
        "SHORT",
    }
)

FORBIDDEN_INTERPRETATION_TERMS: Final[frozenset[str]] = frozenset(
    {
        "BREAKOUT_CONFIRMED",
        "FAILED_BREAKOUT",
        "BUYERS_CONTROL",
        "SELLERS_CONTROL",
        "ABSORPTION",
        "LONG",
        "SHORT",
        "RETEST_HELD",
        "RETEST_FAILED",
    }
)

LEVEL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "LEVEL_TOUCHED",
        "LEVEL_CROSSED_UP",
        "LEVEL_CROSSED_DOWN",
        "LEVEL_RETURNED_BELOW",
        "LEVEL_RETURNED_ABOVE",
    }
)

WALL_HEURISTIC_TYPES: Final[frozenset[str]] = frozenset(
    {
        "HEURISTIC_WALL_PULLED_OR_CANCELLED",
        "HEURISTIC_TRADE_BACKED_REDUCTION",
        "HEURISTIC_WALL_REFILLED_OR_ADDED",
        "HEURISTIC_WALL_PERSISTED",
    }
)
