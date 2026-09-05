"""Tests for liquidity_pool_entry_contract_batch_v1."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import (
    EXPECTED_V3_HASH,
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_RUNNING,
    TARGET_MECHANICAL_COMPLETE,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.capability import (
    assess_mechanical_unblind_separation,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.cases import (
    load_v3_freeze,
    smoke_selection,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.hashes import (
    FrozenInputHashMismatch,
    payload_sha256,
    verify_frozen_inputs,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.runner import (
    BatchError,
    cmd_plan,
    cmd_smoke,
    cmd_unblind_outcomes,
    cmd_verify,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.status import (
    atomic_write_json,
    case_dir,
    is_stale_running,
    mechanical_complete_valid,
    recover_stale_running,
    write_case_status,
)
from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import INTEGRATED_PATHS
from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import sha256_file


def test_v3_hash_verification():
    res = verify_frozen_inputs(OA, label="test")
    assert res["ok"] is True
    assert res["checks"]["v3_freeze"]["sha256"] == EXPECTED_V3_HASH


def test_smoke_selection_exact_exp01_exp03():
    frozen = load_v3_freeze(OA)
    smoke = smoke_selection(frozen)
    assert [c["expansion_case_id"] for c in smoke] == [SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID]
    assert smoke[0]["pool_side"] == "ASK"
    assert smoke[1]["pool_side"] == "BID"


def test_ask_bid_selection_deterministic():
    a = smoke_selection(load_v3_freeze(OA))
    b = smoke_selection(load_v3_freeze(OA))
    assert [c["expansion_case_id"] for c in a] == [c["expansion_case_id"] for c in b]


def test_capability_blocked_without_pipeline_change():
    cap = assess_mechanical_unblind_separation(OA)
    assert cap["separable_without_hashed_file_change"] is False
    assert cap["verdict_if_blocked"] == "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED"
    assert any("mechanical_only" in b or "unblind" in b for b in cap["blockers"])
    assert any("ASK" in b or "BID" in b for b in cap["blockers"])


def test_smoke_stops_with_separation_blocked():
    res = cmd_smoke(OA, mechanical_only=True)
    assert res["verdict"] == "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED"
    assert res["market_data_accessed"] is False
    assert res["outcomes_read"] is False
    assert res["cases_executed"] == []
    assert res["smoke_cases"] == [SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID]


def test_unblind_blocked_when_lt_24_complete():
    with pytest.raises(BatchError) as ei:
        cmd_unblind_outcomes(OA)
    assert ei.value.verdict in (
        "BATCH_UNBLIND_BLOCKED",
        "BATCH_UNBLIND_NOT_IMPLEMENTED_IN_THIS_TASK",
    )


def test_atomic_checkpoint(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text())["a"] == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_resume_skips_valid_complete(tmp_path, monkeypatch):
    # simulate under OA results via case_dir helpers — use real batch case dir for EXP_01
    cid = SMOKE_ASK_CASE_ID
    d = case_dir(OA, cid)
    d.mkdir(parents=True, exist_ok=True)
    mech = {
        "case_id": cid,
        "mechanical_verdict": "NO_TRADE",
        "entry_contract_version": "liquidity_pool_entry_contract/v1",
        "generated_at": "2026-08-31T00:00:00Z",
    }
    mech["mechanical_payload_sha256"] = payload_sha256(mech)
    atomic_write_json(d / "mechanical_verdict_pre_unblind.json", mech)
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_MECHANICAL_COMPLETE,
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
            "updated_at_utc": "2026-08-31T00:00:00Z",
        },
    )
    ok, reason = mechanical_complete_valid(OA, cid)
    assert ok is True, reason
    # cleanup to PENDING for other tests / smoke blocked state
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": "PENDING",
            "mechanical_payload_sha256": None,
            "error": "test_cleanup",
            "updated_at_utc": "2026-08-31T00:00:00Z",
        },
    )
    (d / "mechanical_verdict_pre_unblind.json").unlink(missing_ok=True)


def test_broken_payload_sha_not_skipped():
    cid = SMOKE_BID_CASE_ID
    d = case_dir(OA, cid)
    d.mkdir(parents=True, exist_ok=True)
    mech = {"case_id": cid, "mechanical_verdict": "X", "generated_at": "2026-08-31T00:00:00Z"}
    atomic_write_json(d / "mechanical_verdict_pre_unblind.json", mech)
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_MECHANICAL_COMPLETE,
            "mechanical_payload_sha256": "deadbeef",
            "updated_at_utc": "2026-08-31T00:00:00Z",
        },
    )
    ok, reason = mechanical_complete_valid(OA, cid)
    assert ok is False
    assert reason in ("payload_sha_missing", "payload_sha_mismatch", "status_sha_mismatch")
    write_case_status(
        OA,
        cid,
        {"case_id": cid, "status": "PENDING", "mechanical_payload_sha256": None, "updated_at_utc": "2026-08-31T00:00:00Z"},
    )
    (d / "mechanical_verdict_pre_unblind.json").unlink(missing_ok=True)


def test_stale_running_recovery():
    cid = SMOKE_ASK_CASE_ID
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_RUNNING,
            "started_at_utc": "2020-01-01T00:00:00Z",
            "updated_at_utc": "2020-01-01T00:00:00Z",
            "worker_pid": 1,
        },
    )
    assert is_stale_running(json.loads((case_dir(OA, cid) / "case_status.json").read_text()))
    st = recover_stale_running(OA, cid)
    assert st["status"] == "FAILED_RETRYABLE"
    write_case_status(
        OA,
        cid,
        {"case_id": cid, "status": "PENDING", "updated_at_utc": "2026-08-31T00:00:00Z"},
    )


def test_sigint_resumable_status_helper():
    from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.runner import _mark_interrupted

    cid = SMOKE_BID_CASE_ID
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_RUNNING,
            "started_at_utc": "2026-08-31T00:00:00Z",
            "worker_pid": os.getpid(),
            "updated_at_utc": "2026-08-31T00:00:00Z",
        },
    )
    _mark_interrupted(OA, cid)
    st = json.loads((case_dir(OA, cid) / "case_status.json").read_text())
    assert st["status"] == "FAILED_RETRYABLE"
    assert "interrupted" in (st.get("error") or "")
    write_case_status(
        OA,
        cid,
        {"case_id": cid, "status": "PENDING", "updated_at_utc": "2026-08-31T00:00:00Z"},
    )


def test_concurrency_is_one():
    from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import CONCURRENCY

    assert CONCURRENCY == 1


def test_hashed_contract_files_unchanged():
    contract = json.loads(
        (OA / "results/liquidity_pool_entry_contract_freeze_v1/entry_contract_v1.json").read_text()
    )
    for rel in INTEGRATED_PATHS.values():
        assert sha256_file(OA / rel) == contract["component_hashes"][rel]


def test_no_special_case_thresholds_in_batch_module():
    root = OA / "src/orderbook_analyse/liquidity_pool_entry_contract_batch_v1"
    blob = ""
    for p in root.glob("*.py"):
        blob += p.read_text(encoding="utf-8")
    assert "EXP_01" in blob  # selection constant only
    assert "min_target_distance" not in blob or "EXPECTED" in blob
    # must not redefine room threshold
    assert "min_target_distance_pct = 0." not in blob
    assert "EDGE_TOL_BPS" not in blob


def test_plan_no_market_data():
    plan = cmd_plan(OA)
    assert plan["ok"] is True
    assert plan["smoke_cases"][0]["expansion_case_id"] == SMOKE_ASK_CASE_ID
    assert plan["smoke_cases"][1]["expansion_case_id"] == SMOKE_BID_CASE_ID


def test_verify_cmd():
    res = cmd_verify(OA)
    assert res["ok"] is True
