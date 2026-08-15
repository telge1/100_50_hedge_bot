"""Outcome taxonomy: labels may use future info; features must not."""

from __future__ import annotations

from typing import Any

OUTCOME_TAXONOMY = (
    "BREAK_ACCEPTED",
    "RECLAIM_FAST",
    "RECLAIM_SLOW",
    "HOLD_NO_BREAK",
    "EXCLUDED",
)

# Source labels → coarse taxonomy. Future-looking outcome only.
_BREAK_ACCEPTED = {
    "BREAKDOWN_CONFIRMED",
    "BREAKOUT_CONFIRMED",
    "BEARISH_ACCEPTANCE",
    "RECLAIM_THEN_BREAK_CONTINUATION",
}
_RECLAIM = {
    "RECLAIM_CONFIRMED",
    "RECLAIM_DOWN_CONFIRMED",
    "FAILED_BREAK_SHORT_CANDIDATE",
}
_HOLD = {
    "UNRESOLVED_WITHIN_MAX_WINDOW",
}
_EXCLUDED = {
    "EVENT_DATA_INVALID",
}

FAST_RECLAIM_MAX_MINUTES = 15.0


def map_outcome_label(
    raw_outcome: str,
    *,
    minutes_after_break: float | None = None,
    reclaim_minutes: float | None = None,
) -> dict[str, Any]:
    """Map catalog/inventory outcome to unified taxonomy.

    Outcome may use post-event information. This function is never a feature
    cutoff input.
    """
    raw = (raw_outcome or "").strip()
    minutes = reclaim_minutes if reclaim_minutes is not None else minutes_after_break

    if raw in _EXCLUDED:
        label = "EXCLUDED"
        reason = "source_invalid"
    elif raw in _BREAK_ACCEPTED:
        label = "BREAK_ACCEPTED"
        reason = "acceptance_or_continuation"
    elif raw in _RECLAIM:
        if minutes is not None and minutes <= FAST_RECLAIM_MAX_MINUTES:
            label = "RECLAIM_FAST"
            reason = f"reclaim_within_{FAST_RECLAIM_MAX_MINUTES:g}m"
        elif minutes is not None:
            label = "RECLAIM_SLOW"
            reason = "reclaim_after_fast_window"
        else:
            # Failed break / reclaim confirmed without timing → treat as fast
            label = "RECLAIM_FAST"
            reason = "reclaim_or_failed_break_no_timing"
    elif raw in _HOLD:
        label = "HOLD_NO_BREAK"
        reason = "unresolved_within_max_window"
    else:
        label = "EXCLUDED"
        reason = f"unmapped_raw={raw}"

    return {
        "raw_outcome": raw,
        "outcome_label": label,
        "outcome_map_reason": reason,
        "reclaim_minutes_used": minutes,
        "uses_future_info": True,
        "note": "outcome_label may use post-event data; features must cut off at T",
    }


def is_comparison_eligible(outcome_label: str) -> bool:
    return outcome_label in {"BREAK_ACCEPTED", "RECLAIM_FAST", "RECLAIM_SLOW", "HOLD_NO_BREAK"}
