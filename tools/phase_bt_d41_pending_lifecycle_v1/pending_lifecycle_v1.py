"""Causal pending short limit order lifecycle (BT-D41 research — no fills)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BT_D26 = Path(__file__).resolve().parents[1] / "phase_bt_d26_dynamic_upper_pool_selector_v1"
BT_D7 = Path(__file__).resolve().parents[1] / "phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D23 = Path(__file__).resolve().parents[1] / "phase_bt_d23_pool_first_setup_scanner_v1"
PHASE4D = Path(__file__).resolve().parents[1] / "phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D26), str(BT_D7), str(BT_D23), str(PHASE4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bands_adapter_v1 import snapshot_to_intervals  # noqa: E402
from candidate_identity_v1 import (  # noqa: E402
    ResolvedUpperBand,
    candidate_from_signal_band,
    origin_set_identity_key,
    resolve_tracked_upper_band,
)
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper, detect_short_signal_pool_first  # noqa: E402
from pool_identity_v1 import pool_still_active_for_pending  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402

CONTRACT = "bt_d41_pending_lifecycle_v1"

STATE_NO_CANDIDATE = "NO_CANDIDATE"
STATE_PLANNED = "PLANNED"
STATE_PENDING = "PENDING"
STATE_CANCELLED = "CANCELLED"
STATE_REPLACED = "REPLACED"
STATE_EXPIRED_INVALID = "EXPIRED_INVALID"

EVENT_CREATE = "CREATE"
EVENT_KEEP = "KEEP"
EVENT_CANCEL = "CANCEL"
EVENT_REPLACE = "REPLACE"
EVENT_INVALID = "INVALID"
EVENT_NO_CANDIDATE = "NO_CANDIDATE"
EVENT_POTENTIAL_FILL_UNRESOLVED = "POTENTIAL_FILL_UNRESOLVED"
EVENT_PLANNED_SCANNER = "PLANNED_SCANNER"


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_5m_open_after_close(close_utc: str) -> str:
    return _iso(_parse_utc(close_utc) + timedelta(minutes=5))


def next_5m_close_after(close_utc: str) -> str:
    return next_5m_open_after_close(close_utc)


@dataclass
class PendingOrderV1:
    order_id: str
    lifecycle_state: str
    decision_close_utc: str
    intent_created_utc: str
    earliest_eligible_execution_utc: str
    pool_lower: float
    pool_upper: float
    pool_key: str
    origin_ids: str
    origin_set_key: str
    planned_entry: float
    planned_sl: float
    planned_tp: float | None
    selection_reason: str | None
    scanner_setup_id: str | None = None


@dataclass
class LifecycleEventV1:
    event_type: str
    decision_close_utc: str
    intent_created_utc: str | None
    earliest_eligible_execution_utc: str | None
    next_review_utc: str | None
    lifecycle_state: str
    pool_lower: float | None
    pool_upper: float | None
    pool_key: str | None
    origin_ids: str | None
    origin_set_key: str | None
    planned_entry: float | None
    planned_sl: float | None
    planned_tp: float | None
    selection_reason: str | None
    prior_pool_key: str | None
    prior_planned_entry: float | None
    prior_planned_sl: float | None
    new_pool_key: str | None
    cancel_reason: str | None
    detail: str | None = None


@dataclass
class PlannedLevels:
    entry: float
    sl: float
    tp: float | None
    tp_reason: str | None


def compute_planned_levels(
    config: dict[str, Any],
    *,
    pool_lo: float,
    pool_hi: float,
    snapshot: dict[str, Any],
) -> PlannedLevels:
    se = config["short_entry"]
    entry = compute_short_entry_level(pool_lo, pool_hi, se)
    sl = short_sl(pool_hi, float(config["risk"]["sl_above_upper_top_pct"]))
    intervals = snapshot_to_intervals(snapshot)
    min_tp = float(config["take_profit"]["min_distance_pct"])
    tp_iv, tp_reason = select_short_tp_depth2(entry, intervals, min_tp)
    tp = float(tp_iv["top"]) if tp_iv else None
    return PlannedLevels(entry=entry, sl=sl, tp=tp, tp_reason=tp_reason)


def _band_for_pending(
    config: dict[str, Any],
    snapshot: dict[str, Any],
    pending: PendingOrderV1,
) -> tuple[ResolvedUpperBand | None, str | None]:
    res = resolve_tracked_upper_band(
        config,
        snapshot,
        pool_lo=pending.pool_lower,
        pool_hi=pending.pool_upper,
        origin_ids=pending.origin_ids,
    )
    if res.ok and res.band is not None:
        return res.band, None
    if pool_still_active_for_pending(
        config,
        pool_lo=pending.pool_lower,
        pool_hi=pending.pool_upper,
        origin_ids=pending.origin_ids,
        snapshot=snapshot,
    ):
        return ResolvedUpperBand(
            pool_lower=pending.pool_lower,
            pool_upper=pending.pool_upper,
            origin_ids=pending.origin_ids,
            origin_set_key=pending.origin_set_key,
            pool_key=pending.pool_key,
        ), res.reason
    return None, res.reason or "POOL_NOT_ACTIVE"


@dataclass
class PendingReviewResult:
    pending: PendingOrderV1 | None
    events: list[LifecycleEventV1]


def review_pending_at_close(
    config: dict[str, Any],
    *,
    decision_close_utc: str,
    pending: PendingOrderV1,
    candle: dict[str, float],
    snapshot: dict[str, Any],
    pool_age_bars: dict[str, int],
    require_large_upper_gap: bool = True,
) -> PendingReviewResult:
    """Causal validity check at decision close (no fills)."""
    events: list[LifecycleEventV1] = []
    next_review = next_5m_close_after(decision_close_utc)

    def _base_event(**kwargs: Any) -> LifecycleEventV1:
        return LifecycleEventV1(
            event_type=kwargs.pop("event_type"),
            decision_close_utc=decision_close_utc,
            intent_created_utc=pending.intent_created_utc,
            earliest_eligible_execution_utc=pending.earliest_eligible_execution_utc,
            next_review_utc=next_review,
            lifecycle_state=kwargs.pop("lifecycle_state", pending.lifecycle_state),
            pool_lower=kwargs.get("pool_lower", pending.pool_lower),
            pool_upper=kwargs.get("pool_upper", pending.pool_upper),
            pool_key=kwargs.get("pool_key", pending.pool_key),
            origin_ids=kwargs.get("origin_ids", pending.origin_ids),
            origin_set_key=kwargs.get("origin_set_key", pending.origin_set_key),
            planned_entry=kwargs.get("planned_entry", pending.planned_entry),
            planned_sl=kwargs.get("planned_sl", pending.planned_sl),
            planned_tp=kwargs.get("planned_tp", pending.planned_tp),
            selection_reason=pending.selection_reason,
            prior_pool_key=kwargs.get("prior_pool_key"),
            prior_planned_entry=kwargs.get("prior_planned_entry"),
            prior_planned_sl=kwargs.get("prior_planned_sl"),
            new_pool_key=kwargs.get("new_pool_key"),
            cancel_reason=kwargs.get("cancel_reason"),
            detail=kwargs.get("detail"),
        )

    reconciled, reconcile_note = _band_for_pending(config, snapshot, pending)
    if reconciled is None:
        ev = _base_event(
            event_type=EVENT_CANCEL,
            lifecycle_state=STATE_CANCELLED,
            cancel_reason="POOL_NOT_ACTIVE",
            detail=reconcile_note,
        )
        events.append(ev)
        return PendingReviewResult(pending=None, events=events)

    band_dict = {
        "lower_price": reconciled.pool_lower,
        "upper_price": reconciled.pool_upper,
        "origin_ids": reconciled.origin_ids,
    }
    ok, fail = _structural_eligible_upper(
        config,
        band=band_dict,
        decision_close_utc=decision_close_utc,
        candle=candle,
        snapshot=snapshot,
        pool_age_bars=pool_age_bars,
        require_large_upper_gap=require_large_upper_gap,
    )
    if not ok:
        ev = _base_event(
            event_type=EVENT_CANCEL,
            lifecycle_state=STATE_EXPIRED_INVALID,
            pool_lower=reconciled.pool_lower,
            pool_upper=reconciled.pool_upper,
            pool_key=reconciled.pool_key,
            origin_ids=reconciled.origin_ids,
            origin_set_key=reconciled.origin_set_key,
            cancel_reason=fail or "STRUCTURAL_INELIGIBLE",
            detail="Pool band no longer passes _structural_eligible_upper",
        )
        events.append(ev)
        return PendingReviewResult(pending=None, events=events)

    fresh = detect_short_signal_pool_first(
        config,
        decision_close_utc=decision_close_utc,
        order_active_from_utc=next_5m_open_after_close(decision_close_utc),
        candle=candle,
        snapshot=snapshot,
        pool_age_bars=pool_age_bars,
        require_large_upper_gap=require_large_upper_gap,
        blocked_pool_keys=None,
    )
    if fresh is None:
        ev = _base_event(
            event_type=EVENT_CANCEL,
            lifecycle_state=STATE_EXPIRED_INVALID,
            cancel_reason="NO_ELIGIBLE_UPPER_AT_CLOSE",
            detail="No structurally eligible upper at close while pending existed",
        )
        events.append(ev)
        return PendingReviewResult(pending=None, events=events)

    fresh_pk = fresh.pool_key
    if fresh.pool_lower < reconciled.pool_lower - 1e-12:
        ev = _base_event(
            event_type=EVENT_CANCEL,
            lifecycle_state=STATE_CANCELLED,
            cancel_reason="NOT_NEXT_ELIGIBLE_UPPER",
            detail=(
                f"Closer eligible upper appeared: {fresh.pool_lower}–{fresh.pool_upper} "
                f"(pending {reconciled.pool_lower}–{reconciled.pool_upper})"
            ),
            new_pool_key=fresh_pk,
        )
        events.append(ev)
        return PendingReviewResult(pending=None, events=events)

    levels = compute_planned_levels(
        config,
        pool_lo=reconciled.pool_lower,
        pool_hi=reconciled.pool_upper,
        snapshot=snapshot,
    )
    if levels.tp is None:
        ev = _base_event(
            event_type=EVENT_CANCEL,
            lifecycle_state=STATE_EXPIRED_INVALID,
            cancel_reason=levels.tp_reason or "NO_TP_DEPTH2",
            detail="Depth2 TP no longer available for pending band",
        )
        events.append(ev)
        return PendingReviewResult(pending=None, events=events)

    origin_changed = reconciled.origin_set_key != pending.origin_set_key
    bounds_changed = reconciled.pool_key != pending.pool_key
    level_changed = (
        abs(levels.entry - pending.planned_entry) > 1e-12
        or abs(levels.sl - pending.planned_sl) > 1e-12
        or (pending.planned_tp is not None and levels.tp is not None and abs(levels.tp - pending.planned_tp) > 1e-12)
    )

    updated = PendingOrderV1(
        order_id=pending.order_id,
        lifecycle_state=STATE_PENDING,
        decision_close_utc=pending.decision_close_utc,
        intent_created_utc=pending.intent_created_utc,
        earliest_eligible_execution_utc=pending.earliest_eligible_execution_utc,
        pool_lower=reconciled.pool_lower,
        pool_upper=reconciled.pool_upper,
        pool_key=reconciled.pool_key,
        origin_ids=reconciled.origin_ids,
        origin_set_key=reconciled.origin_set_key,
        planned_entry=levels.entry,
        planned_sl=levels.sl,
        planned_tp=levels.tp,
        selection_reason=pending.selection_reason,
        scanner_setup_id=pending.scanner_setup_id,
    )

    detail_parts = []
    if reconcile_note:
        detail_parts.append(f"reconcile:{reconcile_note}")
    if bounds_changed:
        detail_parts.append("bounds_reconciled_same_zone")
    if origin_changed:
        detail_parts.append("origin_set_membership_changed")
    if level_changed:
        detail_parts.append("entry_sl_tp_refreshed_in_place")

    if fresh_pk != reconciled.pool_key and fresh.pool_lower >= reconciled.pool_lower - 1e-12:
        detail_parts.append(
            f"scanner_lowest_key={fresh_pk} differs from reconciled pending key (not closer lower)"
        )

    ev = _base_event(
        event_type=EVENT_KEEP,
        lifecycle_state=STATE_PENDING,
        pool_lower=updated.pool_lower,
        pool_upper=updated.pool_upper,
        pool_key=updated.pool_key,
        origin_ids=updated.origin_ids,
        origin_set_key=updated.origin_set_key,
        planned_entry=updated.planned_entry,
        planned_sl=updated.planned_sl,
        planned_tp=updated.planned_tp,
        prior_pool_key=pending.pool_key if bounds_changed else None,
        prior_planned_entry=pending.planned_entry if level_changed else None,
        prior_planned_sl=pending.planned_sl if level_changed else None,
        detail="; ".join(detail_parts) if detail_parts else "unchanged",
    )
    events.append(ev)
    return PendingReviewResult(pending=updated, events=events)


def make_pending_from_scanner(
    *,
    order_id: str,
    setup: dict[str, Any],
    selection_reason: str | None,
) -> PendingOrderV1:
    close = setup["decision_utc"]
    return PendingOrderV1(
        order_id=order_id,
        lifecycle_state=STATE_PENDING,
        decision_close_utc=close,
        intent_created_utc=close,
        earliest_eligible_execution_utc=next_5m_open_after_close(close),
        pool_lower=float(setup["pool_lower"]),
        pool_upper=float(setup["pool_upper"]),
        pool_key=str(setup.get("upper_entry_pool_id") or pool_key(float(setup["pool_lower"]), float(setup["pool_upper"]))),
        origin_ids=str(setup.get("upper_origin_ids") or ""),
        origin_set_key=str(setup.get("origin_set_key") or origin_set_identity_key(setup.get("upper_origin_ids"))),
        planned_entry=float(setup["planned_entry"]),
        planned_sl=float(setup["planned_sl"]),
        planned_tp=float(setup["planned_tp"]) if setup.get("planned_tp") is not None else None,
        selection_reason=selection_reason,
        scanner_setup_id=setup.get("setup_id"),
    )


def intrabar_potential_fill_events(
    pending: PendingOrderV1,
    *,
    bar_utc: str,
    ohlc: dict[str, float],
    decision_close_utc: str,
) -> list[LifecycleEventV1]:
    if bar_utc < pending.earliest_eligible_execution_utc:
        return []
    if float(ohlc.get("h", 0)) < pending.planned_entry:
        return []
    return [
        LifecycleEventV1(
            event_type=EVENT_POTENTIAL_FILL_UNRESOLVED,
            decision_close_utc=decision_close_utc,
            intent_created_utc=pending.intent_created_utc,
            earliest_eligible_execution_utc=pending.earliest_eligible_execution_utc,
            next_review_utc=next_5m_close_after(decision_close_utc),
            lifecycle_state=STATE_PENDING,
            pool_lower=pending.pool_lower,
            pool_upper=pending.pool_upper,
            pool_key=pending.pool_key,
            origin_ids=pending.origin_ids,
            origin_set_key=pending.origin_set_key,
            planned_entry=pending.planned_entry,
            planned_sl=pending.planned_sl,
            planned_tp=pending.planned_tp,
            selection_reason=pending.selection_reason,
            prior_pool_key=None,
            prior_planned_entry=None,
            prior_planned_sl=None,
            new_pool_key=None,
            cancel_reason=None,
            detail=f"bar={bar_utc} high={ohlc.get('h')} >= entry={pending.planned_entry}",
        )
    ]
