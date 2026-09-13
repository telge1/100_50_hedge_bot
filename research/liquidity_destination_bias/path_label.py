"""Pure first-touch outcome labeling from a future-only price path."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .contract import AMBIGUITY_MILLISECONDS, Outcome
from .models import FrozenTarget, PriceBucket, utc


def label_first_touch(
    path: Iterable[PriceBucket],
    *,
    t0: datetime,
    horizon_end: datetime,
    upper: FrozenTarget,
    lower: FrozenTarget,
    ambiguity_milliseconds: int = AMBIGUITY_MILLISECONDS,
) -> dict:
    """Label frozen target touches; path data may only be used by this function."""
    t0_u, end_u = utc(t0), utc(horizon_end)
    upper_touch: datetime | None = None
    lower_touch: datetime | None = None
    for bucket in sorted(path, key=lambda x: x.bucket_start):
        ts = utc(bucket.bucket_start)
        if ts < t0_u or ts >= end_u:
            continue
        if upper_touch is None and bucket.high >= upper.touch_price:
            upper_touch = ts
        if lower_touch is None and bucket.low <= lower.touch_price:
            lower_touch = ts
        if upper_touch is not None and lower_touch is not None:
            break

    if upper_touch is None and lower_touch is None:
        outcome = Outcome.NEITHER
        first = None
    elif upper_touch is None:
        outcome, first = Outcome.LOWER_FIRST, lower_touch
    elif lower_touch is None:
        outcome, first = Outcome.UPPER_FIRST, upper_touch
    else:
        delta_ms = abs((upper_touch - lower_touch).total_seconds() * 1000.0)
        if delta_ms <= ambiguity_milliseconds:
            outcome = Outcome.AMBIGUOUS
            first = min(upper_touch, lower_touch)
        elif upper_touch < lower_touch:
            outcome, first = Outcome.UPPER_FIRST, upper_touch
        else:
            outcome, first = Outcome.LOWER_FIRST, lower_touch
    return {
        "outcome": outcome.value,
        "first_touch": first,
        "upper_touch": upper_touch,
        "lower_touch": lower_touch,
    }
