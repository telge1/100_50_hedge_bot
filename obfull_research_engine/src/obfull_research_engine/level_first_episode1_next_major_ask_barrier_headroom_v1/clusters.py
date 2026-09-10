"""Causal ask-liquidity barrier clusters — versioned merge gap, outcome-blind."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import CLUSTER_MERGE_GAP_TICKS, EPSILON, TICK_SIZE


def build_ask_barrier_clusters(
    candidates: list[dict[str, Any]],
    *,
    entry_price: float,
    decision_time: str,
    merge_gap_ticks: int = CLUSTER_MERGE_GAP_TICKS,
    min_percentile_for_relevant: float = 0.75,
) -> list[dict[str, Any]]:
    """Greedy merge of ascending ask walls within merge_gap_ticks.

    Each price level appears in at most one cluster (no double-count).
    """
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda c: c["price"])
    gap = float(merge_gap_ticks) * TICK_SIZE
    clusters: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = [ordered[0]]
    for w in ordered[1:]:
        if w["price"] - cur[-1]["price"] <= gap + 1e-12:
            cur.append(w)
        else:
            clusters.append(cur)
            cur = [w]
    clusters.append(cur)

    entry = float(entry_price)
    out: list[dict[str, Any]] = []
    seen_prices: set[float] = set()
    for i, members in enumerate(clusters):
        prices = [m["price"] for m in members]
        for p in prices:
            if p in seen_prices:
                raise RuntimeError(f"price level duplicated across clusters: {p}")
            seen_prices.add(p)
        qtys = [m["qty_base"] for m in members]
        notionals = [m["notional_usdt"] for m in members]
        total_q = sum(qtys)
        total_n = sum(notionals)
        peak = max(members, key=lambda m: m["qty_base"])
        # qty-weighted centroid
        centroid = sum(m["price"] * m["qty_base"] for m in members) / total_q if total_q > EPSILON else prices[0]
        nearest = min(prices)
        relevant = [
            m
            for m in members
            if (
                m.get("effective_size_percentile")
                if m.get("effective_size_percentile") is not None
                else m.get("rolling_size_percentile")
            )
            is not None
            and float(
                m.get("effective_size_percentile")
                if m.get("effective_size_percentile") is not None
                else m.get("rolling_size_percentile")
            )
            + 1e-15
            >= min_percentile_for_relevant
        ]
        first_vis_times = [m["first_visible_at"] for m in members if m.get("first_visible_at")]
        first_vis = min(first_vis_times, key=_as_dt) if first_vis_times else None
        age_ms = None
        if first_vis:
            age_ms = int(round((_as_dt(decision_time) - _as_dt(first_vis)).total_seconds() * 1000.0))
        pcts = [
            m.get("effective_size_percentile")
            if m.get("effective_size_percentile") is not None
            else m.get("rolling_size_percentile")
            for m in members
            if (
                m.get("effective_size_percentile")
                if m.get("effective_size_percentile") is not None
                else m.get("rolling_size_percentile")
            )
            is not None
        ]
        combined_pct = max(pcts) if pcts else None
        shares = [m.get("local_depth_share") or 0.0 for m in members]
        out.append(
            {
                "barrier_id": f"ask_cluster_{i:04d}",
                "band_low": min(prices),
                "band_high": max(prices),
                "representative_price": peak["price"],
                "nearest_price": nearest,
                "weighted_centroid_price": centroid,
                "peak_wall_price": peak["price"],
                "peak_wall_qty": peak["qty_base"],
                "total_cluster_qty": total_q,
                "total_cluster_notional": total_n,
                "number_of_price_levels": len(members),
                "number_of_relevant_walls": len(relevant),
                "combined_local_depth_share": sum(shares),
                "combined_past_only_percentile": combined_pct,
                "first_visible_at": first_vis,
                "age_at_decision_ms": age_ms,
                "distance_from_entry_ticks": (nearest - entry) / TICK_SIZE,
                "distance_from_entry_bps": ((nearest / entry) - 1.0) * 10_000.0,
                "distance_from_entry_pct": ((nearest / entry) - 1.0) * 100.0,
                "coverage_ok": True,
                "member_prices": prices,  # audit only; not double-added elsewhere
            }
        )
    return out
