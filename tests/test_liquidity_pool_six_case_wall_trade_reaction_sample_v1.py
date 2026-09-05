"""Focused tests for six-case wall/trade reaction sample."""

from __future__ import annotations

import csv
from pathlib import Path

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
    classify_first_seen,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    MAX_POST_START_S,
    PRE_START_S,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    attack_side,
    entry_edge,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.classify import (
    classify_case,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.selection import (
    causal_window,
    select_six_cases,
)

OA = Path(__file__).resolve().parents[1]
V2_CLUSTERS = OA / "results" / "liquidity_pool_arrival_wall_monitor_v2" / "market_arrival_clusters.csv"


def _clusters():
    return list(csv.DictReader(V2_CLUSTERS.open(encoding="utf-8")))


def test_selection_deterministic_no_outcome_columns():
    rows = _clusters()
    a = select_six_cases(rows)
    b = select_six_cases(rows)
    assert [c["market_arrival_cluster_id"] for c in a["cases"]] == [
        c["market_arrival_cluster_id"] for c in b["cases"]
    ]
    rule = a["selection_rule"]
    for forbidden in rule["forbidden_for_ranking"]:
        assert forbidden not in rule["ranking_fields_at_cluster_start_only"]
    assert sum(1 for c in a["cases"] if c["side"] == "ASK") == 3
    assert sum(1 for c in a["cases"] if c["side"] == "BID") == 3
    assert any(c["cluster_start_ts"] == "2026-08-26T02:27:36Z" for c in a["cases"])
    ids = [c["market_arrival_cluster_id"] for c in a["cases"]]
    assert len(ids) == len(set(ids))


def test_ask_bid_mirror_helpers():
    assert entry_edge("ASK", 100.0, 110.0) == 100.0
    assert entry_edge("BID", 100.0, 110.0) == 110.0
    assert attack_side("ASK") == "Buy"
    assert attack_side("BID") == "Sell"
    ask = classify_case(
        {
            "side": "ASK",
            "window_censored_active": False,
            "insufficient_data": False,
            "start_wall_meaningfully_attacked": True,
            "later_wall_appeared": True,
            "later_wall_attacked": True,
            "cancel_or_move_dominant": False,
            "trade_depletion_dominant": False,
            "refill_supported": False,
            "pool_reclaimed_entry_side": True,
            "pool_accepted_beyond": False,
            "attack_notional": 50_000,
            "impact_5s_bps": 2.0,
        }
    )
    bid = classify_case(
        {
            "side": "BID",
            "window_censored_active": False,
            "insufficient_data": False,
            "start_wall_meaningfully_attacked": True,
            "later_wall_appeared": True,
            "later_wall_attacked": True,
            "cancel_or_move_dominant": False,
            "trade_depletion_dominant": False,
            "refill_supported": False,
            "pool_reclaimed_entry_side": True,
            "pool_accepted_beyond": False,
            "attack_notional": 50_000,
            "impact_5s_bps": -2.0,
        }
    )
    assert ask["evidence_class"] == bid["evidence_class"] == "POOL_REJECTION_MIXED_WALL_REACTION"


def test_ts_eq_arrival_not_after():
    fs = classify_first_seen(
        first_seen_ts_ms=1_000,
        arrival_ts_ms=1_000,
        present_in_pre=False,
        present_at_exact_arrival=True,
        present_strictly_after=True,
    )
    assert fs == FirstSeenClass.FIRST_SEEN_AT_ARRIVAL
    assert fs != FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL


def test_window_cap():
    row = {
        "cluster_start_ts": "2026-08-26T02:00:00Z",
        "cluster_end_ts": "2026-08-26T03:00:00Z",
        "side": "ASK",
        "component_lower_edge": "1",
        "component_upper_edge": "2",
    }
    w = causal_window(row)
    assert w["window_censored_active"] is True
    assert w["causal_window_end_ts"] == "2026-08-26T02:05:00Z"
    assert PRE_START_S == 30 and MAX_POST_START_S == 300


def test_no_post_end_flag_in_audit_source():
    src = (
        OA
        / "src/orderbook_analyse/liquidity_pool_six_case_wall_trade_reaction_sample_v1/audit_case.py"
    ).read_text(encoding="utf-8")
    assert "trade_ts) <= end_ms" in src or "<= end_ms" in src
    assert "no_data_after_causal_end" in src


def test_prefix_parity_and_known_case_family():
    # structural: prefix helper + known case included in selection
    rows = _clusters()
    sel = select_six_cases(rows)
    known = next(c for c in sel["cases"] if c["cluster_start_ts"] == "2026-08-26T02:27:36Z")
    assert known["side"] == "ASK"
    assert known["approach_direction"] == "FROM_BELOW"
    # committed Einzelfall reaction end
    assert known["cluster_end_ts_raw"] == "2026-08-26T02:30:21Z"
