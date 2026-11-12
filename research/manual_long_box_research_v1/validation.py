"""Strict long box geometry (no TRP auto-fix)."""

from __future__ import annotations

import math


def pct_dist(a: float, b: float) -> float:
    if abs(a) < 1e-18:
        return 0.0
    return abs(b - a) / abs(a) * 100.0


def validate_long_geometry(
    stop: float, entry: float, target: float
) -> tuple[bool, str]:
    for name, val in (("stop", stop), ("entry", entry), ("target", target)):
        if not math.isfinite(val) or val <= 0:
            return False, f"{name}_not_finite_positive"
    if stop >= entry:
        return False, "stop_not_below_entry"
    if target <= entry:
        return False, "target_not_above_entry"
    return True, ""


def validate_short_geometry(
    stop: float, entry: float, target: float
) -> tuple[bool, str]:
    """Short manual box: TP < entry < SL."""
    for name, val in (("stop", stop), ("entry", entry), ("target", target)):
        if not math.isfinite(val) or val <= 0:
            return False, f"{name}_not_finite_positive"
    if target >= entry:
        return False, "target_not_below_entry"
    if stop <= entry:
        return False, "stop_not_above_entry"
    return True, ""


def validate_long_geometry_at_fill(
    stop: float, fill: float, target: float
) -> tuple[bool, str]:
    """After delayed confirmation, prices may have moved — still require valid risk."""
    return validate_long_geometry(stop, fill, target)


MIN_REMAINING_TP_PCT_AT_FILL = 0.50


def remaining_tp_pct_at_fill(fill_price: float, manual_tp_price: float) -> float:
    """Percent gain from actual fill to unchanged manual TP: (tp/fill - 1) * 100."""
    return (manual_tp_price / fill_price - 1.0) * 100.0


def allow_long_at_fill(
    fill_price: float,
    manual_tp_price: float,
    *,
    min_remaining_pct: float = MIN_REMAINING_TP_PCT_AT_FILL,
) -> tuple[bool, float, str]:
    if not math.isfinite(fill_price) or fill_price <= 0:
        return False, 0.0, "fill_not_finite_positive"
    if not math.isfinite(manual_tp_price) or manual_tp_price <= 0:
        return False, 0.0, "target_not_finite_positive"
    rem = remaining_tp_pct_at_fill(fill_price, manual_tp_price)
    min_tp_price = fill_price * (1.0 + min_remaining_pct / 100.0)
    if manual_tp_price < min_tp_price:
        return False, rem, "BLOCKED_TP_DISTANCE"
    return True, rem, ""
