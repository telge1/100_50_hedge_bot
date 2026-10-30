"""Phase 2I — detailed bar timeline for anchor lower region + P/Q audit."""

from __future__ import annotations

from typing import Any

from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H, _price_overlap
from long_ema_gate_v1 import evaluate_long_ema_gate
from long_risk_audit_v1 import risk_pct
from long_support_selection_v2e_v1 import (
    audit_region_qualification,
    region_pool_height_pct,
    resolve_q_for_p_v2e,
    _stacked,
)
from lower_pool_gap_tp_v1 import LowerRegionV1
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks


def _pick_anchor_region(
    regions: list[LowerRegionV1],
    close: float,
    anchor_lo: float,
    anchor_hi: float,
) -> LowerRegionV1 | None:
    cands = []
    for r in regions:
        if r.upper_edge >= close - 1e-12:
            continue
        if _price_overlap(r.lower_edge, r.upper_edge, anchor_lo, anchor_hi) < -0.5:
            continue
        cands.append(r)
    if not cands:
        return None
    return max(cands, key=lambda r: (r.upper_edge, r.lower_edge))


def build_phase2i_bar_timeline(
    *,
    session,
    closes: list[str],
    prices: dict[str, dict[str, float]],
    yaml_sup,
    start_bar: str,
    end_bar: str,
    anchor_lo: float,
    anchor_hi: float,
    emas: dict[int, dict[str, float]],
) -> list[dict[str, Any]]:
    i_start, i_end = closes.index(start_bar), closes.index(end_bar)
    live: dict[str, int] = {}
    tracker = CausalLineageTrackerV2H(
        min_unique_origins=yaml_sup.min_unique_origins,
        min_age_bars=yaml_sup.min_pool_age_bars,
    )
    rows: list[dict[str, Any]] = []
    for i in range(0, i_end + 1):
        bar = closes[i]
        snap = session.collect_at_close(bar)
        update_consecutive_lower_streaks(snap, live, min_unique_origins=yaml_sup.min_unique_origins)
        close = float(prices[bar]["c"])
        tracker.advance(
            bar_utc=bar,
            regions=tracker.regions_from_snapshot(snap),
            market_close=close,
            atomic_streak=live,
        )
        if i < i_start:
            continue
        stacked = _stacked(snap, yaml_sup.min_unique_origins)
        region = _pick_anchor_region(stacked, close, anchor_lo, anchor_hi)
        if region is None:
            continue
        h = region_pool_height_pct(region)
        lid = tracker.lineage_id_for_region(region) or ""
        lin_age = tracker.region_lineage_age_for_region(region)
        bp = tracker.below_price_streak_for_region(region)
        pa_legacy = audit_region_qualification(
            None,
            region=region,
            decision_close_utc=bar,
            market_close=close,
            prices=prices,
            yaml=yaml_sup,
            live_consecutive_streak=live,
            bar_index=i,
            closes=closes,
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            lineage_tracker=tracker,
            formation_semantics="LEGACY_LINEAGE_START",
        )
        pa_segment = audit_region_qualification(
            None,
            region=region,
            decision_close_utc=bar,
            market_close=close,
            prices=prices,
            yaml=yaml_sup,
            live_consecutive_streak=live,
            bar_index=i,
            closes=closes,
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            lineage_tracker=tracker,
            formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        )
        e200_p6 = emas[200].get(closes[i - 6]) if i >= 6 else None
        ema = evaluate_long_ema_gate(
            ema9=emas[9].get(bar),
            ema20=emas[20].get(bar),
            ema59=emas[59].get(bar),
            ema200=emas[200].get(bar),
            ema200_prev6=e200_p6,
        )
        q, g, _ = resolve_q_for_p_v2e(
            None,
            p=region,
            stacked=stacked,
            decision_close_utc=bar,
            market_close=close,
            prices=prices,
            yaml=yaml_sup,
            live_consecutive_streak=live,
            bar_index=i,
            closes=closes,
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            lineage_tracker=tracker,
            formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        )
        entry = region.upper_edge
        sl = q.upper_edge if q else None
        rp = risk_pct(entry, sl) if sl is not None and sl < entry else None
        p_qual = (
            pa_segment.qualified
            and ema.authorized
            and q is not None
            and g is not None
            and g + 1e-12 >= yaml_sup.lower_gap_threshold_pct
        )
        rows.append(
            {
                "utc": bar,
                "close": close,
                "region_lower": region.lower_edge,
                "region_upper": region.upper_edge,
                "pool_height_pct": round(h, 8),
                "origin_ids": list(region.distinct_origin_ids),
                "lineage_id": lid,
                "region_lineage_age_bars": lin_age,
                "below_price_streak_bars": bp,
                "formation_A_legacy": pa_legacy.qualified,
                "formation_A_reason": pa_legacy.disqualify_reason,
                "formation_B_segment": pa_segment.qualified,
                "formation_B_reason": pa_segment.disqualify_reason,
                "P_qualified_segment_ema": p_qual,
                "ema_ok": ema.authorized,
                "ema_reason": ema.reason_code,
                "Q_region": f"{q.lower_edge}|{q.upper_edge}" if q else None,
                "gap_pct": round(g, 8) if g is not None else None,
                "entry_limit": entry,
                "structural_sl": sl,
                "risk_pct": round(rp, 6) if rp is not None else None,
            }
        )
    return rows
