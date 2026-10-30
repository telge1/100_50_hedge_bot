"""CAUSAL_REGION_LINEAGE_5 — deterministic lower-region continuity (Phase 2H research)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHORT_PKG = ROOT / "tools/phase_short_main_v2_combined_exit_v1"
if str(SHORT_PKG) not in sys.path:
    sys.path.insert(0, str(SHORT_PKG))

from lower_pool_gap_tp_v1 import LowerRegionV1, merge_atomic_lowers_into_regions, active_atomic_lowers_from_snapshot


class LineageContinuityClassV2H(str, Enum):
    A_STABLE_ATOMIC_SET = "A_IDENTICAL_ATOMIC_BANDS"
    B_ORIGIN_EXTENSION = "B_ORIGIN_EXTENSION"
    C_ORIGIN_PARTIAL_LOSS = "C_ORIGIN_PARTIAL_LOSS"
    D_DISCONTINUITY = "D_DISCONTINUITY"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class RegionFrameV2H:
    region_key: str
    lower: float
    upper: float
    origin_ids: frozenset[str]
    atomic_keys: frozenset[str]


def _region_key(r: LowerRegionV1) -> str:
    return f"{r.lower_edge}|{r.upper_edge}"


def _frame(r: LowerRegionV1) -> RegionFrameV2H:
    return RegionFrameV2H(
        region_key=_region_key(r),
        lower=r.lower_edge,
        upper=r.upper_edge,
        origin_ids=frozenset(r.distinct_origin_ids),
        atomic_keys=frozenset(f"{a.lower_edge}|{a.upper_edge}" for a in r.atomics),
    )


def _price_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> float:
    lo = max(lo1, lo2)
    hi = min(hi1, hi2)
    if hi + 1e-12 < lo:
        if abs(hi1 - lo2) <= 1e-12 or abs(hi2 - lo1) <= 1e-12:
            return 0.0
        return -1.0
    return hi - lo


def _origin_overlap(a: frozenset[str], b: frozenset[str]) -> int:
    return len(a & b)


def _classify(prev: RegionFrameV2H, curr: RegionFrameV2H, overlap_n: int) -> LineageContinuityClassV2H:
    if overlap_n <= 0:
        return LineageContinuityClassV2H.D_DISCONTINUITY
    if curr.atomic_keys == prev.atomic_keys:
        return LineageContinuityClassV2H.A_STABLE_ATOMIC_SET
    if curr.origin_ids >= prev.origin_ids and (
        curr.lower <= prev.lower + 1e-12 and curr.upper >= prev.upper - 1e-12
    ):
        return LineageContinuityClassV2H.B_ORIGIN_EXTENSION
    if overlap_n < len(prev.origin_ids):
        return LineageContinuityClassV2H.C_ORIGIN_PARTIAL_LOSS
    return LineageContinuityClassV2H.B_ORIGIN_EXTENSION


@dataclass
class LineageBarRecordV2H:
    bar_utc: str
    lineage_id: str
    frame: RegionFrameV2H
    prev_region_key: str | None
    origin_overlap: int
    price_overlap_width: float
    continuity_class: LineageContinuityClassV2H
    atomic_age_min: int
    lineage_age: int
    lineage_age_status: str
    below_price_streak: int = 0
    below_market: bool | None = None
    match_ambiguous: bool = False


@dataclass
class CausalLineageTrackerV2H:
    """Tracks lineage streaks; call advance() once per closed bar."""

    min_unique_origins: int = 2
    min_age_bars: int = 5
    _seq: int = 0
    _prev_frames: list[tuple[str, RegionFrameV2H]] = field(default_factory=list)
    streak_by_lineage: dict[str, int] = field(default_factory=dict)
    region_lineage: dict[str, str] = field(default_factory=dict)
    last_class: dict[str, LineageContinuityClassV2H] = field(default_factory=dict)
    bounds_history: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    below_price_streak_by_lineage: dict[str, int] = field(default_factory=dict)
    bar_records: list[LineageBarRecordV2H] = field(default_factory=list)
    history_complete: bool = True

    def _new_lineage_id(self) -> str:
        self._seq += 1
        return f"lin_v2h_{self._seq}"

    def advance(
        self,
        *,
        bar_utc: str,
        regions: list[LowerRegionV1],
        market_close: float | None = None,
        atomic_streak: dict[str, int] | None = None,
    ) -> None:
        if not self.history_complete:
            cont_class = LineageContinuityClassV2H.UNDETERMINED
        else:
            cont_class = None

        curr_frames = [_frame(r) for r in regions]
        matched_prev: set[str] = set()
        new_region_lineage: dict[str, str] = {}

        for cf in sorted(curr_frames, key=lambda f: (-f.upper, -f.lower)):
            candidates: list[tuple[int, float, str, RegionFrameV2H]] = []
            for prev_lid, pf in self._prev_frames:
                if prev_lid in matched_prev:
                    continue
                po = _price_overlap(cf.lower, cf.upper, pf.lower, pf.upper)
                if po < -0.5:
                    continue
                oo = _origin_overlap(cf.origin_ids, pf.origin_ids)
                if oo <= 0:
                    continue
                pw = max(po, 0.0)
                candidates.append((oo, pw, prev_lid, pf))

            best: tuple[int, float, str, RegionFrameV2H] | None = None
            ambiguous = False
            if candidates:
                candidates.sort(key=lambda x: (-x[0], -x[1], x[2]))
                best = candidates[0]
                if len(candidates) > 1 and (candidates[0][0], candidates[0][1]) == (
                    candidates[1][0],
                    candidates[1][1],
                ):
                    ambiguous = True

            if best is None or not self.history_complete or ambiguous:
                lid = self._new_lineage_id()
                cls = LineageContinuityClassV2H.UNDETERMINED if not self.history_complete else LineageContinuityClassV2H.D_DISCONTINUITY
                if ambiguous:
                    cls = LineageContinuityClassV2H.UNDETERMINED
                self.streak_by_lineage[lid] = 1
                prev_key = None
                oo = 0
                pw = -1.0
            else:
                oo, pw, prev_lid, pf = best
                matched_prev.add(prev_lid)
                lid = prev_lid
                cls = _classify(pf, cf, oo)
                self.streak_by_lineage[lid] = self.streak_by_lineage.get(lid, 0) + 1
                prev_key = pf.region_key

            new_region_lineage[cf.region_key] = lid
            self.last_class[lid] = cls
            self.bounds_history[(lid, bar_utc)] = (cf.lower, cf.upper)

            atomic_min = 0
            if atomic_streak is not None and cf.atomic_keys:
                from pool_consecutive_streak_v2e_v1 import streak_for_band

                streaks = []
                for ak in cf.atomic_keys:
                    lo_s, hi_s = ak.split("|", 1)
                    streaks.append(streak_for_band(atomic_streak, float(lo_s), float(hi_s)))
                atomic_min = min(streaks) if streaks else 0

            lin_age = self.streak_by_lineage[lid]
            lin_status = "OK" if lin_age >= self.min_age_bars else "ACCUMULATING"
            if cls == LineageContinuityClassV2H.UNDETERMINED:
                lin_status = "UNDETERMINED"

            below = None
            bp_streak = 0
            if market_close is not None:
                below = cf.upper < market_close - 1e-12
                if below:
                    bp_streak = self.below_price_streak_by_lineage.get(lid, 0) + 1
                else:
                    bp_streak = 0
                self.below_price_streak_by_lineage[lid] = bp_streak

            self.bar_records.append(
                LineageBarRecordV2H(
                    bar_utc=bar_utc,
                    lineage_id=lid,
                    frame=cf,
                    prev_region_key=prev_key,
                    origin_overlap=oo,
                    price_overlap_width=pw,
                    continuity_class=cls,
                    atomic_age_min=atomic_min if atomic_streak else 0,
                    lineage_age=lin_age,
                    lineage_age_status=lin_status,
                    below_price_streak=bp_streak,
                    below_market=below,
                    match_ambiguous=ambiguous,
                )
            )

        self.region_lineage = new_region_lineage
        self._prev_frames = [(new_region_lineage[f.region_key], f) for f in curr_frames if f.region_key in new_region_lineage]

    def lineage_id_for_region(self, region: LowerRegionV1) -> str | None:
        return self.region_lineage.get(_region_key(region))

    def streak_for_region(self, region: LowerRegionV1) -> int:
        lid = self.lineage_id_for_region(region)
        if lid is None:
            return 0
        return self.streak_by_lineage.get(lid, 0)

    def below_price_streak_for_region(self, region: LowerRegionV1) -> int:
        lid = self.lineage_id_for_region(region)
        if lid is None:
            return 0
        return self.below_price_streak_by_lineage.get(lid, 0)

    def region_lineage_age_for_region(self, region: LowerRegionV1) -> int:
        return self.streak_for_region(region)

    def find_region_by_lineage(
        self, regions: list[LowerRegionV1], lineage_id: str
    ) -> LowerRegionV1 | None:
        for r in regions:
            if self.lineage_id_for_region(r) == lineage_id:
                return r
        return None

    def formation_legacy_lineage_start(
        self,
        region: LowerRegionV1,
        *,
        bar_index: int,
        closes: list[str],
        prices: dict[str, dict[str, float]],
    ) -> tuple[bool, float | None]:
        lid = self.lineage_id_for_region(region)
        if lid is None:
            return False, None
        streak = self.streak_for_region(region)
        if streak <= 0 or bar_index - streak + 1 < 0:
            return False, None
        start_bar = closes[bar_index - streak + 1]
        bounds = self.bounds_history.get((lid, start_bar))
        if bounds is None:
            return False, None
        _, upper_at_start = bounds
        o = prices.get(start_bar)
        if o is None:
            return False, None
        ref = float(o["c"])
        return upper_at_start < ref - 1e-12, ref

    formation_below_price_ok = formation_legacy_lineage_start

    def formation_below_price_segment_ok(self, region: LowerRegionV1) -> bool:
        return (
            self.region_lineage_age_for_region(region) >= self.min_age_bars
            and self.below_price_streak_for_region(region) >= self.min_age_bars
        )

    def regions_from_snapshot(self, snapshot: dict[str, Any]) -> list[LowerRegionV1]:
        d2 = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=self.min_unique_origins)
        return merge_atomic_lowers_into_regions(d2)


def build_anchor_timeline(
    tracker: CausalLineageTrackerV2H,
    *,
    anchor_lo: float,
    anchor_hi: float,
    start_bar: str,
    end_bar: str,
) -> list[dict[str, Any]]:
    """Filter bar records for regions overlapping anchor band (price + lineage)."""
    rows: list[dict[str, Any]] = []
    anchor_origins: set[str] = set()
    for rec in tracker.bar_records:
        if rec.bar_utc < start_bar or rec.bar_utc > end_bar:
            continue
        f = rec.frame
        if _price_overlap(f.lower, f.upper, anchor_lo, anchor_hi) < -0.5:
            continue
        if anchor_origins and _origin_overlap(f.origin_ids, frozenset(anchor_origins)) <= 0 and rec.lineage_age == 1:
            continue
        anchor_origins |= set(f.origin_ids)
        rows.append(
            {
                "utc": rec.bar_utc,
                "region_key": f.region_key,
                "union_lower": f.lower,
                "union_upper": f.upper,
                "atomic_bands": sorted(f.atomic_keys),
                "origin_ids": sorted(f.origin_ids),
                "prev_region_key": rec.prev_region_key,
                "origin_overlap_prev": rec.origin_overlap,
                "price_overlap_prev": rec.price_overlap_width,
                "continuity_class": rec.continuity_class.value,
                "atomic_age_min": rec.atomic_age_min,
                "lineage_age": rec.lineage_age,
                "lineage_age_status": rec.lineage_age_status,
                "below_market": rec.below_market,
                "lineage_id": rec.lineage_id,
            }
        )
    return rows
