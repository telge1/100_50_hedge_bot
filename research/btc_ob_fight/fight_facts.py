"""Phase 2A causal fight fact engine — structured facts only, NOT_EVALUATED interpretation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .aggression_facts import aggression_for_episode, build_aggression_buckets
from .config import iso_z, utc
from .facts import json_safe
from .level_registry import build_level_registry
from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    build_frozen_profile_edges,
    price_to_tick,
)
from .profile_state_episodes import (
    build_profile_state_episodes,
    episode_time_span,
    episodes_by_state,
)
from .outside_reclaim import build_canonical_reclaim_pipeline, RECLAIM_EVENT_CONTRACT_V3
from .same_timestamp_audit import build_same_timestamp_ordering_audit, ambiguous_timestamp_set
from .wall_events import tick_to_price

FIGHT_FACT_CONTRACT = "fight_fact_contract_v1"
SCHEMA_FIGHT_V2 = "btc_ob_fight_facts_v2_0"
SCHEMA_FIGHT_V22 = "btc_ob_fight_facts_v2_2"
INTERPRETATION_NOT_EVALUATED = "NOT_EVALUATED"

# Factual OB event type mapping (spec section 5)
TT_TRADE_ASK_DEC = "TRADE_ASSOCIATED_ASK_QTY_DECREASE"
TT_TRADE_BID_DEC = "TRADE_ASSOCIATED_BID_QTY_DECREASE"
TT_UNMATCHED_ASK = "UNMATCHED_ASK_QTY_DECREASE"
TT_UNMATCHED_BID = "UNMATCHED_BID_QTY_DECREASE"
TT_ASK_DISAP = "ASK_DISAPPEARANCE_OBSERVED"
TT_BID_DISAP = "BID_DISAPPEARANCE_OBSERVED"


def build_fight_facts(
    *,
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    trades: list[dict[str, Any]],
    wall_bundle: dict[str, Any],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
    anchor: datetime,
    window_end: datetime,
    reference_price: float | None = None,
) -> dict[str, Any]:
    """Build full Phase-2A fight fact bundle (causal profiles frozen at anchor)."""
    anchor = utc(anchor)
    window_end = utc(window_end)
    anchor_iso = iso_z(anchor)

    edges = build_frozen_profile_edges(
        tpo_profile,
        volume_profile,
        anchor_cutoff_utc=anchor_iso,
    )
    episode_bundle = build_profile_state_episodes(
        trades, edges, anchor=anchor, window_end=window_end
    )
    episodes = episode_bundle.get("episodes") or []
    transitions = episode_bundle.get("transitions") or []

    obs_trades = sorted(
        [t for t in trades if anchor <= t["ts"] < window_end],
        key=lambda t: (t["ts"], t["trade_id"]),
    )

    aggression_buckets = build_aggression_buckets(trades, anchor, window_end)
    episode_aggression = [aggression_for_episode(ep, obs_trades) for ep in episodes]

    edge_ticks = _edge_price_ticks(edges)
    consumption_events = _build_edge_consumption_events(
        wall_bundle, edges, edge_ticks, episodes, obs_trades
    )
    refill_events = _build_post_trade_refill_events(consumption_events, wall_bundle, episodes)
    outside_facts = _build_outside_episode_facts(episodes, obs_trades, edges)

    st_audit, st_rows = build_same_timestamp_ordering_audit(
        trades, edges, anchor=anchor, window_end=window_end
    )
    ambiguous_ts = ambiguous_timestamp_set(st_rows)
    canonical_bundle = build_canonical_reclaim_pipeline(
        episodes, transitions, obs_trades, edges, ambiguous_timestamps=ambiguous_ts
    )
    reclaim_events = canonical_bundle["reclaim_events"]
    edge_visits = canonical_bundle["visits"]
    outside_excursions = canonical_bundle["outside_excursions"]

    retest_events = _build_retest_proximity(reclaim_events, obs_trades, edges)
    oi_liq_context = _episode_oi_liquidation_context(episodes, oi_rows, liq_rows)
    level_registry = build_level_registry(tpo_profile, volume_profile, reference_price=reference_price)

    fight_episodes = _assemble_fight_episodes(
        episodes,
        episode_aggression,
        consumption_events,
        refill_events,
        outside_facts,
        reclaim_events,
        retest_events,
        oi_liq_context,
        level_registry,
        edges,
    )

    return json_safe(
        {
            "schema_version": SCHEMA_FIGHT_V22,
            "fight_fact_contract": FIGHT_FACT_CONTRACT,
            "canonical_reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
            "ambiguous_reclaim_output": "ambiguous_reclaim_candidates.csv",
            "raw_outside_output": "raw_outside_excursions.csv",
            "canonical_reclaims_only_in_primary_output": True,
            "ambiguous_events_decision_eligible": False,
            "exchange_order_proven": False,
            "interpretation_status": INTERPRETATION_NOT_EVALUATED,
            "rules_frozen": False,
            "trade_verdict_evaluated": False,
            "direction": None,
            "legacy_global_first_reclaim_enabled": False,
            "same_timestamp_ordering_audit": st_audit,
            "same_timestamp_multistate_groups": st_rows,
            "frozen_profile_edges": edges,
            "profile_state_transitions": transitions,
            "profile_state_episodes": episodes,
            "aggression_buckets": aggression_buckets,
            "episode_aggression": episode_aggression,
            "edge_consumption_events": consumption_events,
            "post_trade_refill_events": refill_events,
            "outside_profile_episodes": outside_facts,
            "reclaim_events": reclaim_events,
            "ambiguous_reclaim_candidates": canonical_bundle["ambiguous_reclaim_candidates"],
            "outside_excursions": outside_excursions,
            "raw_outside_excursions": canonical_bundle["raw_outside_excursions"],
            "canonical_outside_excursions": canonical_bundle["canonical_outside_excursions"],
            "ambiguous_same_timestamp_excursions": canonical_bundle["ambiguous_same_timestamp_excursions"],
            "canonical_eligibility_summary": canonical_bundle["canonical_eligibility_summary"],
            "edge_visits": edge_visits,
            "retest_proximity_events": retest_events,
            "episode_oi_liquidation_context": oi_liq_context,
            "level_registry": level_registry,
            "fight_episodes": fight_episodes,
            "fight_episode_summary": [_fight_episode_summary(fe) for fe in fight_episodes],
            "manifest": {
                "profile_state_episode_count": len(episodes),
                "outside_episode_count": len(outside_facts),
                "edge_consumption_count": len(consumption_events),
                "post_trade_refill_count": len(refill_events),
                "reclaim_count": len(reclaim_events),
                "ambiguous_reclaim_candidate_count": len(canonical_bundle["ambiguous_reclaim_candidates"]),
                "raw_outside_count": len(canonical_bundle["raw_outside_excursions"]),
                "reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
                "edge_visit_count": len(edge_visits),
                "outside_excursion_count": len(outside_excursions),
                "canonical_outside_excursion_count": len(canonical_bundle["canonical_outside_excursions"]),
                "ambiguous_outside_excursion_count": len(canonical_bundle["ambiguous_same_timestamp_excursions"]),
                "retest_proximity_count": len(retest_events),
            },
        }
    )


def _edge_price_ticks(edges: dict[str, Any]) -> dict[str, set[int]]:
    if edges.get("profile_state") != "VALID":
        return {"upper": set(), "lower": set()}
    upper = {
        int(edges["upper_inner_edge_tick"]),
        int(edges["upper_outer_edge_tick"]),
    }
    lower = {
        int(edges["lower_inner_edge_tick"]),
        int(edges["lower_outer_edge_tick"]),
    }
    return {"upper": upper, "lower": lower}


def _map_transition_type(raw: str, side: str) -> str:
    if raw == "TRADE_ASSOCIATED_QTY_DECREASE":
        return TT_TRADE_ASK_DEC if side == "ASK" else TT_TRADE_BID_DEC
    if raw == "UNMATCHED_QTY_DECREASE":
        return TT_UNMATCHED_ASK if side == "ASK" else TT_UNMATCHED_BID
    if raw in ("TRADE_ASSOCIATED_DISAPPEARANCE", "UNMATCHED_DISAPPEARANCE"):
        return TT_ASK_DISAP if side == "ASK" else TT_BID_DISAP
    return raw


def _assign_episode_id(episodes: list[dict[str, Any]], ts: datetime) -> str | None:
    for ep in episodes:
        start, end = episode_time_span(ep)
        if start <= ts <= end:
            return ep["episode_id"]
    return None


def _build_edge_consumption_events(
    wall_bundle: dict[str, Any],
    edges: dict[str, Any],
    edge_ticks: dict[str, set[int]],
    episodes: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if edges.get("profile_state") != "VALID":
        return []
    all_edge_ticks = edge_ticks["upper"] | edge_ticks["lower"]
    out: list[dict[str, Any]] = []
    transitions = wall_bundle.get("transitions") or []
    trade_matches = wall_bundle.get("trade_matches") or []
    matches_by_key: dict[tuple, list] = {}
    for m in trade_matches:
        key = (m.get("track_id"), m.get("previous_ts"), m.get("current_ts"))
        matches_by_key.setdefault(key, []).append(m)

    for tr in transitions:
        tick = int(tr.get("price_tick") or price_to_tick(tr.get("price", 0)))
        if tick not in all_edge_ticks:
            continue
        edge_side = "UPPER" if tick in edge_ticks["upper"] else "LOWER"
        raw_type = tr.get("transition_type") or ""
        if raw_type == "QTY_INCREASE_OBSERVED":
            continue
        event_type = _map_transition_type(raw_type, tr.get("side", "ASK"))
        cur_ts = datetime.fromisoformat(tr["current_ts"].replace("Z", "+00:00"))
        dist_ticks = _distance_to_nearest_edge_tick(tick, edge_ticks)
        key = (tr.get("track_id"), tr.get("previous_ts"), tr.get("current_ts"))
        matched = matches_by_key.get(key, [])
        out.append(
            {
                "consumption_event_id": f"cons_{uuid.uuid4().hex[:12]}",
                "event_type": event_type,
                "side": tr.get("side"),
                "price_tick": tick,
                "price": tr.get("price") or tick_to_price(tick),
                "profile_edge": edge_side,
                "distance_ticks_to_relevant_edge": dist_ticks,
                "previous_visible_qty": tr.get("previous_qty"),
                "new_visible_qty": tr.get("current_qty"),
                "visible_qty_reduction": tr.get("qty_reduced"),
                "matched_trade_volume": tr.get("matching_aggressor_qty"),
                "matched_trade_count": tr.get("trades_at_level_between_samples"),
                "unmatched_qty_reduction": tr.get("qty_reduced")
                if "UNMATCHED" in event_type
                else 0.0,
                "full_disappearance": tr.get("current_qty", 1) == 0,
                "first_observation_ts": tr.get("previous_ts"),
                "last_observation_ts": tr.get("current_ts"),
                "episode_id": _assign_episode_id(episodes, cur_ts),
                "matching_status": "TRADE_ASSOCIATED" if "TRADE_ASSOCIATED" in event_type else "UNMATCHED",
                "matched_trades": [
                    {
                        "trade_id": m["trade_id"],
                        "trade_ts": m.get("trade_ts"),
                        "size": m.get("size"),
                        "notional": m.get("notional"),
                    }
                    for m in matched
                ],
            }
        )
    out.sort(key=lambda x: (x.get("last_observation_ts") or "", x.get("price_tick") or 0))
    return out


def _distance_to_nearest_edge_tick(tick: int, edge_ticks: dict[str, set[int]]) -> int:
    all_ticks = edge_ticks["upper"] | edge_ticks["lower"]
    if tick in all_ticks:
        return 0
    return min(abs(tick - t) for t in all_ticks) if all_ticks else 0


def _build_post_trade_refill_events(
    consumption_events: list[dict[str, Any]],
    wall_bundle: dict[str, Any],
    episodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trade_associated = [
        e
        for e in consumption_events
        if e.get("event_type") in (TT_TRADE_ASK_DEC, TT_TRADE_BID_DEC)
    ]
    increases = [
        t
        for t in (wall_bundle.get("transitions") or [])
        if t.get("transition_type") == "QTY_INCREASE_OBSERVED"
    ]
    out: list[dict[str, Any]] = []
    for cons in trade_associated:
        cons_ts = datetime.fromisoformat(cons["last_observation_ts"].replace("Z", "+00:00"))
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
            out.append(
                {
                    "refill_event_id": f"ref_{uuid.uuid4().hex[:12]}",
                    "parent_consumption_event_id": cons["consumption_event_id"],
                    "event_type": "NO_POST_TRADE_REFILL_OBSERVED_IN_WINDOW",
                    "side": side,
                    "price_tick": tick,
                    "consumption_ts": cons["last_observation_ts"],
                    "refill_ts": None,
                    "seconds_after_consumption": None,
                    "qty_before_consumption": cons.get("previous_visible_qty"),
                    "qty_after_consumption": cons.get("new_visible_qty"),
                    "later_visible_qty": None,
                    "refilled_qty": None,
                    "recovery_fraction": None,
                    "profile_state_episode_id": cons.get("episode_id"),
                }
            )
            continue
        ref = min(refills, key=lambda x: x["current_ts"])
        ref_ts = datetime.fromisoformat(ref["current_ts"].replace("Z", "+00:00"))
        prev_qty = float(cons.get("previous_visible_qty") or 0)
        refilled = float(ref.get("qty_added") or 0)
        recovery = refilled / prev_qty if prev_qty > 0 else None
        if recovery is not None and recovery >= 0.99:
            etype = "POST_TRADE_FULL_VISIBLE_QTY_RECOVERY"
        elif recovery is not None and recovery > 0:
            etype = "POST_TRADE_PARTIAL_VISIBLE_QTY_RECOVERY"
        else:
            etype = "POST_TRADE_QTY_INCREASE_OBSERVED"
        out.append(
            {
                "refill_event_id": f"ref_{uuid.uuid4().hex[:12]}",
                "parent_consumption_event_id": cons["consumption_event_id"],
                "event_type": etype,
                "side": side,
                "price_tick": tick,
                "consumption_ts": cons["last_observation_ts"],
                "refill_ts": ref["current_ts"],
                "seconds_after_consumption": (ref_ts - cons_ts).total_seconds(),
                "qty_before_consumption": prev_qty,
                "qty_after_consumption": cons.get("new_visible_qty"),
                "later_visible_qty": ref.get("current_qty"),
                "refilled_qty": refilled,
                "recovery_fraction": recovery,
                "additional_trades_at_level": ref.get("trades_at_level_between_samples"),
                "profile_state_episode_id": cons.get("episode_id"),
            }
        )
    return out


def _build_outside_episode_facts(
    episodes: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
) -> list[dict[str, Any]]:
    outer_upper = edges.get("upper_outer_edge")
    outer_lower = edges.get("lower_outer_edge")
    out: list[dict[str, Any]] = []
    for ep in episodes_by_state(episodes, STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW):
        start, end = episode_time_span(ep)
        chunk = [t for t in trades if start <= t["ts"] <= end]
        ref_edge = outer_upper if ep["state"] == STATE_OUTSIDE_ABOVE else outer_lower
        dists = []
        if ref_edge:
            for t in chunk:
                dists.append(abs(t["price"] - ref_edge) / t["price"] * 10000.0)
        returned = _detect_return_to_edge(ep, episodes, edges)
        out.append(
            {
                "episode_id": ep["episode_id"],
                "state": ep["state"],
                "duration_seconds": ep.get("duration_seconds"),
                "trade_count": ep.get("trade_count"),
                "base_volume": ep.get("base_volume"),
                "quote_notional": ep.get("quote_notional"),
                "taker_buy_quote": ep.get("taker_buy_quote"),
                "taker_sell_quote": ep.get("taker_sell_quote"),
                "taker_delta_quote": ep.get("taker_delta_quote"),
                "mean_distance_bps_from_outer_edge": sum(dists) / len(dists) if dists else None,
                "max_distance_bps_from_outer_edge": max(dists) if dists else ep.get("max_distance_bps_from_relevant_edge"),
                "close_distance_bps_from_outer_edge": dists[-1] if dists else None,
                "returned_to_edge": returned.get("returned"),
                "return_ts": returned.get("return_ts"),
                "outer_edge_price": ref_edge,
            }
        )
    return out


def _detect_return_to_edge(
    outside_ep: dict[str, Any],
    all_episodes: list[dict[str, Any]],
    edges: dict[str, Any],
) -> dict[str, Any]:
    idx = outside_ep.get("episode_index", -1)
    for ep in all_episodes:
        if ep.get("episode_index", -1) <= idx:
            continue
        if outside_ep["state"] == STATE_OUTSIDE_ABOVE:
            if ep["state"] in (STATE_BETWEEN_UPPER, STATE_INSIDE_BOTH, STATE_BETWEEN_LOWER, STATE_OUTSIDE_BELOW):
                if ep["state"] != STATE_OUTSIDE_ABOVE:
                    return {"returned": True, "return_ts": ep.get("start_ts")}
        if outside_ep["state"] == STATE_OUTSIDE_BELOW:
            if ep["state"] in (STATE_BETWEEN_LOWER, STATE_INSIDE_BOTH, STATE_BETWEEN_UPPER, STATE_OUTSIDE_ABOVE):
                if ep["state"] != STATE_OUTSIDE_BELOW:
                    return {"returned": True, "return_ts": ep.get("start_ts")}
    return {"returned": False, "return_ts": None}


def _build_retest_proximity(
    reclaim_events: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
) -> list[dict[str, Any]]:
    if edges.get("profile_state") != "VALID":
        return []
    out: list[dict[str, Any]] = []
    for rcl in reclaim_events:
        cross_ts = datetime.fromisoformat(rcl["cross_ts"].replace("Z", "+00:00"))
        edge_side = rcl.get("edge")
        if edge_side == "UPPER" or "UPPER" in str(rcl.get("event_type", "")):
            edge = edges.get("upper_outer_edge")
        else:
            edge = edges.get("lower_outer_edge")
        if edge is None:
            continue
        edge_tick = price_to_tick(edge)
        post = [t for t in trades if t["ts"] > cross_ts]
        if not post:
            continue
        min_abs_dist = None
        min_tick_dist = None
        min_bps = None
        min_ts = None
        min_price = None
        for t in post:
            tick = price_to_tick(t["price"])
            td = abs(tick - edge_tick)
            bps = abs(t["price"] - edge) / t["price"] * 10000.0
            if min_tick_dist is None or td < min_tick_dist:
                min_tick_dist = td
                min_abs_dist = abs(t["price"] - edge)
                min_bps = bps
                min_ts = iso_z(t["ts"])
                min_price = t["price"]
        out.append(
            {
                "retest_proximity_id": f"rt_{uuid.uuid4().hex[:12]}",
                "reclaim_event_id": rcl.get("reclaim_event_id"),
                "edge_price": edge,
                "edge_tick": edge_tick,
                "min_absolute_distance": min_abs_dist,
                "min_distance_ticks": min_tick_dist,
                "min_distance_bps": min_bps,
                "nearest_approach_ts": min_ts,
                "nearest_approach_price": min_price,
                "retest_candidate_status": "UNFROZEN_HEURISTIC",
                "interpretation_status": INTERPRETATION_NOT_EVALUATED,
            }
        )
    return out


def _episode_oi_liquidation_context(
    episodes: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ep in episodes:
        start, end = episode_time_span(ep)
        oi_slice = [r for r in oi_rows if start <= r["ts"] <= end]
        liq_slice = [r for r in liq_rows if start <= r["ts"] <= end]
        if not oi_slice and not liq_slice:
            ctx_status = "DATA_INSUFFICIENT"
        else:
            ctx_status = "COMPUTED"
        oi_first = oi_slice[0]["oi"] if oi_slice else None
        oi_last = oi_slice[-1]["oi"] if oi_slice else None
        oi_delta = (oi_last - oi_first) if oi_first is not None and oi_last is not None else None
        oi_delta_pct = (oi_delta / oi_first * 100.0) if oi_first and oi_delta is not None else None
        long_liq = [r for r in liq_slice if "LONG" in str(r.get("side", "")).upper() or str(r.get("side", "")).upper().startswith("BUY")]
        short_liq = [r for r in liq_slice if "SHORT" in str(r.get("side", "")).upper() or str(r.get("side", "")).upper().startswith("SELL")]
        largest = max(liq_slice, key=lambda r: r.get("notional", 0), default=None)
        out.append(
            {
                "episode_id": ep["episode_id"],
                "profile_state": ep.get("state"),
                "context_status": ctx_status,
                "oi_start": oi_first,
                "oi_end": oi_last,
                "oi_delta": oi_delta,
                "oi_delta_pct": oi_delta_pct,
                "oi_sample_count": len(oi_slice),
                "long_liquidation_count": len(long_liq),
                "short_liquidation_count": len(short_liq),
                "long_liquidation_notional": sum(r.get("notional", 0) for r in long_liq) or None,
                "short_liquidation_notional": sum(r.get("notional", 0) for r in short_liq) or None,
                "largest_liquidation_notional": largest.get("notional") if largest else None,
                "largest_liquidation_side": largest.get("side") if largest else None,
                "largest_liquidation_ts": iso_z(largest["ts"]) if largest else None,
            }
        )
    return out


def _assemble_fight_episodes(
    episodes: list[dict[str, Any]],
    episode_aggression: list[dict[str, Any]],
    consumption: list[dict[str, Any]],
    refills: list[dict[str, Any]],
    outside: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
    retests: list[dict[str, Any]],
    oi_ctx: list[dict[str, Any]],
    level_registry: dict[str, Any],
    edges: dict[str, Any],
) -> list[dict[str, Any]]:
    agg_by_ep = {a["episode_id"]: a for a in episode_aggression if a.get("episode_id")}
    cons_by_ep: dict[str, list] = {}
    for c in consumption:
        cons_by_ep.setdefault(c.get("episode_id") or "", []).append(c)
    refill_by_ep: dict[str, list] = {}
    for r in refills:
        refill_by_ep.setdefault(r.get("profile_state_episode_id") or "", []).append(r)
    outside_by_ep = {o["episode_id"]: o for o in outside}
    oi_by_ep = {o["episode_id"]: o for o in oi_ctx}

    edge_relevant_states = {
        STATE_BETWEEN_UPPER,
        STATE_BETWEEN_LOWER,
        STATE_OUTSIDE_ABOVE,
        STATE_OUTSIDE_BELOW,
    }
    fight_eps: list[dict[str, Any]] = []
    for ep in episodes:
        if ep.get("state") not in edge_relevant_states:
            continue
        edge = "UPPER" if ep.get("direction") == "UPPER" or ep.get("state") in (STATE_BETWEEN_UPPER, STATE_OUTSIDE_ABOVE) else "LOWER"
        eid = ep["episode_id"]
        fight_eps.append(
            {
                "fight_episode_id": f"fight_{eid}",
                "edge": edge,
                "profile_state": ep.get("state"),
                "profile_state_path": [ep.get("state")],
                "source_episode_id": eid,
                "aggression_facts": agg_by_ep.get(eid, {}),
                "price_response_facts": {
                    "price_change_bps": ep.get("price_change_bps"),
                    "max_distance_bps_from_relevant_edge": ep.get("max_distance_bps_from_relevant_edge"),
                    "duration_seconds": ep.get("duration_seconds"),
                },
                "trade_backed_consumption_facts": cons_by_ep.get(eid, []),
                "post_trade_refill_facts": refill_by_ep.get(eid, []),
                "outside_facts": outside_by_ep.get(eid),
                "reclaim_facts": [r for r in reclaims if r.get("source_outside_episode_id") == eid],
                "retest_proximity_facts": retests,
                "oi_liquidation_context": oi_by_ep.get(eid, {}),
                "next_level_context": level_registry.get("next_eligible_level"),
                "interpretation_status": INTERPRETATION_NOT_EVALUATED,
            }
        )
    return fight_eps


def _fight_episode_summary(fe: dict[str, Any]) -> dict[str, Any]:
    agg = fe.get("aggression_facts") or {}
    cons = fe.get("trade_backed_consumption_facts") or []
    return {
        "fight_episode_id": fe.get("fight_episode_id"),
        "edge": fe.get("edge"),
        "profile_state": fe.get("profile_state"),
        "duration_seconds": (fe.get("price_response_facts") or {}).get("duration_seconds"),
        "taker_delta_quote": agg.get("taker_delta_quote"),
        "price_change_bps": agg.get("price_change_bps"),
        "trade_associated_consumption_count": sum(
            1 for c in cons if c.get("matching_status") == "TRADE_ASSOCIATED"
        ),
        "interpretation_status": INTERPRETATION_NOT_EVALUATED,
    }
