"""Phase 2F — read-only V2E qualification funnel (no threshold changes)."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHORT_PKG = ROOT / "tools/phase_short_main_v2_combined_exit_v1"
if str(SHORT_PKG) not in sys.path:
    sys.path.insert(0, str(SHORT_PKG))

from lower_pool_gap_tp_v1 import (  # noqa: E402
    LowerRegionV1,
    active_atomic_lowers_from_snapshot,
    gap_pct_between_regions,
    merge_atomic_lowers_into_regions,
)

from long_config_yaml_v1 import LongSupportQualificationYamlV1  # noqa: E402
from long_support_selection_v2e_v1 import (  # noqa: E402
    _audit_atomic,
    _region_key,
    _stacked,
    audit_region_qualification,
    region_pool_height_pct,
    resolve_q_for_p_v2e,
)
from pool_consecutive_streak_v2e_v1 import streak_for_band  # noqa: E402


NARROW_KEY = "0.086776|0.08679"


def _atomic_height_pct(lo: float, hi: float) -> float:
    return (hi - lo) / lo * 100.0 if lo > 0 else 0.0


def _all_active_lowers(snapshot: dict[str, Any]) -> list[dict]:
    out = []
    for b in snapshot.get("lower_bands") or []:
        if str(b.get("pool_status") or "").upper() == "ACTIVE":
            out.append(b)
    return out


def _depth2_atomics(snapshot: dict[str, Any], min_unique: int) -> list:
    return active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=min_unique)


def _region_lineage_key(region: LowerRegionV1) -> str:
    return "|".join(sorted(region.distinct_origin_ids))


@dataclass
class RegionFlagsV1:
    region_key: str
    upper: float
    lower: float
    union_height_pct: float
    atomic_count: int
    distinct_origins: int
    below_price: bool
    height_ok: bool
    age_a_ok: bool
    age_b_ok: bool
    formation_ok: bool
    p_qualified_a: bool
    has_q_a: bool
    gap_ok_a: bool
    p_entry_ok_a: bool
    has_q_b: bool
    gap_ok_b: bool
    p_entry_ok_b: bool
    fail_reasons: list[str] = field(default_factory=list)


def _q_depth2_only(q: LowerRegionV1, min_unique: int) -> bool:
    if not q.atomics:
        return False
    return all(a.unique_origin_count >= min_unique for a in q.atomics)


def analyze_bar_funnel(
    *,
    snapshot: dict[str, Any],
    market_close: float,
    bar_utc: str,
    bar_index: int,
    closes: list[str],
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    live_streak: dict[str, int],
    lineage_streak: dict[str, int],
) -> dict[str, Any]:
    active_bands = _all_active_lowers(snapshot)
    d2 = _depth2_atomics(snapshot, yaml.min_unique_origins)
    regions = merge_atomic_lowers_into_regions(d2)
    stacked = sorted(regions, key=lambda r: (-r.upper_edge, -r.lower_edge))

    flags: list[RegionFlagsV1] = []
    for r in stacked:
        atom_heights = [_atomic_height_pct(a.lower_edge, a.upper_edge) for a in r.atomics]
        audits = [
            _audit_atomic(
                None,
                decision_close_utc=bar_utc,
                atomic_lo=a.lower_edge,
                atomic_hi=a.upper_edge,
                prices=prices,
                yaml=yaml,
                live_consecutive_streak=live_streak,
                bar_index=bar_index,
                closes=closes,
            )
            for a in r.atomics
        ]
        age_a = all(aa.age_ok for aa in audits)
        lk = _region_lineage_key(r)
        age_b = lineage_streak.get(lk, 0) >= yaml.min_pool_age_bars
        formation_ok = all(aa.formation_below_price_ok for aa in audits) if audits else False
        h = region_pool_height_pct(r)
        below = r.upper_edge < market_close - 1e-12
        height_ok = h + 1e-12 >= yaml.min_pool_height_pct

        pa = audit_region_qualification(
            None,
            region=r,
            decision_close_utc=bar_utc,
            market_close=market_close,
            prices=prices,
            yaml=yaml,
            live_consecutive_streak=live_streak,
            bar_index=bar_index,
            closes=closes,
        )
        p_qual_a = pa.qualified

        q_a, g_a, _ = (None, None, ())
        if p_qual_a:
            q_a, g_a, _ = resolve_q_for_p_v2e(
                None,
                p=r,
                stacked=stacked,
                decision_close_utc=bar_utc,
                market_close=market_close,
                prices=prices,
                yaml=yaml,
                live_consecutive_streak=live_streak,
                bar_index=bar_index,
                closes=closes,
            )
        gap_a = q_a is not None and g_a is not None and g_a + 1e-12 >= yaml.lower_gap_threshold_pct
        entry_a = p_qual_a and gap_a

        q_b = None
        g_b = None
        try:
            idx = next(i for i, rr in enumerate(stacked) if _region_key(rr) == _region_key(r))
        except StopIteration:
            idx = -1
        if idx >= 0:
            for j in range(idx + 1, len(stacked)):
                cand = stacked[j]
                if not _q_depth2_only(cand, yaml.min_unique_origins):
                    continue
                g = gap_pct_between_regions(r, cand)
                q_b, g_b = cand, g
                break
        gap_b = q_b is not None and g_b is not None and g_b + 1e-12 >= yaml.lower_gap_threshold_pct
        entry_b = p_qual_a and gap_b

        fails: list[str] = []
        if not below:
            fails.append("NOT_BELOW_MARKET_CLOSE")
        if not height_ok:
            fails.append("HEIGHT_BELOW_MIN")
        if not age_a:
            fails.append("ATOMIC_AGE_INSUFFICIENT")
        if not formation_ok:
            fails.append("FORMATION_NOT_BELOW_PRICE")
        if below and height_ok and age_a and formation_ok and not gap_a:
            if q_a is None:
                fails.append("NO_DEEPER_QUALIFIED_Q_A")
            else:
                fails.append("GAP_BELOW_THRESHOLD")

        flags.append(
            RegionFlagsV1(
                region_key=_region_key(r),
                upper=r.upper_edge,
                lower=r.lower_edge,
                union_height_pct=round(h, 8),
                atomic_count=len(r.atomics),
                distinct_origins=len(r.distinct_origin_ids),
                below_price=below,
                height_ok=height_ok,
                age_a_ok=age_a,
                age_b_ok=age_b,
                formation_ok=formation_ok,
                p_qualified_a=p_qual_a,
                has_q_a=q_a is not None,
                gap_ok_a=gap_a,
                p_entry_ok_a=entry_a,
                has_q_b=q_b is not None,
                gap_ok_b=gap_b,
                p_entry_ok_b=entry_b,
                fail_reasons=fails,
            )
        )

    def _count(pred) -> int:
        return sum(1 for f in flags if pred(f))

    # Independent pass counts (each filter evaluated on full stacked regions)
    indep = {
        "below_price": _count(lambda f: f.below_price),
        "height_min": _count(lambda f: f.height_ok),
        "age_atomic_5": _count(lambda f: f.age_a_ok),
        "age_lineage_5": _count(lambda f: f.age_b_ok),
        "formation_below": _count(lambda f: f.formation_ok),
        "p_full_a": _count(lambda f: f.p_qualified_a),
        "has_q_a": _count(lambda f: f.has_q_a),
        "gap_a": _count(lambda f: f.gap_ok_a),
        "entry_a": _count(lambda f: f.p_entry_ok_a),
        "gap_b_q_relaxed": _count(lambda f: f.gap_ok_b),
        "entry_b_q_relaxed": _count(lambda f: f.p_entry_ok_b),
    }

    sequential = {
        "A_active_lower_atomics": len(active_bands),
        "B_depth2_atomics": len(d2),
        "C_union_regions": len(regions),
        "D_below_price": indep["below_price"],
        "E_height_ge_min": sum(1 for f in flags if f.below_price and f.height_ok),
        "F_age_atomic_5": sum(1 for f in flags if f.below_price and f.height_ok and f.age_a_ok),
        "G_formation": sum(
            1 for f in flags if f.below_price and f.height_ok and f.age_a_ok and f.formation_ok
        ),
        "H_has_q_a": sum(
            1
            for f in flags
            if f.below_price and f.height_ok and f.age_a_ok and f.formation_ok and f.has_q_a
        ),
        "I_gap_ge_min": sum(1 for f in flags if f.p_entry_ok_a),
        "J_entry_supports": sum(1 for f in flags if f.p_entry_ok_a),
    }

    v2e_select = next((f.region_key for f in flags if f.p_entry_ok_a), None)
    v2e_reason = None if v2e_select else "NO_V2E_QUALIFIED_SUPPORT"

    return {
        "bar_utc": bar_utc,
        "close": market_close,
        "sequential_funnel": sequential,
        "independent_counts_among_regions": indep,
        "regions_total": len(flags),
        "v2e_select": v2e_select,
        "v2e_select_reason": v2e_reason,
        "region_flags": [f.__dict__ for f in flags],
    }


def update_lineage_streaks(snapshot: dict[str, Any], lineage_streak: dict[str, int], min_unique: int) -> None:
    """Variant B: streak by sorted distinct origin set of each merged region."""
    present: set[str] = set()
    for r in merge_atomic_lowers_into_regions(_depth2_atomics(snapshot, min_unique)):
        present.add(_region_lineage_key(r))
    for key in set(lineage_streak.keys()) | present:
        lineage_streak[key] = lineage_streak.get(key, 0) + 1 if key in present else 0


def aggregate_funnel(bar_reports: list[dict[str, Any]]) -> dict[str, Any]:
    seq_keys = list(bar_reports[0]["sequential_funnel"].keys()) if bar_reports else []
    seq_min = {k: min(r["sequential_funnel"][k] for r in bar_reports) for k in seq_keys}
    seq_max = {k: max(r["sequential_funnel"][k] for r in bar_reports) for k in seq_keys}
    seq_zero_bars = {k: sum(1 for r in bar_reports if r["sequential_funnel"][k] == 0) for k in seq_keys}

    reason_counter: Counter[str] = Counter()
    overlap: Counter[str] = Counter()
    for r in bar_reports:
        for reg in r["region_flags"]:
            if not reg["below_price"]:
                continue
            rs = reg["fail_reasons"]
            for reason in rs:
                reason_counter[reason] += 1
            if len(rs) > 1:
                overlap["+".join(sorted(rs))] += 1

    v2e_hits = sum(1 for r in bar_reports if r["v2e_select"] is not None)

    indep_avg = {}
    if bar_reports:
        keys = bar_reports[0]["independent_counts_among_regions"].keys()
        for k in keys:
            indep_avg[k] = round(
                sum(r["independent_counts_among_regions"][k] for r in bar_reports) / len(bar_reports), 4
            )

    return {
        "bars": len(bar_reports),
        "v2e_bars_with_entry_support": v2e_hits,
        "sequential_funnel_min_across_bars": seq_min,
        "sequential_funnel_max_across_bars": seq_max,
        "sequential_funnel_bars_where_stage_zero": seq_zero_bars,
        "exclusion_reason_counts_below_price_regions": dict(reason_counter),
        "multi_reason_overlaps": dict(overlap.most_common(20)),
        "independent_filter_avg_count_per_bar": indep_avg,
    }


def narrow_pool_report(bar_reports: list[dict[str, Any]]) -> dict[str, Any]:
    samples = []
    for r in bar_reports:
        for reg in r["region_flags"]:
            if reg["region_key"] == NARROW_KEY or abs(reg["lower"] - 0.086776) < 1e-6:
                samples.append({"bar": r["bar_utc"], **reg})
                break
    return {
        "reference_key": NARROW_KEY,
        "height_pct": region_pool_height_pct(
            type("R", (), {"lower_edge": 0.086776, "upper_edge": 0.08679})()
        ),
        "excluded_by_height_rule": True,
        "bar_samples": samples[:5],
    }


def forensic_window_detail(bar_reports: list[dict[str, Any]], start: str, end: str) -> list[dict]:
    out = []
    for r in bar_reports:
        if start <= r["bar_utc"] <= end:
            below = [x for x in r["region_flags"] if x["below_price"]]
            out.append(
                {
                    "bar_utc": r["bar_utc"],
                    "close": r["close"],
                    "funnel": r["sequential_funnel"],
                    "regions_below_price": below,
                    "best_last_stage": max(
                        (x for x in below if x["p_qualified_a"]),
                        key=lambda x: x["upper"],
                        default=None,
                    ),
                }
            )
    return out
