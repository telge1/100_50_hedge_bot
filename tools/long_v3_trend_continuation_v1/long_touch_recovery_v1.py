"""Touch / recovery with causal pool-as-of semantics.

Touch on bar t uses ONLY snapshot at close t-1 (pool known before bar t).
Recovery evaluated at close t uses snapshot at t for validity + close[t].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from long_support_selection_v1 import (
    SelectedSupportV1,
    region_known_in_snapshot,
    select_support_below_price,
    support_still_valid,
)


@dataclass
class TouchRecoveryTrackerV1:
    phase: str = "IDLE"  # IDLE | TOUCHED | RECOVERED
    touch_bar: str | None = None
    frozen_support: SelectedSupportV1 | None = None
    frozen_v2d: Any = None
    frozen_lineage_id: str | None = None
    frozen_origin_ids: str | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)


def _append(tr: TouchRecoveryTrackerV1, event: str, **detail: Any) -> None:
    tr.audit.append({"event": event, **detail})


def process_touch_recovery_at_close(
    *,
    bar: str,
    prev_bar: str | None,
    snap_prev: dict[str, Any] | None,
    snap_now: dict[str, Any],
    close_prev: float | None,
    ohlc: dict[str, float],
    ema_authorized: bool,
    pool_history_ok: bool,
    tracker: TouchRecoveryTrackerV1,
) -> tuple[TouchRecoveryTrackerV1, SelectedSupportV1 | None]:
    """
    Returns updated tracker and authorized support for PLACE if recovery confirmed this close.
    """
    if not pool_history_ok or not ema_authorized:
        if tracker.phase != "IDLE":
            _append(tracker, "RESET_CONTEXT_LOST", bar=bar)
            tracker.phase = "IDLE"
            tracker.frozen_support = None
            tracker.touch_bar = None
        return tracker, None

    close = float(ohlc["c"])
    low = float(ohlc["l"])

    if tracker.phase == "TOUCHED" and tracker.frozen_support is not None:
        fs = tracker.frozen_support
        ok, reason = support_still_valid(
            snap_now,
            region_lower=fs.region.lower_edge,
            region_upper=fs.region.upper_edge,
        )
        if close < fs.region.lower_edge - 1e-12:
            _append(tracker, "TOUCH_ABORT_BREAK", bar=bar, close=close)
            tracker.phase = "IDLE"
            tracker.frozen_support = None
            tracker.touch_bar = None
            return tracker, None
        if not ok:
            _append(tracker, "TOUCH_ABORT_INVALID", bar=bar, reason=reason)
            tracker.phase = "IDLE"
            tracker.frozen_support = None
            tracker.touch_bar = None
            return tracker, None
        if close > fs.region.upper_edge + 1e-12:
            _append(tracker, "RECOVERY_CONFIRMED", bar=bar, close=close)
            tracker.phase = "RECOVERED"
            return tracker, fs
        return tracker, None

    if tracker.phase == "RECOVERED":
        return tracker, None

    # IDLE — detect new touch using snap_prev only (causal)
    if prev_bar is None or snap_prev is None or close_prev is None:
        return tracker, None

    sel_prev, reason = select_support_below_price(snap_prev, market_price=close_prev)
    if sel_prev is None:
        return tracker, None
    if not region_known_in_snapshot(sel_prev.region, snap_prev):
        _append(tracker, "TOUCH_REJECTED_POOL_NOT_KNOWN_PREV", bar=bar, reason=reason)
        return tracker, None

    # Pool must NOT be attributed from snap_now first appearance only
    if not region_known_in_snapshot(sel_prev.region, snap_prev):
        return tracker, None
    if low <= sel_prev.region.upper_edge + 1e-12:
        if not region_known_in_snapshot(sel_prev.region, snap_now):
            _append(tracker, "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE", bar=bar)
            return tracker, None
        tracker.phase = "TOUCHED"
        tracker.touch_bar = bar
        tracker.frozen_support = sel_prev
        _append(
            tracker,
            "TOUCH_BEGIN",
            bar=bar,
            pool_upper=sel_prev.region.upper_edge,
            pool_lower=sel_prev.region.lower_edge,
            as_of_prev=prev_bar,
        )
    return tracker, None
