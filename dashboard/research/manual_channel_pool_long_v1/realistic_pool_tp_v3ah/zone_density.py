"""Integrated D2 zone density + CAP/dominance TP selection (V3W merge reuse)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pool_d2_metrics import analyze_d2_landscape
from zone_features import ResearchZone, enrich_zone_origins, merge_qualified_zones

Dominance = Literal["D20", "D25"]
NoTpPolicy = Literal["BLOCK", "ALLOW_NOPOOL"]

DOMINANCE_FRAC = {"D20": 0.20, "D25": 0.25}
MERGE_GAP_PCT = 0.15


@dataclass(frozen=True)
class RealisticTPParams:
    code: str
    cap_dist_pct: float
    dominance: Dominance
    no_tp_policy: NoTpPolicy


def _integrated_density(zone: ResearchZone, qualified: list[dict[str, Any]]) -> float:
    lo, hi = zone.bottom, zone.top
    total = 0.0
    for q in qualified:
        qb = float(q["lower_bound"])
        qt = float(q["upper_bound"])
        if qt <= lo + 1e-15 or qb >= hi - 1e-15:
            continue
        a = max(lo, qb)
        b = min(hi, qt)
        w = b - a
        if w > 0:
            total += int(q.get("d2_depth") or 0) * w
    return total


def build_zones_with_density(
    members: list[dict] | None,
    dc: datetime,
    entry: float,
) -> tuple[list[dict[str, Any]], list[ResearchZone]]:
    """Merge qualified D2 bands; attach integrated_density and density_rank (1=strongest)."""
    _m, _bands, qualified = analyze_d2_landscape(
        members, dc, entry, apply_provisional_research_filter=False
    )
    if not qualified:
        return [], []
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=MERGE_GAP_PCT)
    enrich_zone_origins(zones, members, dc, entry)
    rows: list[dict[str, Any]] = []
    for z in zones:
        integ = _integrated_density(z, qualified)
        dist = (z.bottom - entry) / entry * 100.0
        rows.append(
            {
                "zone": z,
                "integrated_density": integ,
                "dist_from_entry_pct": dist,
            }
        )
    rows.sort(key=lambda r: (-r["integrated_density"], r["zone"].bottom))
    n = len(rows)
    cut20 = max(1, math.ceil(n * DOMINANCE_FRAC["D20"]))
    cut25 = max(1, math.ceil(n * DOMINANCE_FRAC["D25"]))
    for i, r in enumerate(rows):
        r["density_rank"] = i + 1
        r["in_d20"] = r["density_rank"] <= cut20
        r["in_d25"] = r["density_rank"] <= cut25
    return rows, [r["zone"] for r in sorted(rows, key=lambda r: r["zone"].bottom)]


@dataclass
class TPResolution:
    ok: bool
    reason: str
    tp_bottom: float | None
    zone_top: float | None
    integrated_density: float | None
    density_rank: int | None
    n_zones: int
    dist_pct: float | None
    meta: dict[str, Any]


def _dominance_ok(row: dict[str, Any], dominance: Dominance) -> bool:
    if dominance == "D20":
        return bool(row["in_d20"])
    return bool(row["in_d25"])


def resolve_realistic_tp(
    members: list[dict] | None,
    dc: datetime,
    entry: float,
    *,
    cap_dist_pct: float,
    dominance: Dominance,
) -> TPResolution:
    scored, _ = build_zones_with_density(members, dc, entry)
    n = len(scored)
    if not scored:
        return TPResolution(
            False,
            "NO_QUALIFIED_TP",
            None,
            None,
            None,
            None,
            0,
            None,
            {"cap_pct": cap_dist_pct, "dominance": dominance},
        )
    by_price = sorted(scored, key=lambda r: r["zone"].bottom)
    for r in by_price:
        dist = r["dist_from_entry_pct"]
        if dist < 0.5 - 1e-9:
            continue
        if dist > cap_dist_pct + 1e-9:
            continue
        if not _dominance_ok(r, dominance):
            continue
        z = r["zone"]
        return TPResolution(
            True,
            "",
            z.bottom,
            z.top,
            r["integrated_density"],
            r["density_rank"],
            n,
            dist,
            {
                "cap_pct": cap_dist_pct,
                "dominance": dominance,
                "max_depth": z.max_depth,
                "unique_origins": z.unique_origins,
                "width_pct": z.width_pct,
                "band_count": z.band_count,
            },
        )
    return TPResolution(
        False,
        "NO_QUALIFIED_TP",
        None,
        None,
        None,
        None,
        n,
        None,
        {"cap_pct": cap_dist_pct, "dominance": dominance},
    )


def all_research_params() -> tuple[RealisticTPParams, ...]:
    out: list[RealisticTPParams] = []
    for cap, cap_lbl in ((3.0, "CAP3"), (5.0, "CAP5"), (6.0, "CAP6")):
        for dom in ("D20", "D25"):
            for pol, pol_lbl in (("BLOCK", "BLOCK"), ("ALLOW_NOPOOL", "ALLOW")):
                code = f"{cap_lbl}_{dom}_{pol_lbl}"
                out.append(
                    RealisticTPParams(
                        code=code,
                        cap_dist_pct=cap,
                        dominance=dom,  # type: ignore[arg-type]
                        no_tp_policy=pol,  # type: ignore[arg-type]
                    )
                )
    return tuple(out)


def resolution_to_meta(res: TPResolution) -> str:
    return json.dumps(
        {
            "reason": res.reason,
            "tp_bottom": res.tp_bottom,
            "zone_top": res.zone_top,
            "integrated_density": res.integrated_density,
            "density_rank": res.density_rank,
            "n_zones": res.n_zones,
            "dist_pct": res.dist_pct,
            **(res.meta or {}),
        },
        sort_keys=True,
    )
