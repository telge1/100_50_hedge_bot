"""Central fail-closed eligibility rules for Phase-1 candidates."""

from __future__ import annotations

from datetime import datetime

from .contract import ExclusionReason
from .models import FrozenTarget, utc


def evaluate_candidate(
    *,
    t0: datetime,
    price_t0: float | None,
    upper: FrozenTarget | None,
    lower: FrozenTarget | None,
    warmup_complete: bool,
    source_complete: bool,
    horizon_closed: bool,
    require_complete: bool,
) -> ExclusionReason | None:
    t0_u = utc(t0)
    if price_t0 is None:
        return ExclusionReason.PRICE_MISSING_AT_T0
    if upper is None:
        return ExclusionReason.MISSING_UPPER_TARGET
    if lower is None:
        return ExclusionReason.MISSING_LOWER_TARGET
    if upper.available_at > t0_u or lower.available_at > t0_u:
        return ExclusionReason.CAUSALITY_VIOLATION
    if upper.lower_price > upper.upper_price or lower.lower_price > lower.upper_price:
        return ExclusionReason.TARGET_ORDER_INVALID
    if lower.upper_price >= upper.lower_price:
        return ExclusionReason.TARGETS_OVERLAP
    if price_t0 >= upper.touch_price or price_t0 <= lower.touch_price:
        return ExclusionReason.TARGET_ALREADY_TOUCHED_AT_T0
    if not warmup_complete:
        return ExclusionReason.INSUFFICIENT_WARMUP
    if not horizon_closed:
        return ExclusionReason.HORIZON_NOT_CLOSED
    if require_complete and not source_complete:
        return ExclusionReason.SOURCE_COVERAGE_INCOMPLETE
    return None
