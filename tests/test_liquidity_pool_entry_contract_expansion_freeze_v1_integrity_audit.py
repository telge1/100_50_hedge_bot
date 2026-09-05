"""Tests for expansion freeze v1 integrity audit and v2 correction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.integrity_audit import (
    EXPECTED_V1_HASH,
    pairwise_audit,
    run_integrity_audit,
    verify_expansion_freeze_v2,
)

V1 = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v1"
V2 = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v2"
AUDIT = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v1_integrity_audit"


@pytest.fixture(scope="module")
def audited():
    return run_integrity_audit(OA)


def test_v1_dedup_failure_verdict(audited):
    assert audited["verdict"] == "EXPANSION_FREEZE_V1_DEDUP_INTEGRITY_FAILURE"
    assert audited["violation_count"] == 2
    assert audited["v1_hash"] == EXPECTED_V1_HASH


def test_v1_not_overwritten(audited):
    frozen = json.loads((V1 / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    assert frozen["expansion_freeze_bundle_sha256"] == EXPECTED_V1_HASH


def test_v2_built_and_clean(audited):
    assert "v2" in audited
    assert audited["v2"]["selected_count"] == 24
    frozen = json.loads((V2 / "frozen_expansion_cases_v2.json").read_text(encoding="utf-8"))
    pairs, viol = pairwise_audit(frozen["ordered_cases"])
    assert len(pairs) == 276
    assert viol == []
    ask = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "ASK")
    bid = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "BID")
    assert ask == 12 and bid == 12


def test_verify_v2(audited):
    res = verify_expansion_freeze_v2(OA)
    assert res["ok"] is True
    assert res["violations"] == 0


def test_mutation_v2(audited):
    res = verify_expansion_freeze_v2(OA, mutate=True)
    assert res["mutation_detected"] is True
    assert res["mutated_sha256"] != res["original_sha256"]


def test_audit_outputs_exist(audited):
    for name in (
        "integrity_audit.json",
        "pairwise_conflicts.csv",
        "dedup_violations.csv",
        "v1_bug_trace.json",
        "audit_window_analysis.json",
        "INTEGRITY_AUDIT_REPORT.md",
    ):
        assert (AUDIT / name).is_file()
