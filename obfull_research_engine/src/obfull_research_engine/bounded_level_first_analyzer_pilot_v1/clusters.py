"""Deterministic geometric MP-level clusters. Overlap only; no percent distance."""

from __future__ import annotations

from typing import Any

from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from . import CONFLUENCE_CLASSES, TF_RANK
from .bins import intervals_overlap


def cluster_id(member_ids: list[str], price_low: float, price_high: float) -> str:
    payload = {
        "members": sorted(member_ids),
        "price_low": float(price_low),
        "price_high": float(price_high),
    }
    return "cl_" + sha256_hex(payload)[:16]


def persistent_cluster_id(members: list[dict[str, Any]]) -> str:
    """Stable across snapshot minutes; ignores developing price drift."""
    keys = sorted(
        {
            str(m.get("logical_level_id") or m["level_id"])
            for m in members
        }
    )
    return "pc_" + sha256_hex({"logical_members": keys})[:16]


def confluence_class(members: list[dict[str, Any]]) -> str:
    states = {str(m["profile_state"]) for m in members}
    tfs = {str(m["timeframe"]) for m in members}
    if "CLOSED" in states and "DEVELOPING" in states:
        mixed = "MIXED_CLOSED_DEVELOPING"
    else:
        mixed = None
    if tfs == {"30m"}:
        geo = "SINGLE_30M"
    elif tfs == {"1h"}:
        geo = "SINGLE_1H"
    elif tfs == {"4h"}:
        geo = "SINGLE_4H"
    elif tfs == {"30m", "1h"}:
        geo = "MULTI_TF_30M_1H"
    elif tfs == {"30m", "4h"}:
        geo = "MULTI_TF_30M_4H"
    elif tfs == {"1h", "4h"}:
        geo = "MULTI_TF_1H_4H"
    elif tfs == {"30m", "1h", "4h"}:
        geo = "MULTI_TF_30M_1H_4H"
    else:
        raise ValueError(f"unexpected timeframes: {tfs}")
    if mixed:
        return mixed
    assert geo in CONFLUENCE_CLASSES
    return geo


def highest_timeframe(members: list[dict[str, Any]]) -> str:
    return max((str(m["timeframe"]) for m in members), key=lambda tf: TF_RANK[tf])


def cluster_levels(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union-find on inclusive bin-zone overlap. Deterministic member order."""
    if not levels:
        return []
    ordered = sorted(
        levels,
        key=lambda r: (
            r["level_id"],
            float(r["level_zone_low"]),
            float(r["level_zone_high"]),
        ),
    )
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if intervals_overlap(
                a["level_zone_low"],
                a["level_zone_high"],
                b["level_zone_low"],
                b["level_zone_high"],
            ):
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, row in enumerate(ordered):
        groups.setdefault(find(i), []).append(row)

    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        members = sorted(members, key=lambda m: m["level_id"])
        low = min(float(m["level_zone_low"]) for m in members)
        high = max(float(m["level_zone_high"]) for m in members)
        cid = cluster_id([m["level_id"] for m in members], low, high)
        clusters.append(
            {
                "level_cluster_id": cid,
                "persistent_cluster_id": persistent_cluster_id(members),
                "member_level_ids": [m["level_id"] for m in members],
                "member_logical_level_ids": [m.get("logical_level_id") or m["level_id"] for m in members],
                "member_timeframes": sorted({m["timeframe"] for m in members}, key=lambda tf: TF_RANK[tf]),
                "member_level_types": [m["tpo_type"] for m in members],
                "member_level_classes": [m["level_class"] for m in members],
                "member_profile_states": [m["profile_state"] for m in members],
                "cluster_price_low": low,
                "cluster_price_high": high,
                "highest_timeframe": highest_timeframe(members),
                "closed_member_count": sum(1 for m in members if m["profile_state"] == "CLOSED"),
                "developing_member_count": sum(1 for m in members if m["profile_state"] == "DEVELOPING"),
                "confluence_class": confluence_class(members),
                "members": members,
            }
        )
    clusters.sort(key=lambda c: (c["cluster_price_low"], c["cluster_price_high"], c["level_cluster_id"]))
    return clusters
