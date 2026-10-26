"""Config-driven short signal detection on Depth-2 bands (no 2A CSV)."""

from __future__ import annotations

from typing import Any

from entry_price_v1 import compute_short_entry_level
from gap_detection_v1 import has_upper_large_gap_above_pool
from pool_first_signal_v1 import detect_short_signal_pool_first
from signal_types_v1 import ShortSignalV1, pool_key as _pool_key


def _bearish_rejection(candle: dict[str, float]) -> bool:
    return float(candle["c"]) < float(candle["o"])


def _proximity_ok(candle: dict[str, float], pool_lo: float, pool_hi: float, proximity_pct: float) -> bool:
    bot = pool_lo
    top = pool_hi
    tol = bot * (1 - proximity_pct / 100.0)
    h, l = float(candle["h"]), float(candle["l"])
    inside = h >= bot and l <= top
    touch = h >= tol and l <= top
    near_below = float(candle["c"]) < bot and float(candle["c"]) >= tol
    return inside or touch or near_below


def detect_short_signal(
    config: dict[str, Any],
    *,
    decision_close_utc: str,
    order_active_from_utc: str,
    candle: dict[str, float],
    snapshot: dict[str, Any],
    pool_age_bars: dict[str, int],
    require_large_upper_gap: bool = True,
    blocked_pool_keys: set[str] | None = None,
) -> ShortSignalV1 | None:
    se = config["short_entry"]
    mode = se["entry_pool_selection_mode"]
    if mode == "pool_first_structural_v1":
        return detect_short_signal_pool_first(
            config,
            decision_close_utc=decision_close_utc,
            order_active_from_utc=order_active_from_utc,
            candle=candle,
            snapshot=snapshot,
            pool_age_bars=pool_age_bars,
            require_large_upper_gap=require_large_upper_gap,
            blocked_pool_keys=blocked_pool_keys,
        )
    if mode != "nearest_upper_proximity_v1":
        return None
    if se["upper_gap_measurement_mode"] != "adjacent_union_interval_gap_pct_v1":
        return None
    min_gap = float(se["upper_gap_min_pct"])
    prox = float(se["upper_pool_proximity_pct"])
    min_age = int(se["pool_min_age_bars"])

    uppers = snapshot.get("upper_bands") or []
    if not uppers:
        return None
    if not _bearish_rejection(candle):
        return None

    best = None
    best_dist = float("inf")
    for b in uppers:
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        if not _proximity_ok(candle, lo, hi, prox):
            continue
        dist = abs(float(candle["h"]) - lo) / lo * 100.0
        if dist < best_dist:
            best_dist = dist
            best = b
    if not best:
        return None

    pool_lo = float(best["lower_price"])
    pool_hi = float(best["upper_price"])
    pk = _pool_key(pool_lo, pool_hi)
    if min_age > 0 and pool_age_bars.get(pk, 0) < min_age:
        return None

    if require_large_upper_gap:
        if not has_upper_large_gap_above_pool(uppers, pool_hi, min_gap):
            return None

    entry_level = compute_short_entry_level(pool_lo, pool_hi, se)

    return ShortSignalV1(
        decision_close_utc=decision_close_utc,
        order_active_from_utc=order_active_from_utc,
        pool_lower=pool_lo,
        pool_upper=pool_hi,
        pool_key=pk,
        entry_level=entry_level,
        gap_min_pct_used=min_gap,
        require_large_upper_gap=require_large_upper_gap,
        experimental_selection="nearest_upper_proximity_v1_EXPERIMENTAL_NOT_FINAL_APPROVED",
    )
