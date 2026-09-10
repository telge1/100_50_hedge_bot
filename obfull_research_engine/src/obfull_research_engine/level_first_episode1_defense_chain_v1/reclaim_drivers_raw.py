"""Raw reclaim-driver components — no classification, no multi-counting as votes.

For Episode 1 within Epoch-4 coverage there is no reclaim; drivers must not be asserted.
"""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import ASK_WALL_BREACH, EPSILON


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_reclaim_drivers_raw(
    *,
    wall_flow_rows: list[dict[str, Any]],
    coverage_start: str,
    coverage_end: str,
    breach_iso: str = ASK_WALL_BREACH,
) -> dict[str, Any]:
    """Compute A/B/C raw series within coverage; reclaim_claimed=False for Episode 1."""
    rows = [
        r
        for r in wall_flow_rows
        if _as_dt(coverage_start)
        <= _as_dt(r.get("feature_available_at") or r.get("timestamp"))
        <= _as_dt(coverage_end)
    ]
    if not rows:
        return {
            "reclaim_observed_in_coverage": False,
            "reclaim_claimed": False,
            "buyer_exhaustion": {},
            "active_seller_pressure": {},
            "bid_liquidity_change": {},
            "note": "no wall-flow rows in coverage",
        }

    def series(key):
        return [_f(r.get(key)) for r in rows]

    hit_rate = series("hit_rate")
    pers = series("persistence_ratio")
    impact = series("impact_efficiency")
    # A: buyer exhaustion proxies (ask-wall attack = buy hits)
    def first_last(xs):
        xs2 = [x for x in xs if x is not None]
        if len(xs2) < 2:
            return None, None, None
        return xs2[0], xs2[-1], xs2[-1] - xs2[0]

    hr0, hr1, d_hr = first_last(hit_rate)
    p0, p1, d_p = first_last(pers)
    i0, i1, d_i = first_last(impact)

    # B/C: without separate sell-vs-bid attribution in wall-flow ask package, leave null-honest
    return {
        "reclaim_observed_in_coverage": False,
        "reclaim_claimed": False,
        "buyer_exhaustion": {
            "buy_hit_rate_start": hr0,
            "buy_hit_rate_end": hr1,
            "buy_hit_rate_delta": d_hr,
            "buy_persistence_start": p0,
            "buy_persistence_end": p1,
            "buy_persistence_delta": d_p,
            "impact_efficiency_start": i0,
            "impact_efficiency_end": i1,
            "impact_efficiency_delta": d_i,
            "note": "Descriptive deltas only; not labeled as exhaustion event",
        },
        "active_seller_pressure": {
            "aggressive_sell_notional": None,
            "sell_hit_rate_vs_bids": None,
            "sell_persistence": None,
            "negative_price_impact": None,
            "note": "Not attributed in ask-wall-flow view for Episode 1 — left null, not invented",
        },
        "bid_liquidity_change": {
            "bid_pulls": None,
            "bid_depth_loss": None,
            "spread_widening": None,
            "downside_vacuum_score": None,
            "note": "Requires separate bid-side mass balance — not double-counted from ask features",
        },
        "counting_rule": "A/B/C are separate feature groups; not independent voting voices",
        "episode1_statement": (
            "No reclaim within Epoch-4 continuous coverage; reclaim drivers are not asserted."
        ),
    }
