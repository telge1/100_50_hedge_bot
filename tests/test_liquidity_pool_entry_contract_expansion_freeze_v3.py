"""Tests for expansion freeze v3 audit-window independence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze_v3 import (
    EXPECTED_V1_HASH,
    EXPECTED_V2_HASH,
    build_expansion_freeze_v3,
    pairwise_independence,
    verify_expansion_freeze_v3,
)

V1 = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v1"
V2 = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v2"
V3 = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v3"


@pytest.fixture(scope="module")
def built():
    return build_expansion_freeze_v3(OA)


def test_verdict_and_count(built):
    assert built["verdict"] == "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V3"
    assert built["selected_count"] == 24


def test_ask_bid_12_12(built):
    frozen = json.loads((V3 / "frozen_expansion_cases_v3.json").read_text(encoding="utf-8"))
    ask = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "ASK")
    bid = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "BID")
    assert ask == 12 and bid == 12


def test_276_pairs_independent(built):
    frozen = json.loads((V3 / "frozen_expansion_cases_v3.json").read_text(encoding="utf-8"))
    pairs, viol = pairwise_independence(frozen["ordered_cases"])
    assert len(pairs) == 276
    assert viol == []
    assert not any(p["audit_window_overlap_same_symbol"] for p in pairs)
    assert not any(p["violates_le_300s"] for p in pairs)
    assert not any(p["violates_event_family"] for p in pairs)


def test_predecessors_unchanged(built):
    v1 = json.loads((V1 / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    v2 = json.loads((V2 / "frozen_expansion_cases_v2.json").read_text(encoding="utf-8"))
    assert v1["expansion_freeze_bundle_sha256"] == EXPECTED_V1_HASH
    assert v2["expansion_freeze_bundle_sha256"] == EXPECTED_V2_HASH


def test_verify_and_second_hash(built):
    a = built["expansion_freeze_bundle_sha256"]
    res = verify_expansion_freeze_v3(OA)
    assert res["ok"] is True
    assert res["expansion_freeze_bundle_sha256"] == a


def test_mutation(built):
    res = verify_expansion_freeze_v3(OA, mutate=True)
    assert res["mutation_detected"] is True
    assert res["mutated_sha256"] != res["original_sha256"]


def test_no_outcome_fields(built):
    audit = json.loads((V3 / "outcome_blindness_audit.json").read_text(encoding="utf-8"))
    assert audit["outcome_fields_read_for_selection"] is False
    assert audit["micro_room_used_for_selection"] is False
