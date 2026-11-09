"""Derive research zones from canonical D2 qualified bands (not SSOT mutation)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pd1_exact_d2_adapter import members_to_instant_pools, origin_logical_v2_key, _iso_ms, _pool_active_at


@dataclass
class ResearchZone:
    bottom: float
    top: float
    max_depth: int
    band_count: int
    width_pct: float
    unique_origins: int
    dist_from_entry_pct: float


def _origins_in_span(members: list[dict], dc: datetime, entry: float, lo: float, hi: float) -> set[str]:
    pools = members_to_instant_pools(members, dc)
    t_ms = _iso_ms(dc)
    out: set[str] = set()
    for pl in pools:
        if pl.get("side") != "upper" or not _pool_active_at(pl, t_ms):
            continue
        b, t = float(pl["bottom"]), float(pl["top"])
        if t < lo or b > hi:
            continue
        out.add(str(pl["origin"]))
    return out


def merge_qualified_zones(qualified: list[dict[str, Any]], entry: float, *, merge_gap_pct: float) -> list[ResearchZone]:
    if not qualified:
        return []
    ql = sorted(qualified, key=lambda x: float(x["lower_bound"]))
    gap_abs = entry * merge_gap_pct / 100.0
    groups: list[list[dict]] = [[ql[0]]]
    for q in ql[1:]:
        prev = groups[-1][-1]
        if float(q["lower_bound"]) - float(prev["upper_bound"]) <= gap_abs + 1e-15:
            groups[-1].append(q)
        else:
            groups.append([q])
    zones: list[ResearchZone] = []
    for g in groups:
        bottom = float(g[0]["lower_bound"])
        top = max(float(x["upper_bound"]) for x in g)
        max_depth = max(int(x.get("d2_depth") or 0) for x in g)
        width_pct = (top - bottom) / bottom * 100.0 if bottom > 0 else 0.0
        zones.append(
            ResearchZone(
                bottom=bottom,
                top=top,
                max_depth=max_depth,
                band_count=len(g),
                width_pct=width_pct,
                unique_origins=0,
                dist_from_entry_pct=(bottom - entry) / entry * 100.0,
            )
        )
    return zones


def enrich_zone_origins(zones: list[ResearchZone], members: list[dict] | None, dc: datetime, entry: float) -> None:
    if not members:
        return
    for z in zones:
        z.unique_origins = len(_origins_in_span(members, dc, entry, z.bottom, z.top))


def zone_score(z: ResearchZone, variant: str) -> float:
    if variant == "TP3a_max_depth":
        return float(z.max_depth)
    if variant == "TP3b_max_origins":
        return float(z.unique_origins)
    if variant == "TP3c_depth_x_width":
        return float(z.max_depth) * z.width_pct
    return 0.0
