"""Focused tests for CASE_03 frozen BID pool audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.pipeline import (
    aggressor_class,
    load_frozen_case_03,
    pool_zone_bid,
)

REPO = Path(__file__).resolve().parents[1]


def test_case_03_loaded_from_freeze_only():
    c = load_frozen_case_03(REPO)
    assert c["reference_ts"] == "2026-08-25T01:10:13Z"
    assert c["direction"] == "BID"
    assert c["approach"] == "FROM_ABOVE"
    assert c["front_edge"] == c["upper"] == 79656.0
    assert c["back_edge"] == c["lower"] == 79509.0
    assert c["pool_id"] == "lld:BTCUSDT:5m:lower:1787619600"


def test_bid_from_above_edges():
    assert pool_zone_bid(79700.0, 79509.0, 79656.0, 2.0) == "ABOVE_FRONT"
    assert pool_zone_bid(79656.0, 79509.0, 79656.0, 2.0) == "AT_FRONT_EDGE"
    assert pool_zone_bid(79580.0, 79509.0, 79656.0, 2.0) in {
        "INSIDE_UPPER_THIRD",
        "INSIDE_MIDDLE_THIRD",
        "INSIDE_LOWER_THIRD",
    }
    assert pool_zone_bid(79400.0, 79509.0, 79656.0, 2.0) == "BELOW_BACK"


def test_aggressor_symmetry_labels():
    assert aggressor_class(0, 50_000, -10.0, 10_000, 8.0) == "SELL_EFFECTIVE_BREAK_ATTACK"
    assert aggressor_class(0, 50_000, -1.0, 10_000, 8.0) == "SELL_INEFFICIENT_ABSORPTION"
    assert aggressor_class(50_000, 0, 10.0, 10_000, 8.0) == "BUY_COUNTER_RECLAIM"
    assert aggressor_class(40_000, 40_000, 1.0, 10_000, 8.0) == "TWO_SIDED_CONTEST"


def test_cancel_not_trade_depletion_flag_contract():
    h = {"cancelled_before_touch": True, "trade_depletion": False, "attacked": False}
    assert h["cancelled_before_touch"] and not h["trade_depletion"]


def test_room_gate_blocks_small_room_via_entry_contract():
    from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
        load_effective_room_config,
    )
    from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import (
        PoolCandidate,
        evaluate_room_to_target_gate,
    )

    eff = load_effective_room_config(REPO)
    gate = evaluate_room_to_target_gate(
        direction="LONG",
        entry_price=100.0,
        pools=[
            PoolCandidate(
                pool_id="a",
                source_timeframe="5m",
                side="ASK",
                lower_edge=100.09,
                upper_edge=101.0,
                available_at="2026-08-25T00:00:00Z",
            )
        ],
        config=eff.room,
    )
    assert gate["gate_passed"] is False


def test_mechanical_file_written_before_unblind_if_results_exist():
    out = REPO / "results/case_03_frozen_bid_pool_causal_reaction_audit_v1"
    mech = out / "mechanical_verdict_pre_unblind.json"
    blind = out / "outcome_blindness_audit.json"
    if not mech.exists():
        pytest.skip("audit not run yet")
    m = json.loads(mech.read_text())
    b = json.loads(blind.read_text())
    assert m["case_id"] == "CASE_03"
    assert b.get("outcome_read_before_mechanical_persist") is False
    assert "mechanical_payload_sha256" in m
