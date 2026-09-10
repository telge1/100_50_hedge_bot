"""Build DefenseChain within a single continuous epoch segment."""

from __future__ import annotations

import hashlib
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import (
    ASK_WALL_BREACH,
    CHAIN_CALIBRATION_STATUS,
    CHAIN_STATUS_EP1,
    EPISODE_ID,
    EPSILON,
    ORIGINAL_WALL_PRICE,
    SYMBOL,
    TICK_SIZE,
    TRANSITION_CENSORED,
    TRANSITION_NEXT_NEW,
    TRANSITION_NEXT_PRE_EXISTING,
    TRANSITION_NO_NEXT,
    TRANSITION_SAME_PRICE_NEW_GEN,
    ZONE_ID,
)
from .models import DefenseChainEvent, DefenseChainNode


def _chain_id(episode_id: str, epoch: int, start: str, first_price: float) -> str:
    raw = f"{episode_id}|epoch={epoch}|start={start}|first={first_price:.10f}"
    return "dch_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _wf_asof(wf_rows: list[dict[str, Any]], ts) -> dict[str, Any] | None:
    last = None
    t = _as_dt(ts)
    for r in wf_rows:
        avail = r.get("feature_available_at") or r.get("timestamp")
        if avail and _as_dt(avail) <= t:
            last = r
        else:
            if avail and _as_dt(avail) > t:
                break
    return last


def _liq_asof(liq: list[dict[str, Any]], ts) -> dict[str, Any] | None:
    last = None
    t = _as_dt(ts)
    for r in liq:
        if _as_dt(r["timestamp"]) <= t:
            last = r
        else:
            break
    return last


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify_transition(prev: dict[str, Any], nxt: dict[str, Any] | None, *, coverage_end) -> str:
    if nxt is None:
        # if prev ended before coverage end without next → no next layer; else censored
        if prev.get("end_time") and _as_dt(prev["end_time"]) < coverage_end:
            return TRANSITION_NO_NEXT
        return TRANSITION_CENSORED
    if abs(float(prev["price"]) - float(nxt["price"])) <= 1e-9:
        return TRANSITION_SAME_PRICE_NEW_GEN
    if nxt.get("pre_existing") or str(nxt.get("layer_class") or "").startswith("PRE_EXISTING"):
        return TRANSITION_NEXT_PRE_EXISTING
    return TRANSITION_NEXT_NEW


