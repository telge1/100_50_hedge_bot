"""BT-D53: strict vs touch-or-overlap price-level components from entry band."""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Literal

from signal_types_v1 import pool_key

ConnectivityMode = Literal["strict_overlap", "touch_or_overlap"]


def intervals_strict_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 < hi2 and lo2 < hi1


def intervals_touch_or_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 <= hi2 and lo2 <= hi1


def _connected(mode: ConnectivityMode, lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    if mode == "strict_overlap":
        return intervals_strict_overlap(lo1, hi1, lo2, hi2)
    return intervals_touch_or_overlap(lo1, hi1, lo2, hi2)


def edge_kind(lo1: float, hi1: float, lo2: float, hi2: float) -> str:
    if intervals_strict_overlap(lo1, hi1, lo2, hi2):
        return "genuine_overlap"
    if intervals_touch_or_overlap(lo1, hi1, lo2, hi2):
        return "edge_touch_only"
    return "disconnected"


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
        if lo > cursor:
            gaps.append({"gap_lower": cursor, "gap_upper": lo, "width": lo - cursor})
        cursor = max(cursor, hi)
    if cursor < comp_hi:
        gaps.append({"gap_lower": cursor, "gap_upper": comp_hi, "width": comp_hi - cursor})
    return gaps


def _articulation_keys(members: list[dict[str, Any]], mode: ConnectivityMode) -> list[str]:
    if len(members) <= 2:
        return []

    def connected_without(skip_pk: str) -> bool:
        remain = [m for m in members if m["pool_key"] != skip_pk]
        if len(remain) <= 1:
            return True
        seen = {remain[0]["pool_key"]}
        q = deque([remain[0]])
        while q:
            cur = q.popleft()
            for m in remain:
                if m["pool_key"] in seen:
                    continue
                if _connected(
                    mode,
                    cur["pool_lower"],
                    cur["pool_upper"],
                    m["pool_lower"],
                    m["pool_upper"],
                ):
                    seen.add(m["pool_key"])
                    q.append(m)
        return len(seen) == len(remain)

    return [m["pool_key"] for m in members if not connected_without(m["pool_key"])]


def _count_edges(members: list[dict[str, Any]]) -> dict[str, int]:
    touch = 0
    overlap = 0
    for i, a in enumerate(members):
        for b in members[i + 1 :]:
            k = edge_kind(a["pool_lower"], a["pool_upper"], b["pool_lower"], b["pool_upper"])
            if k == "genuine_overlap":
                overlap += 1
            elif k == "edge_touch_only":
                touch += 1
    return {"genuine_overlap_edges": overlap, "edge_touch_only_edges": touch}


def _overlap_depth_histogram(members: list[dict[str, Any]]) -> dict[str, int]:
    c: Counter[int] = Counter()
    for m in members:
        n = sum(
            1
            for o in members
            if o["pool_key"] != m["pool_key"]
            and intervals_strict_overlap(
                m["pool_lower"], m["pool_upper"], o["pool_lower"], o["pool_upper"]
            )
        )
        c[n] += 1
    return {str(k): v for k, v in sorted(c.items())}


def _path_to_uppermost(
    entry: dict[str, Any],
    members: list[dict[str, Any]],
    mode: ConnectivityMode,
) -> list[str]:
    by_pk = {m["pool_key"]: m for m in members}
    target_upper = max(m["pool_upper"] for m in members)
    targets = {m["pool_key"] for m in members if m["pool_upper"] == target_upper}
    parent: dict[str, str | None] = {entry["pool_key"]: None}
    q = deque([entry])
    found: str | None = None
    while q and found is None:
        cur = q.popleft()
        if cur["pool_key"] in targets:
            found = cur["pool_key"]
            break
        for m in members:
            if m["pool_key"] in parent:
                continue
            if _connected(
                mode,
                cur["pool_lower"],
                cur["pool_upper"],
                m["pool_lower"],
                m["pool_upper"],
            ):
                parent[m["pool_key"]] = cur["pool_key"]
                q.append(m)
    if found is None:
        return [entry["pool_key"]]
    path: list[str] = []
    cur: str | None = found
    while cur is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()
    return path


def component_from_entry(
    snapshot: dict[str, Any],
    *,
    entry_pool_lower: float,
    entry_pool_upper: float,
    mode: ConnectivityMode,
) -> dict[str, Any]:
    entry_pk = pool_key(entry_pool_lower, entry_pool_upper)
    bands = [_band_row(b) for b in snapshot.get("upper_bands") or []]
    by_pk = {b["pool_key"]: b for b in bands}
    if entry_pk not in by_pk:
        return {
            "connectivity_mode": mode,
            "status": "UNRESOLVED_PRICE_LEVEL_EDGE",
            "unresolved_reason": "ENTRY_BAND_NOT_IN_SNAPSHOT",
            "entry_pool_key": entry_pk,
        }

    entry = by_pk[entry_pk]
    component_keys: set[str] = {entry_pk}
    q: deque[dict[str, Any]] = deque([entry])
    while q:
        cur = q.popleft()
        for b in bands:
            if b["pool_key"] in component_keys:
                continue
            if _connected(
                mode,
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

    edges = _count_edges(members)
    gaps = internal_coverage_gaps(members, comp_lo, comp_hi)
    arts = _articulation_keys(members, mode)
    path = _path_to_uppermost(entry, members, mode)
    widths = [m["pool_upper"] - m["pool_lower"] for m in members]
    median_width = sorted(widths)[len(widths) // 2]

    unresolved: str | None = None
    if gaps:
        unresolved = "INTERNAL_COVERAGE_GAP_IN_ENVELOPE"
    elif arts and len(members) >= 3:
        unresolved = "WEAK_ARTICULATION_BRIDGE_CHAIN"
    elif len(members) > 1 and edges["genuine_overlap_edges"] == 0:
        unresolved = "TOUCH_ONLY_CONNECTION_CHAIN"

    status = "CONFIRMED_PRICE_LEVEL_EDGE" if unresolved is None else "UNRESOLVED_PRICE_LEVEL_EDGE"

    return {
        "connectivity_mode": mode,
        "status": status,
        "unresolved_reason": unresolved,
        "entry_pool_key": entry_pk,
        "component_lower": comp_lo,
        "component_upper": comp_hi,
        "component_width": comp_hi - comp_lo,
        "member_count": len(members),
        "member_pool_keys": [m["pool_key"] for m in members],
        "unique_origin_ids": sorted(origins),
        "unique_origin_count": len(origins),
        "edge_counts": edges,
        "overlap_depth_histogram": _overlap_depth_histogram(members),
        "internal_coverage_gaps": gaps,
        "articulation_pool_keys": arts,
        "path_entry_to_uppermost_pool_keys": path,
        "uppermost_pool_key": path[-1] if path else entry_pk,
        "median_member_width": median_width,
        "component_span_vs_median_width_ratio": (comp_hi - comp_lo) / median_width if median_width > 0 else None,
    }


def sl_from_component_upper(
    component: dict[str, Any],
    *,
    sl_pct: float,
    short_sl_fn,
    use_confirmed_only: bool = True,
) -> dict[str, Any]:
    top = component.get("component_upper")
    if top is None:
        return {"sl": None, "sl_status": "no_component"}
    sl = short_sl_fn(float(top), sl_pct)
    if use_confirmed_only and component.get("status") != "CONFIRMED_PRICE_LEVEL_EDGE":
        return {
            "sl": sl,
            "sl_status": "computed_but_UNRESOLVED",
            "component_upper_used": top,
        }
    return {
        "sl": sl,
        "sl_status": "CONFIRMED",
        "component_upper_used": top,
    }
