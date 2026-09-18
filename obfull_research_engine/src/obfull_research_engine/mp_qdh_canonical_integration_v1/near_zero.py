"""Near-zero / exhausted queue policy (no trading threshold)."""

from __future__ import annotations

from typing import Any

from . import NEAR_ZERO_QUEUE_WARN_QTY, QUEUE_EXHAUSTED_EPS


def apply_queue_policy(
    *,
    queue_remaining_qty: float | None,
    qdh_base: float | None,
    queue_runway_seconds: float | None,
) -> dict[str, Any]:
    """Annotate QDH validity without inventing profit thresholds."""
    q = float(queue_remaining_qty) if queue_remaining_qty is not None else None
    if q is None:
        return {
            "qdh_valid": False,
            "qdh_invalid_reason": "QUEUE_UNKNOWN",
            "queue_state": "UNKNOWN",
            "queue_runway_seconds": queue_runway_seconds,
            "canonical_qdh_base": qdh_base,
            "near_zero_queue_warning": False,
            "queue_fraction_remaining": None,
        }
    if q <= QUEUE_EXHAUSTED_EPS:
        return {
            "qdh_valid": False,
            "qdh_invalid_reason": "QUEUE_EXHAUSTED",
            "queue_state": "QUEUE_EXHAUSTED",
            "queue_runway_seconds": 0.0,
            "canonical_qdh_base": None,  # do not emit huge artificial hazard
            "near_zero_queue_warning": False,
            "queue_fraction_remaining": 0.0,
            "raw_qdh_base_suppressed": qdh_base,
        }
    warn = q < float(NEAR_ZERO_QUEUE_WARN_QTY)
    return {
        "qdh_valid": True,
        "qdh_invalid_reason": None,
        "queue_state": "NEAR_ZERO_QUEUE_UNCALIBRATED" if warn else "QUEUE_POSITIVE",
        "queue_runway_seconds": queue_runway_seconds,
        "canonical_qdh_base": qdh_base,
        "near_zero_queue_warning": warn,
        "queue_fraction_remaining": None,  # filled by caller when baseline known
        "raw_qdh_base": qdh_base,
    }
