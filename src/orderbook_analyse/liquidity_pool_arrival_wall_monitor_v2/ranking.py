"""Full-side ranking; pool filter applied only after rank."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import notional


def significance_class(rank: int, percentile: float) -> str:
    if rank <= 5 or percentile >= 0.95:
        return "MAJOR"
    if rank <= 20 or percentile >= 0.80:
        return "MODERATE"
    return "MINOR"


def side_levels_ranked_full(levels: list[tuple[float, float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for price, qty in levels:
        if qty <= 0:
            continue
        rows.append({"price": float(price), "qty": float(qty), "notional": notional(price, qty)})
    rows.sort(key=lambda r: r["notional"], reverse=True)
    n = len(rows)
    notionals = [r["notional"] for r in rows]
    for i, r in enumerate(rows):
        rank = i + 1
        pct = sum(1 for x in notionals if x <= r["notional"]) / n if n else 0.0
        r["full_side_rank"] = rank
        r["full_side_percentile"] = pct
        r["significance_class"] = significance_class(rank, pct)
    return rows


def pool_filter_after_rank(
    ranked_full: list[dict[str, Any]], lower_edge: float, upper_edge: float
) -> list[dict[str, Any]]:
    """Invariant: pool_filter_applied_after_full_side_rank = true."""
    return [r for r in ranked_full if lower_edge <= r["price"] <= upper_edge]


def strongest_inside(
    ranked_full: list[dict[str, Any]], lower_edge: float, upper_edge: float
) -> dict[str, Any] | None:
    inside = pool_filter_after_rank(ranked_full, lower_edge, upper_edge)
    if not inside:
        return None
    return max(inside, key=lambda r: r["notional"])
