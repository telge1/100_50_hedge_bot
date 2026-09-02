"""Structural edge visits over raw profile-state episodes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .config import iso_z
from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    price_to_tick,
)
from .profile_state_episodes import END_REASON_WINDOW_END, episode_time_span

EDGE_VISIT_CONTRACT = "edge_visit_contract_v1"
INTERPRETATION_NOT_EVALUATED = "NOT_EVALUATED"

VISIT_STATES_UPPER = {STATE_BETWEEN_UPPER, STATE_OUTSIDE_ABOVE}
VISIT_STATES_LOWER = {STATE_BETWEEN_LOWER, STATE_OUTSIDE_BELOW}


def build_edge_visits(
    episodes: list[dict[str, Any]],
    *,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build edge visits from chronological raw episodes (episodes unchanged)."""
    visits: list[dict[str, Any]] = []
    i = 0
    n = len(episodes)
    while i < n:
        ep = episodes[i]
        edge = _visit_edge_for_episode(ep)
        if edge is None:
            i += 1
            continue
        if not _is_visit_start(episodes, i):
            i += 1
            continue
        visit_states = VISIT_STATES_UPPER if edge == "UPPER" else VISIT_STATES_LOWER
        visit_eps: list[dict[str, Any]] = []
        j = i
        while j < n:
            cur = episodes[j]
            if cur["state"] == STATE_INSIDE_BOTH:
                break
            if _visit_edge_for_episode(cur) != edge:
                break
            if cur["state"] not in visit_states:
                break
            visit_eps.append(cur)
            j += 1
        visit = _assemble_visit(edge, visit_eps)
        visits.append(visit)
        i = j if j > i else i + 1
    return visits


def _visit_edge_for_episode(ep: dict[str, Any]) -> str | None:
    st = ep.get("state")
    if st in VISIT_STATES_UPPER:
        return "UPPER"
    if st in VISIT_STATES_LOWER:
        return "LOWER"
    return None


def _is_visit_start(episodes: list[dict[str, Any]], idx: int) -> bool:
    ep = episodes[idx]
    edge = _visit_edge_for_episode(ep)
    if edge is None:
        return False
    if idx == 0:
        return True
    prev = episodes[idx - 1]
    if prev["state"] == STATE_INSIDE_BOTH:
        return True
    prev_edge = _visit_edge_for_episode(prev)
    return prev_edge != edge


def _assemble_visit(edge: str, visit_eps: list[dict[str, Any]]) -> dict[str, Any]:
    first, last = visit_eps[0], visit_eps[-1]
    start_dt, _ = episode_time_span(first)
    _, end_dt = episode_time_span(last)
    if last.get("end_reason") == END_REASON_WINDOW_END:
        closed = False
        end_reason = END_REASON_WINDOW_END
    else:
        closed = True
        end_reason = "RETURNED_INSIDE_BOTH"

    outside_states = {STATE_OUTSIDE_ABOVE} if edge == "UPPER" else {STATE_OUTSIDE_BELOW}
    outside_count = sum(1 for e in visit_eps if e["state"] in outside_states)
    between_count = len(visit_eps) - outside_count

    min_p = min(e["min_price"] for e in visit_eps)
    max_p = max(e["max_price"] for e in visit_eps)
    inner = first.get("upper_outer_edge") if edge == "UPPER" else first.get("lower_outer_edge")
    outer = first.get("upper_outer_edge") if edge == "UPPER" else first.get("lower_outer_edge")
    inner_edge = first.get("upper_inner_edge") if edge == "UPPER" else first.get("lower_inner_edge")
    outer_edge = first.get("upper_outer_edge") if edge == "UPPER" else first.get("lower_outer_edge")

    max_dist_inner = None
    max_dist_outer = None
    for e in visit_eps:
        for p in (e.get("min_price"), e.get("max_price"), e.get("start_price"), e.get("end_price")):
            if p is None or inner_edge is None or outer_edge is None:
                continue
            di = abs(price_to_tick(p) - price_to_tick(inner_edge))
            do = abs(price_to_tick(p) - price_to_tick(outer_edge))
            max_dist_inner = di if max_dist_inner is None else max(max_dist_inner, di)
            max_dist_outer = do if max_dist_outer is None else max(max_dist_outer, do)

    return {
        "edge_visit_id": f"ev_{uuid.uuid4().hex[:12]}",
        "contract_version": EDGE_VISIT_CONTRACT,
        "edge": edge,
        "start_ts": first["start_ts"],
        "end_ts": last["end_ts"],
        "duration_seconds": (end_dt - start_dt).total_seconds(),
        "closed": closed,
        "end_reason": end_reason,
        "raw_episode_ids": [e["episode_id"] for e in visit_eps],
        "raw_episode_count": len(visit_eps),
        "outside_excursion_count": outside_count,
        "between_zone_episode_count": between_count,
        "reclaim_count": 0,
        "repeat_outside_crossing_count": max(0, outside_count - 1) if outside_count else 0,
        "start_price": first.get("start_price"),
        "end_price": last.get("end_price"),
        "min_price": min_p,
        "max_price": max_p,
        "max_distance_ticks_to_inner_edge": max_dist_inner,
        "max_distance_ticks_to_outer_edge": max_dist_outer,
        "base_volume": sum(e.get("base_volume") or 0 for e in visit_eps),
        "quote_notional": sum(e.get("quote_notional") or 0 for e in visit_eps),
        "taker_buy_quote": sum(e.get("taker_buy_quote") or 0 for e in visit_eps),
        "taker_sell_quote": sum(e.get("taker_sell_quote") or 0 for e in visit_eps),
        "taker_delta_quote": sum(e.get("taker_delta_quote") or 0 for e in visit_eps),
        "oi_coverage_status": "NOT_COMPUTED_AT_VISIT_LEVEL",
        "liquidation_coverage_status": "NOT_COMPUTED_AT_VISIT_LEVEL",
        "interpretation_status": INTERPRETATION_NOT_EVALUATED,
    }


def assign_episode_to_visit(
    episode_id: str,
    visits: list[dict[str, Any]],
) -> str | None:
    for v in visits:
        if episode_id in v.get("raw_episode_ids", []):
            return v["edge_visit_id"]
    return None
