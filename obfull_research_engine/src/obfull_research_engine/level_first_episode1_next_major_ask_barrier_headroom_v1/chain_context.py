"""Defense-chain context for a barrier — liquidity to overcome, not order identity."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import EPSILON, ORIGINAL_WALL_PRICE, TICK_SIZE


def chain_context_for_barrier(
    *,
    barrier_price: float,
    chain: dict[str, Any] | None,
    decision_time: str,
    wall_first_touch: str | None,
) -> dict[str, Any]:
    nodes = list((chain or {}).get("nodes") or [])
    bp = float(barrier_price)
    dt = _as_dt(decision_time)
    in_chain = False
    node_index = None
    matched = None
    for n in nodes:
        px = float(n.get("wall_price") or n.get("price") or 0.0)
        if abs(px - bp) <= TICK_SIZE + 1e-12:  # within 1 tick = same node band
            in_chain = True
            node_index = n.get("chain_node_index")
            matched = n
            break

    pre_at_wall_touch = None
    if wall_first_touch and matched:
        fv = matched.get("first_visible_at")
        if fv:
            pre_at_wall_touch = _as_dt(fv) <= _as_dt(wall_first_touch)

    attacked_later = None
    if matched and matched.get("first_trade_touch"):
        # OUTCOME-ish flag stored separately by caller; here only whether touch <= decision
        attacked_later = _as_dt(matched["first_trade_touch"]) <= dt

    nodes_before = [
        n
        for n in nodes
        if float(n.get("wall_price") or 0.0) < bp - 1e-12
        and float(n.get("wall_price") or 0.0) > float(ORIGINAL_WALL_PRICE) - 1e-12
    ]
    cum_qty = sum(float(n.get("initial_qty") or 0.0) for n in nodes_before)
    cum_hits = sum(float(n.get("cumulative_hits") or 0.0) for n in nodes_before)
    advance = None
    if nodes_before:
        advance = (bp - float(ORIGINAL_WALL_PRICE)) / TICK_SIZE

    centroid = (chain or {}).get("ask_liquidity_centroid")
    centroid_rel = None
    if centroid is not None:
        try:
            centroid_rel = (float(centroid) - bp) / TICK_SIZE
        except (TypeError, ValueError):
            centroid_rel = None

    return {
        "barrier_price": bp,
        "in_defense_chain": in_chain,
        "chain_node_index": node_index,
        "pre_existing_at_wall_first_touch": pre_at_wall_touch,
        "attacked_by_decision_time": attacked_later,
        "relevant_chain_nodes_before": len(nodes_before),
        "cumulative_wall_mass_before": cum_qty,
        "cumulative_aggressive_buy_volume_before": cum_hits,
        "prior_chain_advance_ticks_to_barrier": advance,
        "ask_liquidity_centroid_rel_ticks": centroid_rel,
        "note": (
            "Counts relevant ask liquidity the price must overcome — "
            "not proof that the same order migrated."
        ),
        "epsilon": EPSILON,
    }
