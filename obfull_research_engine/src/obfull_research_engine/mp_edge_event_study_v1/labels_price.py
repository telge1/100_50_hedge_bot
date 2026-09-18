"""Price-only labels — definitions live in touch_detect; this module re-exports docs."""

from __future__ import annotations

LABEL_DEFINITIONS = {
    "ABSORB": (
        "Touch occurred; max_penetration_bps < min_penetration_bps; mid moved at least "
        "min_penetration_bps in fade direction past the inner zone edge. "
        "Trigger = fade-confirm tick."
    ),
    "FAILED_BREAK": (
        "Relevant penetration (>= min_penetration_bps); full reclaim within "
        "max_reclaim_delay_s from penetration_start; reclaim held for reclaim_hold_s "
        "with reclaim_tolerance_bps (default 0 = any wrong-side 100ms tick aborts hold). "
        "Trigger = reclaim hold confirmation time."
    ),
    "TRUE_BREAK": (
        "Relevant penetration; continuous time beyond outer edge >= true_break_acceptance_s "
        "without confirmed reclaim. Trigger = acceptance confirmation time."
    ),
    "UNRESOLVED": (
        "No label fully confirmed before window end / profile change / coverage gap. "
        "is_censored=True with censor_reason. Never counted as a loss or TRUE_BREAK."
    ),
}

__all__ = ["LABEL_DEFINITIONS"]
