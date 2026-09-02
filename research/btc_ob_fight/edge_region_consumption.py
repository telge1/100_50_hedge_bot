"""Edge-region consumption and refill facts by scope."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .edge_regions import (
    SCOPE_EXACT_LEVEL_TICK,
    SCOPE_PROFILE_EDGE_ZONE,
    SCOPE_TPO_EDGE_BIN,
    SCOPE_VOLUME_EDGE_BIN,
    build_edge_region_catalog,
    distance_to_edges,
)
from .fight_facts import (
    TT_TRADE_ASK_DEC,
    TT_TRADE_BID_DEC,
    TT_UNMATCHED_ASK,
    TT_UNMATCHED_BID,
)
from .profile_edge_state import price_to_tick, tick_to_price
from .profile_price_bin_contract import price_in_interval

NEARBY_LIQUIDITY_INCREASE = "NEARBY_POST_TRADE_LIQUIDITY_INCREASE"


def _coerce_price(raw: Any, *, tick: int = 0) -> float:
    if raw is None:
        return tick_to_price(tick)
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return tick_to_price(tick)


def build_edge_region_consumption(
    wall_bundle: dict[str, Any],
    region_catalog: dict[str, Any],
    edges: dict[str, Any],
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    book_coverage_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate wall transitions into edge scopes."""
    coverage_by_scope = _coverage_lookup(book_coverage_rows or [])
    events: list[dict[str, Any]] = []
    transitions = wall_bundle.get("transitions") or []
    exc_by_ep = {e["source_episode_id"]: e for e in excursions}
    visit_by_ep: dict[str, str] = {}
    for v in visits:
        for eid in v.get("raw_episode_ids") or []:
            visit_by_ep[eid] = v["edge_visit_id"]

    for tr in transitions:
        raw_type = tr.get("transition_type") or ""
        if raw_type == "QTY_INCREASE_OBSERVED":
            continue
        tick = int(tr.get("price_tick") or price_to_tick(_coerce_price(tr.get("price"), tick=0)))
        price = _coerce_price(tr.get("price"), tick=tick)
        scopes = _match_scopes(tick, price, region_catalog)
        if not scopes:
            continue
        event_type = tr.get("transition_type") or ""
        if "TRADE_ASSOCIATED" in event_type:
            et = TT_TRADE_ASK_DEC if tr.get("side") == "ASK" else TT_TRADE_BID_DEC
        elif "UNMATCHED" in event_type:
            et = TT_UNMATCHED_ASK if tr.get("side") == "ASK" else TT_UNMATCHED_BID
        else:
            et = event_type
        cur_ts = tr.get("current_ts")
        ep_id = _episode_at_ts(episodes, cur_ts)
        for sc in scopes:
            dist = distance_to_edges(price, edges, edge_side=sc.get("edge", "UPPER"))
            cov = coverage_by_scope.get((sc["scope"], sc["edge"]), "UNKNOWN")
            events.append(
                {
                    "consumption_event_id": f"erc_{uuid.uuid4().hex[:12]}",
                    "scope": sc["scope"],
                    "edge": sc["edge"],
                    "event_type": et,
                    "side": tr.get("side"),
                    "price": price,
                    "price_tick": tick,
                    "profile_bin_index": sc.get("bin_index"),
                    "distance_ticks_to_inner_edge": dist.get("distance_ticks_to_inner_edge"),
                    "distance_ticks_to_outer_edge": dist.get("distance_ticks_to_outer_edge"),
                    "distance_bps_to_inner_edge": dist.get("distance_bps_to_inner_edge"),
                    "distance_bps_to_outer_edge": dist.get("distance_bps_to_outer_edge"),
                    "visible_qty_reduction": tr.get("qty_reduced"),
                    "matched_trade_volume": tr.get("matching_aggressor_qty"),
                    "matching_status": "TRADE_ASSOCIATED" if "TRADE_ASSOCIATED" in et else "UNMATCHED",
                    "observation_ts": cur_ts,
                    "edge_visit_id": visit_by_ep.get(ep_id) if ep_id else None,
                    "outside_excursion_id": (exc_by_ep.get(ep_id) or {}).get("outside_excursion_id") if ep_id else None,
                    "raw_episode_id": ep_id,
                    "coverage_status": cov,
                }
            )

    summary = _consumption_summary(events)
    return events, summary


