"""Upper-pool gap TP selection (mirror of lower_pool_gap_tp_v1 for LONG B)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHORT_PKG = ROOT / "tools/phase_short_main_v2_combined_exit_v1"
if str(SHORT_PKG) not in sys.path:
    sys.path.insert(0, str(SHORT_PKG))

from lower_pool_gap_tp_v1 import _parse_origin_ids  # noqa: E402


@dataclass(frozen=True)
class AtomicUpperBandV1:
    pool_key: str
    lower_edge: float
    upper_edge: float
    origin_ids: tuple[str, ...]
    unique_origin_count: int


@dataclass
class UpperRegionV1:
    lower_edge: float
    upper_edge: float
    atomics: list[AtomicUpperBandV1] = field(default_factory=list)
    distinct_origin_ids: tuple[str, ...] = ()

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "lower_edge": self.lower_edge,
            "upper_edge": self.upper_edge,
            "distinct_origin_ids": list(self.distinct_origin_ids),
        }


def active_atomic_uppers_from_snapshot(
    snapshot: dict[str, Any],
    *,
    min_unique_origins: int = 2,
) -> list[AtomicUpperBandV1]:
    out: list[AtomicUpperBandV1] = []
    for b in snapshot.get("upper_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        n = int(b.get("unique_origin_count") or 0)
        if n < min_unique_origins:
            continue
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        oids = _parse_origin_ids(b.get("origin_ids"))
        out.append(
            AtomicUpperBandV1(
                pool_key=f"{b['lower_price']}|{b['upper_price']}",
                lower_edge=lo,
                upper_edge=hi,
                origin_ids=oids,
                unique_origin_count=n,
            )
        )
    out.sort(key=lambda x: (x.lower_edge, x.upper_edge))
    return out


def merge_atomic_uppers_into_regions(atomics: list[AtomicUpperBandV1]) -> list[UpperRegionV1]:
    if not atomics:
        return []
    regions: list[UpperRegionV1] = []
    cur = [atomics[0]]
    cur_lo, cur_hi = atomics[0].lower_edge, atomics[0].upper_edge
    for a in atomics[1:]:
        overlaps = a.lower_edge <= cur_hi + 1e-12 and a.upper_edge >= cur_lo - 1e-12
        adjacent = abs(a.lower_edge - cur_hi) <= 1e-12
        if overlaps or adjacent:
            cur.append(a)
            cur_lo = min(cur_lo, a.lower_edge)
            cur_hi = max(cur_hi, a.upper_edge)
        else:
            regions.append(_region_from_atomics(cur, cur_lo, cur_hi))
            cur = [a]
            cur_lo, cur_hi = a.lower_edge, a.upper_edge
    regions.append(_region_from_atomics(cur, cur_lo, cur_hi))
    return regions


def _region_from_atomics(atomics: list[AtomicUpperBandV1], lo: float, hi: float) -> UpperRegionV1:
    oset: set[str] = set()
    for a in atomics:
        oset.update(a.origin_ids)
    return UpperRegionV1(lower_edge=lo, upper_edge=hi, atomics=list(atomics), distinct_origin_ids=tuple(sorted(oset)))


def gap_pct_between_upper_regions(p: UpperRegionV1, q: UpperRegionV1) -> float:
    if q.lower_edge <= p.upper_edge + 1e-15:
        return 0.0
    return (q.lower_edge - p.upper_edge) / p.upper_edge * 100.0


def select_upper_pool_gap_tp_candidate(
    snapshot: dict[str, Any],
    *,
    market_price: float,
    entry_fill: float,
    gap_threshold_pct: float,
    min_unique_origins: int = 2,
) -> dict[str, Any] | None:
    atomics = active_atomic_uppers_from_snapshot(snapshot, min_unique_origins=min_unique_origins)
    regions = merge_atomic_uppers_into_regions(atomics)
    if not regions:
        return None
    stacked = sorted(regions, key=lambda r: (r.lower_edge, r.upper_edge))
    cands: list[dict[str, Any]] = []
    for i, p in enumerate(stacked):
        if i + 1 >= len(stacked):
            continue
        q = stacked[i + 1]
        g = gap_pct_between_upper_regions(p, q)
        if g + 1e-12 < gap_threshold_pct:
            continue
        tp_px = p.lower_edge
        if tp_px <= entry_fill + 1e-12:
            continue
        cands.append({"region": p, "gap_pct": g, "higher": q, "tp_limit_price": tp_px})
    if not cands:
        return None
    pick = max(cands, key=lambda c: c["region"].lower_edge)
    reg: UpperRegionV1 = pick["region"]
    return {
        "tp_limit_price": reg.lower_edge,
        "region": reg.to_audit_dict(),
        "gap_pct": round(pick["gap_pct"], 6),
        "market_price_at_decision": market_price,
        "higher_region": pick["higher"].to_audit_dict(),
        "entry_fill": entry_fill,
    }
