"""Phase 2G — SSOT pool identification and V2E qualification checklist."""

from __future__ import annotations

import sys
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
from long_ema_gate_v1 import evaluate_long_ema_gate  # noqa: E402
from long_support_selection_v2e_v1 import (  # noqa: E402
    _region_key,
    audit_region_qualification,
    region_pool_height_pct,
    resolve_q_for_p_v2e,
)
from pool_consecutive_streak_v2e_v1 import streak_for_band  # noqa: E402


@dataclass
class PoolQualChecklistV1:
    utc: str
    region_key: str
    lower: float
    upper: float
    height_pct: float
    age_bars: int
    FOUND_IN_SSOT: bool
    DEPTH2_OK: bool
    AGE_5_BARS_OK: bool
    HEIGHT_0_1_PCT_OK: bool
    FORMATION_BELOW_PRICE_OK: bool
    EMA_PERMISSION_OK: bool
    DEEPER_Q_FOUND: bool
    GAP_0_6_PCT_OK: bool
    LONG_ENTRY_ELIGIBLE: bool
    disqualify_reason: str | None = None
    ema_reason: str | None = None
    q_region_key: str | None = None
    gap_pct: float | None = None
    entry_limit: float | None = None
    structural_sl: float | None = None
    risk_pct: float | None = None
    distance_close_to_limit_pct: float | None = None
    atomics: list[dict[str, Any]] = field(default_factory=list)
    distinct_origins: list[str] = field(default_factory=list)


def _max_atomic_streak(region: LowerRegionV1, live_streak: dict[str, int]) -> int:
    if not region.atomics:
        return 0
    return min(streak_for_band(live_streak, a.lower_edge, a.upper_edge) for a in region.atomics)


def _stacked(snapshot: dict[str, Any], min_unique: int) -> list[LowerRegionV1]:
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=min_unique)
    return sorted(merge_atomic_lowers_into_regions(atomics), key=lambda r: (-r.upper_edge, -r.lower_edge))


def regions_below_price(snapshot: dict[str, Any], market_close: float, min_unique: int) -> list[LowerRegionV1]:
    return [r for r in _stacked(snapshot, min_unique) if r.upper_edge < market_close - 1e-12]


