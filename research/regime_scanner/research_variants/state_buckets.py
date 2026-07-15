"""Canonical research-only state → bucket classification.

This module is the single source of truth for how raw scanner ``TrendState``
values are grouped into research regime buckets. It is used ONLY for research
metrics/reporting and never influences scanner behaviour.

The 12 canonical TrendState values (see ``trend_state_machine.TrendState``):

    neutral,
    bearish_warning, early_bearish, strong_bearish, bearish_weakening, bottoming,
    bullish_warning, early_bullish, strong_bullish, bullish_weakening, topping,
    unavailable

Mapping rationale (documented per state, not guessed by name):

* ``strong_*`` / ``early_*`` / ``*_warning`` are directional trend states -> up/down.
* ``neutral`` is the explicit no-trend/range state -> range.
* ``*_weakening`` / ``topping`` / ``bottoming`` are the state machine's *turning*
  states: a prior trend that is losing conviction or a top/bottom forming. They
  are intermediate/ambiguous by construction (see MIN_HOLD_DEFAULTS and the
  topping/bottoming turn-evidence logic) -> transition.
* ``unavailable`` is warmup / no-data -> unknown.

The mapping is exhaustive: every raw state maps to exactly one bucket, so
``sum(bucket_counts) == number_of_state_rows`` always holds.
"""

from __future__ import annotations

from typing import Any

BUCKET_UPTREND = "uptrend"
BUCKET_DOWNTREND = "downtrend"
BUCKET_RANGE = "range"
BUCKET_TRANSITION = "transition"
BUCKET_UNKNOWN = "unknown"

ALL_BUCKETS = (
    BUCKET_UPTREND,
    BUCKET_DOWNTREND,
    BUCKET_RANGE,
    BUCKET_TRANSITION,
    BUCKET_UNKNOWN,
)

# state -> (bucket, reason). Kept identical to the prior stability.py taxonomy so
# existing score_version=1 results remain reproducible.
STATE_BUCKET_MAP: dict[str, tuple[str, str]] = {
    "strong_bullish": (BUCKET_UPTREND, "confirmed bullish trend"),
    "early_bullish": (BUCKET_UPTREND, "emerging bullish trend"),
    "bullish_warning": (BUCKET_UPTREND, "bullish trend with early warning"),
    "strong_bearish": (BUCKET_DOWNTREND, "confirmed bearish trend"),
    "early_bearish": (BUCKET_DOWNTREND, "emerging bearish trend"),
    "bearish_warning": (BUCKET_DOWNTREND, "bearish trend with early warning"),
    "neutral": (BUCKET_RANGE, "explicit no-trend / range state"),
    "bullish_weakening": (BUCKET_TRANSITION, "post-bullish turning/weakening state"),
    "bearish_weakening": (BUCKET_TRANSITION, "post-bearish turning/weakening state"),
    "topping": (BUCKET_TRANSITION, "top formation / turning state"),
    "bottoming": (BUCKET_TRANSITION, "bottom formation / turning state"),
    "unavailable": (BUCKET_UNKNOWN, "warmup / unavailable / no data"),
}

# Frozensets derived from the canonical map (consumed by stability.py for parity).
UPTREND_STATES = frozenset(s for s, (b, _) in STATE_BUCKET_MAP.items() if b == BUCKET_UPTREND)
DOWNTREND_STATES = frozenset(s for s, (b, _) in STATE_BUCKET_MAP.items() if b == BUCKET_DOWNTREND)
RANGE_STATES = frozenset(s for s, (b, _) in STATE_BUCKET_MAP.items() if b == BUCKET_RANGE)
TRANSITION_STATES = frozenset(s for s, (b, _) in STATE_BUCKET_MAP.items() if b == BUCKET_TRANSITION)
UNKNOWN_STATES = frozenset(s for s, (b, _) in STATE_BUCKET_MAP.items() if b == BUCKET_UNKNOWN)


def classify_research_state_bucket(snapshot: Any) -> str:
    """Return the research regime bucket for a state string or trend-state row.

    Accepts either a raw state string or a mapping with a ``state`` key. Unknown
    or empty states map to ``unknown`` (they behave like ``unavailable``).
    """
    if isinstance(snapshot, str):
        state = snapshot
    elif isinstance(snapshot, dict):
        state = str(snapshot.get("state") or "")
    else:
        state = str(getattr(snapshot, "state", "") or "")
    entry = STATE_BUCKET_MAP.get(state)
    if entry is None:
        return BUCKET_UNKNOWN
    return entry[0]


def bucket_reason(state: str) -> str:
    entry = STATE_BUCKET_MAP.get(str(state))
    return entry[1] if entry else "unmapped -> unknown"


def bucket_counts(states: list[str]) -> dict[str, int]:
    counts = {b: 0 for b in ALL_BUCKETS}
    for s in states:
        counts[classify_research_state_bucket(s)] += 1
    return counts
