"""Build OutcomeFacts for a single (anchor, horizon) — causal future only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import (
    CENSOR_BOOK_COVERAGE_MISSING,
    CENSOR_EPOCH_BOUNDARY,
    CENSOR_HORIZON_NOT_REACHED,
    CONTRACT_VERSION,
)
from .models import OutcomeFacts, empty_facts_template
from .side import (
    attack_progress_bps,
    attack_progress_ticks,
    defender_progress_bps,
    defender_progress_ticks,
    on_attack_side,
    on_defender_side,
    side_label,
)

EPSILON = 1e-12


def _ms(a: datetime, b: datetime) -> int:
    return int(round((b - a).total_seconds() * 1000.0))


def _row_at_or_before(rows: list[dict[str, Any]], t: datetime, time_key: str = "decision_time") -> dict[str, Any] | None:
    last = None
    for r in rows:
        rt = _as_dt(r.get(time_key) or r.get("timestamp") or r.get("feature_available_at"))
        if rt <= t:
            last = r
        else:
            break
    return last


def _row_at_or_after(rows: list[dict[str, Any]], t: datetime, time_key: str = "decision_time") -> dict[str, Any] | None:
    for r in rows:
        rt = _as_dt(r.get(time_key) or r.get("timestamp") or r.get("feature_available_at"))
        if rt >= t:
            return r
    return None


def compute_outcome_facts(
    *,
    episode_id: str,
    symbol: str,
    zone_id: str,
    wall_side: str,
    wall_price: float,
    anchor_type: str,
    anchor_time: str,
    horizon_ms: int,
    timeline_rows: list[dict[str, Any]],
    coverage_end: datetime,
    chain: dict[str, Any] | None = None,
    wall_generation_id: str | None = None,
    replay_epoch: int | None = None,
    pull_share_raw: float | None = None,
    cluster_meta: dict[str, Any] | None = None,
    past_only_wall_feats: dict[str, Any] | None = None,
    forced_censor_reason: str | None = None,
    wall_first_touch_time: str | None = None,
) -> OutcomeFacts:
    """Future window (anchor, anchor+horizon]; never uses data before anchor for end-state.

    If horizon_end > coverage_end → CENSORED (HORIZON_NOT_REACHED / EPOCH_BOUNDARY),
    never silently shortened.
    """
    anchor = _as_dt(anchor_time)
    horizon_end = anchor + timedelta(milliseconds=int(horizon_ms))
    facts = empty_facts_template(
        outcome_contract_version=CONTRACT_VERSION,
        episode_id=episode_id,
        chain_id=None if not chain else chain.get("chain_id"),
        wall_generation_id=wall_generation_id,
        symbol=symbol,
        zone_id=zone_id,
        wall_side=wall_side,
        anchor_type=anchor_type,
        anchor_time=format_utc_z(anchor),
        horizon_ms=int(horizon_ms),
        horizon_end=format_utc_z(horizon_end),
        replay_epoch=replay_epoch,
        pull_share_raw=pull_share_raw,
    )
    if cluster_meta:
        for k in (
            "attack_cluster_id",
            "touch_index_within_cluster",
            "first_touch_in_cluster",
            "last_touch_in_cluster",
            "cluster_start",
            "cluster_end",
        ):
            if k in cluster_meta:
                setattr(facts, k, cluster_meta[k])
    if past_only_wall_feats:
        for k, v in past_only_wall_feats.items():
            if hasattr(facts, k):
                setattr(facts, k, v)

    if forced_censor_reason:
        facts.coverage_ok = False
        facts.censor_reason = forced_censor_reason
        return facts

    if horizon_end > coverage_end + timedelta(milliseconds=50):
        # Do not shorten — censor full horizon
        reason = CENSOR_EPOCH_BOUNDARY if replay_epoch is not None else CENSOR_HORIZON_NOT_REACHED
        # Prefer RAW_DATA_END if coverage ends for raw data reasons; caller can override
        facts.coverage_ok = False
        facts.censor_reason = reason
        facts.max_input_available_at = format_utc_z(coverage_end)
        return facts

    # Path: (anchor, horizon_end]. Start: at/before anchor; if book begins
    # milliseconds after touch, first at/after anchor (still causal — no pre-anchor future).
    sorted_rows = sorted(
        timeline_rows,
        key=lambda r: _as_dt(r.get("decision_time") or r.get("timestamp")),
    )
    start_row = _row_at_or_before(sorted_rows, anchor)
    if start_row is None:
        start_row = _row_at_or_after(sorted_rows, anchor)
        if start_row is None or _as_dt(start_row.get("decision_time") or start_row.get("timestamp")) > horizon_end:
            facts.coverage_ok = False
            facts.censor_reason = CENSOR_BOOK_COVERAGE_MISSING
            return facts

    path = [
        r
        for r in sorted_rows
        if _as_dt(r.get("decision_time") or r.get("timestamp")) > anchor
        and _as_dt(r.get("decision_time") or r.get("timestamp")) <= horizon_end
        and (r.get("coverage_ok") in (True, "True", "true", 1) if "coverage_ok" in r else True)
    ]
    end_row = _row_at_or_before(sorted_rows, horizon_end)
    end_t = None if end_row is None else _as_dt(end_row.get("decision_time") or end_row.get("timestamp"))
    if end_row is None or end_t is None or end_t < anchor:
        facts.coverage_ok = False
        facts.censor_reason = CENSOR_BOOK_COVERAGE_MISSING
        return facts
    # End snapshot must not precede start snapshot when start was post-anchor.
    start_t = _as_dt(start_row.get("decision_time") or start_row.get("timestamp"))
    if end_t < start_t:
        facts.coverage_ok = False
        facts.censor_reason = CENSOR_BOOK_COVERAGE_MISSING
        return facts

    # Look-ahead: max input must be <= horizon_end
    max_in = _as_dt(end_row.get("max_input_available_at") or end_row.get("decision_time") or end_row.get("timestamp"))
    facts.max_input_available_at = format_utc_z(max_in)
    if max_in > horizon_end:
        facts.look_ahead = True
        facts.coverage_ok = False
        facts.censor_reason = CENSOR_BOOK_COVERAGE_MISSING
        return facts

    def mid(r):
        if r.get("midprice") is not None:
            return float(r["midprice"])
        bb, ba = r.get("best_bid"), r.get("best_ask")
        if bb is not None and ba is not None:
            return 0.5 * (float(bb) + float(ba))
        return None

    def micro(r):
        return float(r["microprice"]) if r.get("microprice") not in (None, "") else None

    facts.start_bid = float(start_row["best_bid"]) if start_row.get("best_bid") not in (None, "") else None
    facts.start_ask = float(start_row["best_ask"]) if start_row.get("best_ask") not in (None, "") else None
    facts.start_midprice = mid(start_row)
    facts.start_microprice = micro(start_row)
    facts.end_bid = float(end_row["best_bid"]) if end_row.get("best_bid") not in (None, "") else None
    facts.end_ask = float(end_row["best_ask"]) if end_row.get("best_ask") not in (None, "") else None
    facts.end_midprice = mid(end_row)
    facts.end_microprice = micro(end_row)
    facts.end_price_side = side_label(facts.end_midprice, wall_price=wall_price, wall_side=wall_side)
    facts.end_microprice_side = side_label(facts.end_microprice, wall_price=wall_price, wall_side=wall_side)

    # Breach / reclaim along path (after anchor only)
    prev_p = on_attack_side(facts.start_midprice, wall_price=wall_price, wall_side=wall_side)
    prev_m = on_attack_side(facts.start_microprice, wall_price=wall_price, wall_side=wall_side)
    first_p_breach = first_m_breach = first_j_breach = None
    first_p_reclaim = first_m_reclaim = first_j_reclaim = None
    recross = 0
    # dwell
    att_start = None
    def_start = None
    longest_att = 0
    longest_def = 0
    cur_att = 0
    cur_def = 0
    max_att_ticks = 0.0
    max_def_ticks = 0.0
    max_att_bps = 0.0
    max_def_bps = 0.0
    ref = facts.start_midprice

    was_attacked = False
    # Anchor semantics: wall touch / joint breach anchors imply attack already occurred.
    if anchor_type in ("WALL_FIRST_TOUCH", "FIRST_JOINT_BREACH"):
        was_attacked = True
    elif wall_first_touch_time:
        wft = _as_dt(wall_first_touch_time)
        if anchor <= wft <= horizon_end:
            was_attacked = True
    # If start already on attack side after a prior breach relative to wall — still count path
    for r in path:
        t = _as_dt(r.get("decision_time") or r.get("timestamp"))
        mp = mid(r)
        mc = micro(r)
        p_att = on_attack_side(mp, wall_price=wall_price, wall_side=wall_side)
        m_att = on_attack_side(mc, wall_price=wall_price, wall_side=wall_side)
        if p_att:
            was_attacked = True
        if prev_p is False and p_att is True:
            if first_p_breach is None:
                first_p_breach = t
            else:
                recross += 1
        if prev_m is False and m_att is True:
            if first_m_breach is None:
                first_m_breach = t
        if p_att and m_att and first_j_breach is None:
            first_j_breach = t
        if prev_p is True and p_att is False:
            if first_p_breach is not None and first_p_reclaim is None:
                first_p_reclaim = t
        if prev_m is True and m_att is False:
            if first_m_breach is not None and first_m_reclaim is None:
                first_m_reclaim = t
        if (
            first_j_breach is not None
            and first_j_reclaim is None
            and on_defender_side(mp, wall_price=wall_price, wall_side=wall_side)
            and on_defender_side(mc, wall_price=wall_price, wall_side=wall_side)
        ):
            first_j_reclaim = t

        # dwell
        if p_att:
            if att_start is None:
                att_start = t
            cur_att = _ms(att_start, t)
            longest_att = max(longest_att, cur_att)
            def_start = None
            cur_def = 0
        elif on_defender_side(mp, wall_price=wall_price, wall_side=wall_side):
            if def_start is None:
                def_start = t
            cur_def = _ms(def_start, t)
            longest_def = max(longest_def, cur_def)
            att_start = None
            cur_att = 0

        if ref is not None and mp is not None:
            at = attack_progress_ticks(mp, reference=ref, wall_side=wall_side)
            dtcks = defender_progress_ticks(mp, reference=ref, wall_side=wall_side)
            ab = attack_progress_bps(mp, reference=ref, wall_side=wall_side)
            db = defender_progress_bps(mp, reference=ref, wall_side=wall_side)
            if at is not None:
                max_att_ticks = max(max_att_ticks, max(at, 0.0))
            if dtcks is not None:
                max_def_ticks = max(max_def_ticks, max(dtcks, 0.0))
            if ab is not None:
                max_att_bps = max(max_att_bps, max(ab, 0.0))
            if db is not None:
                max_def_bps = max(max_def_bps, max(db, 0.0))

        prev_p = p_att
        prev_m = m_att

    facts.first_price_breach_time = format_utc_z(first_p_breach) if first_p_breach else None
    facts.first_microprice_breach_time = format_utc_z(first_m_breach) if first_m_breach else None
    facts.first_joint_breach_time = format_utc_z(first_j_breach) if first_j_breach else None
    facts.first_price_reclaim_time = format_utc_z(first_p_reclaim) if first_p_reclaim else None
    facts.first_microprice_reclaim_time = format_utc_z(first_m_reclaim) if first_m_reclaim else None
    facts.first_joint_reclaim_time = format_utc_z(first_j_reclaim) if first_j_reclaim else None
    facts.recross_count = recross
    facts.attack_side_dwell_ms = cur_att
    facts.defender_side_dwell_ms = cur_def
    facts.longest_attack_side_dwell_ms = longest_att
    facts.longest_defender_side_dwell_ms = longest_def
    facts.max_attack_excursion_ticks = max_att_ticks
    facts.max_defender_excursion_ticks = max_def_ticks
    facts.max_attack_excursion_bps = max_att_bps
    facts.max_defender_excursion_bps = max_def_bps
    facts.attack_progress_ticks = attack_progress_ticks(
        facts.end_midprice, reference=ref, wall_side=wall_side
    ) if ref is not None else None
    facts.defender_progress_ticks = defender_progress_ticks(
        facts.end_midprice, reference=ref, wall_side=wall_side
    ) if ref is not None else None
    facts.attack_progress_bps = attack_progress_bps(
        facts.end_midprice, reference=ref, wall_side=wall_side
    ) if ref is not None else None
    facts.defender_progress_bps = defender_progress_bps(
        facts.end_midprice, reference=ref, wall_side=wall_side
    ) if ref is not None else None

    facts.wall_was_attacked = was_attacked or bool(first_p_breach)
    facts.joint_breach_observed = bool(first_j_breach)
    facts.joint_reclaim_observed = bool(first_j_reclaim)

    if chain:
        nodes = chain.get("nodes") or []
        # Count nodes attacked with first_trade_touch <= horizon_end and >= anchor
        att_n = 0
        crossed = 0
        surviving = 0
        highest = None
        for n in nodes:
            touch = n.get("first_trade_touch")
            if touch and anchor <= _as_dt(touch) <= horizon_end:
                att_n += 1
            # crossed if wall price reached by mid on path
            px = float(n["wall_price"])
            for r in path:
                mp = mid(r)
                if mp is not None and on_attack_side(mp, wall_price=px, wall_side=wall_side):
                    crossed += 1
                    highest = px if highest is None else (
                        max(highest, px) if wall_side == "ask" else min(highest, px)
                    )
                    break
            end_t = n.get("end_time")
            if end_t is None or _as_dt(end_t) > horizon_end:
                surviving += 1
        facts.attacked_chain_node_count = att_n
        facts.crossed_chain_node_count = crossed
        facts.surviving_relevant_wall_count = surviving
        facts.highest_or_lowest_reached_chain_node = highest
        facts.chain_advance_ticks = chain.get("chain_advance_ticks")
        facts.cumulative_attributed_hits = chain.get("cumulative_attributed_fill_qty")
        facts.cumulative_residual_pulls = chain.get("cumulative_residual_pull_qty")
        facts.cumulative_refills = chain.get("cumulative_refill_qty")
        facts.cumulative_attack_notional = chain.get("cumulative_attack_notional")
        facts.cumulative_counterflow_notional = chain.get("cumulative_counterflow_notional")

    facts.coverage_ok = True
    facts.censor_reason = None
    facts.look_ahead = False
    return facts
