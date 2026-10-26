"""Pool-first SHORT signal (no bearish / proximity / touch trigger) — BT-D21."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from bands_adapter_v1 import snapshot_to_intervals
from entry_price_v1 import compute_short_entry_level
from gap_detection_v1 import has_upper_large_gap_above_pool
from signal_types_v1 import ShortSignalV1, pool_key as _pool_key

PHASE4D = Path(__file__).resolve().parents[2] / "phase4d_pool_landscape_backtester_v2"
if str(PHASE4D) not in sys.path:
    sys.path.insert(0, str(PHASE4D))
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402


def _structural_eligible_upper(
    config: dict[str, Any],
    *,
    band: dict[str, Any],
    decision_close_utc: str,
    candle: dict[str, float],
    snapshot: dict[str, Any],
    pool_age_bars: dict[str, int],
    require_large_upper_gap: bool,
) -> tuple[bool, str | None]:
    se = config["short_entry"]
    if se["upper_gap_measurement_mode"] != "adjacent_union_interval_gap_pct_v1":
        return False, "GAP_MODE_UNSUPPORTED"

    pool_lo = float(band["lower_price"])
    pool_hi = float(band["upper_price"])
    close_px = float(candle["c"])
    if pool_lo <= close_px:
        return False, "POOL_NOT_ABOVE_PRICE"

    pk = _pool_key(pool_lo, pool_hi)
    min_age = int(se["pool_min_age_bars"])
    if min_age > 0 and pool_age_bars.get(pk, 0) < min_age:
        return False, "POOL_TOO_YOUNG"

    min_gap = float(se["upper_gap_min_pct"])
    uppers = snapshot.get("upper_bands") or []
    if require_large_upper_gap:
        if not has_upper_large_gap_above_pool(uppers, pool_hi, min_gap):
            return False, "UPPER_GAP_FAIL"

    try:
        entry_level = compute_short_entry_level(pool_lo, pool_hi, se)
    except ValueError:
        return False, "ENTRY_PRICE_INVALID"

    ref = str(se["entry_price_reference"])
    if ref == "upper_pool_upper_edge":
        return False, "POOL_FIRST_UPPER_EDGE_NOT_ALLOWED"

    sl = short_sl(pool_hi, float(config["risk"]["sl_above_upper_top_pct"]))
    if entry_level >= sl:
        return False, "ENTRY_NOT_BELOW_SL"

    intervals = snapshot_to_intervals(snapshot)
    min_tp = float(config["take_profit"]["min_distance_pct"])
    tp_iv, tp_reason = select_short_tp_depth2(entry_level, intervals, min_tp)
    if not tp_iv:
        return False, tp_reason or "NO_TP"

    return True, None


def detect_short_signal_pool_first(
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
    if se["entry_pool_selection_mode"] != "pool_first_structural_v1":
        return None

    uppers = snapshot.get("upper_bands") or []
    if not uppers:
        return None

    min_gap = float(se["upper_gap_min_pct"])
    blocked = blocked_pool_keys or set()

    candidates: list[tuple[float, dict[str, Any]]] = []
    for b in uppers:
        ok, _reason = _structural_eligible_upper(
            config,
            band=b,
            decision_close_utc=decision_close_utc,
            candle=candle,
            snapshot=snapshot,
            pool_age_bars=pool_age_bars,
            require_large_upper_gap=require_large_upper_gap,
        )
        if not ok:
            continue
        pool_lo = float(b["lower_price"])
        pk = _pool_key(pool_lo, float(b["upper_price"]))
        if pk in blocked:
            continue
        candidates.append((pool_lo, b))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]
    pool_lo = float(best["lower_price"])
    pool_hi = float(best["upper_price"])
    entry_level = compute_short_entry_level(pool_lo, pool_hi, se)

    return ShortSignalV1(
        decision_close_utc=decision_close_utc,
        order_active_from_utc=order_active_from_utc,
        pool_lower=pool_lo,
        pool_upper=pool_hi,
        pool_key=_pool_key(pool_lo, pool_hi),
        entry_level=entry_level,
        gap_min_pct_used=min_gap,
        require_large_upper_gap=require_large_upper_gap,
        experimental_selection="pool_first_structural_v1_BT_D21",
        origin_ids=str(best.get("origin_ids") or "") or None,
    )
