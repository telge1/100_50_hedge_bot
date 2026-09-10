"""Confirmed-refill rule. Ordinary LEVEL_INCREASE is not a refill.

Definition (Episode-1 persist readback):

A REFILL_CANDIDATE is a size reduction at (side, price) followed by a size
increase on the same side within refill_window_ms, either at the exact price
or within nearby_refill_max_bps.

A CONFIRMED REFILL additionally requires all of:

1. Prior visibility: old_size > 0 on the reduction.
2. Depletion: LEVEL_REMOVE, or LEVEL_DECREASE whose notional drop is at least
   wall_partial_consume_min_ratio of the pre-event notional.
3. Restoring update at the exact same price (nearby is candidate-only).
4. Restoring kind is LEVEL_ADD or LEVEL_INCREASE.
5. 0 < latency_ms <= refill_window_ms.
6. Same replay_epoch on reduction and restore.
7. No book_reset with reduction_time < reset_time <= restore_time
   (reset restoration is not a refill; epoch change is not a refill).

A plain size increase without a qualifying prior depletion is not a refill.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from .classify_levels import classify_level_kind, is_size_increase, is_size_reduction
from .persist import event_available_at, ts_cell

REFILL_DEFINITION = {
    "prior_visibility": "old_size > 0 on the depleting update",
    "depletion": "LEVEL_REMOVE or LEVEL_DECREASE with notional drop >= wall_partial_consume_min_ratio * (old_size * price)",
    "restore": "LEVEL_ADD or LEVEL_INCREASE at the exact same (side, price)",
    "window": "0 < latency_ms <= refill_window_ms",
    "nearby_candidates": "same-side restore within nearby_refill_max_bps is a candidate, never confirmed",
    "resets": "any book_reset strictly after the reduction and at or before the restore rejects the candidate",
    "epoch": "replay_epoch must be identical on reduction and restore",
    "plain_increase": "LEVEL_INCREASE without a qualifying depletion is not a refill",
}


def _px(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _kind(row: dict[str, Any]) -> str:
    return str(row.get("level_kind") or classify_level_kind(row.get("old_size"), row.get("new_size")))


def _depletion_ok(row: dict[str, Any], *, partial_ratio: float) -> bool:
    kind = _kind(row)
    old = float(row.get("old_size") or 0.0)
    new = float(row.get("new_size") or 0.0)
    px = _px(row.get("price")) or 0.0
    if old <= 0.0:
        return False
    if kind == "LEVEL_REMOVE" or new <= 0.0:
        return True
    notion_before = abs(old * px)
    dropped = abs(float(row.get("notional_delta") or ((old - new) * px)))
    if notion_before <= 0.0:
        return False
    return dropped + 1e-12 >= float(partial_ratio) * notion_before


def classify_refills(
    *,
    level_changes: list[dict[str, Any]],
    book_resets: list[dict[str, Any]],
    refill_window_ms: int,
    nearby_max_bps: float,
    partial_ratio: float,
    causal_end: datetime,
    mid_hint: float | None,
) -> dict[str, Any]:
    changes = []
    for row in level_changes:
        et = _as_dt(row["event_time"])
        if et >= causal_end:
            continue
        rec = dict(row)
        rec["level_kind"] = _kind(rec)
        rec["_et"] = et
        changes.append(rec)
    changes.sort(key=lambda r: (r["_et"], int(r.get("apply_order") or 0)))

    resets = []
    for rec in book_resets:
        et = _as_dt(rec["event_time"])
        if et >= causal_end:
            continue
        resets.append({"_et": et, **rec})
    resets.sort(key=lambda r: r["_et"])

    reductions = [r for r in changes if is_size_reduction(r["level_kind"])]
    increases = [r for r in changes if is_size_increase(r["level_kind"])]
    win = timedelta(milliseconds=int(refill_window_ms))

    candidates: list[dict[str, Any]] = []
    confirmed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    reject_counts: dict[str, int] = {}

    def _reset_between(a: datetime, b: datetime) -> dict[str, Any] | None:
        for rec in resets:
            if a < rec["_et"] <= b:
                return rec
        return None

    for rem in reductions:
        rt = rem["_et"]
        side = str(rem.get("side") or "")
        px = _px(rem.get("price"))
        if px is None:
            continue
        best: dict[str, Any] | None = None
        for add in increases:
            at = add["_et"]
            if at <= rt:
                continue
            if at - rt > win:
                break
            if str(add.get("side") or "") != side:
                continue
            apx = _px(add.get("price"))
            if apx is None:
                continue
            mid = mid_hint or px
            dist_bps = abs(apx - px) / mid * 1e4 if mid else 0.0
            exact = abs(apx - px) <= 1e-9
            nearby = (not exact) and dist_bps <= float(nearby_max_bps)
            if not exact and not nearby:
                continue
            latency_ms = (at - rt).total_seconds() * 1000.0
            if best is None or latency_ms < float(best["latency_ms"]):
                best = {
                    "removal": rem,
                    "restore": add,
                    "exact": exact,
                    "nearby": nearby,
                    "distance_bps": dist_bps,
                    "latency_ms": latency_ms,
                }
        if best is None:
            continue

        rem = best["removal"]
        add = best["restore"]
        reasons: list[str] = []
        if float(rem.get("old_size") or 0.0) <= 0.0:
            reasons.append("NO_PRIOR_VISIBILITY")
        if not _depletion_ok(rem, partial_ratio=partial_ratio):
            reasons.append("DEPLETION_THRESHOLD_NOT_MET")
        if best["nearby"] and not best["exact"]:
            reasons.append("NEARBY_NOT_EXACT")
        rem_epoch = rem.get("replay_epoch", rem.get("epoch"))
        add_epoch = add.get("replay_epoch", add.get("epoch"))
        if rem_epoch != add_epoch:
            reasons.append("EPOCH_CHANGE")
        reset = _reset_between(rem["_et"], add["_et"])
        if reset is not None:
            reasons.append("RESET_IN_WINDOW")
        if best["latency_ms"] <= 0:
            reasons.append("NON_POSITIVE_LATENCY")

        avail = event_available_at(add["_et"])
        row = {
            "event_type": "REFILL_CANDIDATE" if reasons else "REFILL",
            "level_kind": "REFILL" if not reasons else "REFILL_CANDIDATE",
            "event_time": ts_cell(add["_et"]),
            "event_available_at": ts_cell(avail),
            "state_available_at": ts_cell(avail),
            "removal_event_time": ts_cell(rem["_et"]),
            "removal_event_available_at": ts_cell(event_available_at(rem["_et"])),
            "side": side,
            "price": px,
            "restore_price": _px(add.get("price")),
            "previous_size": rem.get("old_size"),
            "removed_new_size": rem.get("new_size"),
            "restored_old_size": add.get("old_size"),
            "restored_new_size": add.get("new_size"),
            "removed_notional": abs(float(rem.get("notional_delta") or 0.0)),
            "refilled_notional": abs(float(add.get("notional_delta") or 0.0)),
            "latency_ms": best["latency_ms"],
            "distance_bps": best["distance_bps"],
            "exact_or_nearby": "EXACT" if best["exact"] else "NEARBY",
            "replay_epoch": add_epoch,
            "removal_replay_epoch": rem_epoch,
            "removal_event_id": rem.get("source_event_id"),
            "add_event_id": add.get("source_event_id"),
            "causal_source_event_id": add.get("source_event_id"),
            "reject_reasons": reasons,
            "confirmed": not reasons,
        }
        candidates.append(row)
        if reasons:
            rejected.append(row)
            for reason in reasons:
                reject_counts[reason] = reject_counts.get(reason, 0) + 1
        else:
            confirmed.append({**row, "event_type": "REFILL", "level_kind": "REFILL"})

    primary_counts: dict[str, int] = {}
    for row in rejected:
        reasons = row.get("reject_reasons") or []
        primary = reasons[0] if reasons else "UNKNOWN"
        primary_counts[primary] = primary_counts.get(primary, 0) + 1
    multi = sum(1 for row in rejected if len(row.get("reject_reasons") or []) > 1)
    assert len(candidates) == len(confirmed) + len(rejected)
    assert sum(primary_counts.values()) == len(rejected)

    return {
        "definition": REFILL_DEFINITION,
        "candidates": candidates,
        "confirmed": confirmed,
        "rejected": rejected,
        "reject_reason_counts": reject_counts,
        "all_reject_reason_occurrences": reject_counts,
        "primary_reject_reason_counts": primary_counts,
        "multi_reason_candidates": multi,
        "unique_candidates": len(candidates),
        "unique_confirmed": len(confirmed),
        "unique_rejected": len(rejected),
        "n_candidates": len(candidates),
        "n_confirmed": len(confirmed),
        "n_rejected": len(rejected),
    }
