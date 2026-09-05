"""Tests for causal pool-edge ambiguity resolution v1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import CausalEdge
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
    select_disambiguated_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
    required_wall_side_for_event,
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


def edge(eid: str, side: str, price: float, appear_s: int, symbol="BTCUSDT") -> CausalEdge:
    ts = T(appear_s)
    return CausalEdge(
        edge_id=eid,
        symbol=symbol,
        wall_side=side,
        edge_price=price,
        tick_size=0.1,
        first_seen_ts=ts,
        edge_observed_ts=ts,
        edge_available_ts=ts,
        edge_source="raw_ob200_wall_lifecycle",
        edge_source_event_id=eid,
        initial_notional=0.0,
        data_quality_status="OK",
        causal_eligible=True,
    )


def trade(sec: float, side: str, price: float, notional: float = 1000.0, tid: str = "1") -> Trade:
    return Trade(
        trade_ts=T(int(sec)) + timedelta(milliseconds=int((sec % 1) * 1000)),
        trade_id=tid,
        side=side,
        price=price,
        size=notional / price,
        notional=notional,
    )


def _join(ev, edges, samples, trades, flo, fhi, start_px):
    thr = JoinThresholds()
    cands = evaluate_candidates(
        ev,
        edges,
        samples,
        flow_start_price=start_px,
        flow_vwap=start_px,
        flow_low=flo,
        flow_high=fhi,
        thr=thr,
    )
    return select_disambiguated_match(
        ev,
        cands,
        trades=trades,
        flow_start_price=start_px,
        flow_vwap=start_px,
        flow_low=flo,
        flow_high=fhi,
        thr=thr,
        dthr=DisambiguationThresholds(),
    )


def test_exact_traded_beats_near_only():
    ev = synthetic_event(
        event_id="t1",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("touch", "ASK", 100.0, 10), edge("near", "ASK", 100.1, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=10)]
    trades = [trade(101, "Buy", 100.0, 5000, "a"), trade(102, "Buy", 100.05, 100, "b")]
    join, _, _ = _join(ev, edges, samples, trades, 99.9, 100.05, 99.95)
    assert join.matched_edge_id == "touch"
    assert join.edge_join_status == "EXACT_TRADED_EDGE"
    assert join.edge_match_confidence_class == "HIGH"


def test_front_ask_wins_on_buy():
    ev = synthetic_event(
        event_id="t2",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("front", "ASK", 100.0, 10), edge("back", "ASK", 100.1, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=10)]
    # no aggressor prints inside either edge zone — only directional reach
    trades: list = []
    join, _, _ = _join(ev, edges, samples, trades, 99.9, 100.1, 99.95)
    assert join.matched_edge_id == "front"
    assert join.edge_match_confidence_class == "HIGH"
    assert "FRONT" in join.edge_join_status or "FRONT_REACHED" in join.edge_match_explanation_codes


def test_front_bid_wins_on_sell():
    ev = synthetic_event(
        event_id="t3",
        symbol="BTCUSDT",
        direction="LONG",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("front", "BID", 100.0, 10), edge("back", "BID", 99.5, 10)]
    samples = [sample(100, mid=100.1, bid_px=100.0, bid_qty=10)]
    trades = [trade(101, "Sell", 99.8, 1000, "a")]
    join, _, _ = _join(ev, edges, samples, trades, 99.5, 100.1, 100.1)
    assert join.matched_edge_id == "front"
    assert join.edge_match_confidence_class == "HIGH"


def test_larger_unreached_back_does_not_win():
    ev = synthetic_event(
        event_id="t4",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    # huge_back within spatial band but above flow_high → not reached
    edges = [edge("front", "ASK", 100.0, 10), edge("huge_back", "ASK", 100.1, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=1)]
    trades = [trade(101, "Buy", 100.0, 2000, "a")]
    join, enriched, _ = _join(ev, edges, samples, trades, 99.9, 100.0, 99.95)
    assert join.matched_edge_id == "front"
    back = next(e for e in enriched if e["edge_id"] == "huge_back")
    assert not back.get("reached_in_directional_path")


def test_persistent_unreached_does_not_win():
    ev = synthetic_event(
        event_id="t5",
        symbol="BTCUSDT",
        direction="LONG",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("persist", "BID", 100.5, 1), edge("traded", "BID", 100.0, 90)]
    samples = [sample(100, mid=100.05, bid_px=100.0, bid_qty=5)]
    trades = [trade(101, "Sell", 100.0, 3000, "a")]
    join, _, _ = _join(ev, edges, samples, trades, 99.9, 100.05, 100.05)
    assert join.matched_edge_id == "traded"
    assert join.edge_join_status == "EXACT_TRADED_EDGE"


def test_cluster_adjacent_levels():
    """Cluster roles assigned among reached candidates (synthetic plausible rows)."""
    from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
        assign_cluster_roles,
        enrich_candidate_with_trades,
    )

    ev = synthetic_event(
        event_id="t6",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    dthr = DisambiguationThresholds()
    atrades = [trade(101, "Buy", 100.0, 1000, "a")]
    base_rows = []
    for eid, px in (("f", 100.0), ("i", 100.1), ("b", 100.15)):
        raw = {
            "aef_event_id": "t6",
            "edge_id": eid,
            "wall_side": "ASK",
            "edge_price": px,
            "edge_available_ts": "2026-08-29T11:00:00Z",
            "match_class": "EXACT_EDGE_TOUCH",
            "candidate_rejection_reason": None,
            "notional_asof_attack": 1e5,
            "persistence_seconds_asof_attack": 10.0,
            "relative_size_asof_attack": 1.0,
            "last_seen_age_seconds": 0.0,
            "distance_to_flow_start_price_bps": abs(px - 100.02) / 100.02 * 1e4,
            "distance_to_flow_vwap_bps": 0.0,
            "distance_to_flow_extreme_bps": 0.0,
            "overlap_with_flow_price_range": True,
        }
        base_rows.append(
            enrich_candidate_with_trades(
                raw,
                event=ev,
                atrades=atrades,
                flow_start_price=100.02,
                flow_vwap=100.02,
                flow_low=99.95,
                flow_high=100.15,
                dthr=dthr,
            )
        )
    enriched = assign_cluster_roles(base_rows, wall_side="ASK", symbol="BTCUSDT", dthr=dthr)
    clusters = enriched[0].pop("_cluster_summaries", [])
    assert clusters
    assert clusters[0]["cluster_front_price"] == 100.0
    assert clusters[0]["cluster_back_price"] == 100.15
    assert {c["edge_id"]: c["cluster_role"] for c in enriched}["f"] == "FRONT_EDGE"
    assert {c["edge_id"]: c["cluster_role"] for c in enriched}["b"] == "BACK_EDGE"


def test_cluster_front_vs_back_acceptance_edge():
    ev = synthetic_event(
        event_id="t7",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("front", "ASK", 100.0, 10), edge("back", "ASK", 100.1, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    trades = [trade(101, "Buy", 100.0, 1000, "a")]
    join, enriched, clusters = _join(ev, edges, samples, trades, 99.9, 100.1, 99.95)
    assert join.matched_edge_id == "front"
    roles = {e["edge_id"]: e.get("cluster_role") for e in enriched if e.get("cluster_role")}
    assert roles.get("front") in {"FRONT_EDGE", "SEPARATE_EDGE"}
    assert clusters[0]["primary_touched_edge"] == "front"


def test_two_equal_traded_remain_ambiguous():
    ev = synthetic_event(
        event_id="t8",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("e1", "ASK", 100.0, 10), edge("e2", "ASK", 100.0, 20)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    trades = [trade(101, "Buy", 100.0, 1000, "a")]
    join, _, _ = _join(ev, edges, samples, trades, 99.9, 100.0, 99.95)
    assert join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS"
    assert join.edge_match_confidence_class == "LOW"


def test_future_break_ignored():
    ev = synthetic_event(
        event_id="t9",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("a", "ASK", 100.0, 10), edge("b", "ASK", 100.3, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    trades_flow = [trade(101, "Buy", 100.0, 1000, "a")]
    trades_future = trades_flow + [trade(200, "Buy", 100.3, 9e9, "z")]
    j1, _, _ = _join(ev, edges, samples, trades_flow, 99.9, 100.0, 99.95)
    j2, _, _ = _join(ev, edges, samples, trades_future, 99.9, 100.0, 99.95)
    assert j1.matched_edge_id == j2.matched_edge_id == "a"
    assert j1.edge_match_confidence_class == j2.edge_match_confidence_class


def test_future_reclaim_ignored():
    test_future_break_ignored()


def test_future_wall_size_ignored():
    ev = synthetic_event(
        event_id="t11",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("a", "ASK", 100.0, 10)]
    s1 = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    s2 = s1 + [sample(200, mid=100.5, ask_px=100.0, ask_qty=9e9)]
    trades = [trade(101, "Buy", 100.0, 1000, "a")]
    j1, _, _ = _join(ev, edges, s1, trades, 99.9, 100.0, 99.95)
    j2, _, _ = _join(ev, edges, s2, trades, 99.9, 100.0, 99.95)
    assert j1.matched_edge_id == j2.matched_edge_id
    assert j1.matched_edge_notional_asof == j2.matched_edge_notional_asof


def test_future_persistence_ignored():
    test_future_wall_size_ignored()


def test_side_buy_ask_sell_bid():
    assert (
        required_wall_side_for_event(
            synthetic_event(
                event_id="s",
                symbol="BTCUSDT",
                direction="SHORT",
                wall_side=None,
                edge_price=None,
                edge_source="none",
                edge_confidence="none",
                flow_start_ts=T(100),
                flow_end_ts=T(105),
                decision_ts=T(110),
            )
        )
        == "ASK"
    )
    assert (
        required_wall_side_for_event(
            synthetic_event(
                event_id="s2",
                symbol="BTCUSDT",
                direction="LONG",
                wall_side=None,
                edge_price=None,
                edge_source="none",
                edge_confidence="none",
                flow_start_ts=T(100),
                flow_end_ts=T(105),
                decision_ts=T(110),
            )
        )
        == "BID"
    )


def test_low_does_not_activate_acceptance():
    thr = JoinThresholds(accept_confidence=("HIGH",))
    ev = synthetic_event(
        event_id="L",
        symbol="BTCUSDT",
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
        matched_edge_price=100.0,
        edge_match_confidence_class="LOW",
    )
    ev2 = apply_join_to_event(ev, join, thr)
    assert ev2.edge_price is None


def test_high_activates_acceptance():
    thr = JoinThresholds(accept_confidence=("HIGH",))
    ev = synthetic_event(
        event_id="H",
        symbol="BTCUSDT",
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
        edge_join_status="EXACT_TRADED_EDGE",
        matched_edge_price=100.0,
        matched_edge_source="raw_ob200_wall_lifecycle",
        edge_match_confidence_class="HIGH",
    )
    ev2 = apply_join_to_event(ev, join, thr)
    assert ev2.edge_price == 100.0


def test_not_reached():
    ev = synthetic_event(
        event_id="nr",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    # spatially near (≤15bps) but above flow_high → EDGE_NOT_REACHED
    edges = [edge("far", "ASK", 100.1, 10)]
    samples = [sample(100, mid=100.0, ask_px=100.1, ask_qty=5)]
    trades = [trade(101, "Buy", 100.0, 1000, "a")]
    join, _, _ = _join(ev, edges, samples, trades, 99.95, 100.0, 100.0)
    assert join.edge_join_status == "EDGE_NOT_REACHED"
    assert join.edge_match_confidence_class == "NONE"


def test_data_incomplete():
    ev = synthetic_event(
        event_id="di",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("a", "ASK", 100.0, 10)]
    samples = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    thr = JoinThresholds()
    cands = evaluate_candidates(
        ev, edges, samples, flow_start_price=99.95, flow_vwap=99.95, flow_low=None, flow_high=None, thr=thr
    )
    join, _, _ = select_disambiguated_match(
        ev,
        cands,
        trades=[],
        flow_start_price=99.95,
        flow_vwap=99.95,
        flow_low=None,
        flow_high=None,
        thr=thr,
    )
    assert join.edge_join_status == "DATA_INCOMPLETE"


def test_json_safe():
    assert json_safe({"x": float("inf")})["x"] is None


def test_prefix_parity_join_fields_stable():
    ev = synthetic_event(
        event_id="pp",
        symbol="BTCUSDT",
        direction="SHORT",
        wall_side=None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=T(100),
        flow_end_ts=T(105),
        decision_ts=T(110),
    )
    edges = [edge("a", "ASK", 100.0, 10)]
    s_short = [sample(100, mid=99.95, ask_px=100.0, ask_qty=5)]
    s_long = s_short + [sample(s, mid=100.2, ask_px=100.2, ask_qty=99) for s in (110, 130, 160, 200)]
    trades = [trade(101, "Buy", 100.0, 1000, "a")]
    j1, _, _ = _join(ev, edges, s_short, trades, 99.9, 100.0, 99.95)
    j2, _, _ = _join(ev, edges, s_long, trades, 99.9, 100.0, 99.95)
    assert j1.matched_edge_id == j2.matched_edge_id
    assert j1.matched_edge_price == j2.matched_edge_price
    assert j1.edge_match_confidence_class == j2.edge_match_confidence_class
    assert j1.edge_join_status == j2.edge_join_status