def checklist_for_region(
    *,
    bar_utc: str,
    bar_index: int,
    closes: list[str],
    region: LowerRegionV1,
    stacked: list[LowerRegionV1],
    market_close: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    live_streak: dict[str, int],
    ema9: float | None,
    ema20: float | None,
    ema59: float | None,
    ema200: float | None,
    ema200_prev6: float | None,
) -> PoolQualChecklistV1:
    pa = audit_region_qualification(
        None,
        region=region,
        decision_close_utc=bar_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_streak,
        bar_index=bar_index,
        closes=closes,
    )
    ema = evaluate_long_ema_gate(
        ema9=ema9,
        ema20=ema20,
        ema59=ema59,
        ema200=ema200,
        ema200_prev6=ema200_prev6,
    )
    age = _max_atomic_streak(region, live_streak)
    h = region_pool_height_pct(region)
    q, g, _ = resolve_q_for_p_v2e(
        None,
        p=region,
        stacked=stacked,
        decision_close_utc=bar_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_streak,
        bar_index=bar_index,
        closes=closes,
    )
    gap_ok = q is not None and g is not None and g + 1e-12 >= yaml.lower_gap_threshold_pct
    p_ok = pa.qualified
    entry_ok = p_ok and gap_ok and ema.authorized

    reason = pa.disqualify_reason
    if p_ok and not gap_ok:
        reason = "NO_DEEPER_QUALIFIED_Q" if q is None else "GAP_BELOW_THRESHOLD"
    if p_ok and gap_ok and not ema.authorized:
        reason = ema.reason_code

    entry_limit = region.upper_edge if entry_ok else (region.upper_edge if p_ok and gap_ok else None)
    sl = q.upper_edge if q is not None and gap_ok else None
    risk = None
    dist = None
    if entry_limit and sl is not None and sl < entry_limit:
        risk = (entry_limit - sl) / entry_limit * 100.0
    if entry_limit and market_close > 0:
        dist = (market_close - entry_limit) / market_close * 100.0

    atomics_detail = []
    for a in region.atomics:
        st = streak_for_band(live_streak, a.lower_edge, a.upper_edge)
        atomics_detail.append(
            {
                "pool_key": a.pool_key,
                "lower": a.lower_edge,
                "upper": a.upper_edge,
                "height_pct": round((a.upper_edge - a.lower_edge) / a.lower_edge * 100, 8),
                "origin_ids": list(a.origin_ids),
                "unique_origin_count": a.unique_origin_count,
                "consecutive_streak": st,
            }
        )

    return PoolQualChecklistV1(
        utc=bar_utc,
        region_key=_region_key(region),
        lower=region.lower_edge,
        upper=region.upper_edge,
        height_pct=round(h, 8),
        age_bars=age,
        FOUND_IN_SSOT=True,
        DEPTH2_OK=len(region.atomics) > 0
        and all(a.unique_origin_count >= yaml.min_unique_origins for a in region.atomics),
        AGE_5_BARS_OK=age >= yaml.min_pool_age_bars,
        HEIGHT_0_1_PCT_OK=h + 1e-12 >= yaml.min_pool_height_pct,
        FORMATION_BELOW_PRICE_OK=all(aa.formation_below_price_ok for aa in pa.atomics),
        EMA_PERMISSION_OK=ema.authorized,
        DEEPER_Q_FOUND=q is not None and pa.qualified,
        GAP_0_6_PCT_OK=gap_ok and pa.qualified,
        LONG_ENTRY_ELIGIBLE=entry_ok,
        disqualify_reason=reason if not entry_ok else None,
        ema_reason=None if ema.authorized else ema.reason_code,
        q_region_key=_region_key(q) if q else None,
        gap_pct=round(g, 8) if g is not None else None,
        entry_limit=entry_limit,
        structural_sl=sl,
        risk_pct=round(risk, 6) if risk is not None else None,
        distance_close_to_limit_pct=round(dist, 6) if dist is not None else None,
        atomics=atomics_detail,
        distinct_origins=list(region.distinct_origin_ids),
    )


def discover_region_first_active_bar(
    session,
    closes: list[str],
    region_key: str,
    min_unique: int,
    *,
    search_end_index: int,
) -> str | None:
    for i in range(search_end_index + 1):
        snap = session.collect_at_close(closes[i])
        for r in merge_atomic_lowers_into_regions(
            active_atomic_lowers_from_snapshot(snap, min_unique_origins=min_unique)
        ):
            if _region_key(r) == region_key:
                return closes[i]
    return None


def rank_candidate_regions_early_window(
    *,
    session,
    closes: list[str],
    prices: dict[str, dict[str, float]],
    start_i: int,
    end_i: int,
    min_unique: int,
) -> list[dict[str, Any]]:
    """Regions below price in early window — ranked by max upper edge (nearest support)."""
    seen: dict[str, dict[str, Any]] = {}
    for i in range(start_i, end_i + 1):
        bar = closes[i]
        close = float(prices[bar]["c"])
        snap = session.collect_at_close(bar)
        for r in regions_below_price(snap, close, min_unique):
            rk = _region_key(r)
            h = region_pool_height_pct(r)
            row = seen.get(rk)
            if row is None or close > row["max_close"]:
                seen[rk] = {
                    "region_key": rk,
                    "lower": r.lower_edge,
                    "upper": r.upper_edge,
                    "union_height_pct": round(h, 8),
                    "atomic_count": len(r.atomics),
                    "distinct_origins": list(r.distinct_origin_ids),
                    "sample_bar": bar,
                    "max_close": close,
                }
    return sorted(seen.values(), key=lambda x: (-x["upper"], -x["lower"]))
