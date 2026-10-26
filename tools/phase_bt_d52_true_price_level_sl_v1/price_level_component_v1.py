"""BT-D52: overlap-connected upper price-level component from a selected entry pool."""

from __future__ import annotations

from collections import Counter, deque
from typing import Any

from signal_types_v1 import pool_key


def intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 < hi2 and lo2 < hi1


def _band_row(b: dict[str, Any]) -> dict[str, Any]:
    lo = float(b["lower_price"])
    hi = float(b["upper_price"])
    return {
        "pool_lower": lo,
        "pool_upper": hi,
        "pool_key": pool_key(lo, hi),
        "origin_ids": b.get("origin_ids") or "",
    }


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    iv = sorted(intervals)
    merged = [iv[0]]
    for lo, hi in iv[1:]:
        mlo, mhi = merged[-1]
        if lo <= mhi:
            merged[-1] = (mlo, max(mhi, hi))
        else:
            merged.append((lo, hi))
    return merged


def internal_coverage_gaps(members: list[dict[str, Any]], comp_lo: float, comp_hi: float) -> list[dict[str, float]]:
    covered = _merge_intervals([(m["pool_lower"], m["pool_upper"]) for m in members])
    gaps: list[dict[str, float]] = []
    cursor = comp_lo
    for lo, hi in covered:
        if lo > cursor + 1e-15:
            gaps.append({"gap_lower": cursor, "gap_upper": lo, "width": lo - cursor})
        cursor = max(cursor, hi)
    if cursor < comp_hi - 1e-15:
        gaps.append({"gap_lower": cursor, "gap_upper": comp_hi, "width": comp_hi - cursor})
    return gaps


def _articulation_keys(members: list[dict[str, Any]]) -> list[str]:
    if len(members) <= 2:
        return []

    def connected_without(skip_pk: str) -> bool:
        remain = [m for m in members if m["pool_key"] != skip_pk]
        if len(remain) <= 1:
            return True
        seen = {remain[0]["pool_key"]}
        q = deque([remain[0]])
        by_pk = {m["pool_key"]: m for m in remain}
        while q:
            cur = q.popleft()
            for m in remain:
                if m["pool_key"] in seen:
                    continue
                if intervals_overlap(
                    cur["pool_lower"],
                    cur["pool_upper"],
                    m["pool_lower"],
                    m["pool_upper"],
                ):
                    seen.add(m["pool_key"])
                    q.append(m)
        return len(seen) == len(remain)

    return [m["pool_key"] for m in members if not connected_without(m["pool_key"])]


def overlap_depth_histogram(members: list[dict[str, Any]]) -> dict[str, int]:
    c: Counter[int] = Counter()
    for m in members:
        n = sum(
            1
            for o in members
            if o["pool_key"] != m["pool_key"]
            and intervals_overlap(
                m["pool_lower"], m["pool_upper"], o["pool_lower"], o["pool_upper"]
            )
        )
        c[n] += 1
    return {str(k): v for k, v in sorted(c.items())}


def component_from_entry_pool(
    snapshot: dict[str, Any],
    *,
    entry_pool_lower: float,
    entry_pool_upper: float,
) -> dict[str, Any]:
    """
    BFS overlap component containing the entry band (strict overlap, no gap bridging).
    """
    entry_pk = pool_key(entry_pool_lower, entry_pool_upper)
    bands = [_band_row(b) for b in snapshot.get("upper_bands") or []]
    by_pk = {b["pool_key"]: b for b in bands}
    if entry_pk not in by_pk:
        return {
            "status": "UNRESOLVED_PRICE_LEVEL_EDGE",
            "unresolved_reason": "ENTRY_BAND_NOT_IN_SNAPSHOT",
            "entry_pool_key": entry_pk,
        }

    component_keys: set[str] = {entry_pk}
    q: deque[dict[str, Any]] = deque([by_pk[entry_pk]])
    while q:
        cur = q.popleft()
        for b in bands:
            if b["pool_key"] in component_keys:
                continue
            if intervals_overlap(
                cur["pool_lower"],
                cur["pool_upper"],
                b["pool_lower"],
                b["pool_upper"],
            ):
                component_keys.add(b["pool_key"])
                q.append(b)

    members = [by_pk[pk] for pk in sorted(component_keys)]
    comp_lo = min(m["pool_lower"] for m in members)
    comp_hi = max(m["pool_upper"] for m in members)
    origins: set[str] = set()
    for m in members:
        for oid in str(m["origin_ids"]).split("|"):
            if oid.strip():
                origins.add(oid.strip())

    gaps = internal_coverage_gaps(members, comp_lo, comp_hi)
    arts = _articulation_keys(members)
    entry_member = by_pk[entry_pk]
    entry_width = entry_member["pool_upper"] - entry_member["pool_lower"]
    comp_width = comp_hi - comp_lo

    unresolved_reason: str | None = None
    if gaps:
        max_gap = max(g["width"] for g in gaps)
        if max_gap > 1e-12:
            unresolved_reason = "INTERNAL_COVERAGE_GAP_IN_ENVELOPE"
    if unresolved_reason is None and arts and len(members) >= 3:
        unresolved_reason = "WEAK_ARTICULATION_BRIDGE_CHAIN"
    if unresolved_reason is None and len(members) > 1 and comp_width > entry_width * 100:
        unresolved_reason = "ENVELOPE_WIDTH_DISPROPORTIONATE_TO_ENTRY_BAND"

    status = "CONFIRMED_PRICE_LEVEL_EDGE" if unresolved_reason is None else "UNRESOLVED_PRICE_LEVEL_EDGE"

    return {
        "status": status,
        "unresolved_reason": unresolved_reason,
        "entry_pool_key": entry_pk,
        "entry_pool_lower": entry_pool_lower,
        "entry_pool_upper": entry_pool_upper,
        "component_lower": comp_lo,
        "component_upper": comp_hi,
        "component_width": comp_width,
        "member_count": len(members),
        "member_pool_keys": [m["pool_key"] for m in members],
        "unique_origin_ids": sorted(origins),
        "unique_origin_count": len(origins),
        "overlap_depth_histogram": overlap_depth_histogram(members),
        "internal_coverage_gaps": gaps,
        "articulation_pool_keys": arts,
        "entry_band_width": entry_width,
    }


def composite_sl_from_component(
    component: dict[str, Any],
    *,
    sl_pct: float,
    short_sl_fn,
) -> dict[str, Any]:
    """Returns legacy-style SL from component upper when confirmed."""
    if component.get("status") != "CONFIRMED_PRICE_LEVEL_EDGE":
        return {
            "sl_mode": "legacy_atomic_fallback",
            "candidate_composite_sl": None,
            "component_upper_used": None,
        }
    top = float(component["component_upper"])
    return {
        "sl_mode": "composite_price_level_edge",
        "candidate_composite_sl": short_sl_fn(top, sl_pct),
        "component_upper_used": top,
    }
