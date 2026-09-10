"""Per-transition attacker evolution — no thresholds / classification."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import EPSILON, TICK_SIZE


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_attacker_evolution(chain: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare consecutive chain nodes' attack intensity (raw ratios only)."""
    nodes = chain.get("nodes") or []
    out: list[dict[str, Any]] = []
    for i in range(len(nodes) - 1):
        a = nodes[i]
        b = nodes[i + 1]
        notional_a = float(a.get("cumulative_hits") or 0) * float(a.get("wall_price") or 0)
        notional_b = float(b.get("cumulative_hits") or 0) * float(b.get("wall_price") or 0)
        # duration proxies
        def dur_s(n):
            if not n.get("end_time") or not (n.get("first_trade_touch") or n.get("first_visible_at")):
                return None
            t0 = _as_dt(n.get("first_trade_touch") or n["first_visible_at"])
            t1 = _as_dt(n["end_time"])
            return max((t1 - t0).total_seconds(), EPSILON)

        da, db = dur_s(a), dur_s(b)
        hit_rate_a = (float(a.get("cumulative_hits") or 0) / da) if da else None
        hit_rate_b = (float(b.get("cumulative_hits") or 0) / db) if db else None
        pers_a = _f(a.get("attacker_persistence_end") or a.get("attacker_persistence_start"))
        pers_b = _f(b.get("attacker_persistence_end") or b.get("attacker_persistence_start"))
        imp_a = _f(a.get("impact_efficiency_end") or a.get("impact_efficiency_start"))
        imp_b = _f(b.get("impact_efficiency_end") or b.get("impact_efficiency_start"))
        qdh_a = _f(a.get("qdh_base_end") or a.get("qdh_base_start"))
        qdh_b = _f(b.get("qdh_base_end") or b.get("qdh_base_start"))
        # refill survival proxy: final/peak
        surv_a = (float(a["final_qty"]) / float(a["peak_qty"])) if float(a.get("peak_qty") or 0) > EPSILON else None
        surv_b = (float(b["final_qty"]) / float(b["peak_qty"])) if float(b.get("peak_qty") or 0) > EPSILON else None
        # price progress per million USDT on this hop
        mid_a = _f(a.get("price_at_entry"))
        mid_b = _f(b.get("price_at_entry"))
        ticks = None if mid_a is None or mid_b is None else (mid_b - mid_a) / TICK_SIZE
        musd = (notional_a / 1_000_000.0) if notional_a else None
        prog_per_m = (ticks / musd) if ticks is not None and musd and musd > EPSILON else None

        def ratio(x, y):
            if x is None or y is None or abs(y) <= EPSILON:
                return None
            return x / y

        out.append(
            {
                "from_node": a.get("chain_node_index"),
                "to_node": b.get("chain_node_index"),
                "from_price": a.get("wall_price"),
                "to_price": b.get("wall_price"),
                "aggressive_buy_notional_old": notional_a,
                "aggressive_buy_notional_new": notional_b,
                "hit_rate_old": hit_rate_a,
                "hit_rate_new": hit_rate_b,
                "hit_rate_ratio_new_over_old": ratio(hit_rate_b, hit_rate_a),
                "trade_pacing_old": hit_rate_a,
                "trade_pacing_new": hit_rate_b,
                "persistence_old": pers_a,
                "persistence_new": pers_b,
                "persistence_ratio_new_over_old": ratio(pers_b, pers_a),
                "impact_efficiency_old": imp_a,
                "impact_efficiency_new": imp_b,
                "impact_efficiency_ratio_new_over_old": ratio(imp_b, imp_a),
                "price_progress_ticks_per_million_usdt": prog_per_m,
                "qdh_old": qdh_a,
                "qdh_new": qdh_b,
                "qdh_ratio_new_over_old": ratio(qdh_b, qdh_a),
                "refill_survival_old": surv_a,
                "refill_survival_new": surv_b,
                "note": "Raw ratios only — no accelerate/stable/weaken classification",
            }
        )
    return out