def build_ask_defense_chain(
    *,
    scored_gens: list[dict[str, Any]],
    replay_epoch: int,
    coverage_start: str,
    coverage_end: str,
    wall_flow_rows: list[dict[str, Any]] | None = None,
    liquidity_rows: list[dict[str, Any]] | None = None,
    wall_price: float = ORIGINAL_WALL_PRICE,
    episode_id: str = EPISODE_ID,
    breach_iso: str | None = ASK_WALL_BREACH,
) -> DefenseChainEvent:
    """Chain relevant ask walls in attack direction within one epoch only."""
    cov_end = _as_dt(coverage_end)
    cov_start = _as_dt(coverage_start)
    wf = sorted(wall_flow_rows or [], key=lambda r: _as_dt(r.get("feature_available_at") or r.get("timestamp") or coverage_start))
    liq = sorted(liquidity_rows or [], key=lambda r: _as_dt(r["timestamp"]))

    relevant = [
        g
        for g in scored_gens
        if g.get("is_relevant_at_entry")
        and int(g.get("replay_epoch") or replay_epoch) == int(replay_epoch)
        and g.get("coverage_ok", True)
    ]
    # Order: first by first_trade_touch / first attack, else first_relevant_at, preferring original wall first if attacked
    def sort_key(g):
        touch = g.get("first_trade_touch") or g.get("first_relevant_at") or g["start_time"]
        orig = 0 if abs(float(g["price"]) - wall_price) <= 1e-9 else 1
        return (orig, _as_dt(touch), float(g["price"]), int(g.get("generation_index") or 0))

    relevant_sorted = sorted(relevant, key=sort_key)

    # Build sequential chain: start at first attacked relevant (or original relevant), then append next
    # relevant walls that become the new front after previous ends (higher ask price in attack dir).
    nodes_src: list[dict[str, Any]] = []
    if not relevant_sorted:
        chain_start = coverage_start
        return DefenseChainEvent(
            chain_id=_chain_id(episode_id, replay_epoch, chain_start, wall_price),
            episode_id=episode_id,
            symbol=SYMBOL,
            replay_epoch=replay_epoch,
            zone_id=ZONE_ID,
            zone_side="ask_above_zone",
            attack_direction="up",
            chain_start=chain_start,
            chain_end=coverage_end,
            max_input_available_at=coverage_end,
            coverage_ok=True,
            censor_reason=CHAIN_STATUS_EP1,
            node_count=0,
            attacked_node_count=0,
            pre_existing_node_count=0,
            new_post_breach_node_count=0,
            first_wall_price=None,
            current_front_wall_price=None,
            highest_defense_price=None,
            chain_advance_ticks=None,
            chain_advance_bps=None,
            cumulative_initial_wall_qty=0.0,
            cumulative_peak_wall_qty=0.0,
            cumulative_attributed_fill_qty=0.0,
            cumulative_residual_pull_qty=0.0,
            cumulative_refill_qty=0.0,
            cumulative_attack_notional=0.0,
            cumulative_counterflow_notional=0.0,
            price_progress_ticks=None,
            price_progress_bps=None,
            microprice_progress_ticks=None,
            attack_rate_fast=None,
            attack_rate_slow=None,
            attacker_persistence_ratio=None,
            counterflow_rate_fast=None,
            counterflow_rate_slow=None,
            active_wall_count=0,
            nearest_relevant_wall_distance_ticks=None,
            ask_liquidity_centroid=None,
            centroid_shift_ticks=None,
        )

    # Seed: prefer original wall relevant gen, else first attacked, else first relevant
    seed = None
    for g in relevant_sorted:
        if abs(float(g["price"]) - wall_price) <= 1e-9:
            seed = g
            break
    if seed is None:
        for g in relevant_sorted:
            if float(g.get("cumulative_attributed_fills") or 0) > EPSILON or g.get("first_trade_touch"):
                seed = g
                break
    if seed is None:
        seed = relevant_sorted[0]

    nodes_src.append(seed)
    used_ids = {seed["wall_generation_id"]}
    cur = seed
    # Greedy next: after cur ends (or at cur end time), pick next relevant with price >= cur.price
    # (ask attack: higher or equal), not yet used, visible by transition time, same epoch.
    while True:
        t_gate = _as_dt(cur["end_time"]) if cur.get("end_time") else cov_end
        if t_gate >= cov_end and not cur.get("end_time"):
            break
        candidates = []
        for g in relevant_sorted:
            if g["wall_generation_id"] in used_ids:
                continue
            if float(g["price"]) + 1e-12 < float(cur["price"]) and abs(float(g["price"]) - float(cur["price"])) > 1e-9:
                # must be in attack direction (ask: at or above current)
                if float(g["price"]) < float(cur["price"]) - 1e-9:
                    continue
            vis = _as_dt(g["start_time"])
            if vis > t_gate:
                # not yet known at transition — skip unless already pre-existing before gate
                if vis > t_gate:
                    continue
            if vis > cov_end:
                continue
            # known at transition: first_visible <= t_gate
            if vis > t_gate:
                continue
            if float(g["price"]) < float(cur["price"]) - 1e-9:
                continue
            candidates.append(g)
        if not candidates:
            break
        # Prefer nearest price above current, then earliest relevant
        candidates.sort(
            key=lambda g: (
                float(g["price"]) - float(cur["price"]),
                _as_dt(g.get("first_relevant_at") or g["start_time"]),
            )
        )
        nxt = candidates[0]
        # Ensure causal: next must be known at transition
        if _as_dt(nxt["start_time"]) > t_gate:
            break
        nodes_src.append(nxt)
        used_ids.add(nxt["wall_generation_id"])
        cur = nxt
        if len(nodes_src) > 50:
            break

    # Materialize nodes
    nodes: list[DefenseChainNode] = []
    transitions: list[dict[str, Any]] = []
    for i, g in enumerate(nodes_src):
        nxt = nodes_src[i + 1] if i + 1 < len(nodes_src) else None
        t_entry = g.get("first_trade_touch") or g.get("first_relevant_at") or g["start_time"]
        t_exit = g.get("end_time") or coverage_end
        wf_s = _wf_asof(wf, t_entry)
        wf_e = _wf_asof(wf, t_exit)
        liq_s = _liq_asof(liq, t_entry)
        liq_e = _liq_asof(liq, t_exit)
        delay = None
        adv = None
        tr_type = classify_transition(g, nxt, coverage_end=cov_end)
        if nxt is not None:
            delay = int(
                round(
                    (_as_dt(nxt.get("first_trade_touch") or nxt["start_time"]) - _as_dt(t_exit)).total_seconds()
                    * 1000
                )
            )
            adv = (float(nxt["price"]) - float(g["price"])) / TICK_SIZE
            transitions.append(
                {
                    "from_generation_id": g["wall_generation_id"],
                    "to_generation_id": nxt["wall_generation_id"],
                    "from_price": g["price"],
                    "to_price": nxt["price"],
                    "transition_type": tr_type,
                    "delay_ms": delay,
                    "price_advance_ticks": adv,
                    "replay_epoch": replay_epoch,
                }
            )
        else:
            transitions.append(
                {
                    "from_generation_id": g["wall_generation_id"],
                    "to_generation_id": None,
                    "from_price": g["price"],
                    "to_price": None,
                    "transition_type": tr_type,
                    "delay_ms": None,
                    "price_advance_ticks": None,
                    "replay_epoch": replay_epoch,
                }
            )

        nodes.append(
            DefenseChainNode(
                chain_node_index=i,
                wall_generation_id=g["wall_generation_id"],
                wall_price=float(g["price"]),
                first_visible_at=g["start_time"],
                first_relevant_at=g.get("first_relevant_at"),
                first_trade_touch=g.get("first_trade_touch"),
                end_time=g.get("end_time"),
                initial_qty=float(g.get("initial_qty") or 0),
                peak_qty=float(g.get("peak_qty") or 0),
                final_qty=float(g.get("final_qty") or 0),
                rolling_size_percentile_at_entry=g.get("rolling_size_percentile"),
                pre_existing=bool(g.get("pre_existing")),
                cumulative_hits=float(g.get("cumulative_attributed_fills") or 0),
                cumulative_pulls=float(g.get("cumulative_residual_pulls") or 0),
                cumulative_refills=float(g.get("cumulative_refills") or 0),
                qdh_base_start=_f((wf_s or {}).get("qdh_base")),
                qdh_base_peak=_f((wf_s or {}).get("qdh_base_peak_so_far")),
                qdh_base_end=_f((wf_e or {}).get("qdh_base")),
                attacker_persistence_start=_f((wf_s or {}).get("persistence_ratio")),
                attacker_persistence_end=_f((wf_e or {}).get("persistence_ratio")),
                price_at_entry=_f((liq_s or {}).get("midprice")),
                price_at_exit=_f((liq_e or {}).get("midprice")),
                microprice_at_entry=_f((wf_s or {}).get("microprice")),
                microprice_at_exit=_f((wf_e or {}).get("microprice")),
                termination_reason=str(g.get("termination_reason") or "UNKNOWN"),
                next_node_delay_ms=delay,
                price_advance_to_next_node_ticks=adv,
                attribution_confidence=str(g.get("attribution_confidence") or "MEDIUM"),
                coverage_ok=True,
                transition_to_next=tr_type,
                impact_efficiency_start=_f((wf_s or {}).get("impact_efficiency")),
                impact_efficiency_end=_f((wf_e or {}).get("impact_efficiency")),
                layer_class=g.get("layer_class"),
            )
        )

    node_dicts = [n.to_dict() for n in nodes]
    attacked_n = sum(1 for n in nodes if n.cumulative_hits > EPSILON or n.first_trade_touch)
    pre_n = sum(1 for n in nodes if n.pre_existing)
    post_n = sum(1 for n in nodes if not n.pre_existing)
    first_px = nodes[0].wall_price if nodes else None
    front = nodes[-1].wall_price if nodes else None
    highest = max((n.wall_price for n in nodes), default=None)
    advance_ticks = None if first_px is None or highest is None else (highest - first_px) / TICK_SIZE
    advance_bps = None
    if first_px and highest and first_px > 0:
        advance_bps = (highest - first_px) / first_px * 10000.0

    # Price progress from chain start mid to coverage end mid
    liq0 = _liq_asof(liq, nodes[0].first_trade_touch or nodes[0].first_visible_at) if nodes else None
    liq1 = _liq_asof(liq, coverage_end)
    mid0 = _f((liq0 or {}).get("midprice"))
    mid1 = _f((liq1 or {}).get("midprice"))
    micro0 = nodes[0].microprice_at_entry if nodes else None
    micro1 = nodes[-1].microprice_at_exit if nodes else None
    price_prog_ticks = None if mid0 is None or mid1 is None else (mid1 - mid0) / TICK_SIZE
    price_prog_bps = None if mid0 is None or mid1 is None or mid0 <= 0 else (mid1 - mid0) / mid0 * 10000.0
    micro_prog = None if micro0 is None or micro1 is None else (micro1 - micro0) / TICK_SIZE

    cent0 = _f((liq0 or {}).get("ask_liquidity_centroid"))
    cent1 = _f((liq1 or {}).get("ask_liquidity_centroid"))
    cent_shift = None if cent0 is None or cent1 is None else (cent1 - cent0) / TICK_SIZE

    wf_end = _wf_asof(wf, coverage_end)
    attack_notional = sum(
        float(n.cumulative_hits) * float(n.wall_price) for n in nodes
    )  # qty * price proxy; exact notional from wf if present

    # Prefer wall-flow cumulative hit_notional if available at end vs start
    wf_start = _wf_asof(wf, nodes[0].first_visible_at) if nodes else None
    cum_att = _f((wf_end or {}).get("hit_notional"))
    if cum_att is None:
        cum_att = attack_notional

    nearest_dist = None
    if liq1 and liq1.get("nearest_relevant_ask_price") is not None and mid1 is not None:
        nearest_dist = (float(liq1["nearest_relevant_ask_price"]) - mid1) / TICK_SIZE

    chain = DefenseChainEvent(
        chain_id=_chain_id(episode_id, replay_epoch, nodes[0].first_visible_at if nodes else coverage_start, first_px or wall_price),
        episode_id=episode_id,
        symbol=SYMBOL,
        replay_epoch=replay_epoch,
        zone_id=ZONE_ID,
        zone_side="ask_above_zone",
        attack_direction="up",
        chain_start=nodes[0].first_visible_at if nodes else coverage_start,
        chain_end=coverage_end,
        max_input_available_at=coverage_end,
        coverage_ok=True,
        censor_reason=CHAIN_STATUS_EP1,
        node_count=len(nodes),
        attacked_node_count=attacked_n,
        pre_existing_node_count=pre_n,
        new_post_breach_node_count=post_n,
        first_wall_price=first_px,
        current_front_wall_price=front,
        highest_defense_price=highest,
        chain_advance_ticks=advance_ticks,
        chain_advance_bps=advance_bps,
        cumulative_initial_wall_qty=sum(n.initial_qty for n in nodes),
        cumulative_peak_wall_qty=sum(n.peak_qty for n in nodes),
        cumulative_attributed_fill_qty=sum(n.cumulative_hits for n in nodes),
        cumulative_residual_pull_qty=sum(n.cumulative_pulls for n in nodes),
        cumulative_refill_qty=sum(n.cumulative_refills for n in nodes),
        cumulative_attack_notional=float(cum_att or 0),
        cumulative_counterflow_notional=0.0,  # filled in attacker module if sell flow available
        price_progress_ticks=price_prog_ticks,
        price_progress_bps=price_prog_bps,
        microprice_progress_ticks=micro_prog,
        attack_rate_fast=_f((wf_end or {}).get("hit_rate")),
        attack_rate_slow=_f((wf_end or {}).get("trade_rate")),
        attacker_persistence_ratio=_f((wf_end or {}).get("persistence_ratio")),
        counterflow_rate_fast=None,
        counterflow_rate_slow=None,
        active_wall_count=sum(1 for n in nodes if n.end_time is None or _as_dt(n.end_time) >= cov_end),
        nearest_relevant_wall_distance_ticks=nearest_dist,
        ask_liquidity_centroid=cent1,
        centroid_shift_ticks=cent_shift,
        chain_status=CHAIN_STATUS_EP1,
        calibration_status=CHAIN_CALIBRATION_STATUS,
        look_ahead=False,
        nodes=node_dicts,
        transitions=transitions,
    )
    return chain
