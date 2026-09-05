"""Tests for causal pool-edge join (no future walls; side semantics)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import CausalEdge
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
    required_wall_side_for_event,
    select_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    synthetic_event,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe

UTC = timezone.utc
BASE = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def T(sec: int) -> datetime:
    return BASE + timedelta(seconds=sec)


def sample(ts_s: int, *, mid: float, bid_px=None, bid_qty=None, ask_px=None, ask_qty=None):
    return SimpleNamespace(
        ts_ms=int(T(ts_s).timestamp() * 1000),
        mid=mid,
        bid_wall_price=bid_px,
        bid_wall_qty=bid_qty,
        ask_wall_price=ask_px,
        ask_wall_qty=ask_qty,
        warmup=False,
    )


def edge(eid: str, side: str, price: float, appear_s: int, symbol="DOGEUSDT") -> CausalEdge:
    ts = T(appear_s)
    return CausalEdge(
        edge_id=eid,
        symbol=symbol,
        wall_side=side,
        edge_price=price,
        tick_size=0.00001,
        first_seen_ts=ts,
        edge_observed_ts=ts,
        edge_available_ts=ts,
        edge_source="raw_ob200_wall_lifecycle",
        edge_source_event_id=eid,
        initial_notional=0.0,
        data_quality_status="OK",
        causal_eligible=True,
    )


def test_buy_matches_only_ask():
    # SHORT = Buy aggressor → ASK wall
    ev = synthetic_event(
        event_id="B",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side="ASK",
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    assert required_wall_side_for_event(ev) == "ASK"
    edges = [edge("ask1", "ASK", 0.085, 50), edge("bid1", "BID", 0.084, 50)]
    samples = [sample(100, mid=0.0849, ask_px=0.085, ask_qty=1e6, bid_px=0.084, bid_qty=1e6)]
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=0.0849, flow_vwap=0.0849, flow_low=0.0848, flow_high=0.0850, thr=JoinThresholds()
    )
    ok = [c for c in cands if c["candidate_rejection_reason"] is None]
    assert all(c["wall_side"] == "ASK" for c in ok)
    assert any(c["edge_id"] == "bid1" and c["candidate_rejection_reason"] == "SIDE_MISMATCH" for c in cands)


def test_sell_matches_only_bid():
    ev = synthetic_event(
        event_id="S",
        symbol="DOGEUSDT",
        direction="LONG",
        wall_side="BID",
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    assert required_wall_side_for_event(ev) == "BID"
    edges = [edge("ask1", "ASK", 0.085, 50), edge("bid1", "BID", 0.084, 50)]
    samples = [sample(100, mid=0.0841, ask_px=0.085, ask_qty=1e6, bid_px=0.084, bid_qty=1e6)]
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=0.0841, flow_vwap=0.0841, flow_low=0.0840, flow_high=0.0842, thr=JoinThresholds()
    )
    ok = [c for c in cands if c["candidate_rejection_reason"] is None]
    assert all(c["wall_side"] == "BID" for c in ok)


def test_visible_before_attack_matched():
    ev = synthetic_event(
        event_id="V",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("e", "ASK", 0.085, 10)]
    samples = [sample(100, mid=0.08495, ask_px=0.085, ask_qty=5e5)]
    join = select_match(
        ev,
        evaluate_candidates(
            ev, edges, samples, flow_start_price=0.08495, flow_vwap=0.08495, flow_low=0.0849, flow_high=0.0850, thr=JoinThresholds()
        ),
        thr=JoinThresholds(),
    )
    assert join.edge_match_confidence_class in {"HIGH", "MEDIUM"}
    assert join.matched_edge_id == "e"


def test_future_edge_rejected():
    ev = synthetic_event(
        event_id="F",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("future", "ASK", 0.085, 101)]  # appears after flow start
    samples = [sample(100, mid=0.08495, ask_px=0.085, ask_qty=5e5)]
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=0.08495, flow_vwap=None, flow_low=None, flow_high=None, thr=JoinThresholds()
    )
    assert cands[0]["candidate_rejection_reason"] == "EDGE_AFTER_FLOW_START"


def test_too_far_rejected():
    ev = synthetic_event(
        event_id="FAR",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    # edge 50 bps away
    edges = [edge("far", "ASK", 0.0855, 10)]
    samples = [sample(100, mid=0.0850, ask_px=0.0855, ask_qty=5e5)]
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=0.0850, flow_vwap=0.0850, flow_low=0.0849, flow_high=0.0851, thr=JoinThresholds()
    )
    assert cands[0]["candidate_rejection_reason"] == "EDGE_TOO_FAR"


def test_stale_rejected():
    ev = synthetic_event(
        event_id="ST",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("stale", "ASK", 0.085, 10)]
    # sample at t=100 has no ask wall
    samples = [sample(100, mid=0.08495, ask_px=None, ask_qty=None)]
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=0.08495, flow_vwap=None, flow_low=None, flow_high=None, thr=JoinThresholds()
    )
    assert cands[0]["candidate_rejection_reason"] == "EDGE_STALE"


def test_ambiguous_two_edges():
    ev = synthetic_event(
        event_id="A",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("e1", "ASK", 0.08500, 10), edge("e2", "ASK", 0.08500, 20)]
    samples = [sample(100, mid=0.08495, ask_px=0.08500, ask_qty=5e5)]
    join = select_match(
        ev,
        evaluate_candidates(
            ev, edges, samples, flow_start_price=0.08495, flow_vwap=0.08495, flow_low=0.0849, flow_high=0.0850, thr=JoinThresholds()
        ),
        thr=JoinThresholds(),
    )
    assert join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS"
    assert join.edge_match_confidence_class == "LOW"


def test_future_persistence_does_not_change_match():
    """Presence checked only as-of flow_start; later samples ignored."""
    ev = synthetic_event(
        event_id="P",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("e", "ASK", 0.085, 10)]
    samples_short = [sample(100, mid=0.08495, ask_px=0.085, ask_qty=5e5)]
    samples_long = samples_short + [
        sample(200, mid=0.086, ask_px=0.086, ask_qty=9e9),  # future huge wall elsewhere
    ]
    j1 = select_match(
        ev,
        evaluate_candidates(
            ev, edges, samples_short, flow_start_price=0.08495, flow_vwap=0.08495, flow_low=0.0849, flow_high=0.085, thr=JoinThresholds()
        ),
        thr=JoinThresholds(),
    )
    j2 = select_match(
        ev,
        evaluate_candidates(
            ev, edges, samples_long, flow_start_price=0.08495, flow_vwap=0.08495, flow_low=0.0849, flow_high=0.085, thr=JoinThresholds()
        ),
        thr=JoinThresholds(),
    )
    assert j1.matched_edge_id == j2.matched_edge_id
    assert j1.matched_edge_price == j2.matched_edge_price
    assert j1.edge_match_confidence_class == j2.edge_match_confidence_class


def test_low_confidence_does_not_enable_acceptance_edge():
    thr = JoinThresholds()
    ev = synthetic_event(
        event_id="L",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import EdgeJoinResult

    join = EdgeJoinResult(
        aef_event_id="L",
        edge_join_status="MULTIPLE_EDGE_AMBIGUOUS",
        matched_edge_id="x",
        matched_edge_price=0.085,
        edge_match_confidence_class="LOW",
    )
    ev2 = apply_join_to_event(ev, join, thr)
    assert ev2.edge_price is None
    assert ev2.edge_confidence == "low"


def test_high_confidence_sets_edge():
    thr = JoinThresholds()
    ev = synthetic_event(
        event_id="H",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import EdgeJoinResult

    join = EdgeJoinResult(
        aef_event_id="H",
        edge_join_status="EXACT_EDGE_TOUCH",
        matched_edge_id="x",
        matched_edge_price=0.085,
        matched_edge_source="raw_ob200_wall_lifecycle",
        matched_edge_available_ts="2026-08-29T12:00:10Z",
        edge_match_confidence_class="HIGH",
    )
    ev2 = apply_join_to_event(ev, join, thr)
    assert ev2.edge_price == 0.085
    assert ev2.edge_confidence == "high"
    assert ev2.wall_side == "ASK"


def test_json_safe_join():
    assert json_safe({"x": float("nan")})["x"] is None


def test_no_edge_unknown():
    ev = synthetic_event(
        event_id="N",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    join = select_match(ev, [], thr=JoinThresholds())
    assert join.edge_join_status == "NO_CAUSAL_EDGE"
    assert join.edge_match_confidence_class == "NONE"
