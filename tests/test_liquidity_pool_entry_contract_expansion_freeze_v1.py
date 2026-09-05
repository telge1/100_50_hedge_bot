"""Tests for liquidity pool entry contract expansion freeze v1."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze import (
    TARGET_COUNT,
    ExpansionFreezeError,
    build_expansion_freeze,
    selection_hash,
    verify_expansion_freeze,
)

OUT = OA / "results" / "liquidity_pool_entry_contract_expansion_freeze_v1"


@pytest.fixture(scope="module")
def built():
    return build_expansion_freeze(OA)


def test_build_produces_24_cases(built):
    assert built["verdict"] == "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN"
    assert built["selected_count"] == TARGET_COUNT
    frozen = json.loads((OUT / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    assert len(frozen["ordered_cases"]) == TARGET_COUNT


def test_verify_success(built):
    res = verify_expansion_freeze(OA)
    assert res["ok"] is True
    assert res["verdict"] == "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN"


def test_mutation_non_zero(built):
    res = verify_expansion_freeze(OA, mutate=True)
    assert res["mutation_detected"] is True
    assert res["mutated_sha256"] != res["original_sha256"]


def test_ask_bid_strata(built):
    frozen = json.loads((OUT / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    ask = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "ASK")
    bid = sum(1 for c in frozen["ordered_cases"] if c["pool_side"] == "BID")
    assert ask == 12
    assert bid == 12


def test_no_outcome_fields(built):
    audit = json.loads((OUT / "outcome_blindness_audit.json").read_text(encoding="utf-8"))
    assert audit["outcome_fields_read_for_selection"] is False
    frozen = json.loads((OUT / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    forbidden = ("outcome", "verdict", "pnl", "mfe", "mae", "return")
    for c in frozen["ordered_cases"]:
        for k in c:
            assert not any(x in k.lower() for x in forbidden)


def test_selection_hash(built):
    frozen = json.loads((OUT / "frozen_expansion_cases_v1.json").read_text(encoding="utf-8"))
    src_sha = frozen["source_manifest"]["sha256"]
    for c in frozen["ordered_cases"]:
        expected = selection_hash(
            source_sha256=src_sha,
            candidate_id=c["source_candidate_id"],
            pool_id=c["pool_id"],
            reference_ts=c["reference_ts"],
        )
        assert c["deterministic_selection_hash"] == expected


def test_verify_script_exit_zero(built):
    proc = subprocess.run(
        [sys.executable, str(OA / "scripts" / "verify_liquidity_pool_entry_contract_expansion_freeze_v1.py")],
        cwd=OA,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_second_run_identical_hash(built):
    a = built["expansion_freeze_bundle_sha256"]
    b = build_expansion_freeze(OA)["expansion_freeze_bundle_sha256"]
    assert a == b