def _match_scopes(tick: int, price: float, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for side in ("upper", "lower"):
        for reg in catalog.get(side) or []:
            scope = reg.get("scope")
            if scope == SCOPE_EXACT_LEVEL_TICK:
                if tick == reg.get("price_tick"):
                    hits.append(reg)
            elif reg.get("price_low") is not None and price_in_interval(price, reg["price_low"], reg["price_high"]):
                hits.append(reg)
    return hits


def _episode_at_ts(episodes: list[dict[str, Any]], ts_str: str | None) -> str | None:
    if not ts_str:
        return None
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    for ep in episodes:
        start = datetime.fromisoformat(ep["start_ts"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(ep["end_ts"].replace("Z", "+00:00")) if ep.get("end_ts") else start
        if start <= ts <= end:
            return ep["episode_id"]
    return None


def _coverage_lookup(rows: list[dict[str, Any]]) -> dict[tuple, str]:
    out: dict[tuple, str] = {}
    for r in rows:
        key = (r.get("scope"), r.get("edge"))
        st = r.get("coverage_status")
        if key not in out or st == "FULL_EDGE_REGION_COVERAGE":
            out[key] = st
    return out


def _consumption_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_scope: dict[str, dict[str, int]] = {}
    for e in events:
        sc = e.get("scope") or "UNKNOWN"
        by_scope.setdefault(sc, {"trade_associated": 0, "unmatched": 0, "total": 0})
        by_scope[sc]["total"] += 1
        if e.get("matching_status") == "TRADE_ASSOCIATED":
            by_scope[sc]["trade_associated"] += 1
        else:
            by_scope[sc]["unmatched"] += 1
    return {"by_scope": by_scope, "total_events": len(events)}


def build_exact_refill_events(
    consumption_events: list[dict[str, Any]],
    wall_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    """Same-tick refills only."""
    trade_assoc = [
        e
        for e in consumption_events
        if e.get("matching_status") == "TRADE_ASSOCIATED" and e.get("scope") == SCOPE_EXACT_LEVEL_TICK
    ]
    increases = [
        t
        for t in (wall_bundle.get("transitions") or [])
        if t.get("transition_type") == "QTY_INCREASE_OBSERVED"
    ]
    out: list[dict[str, Any]] = []
    for cons in trade_assoc:
        cons_ts = datetime.fromisoformat(cons["observation_ts"].replace("Z", "+00:00"))
        tick = cons["price_tick"]
        side = cons["side"]
        refills = [
            inc
            for inc in increases
            if inc.get("price_tick") == tick
            and inc.get("side") == side
            and datetime.fromisoformat(inc["current_ts"].replace("Z", "+00:00")) > cons_ts
        ]
        if not refills:
            continue
        ref = min(refills, key=lambda x: x["current_ts"])
        ref_ts = datetime.fromisoformat(ref["current_ts"].replace("Z", "+00:00"))
        prev_qty = float(cons.get("visible_qty_reduction") or 0)
        refilled = float(ref.get("qty_added") or 0)
        recovery = refilled / prev_qty if prev_qty > 0 else None
        out.append(
            {
                "refill_event_id": f"xref_{uuid.uuid4().hex[:12]}",
                "parent_consumption_event_id": cons["consumption_event_id"],
                "event_type": "POST_TRADE_FULL_VISIBLE_QTY_RECOVERY"
                if recovery and recovery >= 0.99
                else "POST_TRADE_PARTIAL_VISIBLE_QTY_RECOVERY"
                if recovery and recovery > 0
                else "POST_TRADE_QTY_INCREASE_OBSERVED",
                "side": side,
                "price_tick": tick,
                "consumption_ts": cons["observation_ts"],
                "refill_ts": ref["current_ts"],
                "seconds_after_consumption": (ref_ts - cons_ts).total_seconds(),
                "refilled_qty": refilled,
                "recovery_fraction": recovery,
                "scope": SCOPE_EXACT_LEVEL_TICK,
            }
        )
    return out


def build_nearby_liquidity_increases(
    consumption_events: list[dict[str, Any]],
    wall_bundle: dict[str, Any],
    region_catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Adjacent-tick liquidity increases — not exact refills."""
    trade_assoc = [e for e in consumption_events if e.get("matching_status") == "TRADE_ASSOCIATED"]
    increases = [
        t
        for t in (wall_bundle.get("transitions") or [])
        if t.get("transition_type") == "QTY_INCREASE_OBSERVED"
    ]
    out: list[dict[str, Any]] = []
    for cons in trade_assoc:
        cons_ts = datetime.fromisoformat(cons["observation_ts"].replace("Z", "+00:00"))
        orig_tick = cons["price_tick"]
        side = cons["side"]
        for inc in increases:
            inc_tick = int(inc.get("price_tick") or 0)
            if inc_tick == orig_tick or inc.get("side") != side:
                continue
            inc_ts = datetime.fromisoformat(inc["current_ts"].replace("Z", "+00:00"))
            if inc_ts <= cons_ts:
                continue
            tick_dist = abs(inc_tick - orig_tick)
            if tick_dist > 10:
                continue
            out.append(
                {
                    "nearby_event_id": f"nli_{uuid.uuid4().hex[:12]}",
                    "parent_consumption_event_id": cons["consumption_event_id"],
                    "edge_visit_id": cons.get("edge_visit_id"),
                    "edge": cons.get("edge"),
                    "side": side if side in ("ASK", "BID") else "UNKNOWN",
                    "side_source": "PARENT_CONSUMPTION_EVENT" if side in ("ASK", "BID") else "SOURCE_LINEAGE_MISSING",
                    "original_price_tick": orig_tick,
                    "new_price_tick": inc_tick,
                    "tick_distance": tick_dist,
                    "qty_increase": inc.get("qty_added"),
                    "delay_seconds": (inc_ts - cons_ts).total_seconds(),
                    "scope": cons.get("scope"),
                    "coverage_status": cons.get("coverage_status"),
                    "canonical_eligible": side in ("ASK", "BID"),
                    "interpretation_status": "NOT_EVALUATED",
                    "fact_type": NEARBY_LIQUIDITY_INCREASE,
                    "price_direction": "UP" if inc_tick > orig_tick else "DOWN",
                    "event_id": f"nli_{uuid.uuid4().hex[:12]}",
                }
            )
    return out
