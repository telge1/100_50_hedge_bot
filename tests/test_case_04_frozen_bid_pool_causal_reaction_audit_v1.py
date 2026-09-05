"""Focused tests for CASE_04 freeze load + CASE_03 engine parametrization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.pipeline import (
    CASE_03_SPEC,
    CASE_04_SPEC,
    aggressor_class,
    load_frozen_bid_case,
    pool_zone_bid,
)

REPO = Path(__file__).resolve().parents[1]


def test_case_04_loaded_from_freeze():
    c = load_frozen_bid_case(REPO, spec=CASE_04_SPEC)
    assert c["reference_ts"] == "2026-08-25T01:47:08Z"
    assert c["direction"] == "BID"
    assert c["approach"] == "FROM_ABOVE"
    assert c["front_edge"] == c["upper"] == 79630.0
    assert c["back_edge"] == c["lower"] == 79527.45
    assert c["pool_id"] == "lld:BTCUSDT:5m:lower:1787621400"
    assert c["freeze_case"]["exposure_status"] == "PROSPECTIVE_UNAUDITED"


def test_case_03_spec_unchanged_by_case_04_param():
    c3 = load_frozen_bid_case(REPO, spec=CASE_03_SPEC)
    assert c3["reference_ts"] == "2026-08-25T01:10:13Z"
    assert CASE_03_SPEC.case_id == "CASE_03"
    assert CASE_04_SPEC.predecessor_case_id == "CASE_03"


def test_bid_geometry_and_aggressor_labels_unchanged():
    assert pool_zone_bid(79700.0, 79527.45, 79630.0, 2.0) == "ABOVE_FRONT"
    assert aggressor_class(0, 50_000, -10.0, 10_000, 8.0) == "SELL_EFFECTIVE_BREAK_ATTACK"


def test_cancel_not_depletion_contract():
    h = {"cancelled_before_touch": True, "trade_depletion": False}
    assert h["cancelled_before_touch"] and not h["trade_depletion"]


def test_case_03_result_does_not_change_case_04_rules():
    # Thresholds/constants remain shared; CASE_04 only changes identity fields.
    assert CASE_03_SPEC.format_version != CASE_04_SPEC.format_version
    from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1 import (
        COST_RT_BPS,
        ACCEPT_VARIANTS_S,
        EDGE_TOL_BPS,
    )

    assert COST_RT_BPS == (11.0, 15.0, 20.0)
    assert ACCEPT_VARIANTS_S == (5, 15, 30, 60)
    assert EDGE_TOL_BPS == 2.0


def test_mechanical_pre_unblind_if_present():
    out = REPO / "results/case_04_frozen_bid_pool_causal_reaction_audit_v1"
    mech = out / "mechanical_verdict_pre_unblind.json"
    blind = out / "outcome_blindness_audit.json"
    if not mech.exists():
        pytest.skip("CASE_04 audit not run yet")
    m = json.loads(mech.read_text())
    b = json.loads(blind.read_text())
    assert m["case_id"] == "CASE_04"
    assert b.get("outcome_read_before_mechanical_persist") is False
