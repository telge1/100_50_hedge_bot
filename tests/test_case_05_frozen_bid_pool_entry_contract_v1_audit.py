"""Focused tests for CASE_05 entry-contract audit runner (no frozen-file mutation)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.pipeline import (
    BidCaseAuditSpec,
)

REPO = Path(__file__).resolve().parents[1]

EXPECTED_CASE_SEQ_SHA = "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
EXPECTED_ENTRY_CONTRACT_SHA = "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
EXPECTED_CONFIG_SHA = "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"


def test_case_05_spec_declarative_without_pipeline_edit():
    spec = BidCaseAuditSpec(
        case_id="CASE_05",
        predecessor_case_id="CASE_04",
        format_version="case_05_frozen_bid_pool_entry_contract_v1_audit/v1",
        results_dirname="case_05_frozen_bid_pool_entry_contract_v1_audit",
        manual_review_name="CASE_05_MANUAL_REVIEW.md",
        deep_audit_result_glob="case_05_frozen_bid_pool_entry_contract_v1_audit",
    )
    assert spec.case_id == "CASE_05"
    assert spec.prefix_fail == "CASE_05_PREFIX_PARITY_FAILURE"
    assert spec.previously_exposed == "CASE_05_PREVIOUSLY_EXPOSED"
    assert spec.data_blocked == "CASE_05_DATA_BLOCKED"


def test_frozen_hashes_match_expected():
    from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import verify_freeze
    from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import (
        verify_entry_contract_freeze,
    )

    seq = verify_freeze(REPO, REPO / "results/liquidity_pool_case_sequence_freeze_v1")
    assert seq["freeze_bundle_sha256"] == EXPECTED_CASE_SEQ_SHA
    ec = verify_entry_contract_freeze(REPO)
    assert ec["entry_contract_freeze_sha256"] == EXPECTED_ENTRY_CONTRACT_SHA
    cfg = REPO / "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == EXPECTED_CONFIG_SHA


def test_case_05_loaded_from_freeze_only():
    frozen = json.loads(
        (REPO / "results/liquidity_pool_case_sequence_freeze_v1/frozen_case_sequence_v1.json").read_text()
    )
    cases = [c for c in frozen["ordered_cases"] if c["case_id"] == "CASE_05"]
    assert len(cases) == 1
    c = cases[0]
    assert c["reference_ts"] == "2026-08-25T03:26:08Z"
    assert c["direction"] == "BID"
    assert c["approach"] == "FROM_ABOVE"
    assert c["exposure_status"] == "PROSPECTIVE_UNAUDITED"
    assert frozen["next_after"]["CASE_04"] == "CASE_05"
    assert c["component_lower_edge"] == 80416.85
    assert c["component_upper_edge"] == 80561.3


def test_pipeline_not_mutated_for_case_05_constant():
    """CASE_05_SPEC must not be required inside frozen pipeline.py."""
    src = (
        REPO
        / "src/orderbook_analyse/case_03_frozen_bid_pool_causal_reaction_audit_v1/pipeline.py"
    ).read_text(encoding="utf-8")
    assert "CASE_05_SPEC" not in src
    assert "class BidCaseAuditSpec" in src
    assert "def run_audit" in src


def test_audit_outputs_if_present():
    out = REPO / "results/case_05_frozen_bid_pool_entry_contract_v1_audit"
    mech = out / "mechanical_verdict_pre_unblind.json"
    blind = out / "outcome_blindness_audit.json"
    if not mech.exists():
        pytest.skip("CASE_05 audit not run yet")
    m = json.loads(mech.read_text())
    b = json.loads(blind.read_text())
    assert m["case_id"] == "CASE_05"
    assert m.get("entry_contract_version") == "liquidity_pool_entry_contract/v1"
    assert m.get("room_gate_config_sha256") == EXPECTED_CONFIG_SHA
    assert b.get("outcome_read_before_mechanical_persist") is False
    assert "mechanical_payload_sha256" in m
    for key in (
        "microstructure_gate_passed",
        "room_gate_passed",
        "mechanical_trade_verdict",
        "min_target_distance_pct",
    ):
        assert key in m
