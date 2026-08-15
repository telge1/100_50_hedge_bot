"""Models for TEM decisive-break track (v3, research-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SIGNAL_VERSION_V3 = "tem_decisive_break_v3_d1d2d3"
DECISIVE_RULE_ID = "tem_decisive_break_v3_frozen_candidate_20260723"

# Primary frozen config (do not retune during evaluation)
STABILIZE_4H_BARS = 3
RANGE_LOOKBACK_BARS = 6
RECLAIM_CONFIRM_BARS = 1  # next completed 4h only
DIAG_STABILIZE_VARIANTS = (2, 3, 6)


class DecisiveState(str, Enum):
    DECISIVE_NOT_ARMED = "DECISIVE_NOT_ARMED"
    DECISIVE_ARMING = "DECISIVE_ARMING"
    DECISIVE_LEVEL_READY = "DECISIVE_LEVEL_READY"
    DECISIVE_BREAK_PENDING = "DECISIVE_BREAK_PENDING"
    DECISIVE_BREAK_RECLAIMED = "DECISIVE_BREAK_RECLAIMED"
    DECISIVE_BREAK_CONFIRMED = "DECISIVE_BREAK_CONFIRMED"


@dataclass
class DecisiveLevel:
    value: float
    level_type: str  # confirmed_swing_low_4h | range_support_4h
    source: str
    formed_ts: str
    confirmed_ts: str
    lower_high_ts: str | None = None
    stabilize_bars_used: int = STABILIZE_4H_BARS


@dataclass
class DecisiveRuntime:
    state: DecisiveState = DecisiveState.DECISIVE_NOT_ARMED
    arm_ts: str | None = None
    arm_bar_h4: int | None = None
    v2_break_level: float | None = None
    v2_first_break_ts: str | None = None
    stabilize_bars: int = STABILIZE_4H_BARS
    bars_since_arm: int = 0
    level: DecisiveLevel | None = None
    pending_ts: str | None = None
    pending_close_decision: str | None = None
    reclaim_ts: str | None = None
    confirmed_ts: str | None = None
    reason: str | None = None
    last_lower_high_ts: str | None = None
    last_lower_high_price: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    level_history: list[dict[str, Any]] = field(default_factory=list)
    # diagnostic: would reclaim within 2 bars?
    diag_reclaim_within_2_bars: bool | None = None


DECISIVE_SEMANTICS: dict = {
    "rule_id": DECISIVE_RULE_ID,
    "signal_version": SIGNAL_VERSION_V3,
    "candidates_primary": ["D1_deeper_confirmed_support", "D2_range_support", "D3_lower_high_tag"],
    "candidates_diagnostic_only": ["D4", "D5_stabilize_2_3_6", "D6", "D7"],
    "stabilize_4h_bars_primary": STABILIZE_4H_BARS,
    "stabilize_variants_diagnostic": list(DIAG_STABILIZE_VARIANTS),
    "range_lookback_bars": RANGE_LOOKBACK_BARS,
    "reclaim_window_4h_bars": RECLAIM_CONFIRM_BARS,
    "reclaim_rule": "next_completed_4h_close >= decisive_level (close only, no wick)",
    "level_priority": [
        "1_most_recent_confirmed_swing_low_4h_after_stabilize",
        "2_range_support_min_low_of_last_N_if_no_swing_yet",
    ],
    "arm_trigger": "first v2 BREAK_PENDING_4H signal_available_ts",
    "does_not_mutate_v2": True,
}
