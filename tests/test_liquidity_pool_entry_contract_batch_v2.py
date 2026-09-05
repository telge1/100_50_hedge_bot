"""Tests for liquidity_pool_entry_contract_batch_v2."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    CONCURRENCY,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V2_HASH,
    EXPECTED_V4_HASH,
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_RUNNING,
    TARGET_MECHANICAL_COMPLETE,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.cases import (
    case_spec_from_v4_row,
    load_v4_freeze,
    smoke_selection,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import (
    FrozenInputHashMismatch,
    payload_sha256,
    verify_frozen_inputs,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner import (
    BatchError,
    cmd_plan,
    cmd_smoke,
    cmd_unblind_outcomes,
    cmd_verify,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
    atomic_write_json,
    case_dir,
    is_stale_running,
    mechanical_complete_valid,
    recover_stale_running,
    write_case_status,
)
from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import sha256_file


def test_v2_v4_hash_verification():
    res = verify_frozen_inputs(OA, label="test")
    assert res["ok"] is True
    assert res["checks"]["entry_contract_v2"]["sha256"] == EXPECTED_V2_HASH
    assert res["checks"]["expansion_v4"]["sha256"] == EXPECTED_V4_HASH
    assert res["checks"]["strategy_config"]["sha256"] == EXPECTED_STRATEGY_CONFIG_HASH


def test_smoke_selection_exact_exp01_exp03():
    frozen = load_v4_freeze(OA)
    smoke = smoke_selection(frozen)
    assert [c["expansion_case_id"] for c in smoke] == [SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID]
    assert smoke[0]["pool_side"] == "ASK"
    assert smoke[0]["approach"] == "FROM_BELOW"
    assert smoke[1]["pool_side"] == "BID"
    assert smoke[1]["approach"] == "FROM_ABOVE"


def test_casespec_from_v4():
    frozen = load_v4_freeze(OA)
    smoke = smoke_selection(frozen)
    ask = case_spec_from_v4_row(smoke[0])
    bid = case_spec_from_v4_row(smoke[1])
    assert ask.expansion_case_id == "EXP_01"
    assert ask.pool_side == "ASK"
    assert bid.expansion_case_id == "EXP_03"
    assert bid.pool_side == "BID"


def test_ask_not_loaded_as_bid():
    frozen = load_v4_freeze(OA)
    ask = case_spec_from_v4_row(smoke_selection(frozen)[0])
    assert ask.pool_side != "BID"
    assert ask.approach == "FROM_BELOW"


def test_bid_loaded_correctly():
    frozen = load_v4_freeze(OA)
    bid = case_spec_from_v4_row(smoke_selection(frozen)[1])
    assert bid.pool_side == "BID"
    assert bid.approach == "FROM_ABOVE"


def test_concurrency_is_one():
    assert CONCURRENCY == 1


def test_atomic_persist(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text())["a"] == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_resume_payload_sha(tmp_path, monkeypatch):
    cid = SMOKE_ASK_CASE_ID
    d = case_dir(OA, cid)
    d.mkdir(parents=True, exist_ok=True)
    mech = {
        "case_id": cid,
        "mechanical_verdict": "NO_TRADE",
        "entry_contract_version": "liquidity_pool_entry_contract/v2",
        "generated_at": "2026-08-31T00:00:00Z",
    }
    mech["mechanical_payload_sha256"] = payload_sha256(mech)
    atomic_write_json(d / "mechanical_verdict_pre_unblind.json", mech)
    (d / "mechanical_complete.marker").write_text(mech["mechanical_payload_sha256"] + "\n")
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_MECHANICAL_COMPLETE,
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        },
    )
    ok, reason = mechanical_complete_valid(OA, cid)
    assert ok, reason
    # corrupt sha → invalid
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_MECHANICAL_COMPLETE,
            "mechanical_payload_sha256": "deadbeef",
        },
    )
    ok2, reason2 = mechanical_complete_valid(OA, cid)
    assert not ok2
    assert reason2 == "status_sha_mismatch"


def test_stale_running_recoverable():
    cid = SMOKE_BID_CASE_ID
    case_dir(OA, cid).mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(seconds=4000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_RUNNING,
            "started_at_utc": old,
            "worker_pid": 1,
        },
    )
    st = recover_stale_running(OA, cid)
    assert st["status"] == "FAILED_RETRYABLE"
    assert "stale_RUNNING" in (st.get("error") or "")


def test_tmp_not_counted_complete():
    cid = "EXP_02"
    d = case_dir(OA, cid)
    d.mkdir(parents=True, exist_ok=True)
    mech = {"case_id": cid, "generated_at": "2026-08-31T00:00:00Z"}
    mech["mechanical_payload_sha256"] = payload_sha256(mech)
    atomic_write_json(d / "mechanical_verdict_pre_unblind.json", mech)
    (d / "mechanical_complete.marker").write_text(mech["mechanical_payload_sha256"] + "\n")
    (d / "mechanical_verdict_pre_unblind.json.tmp").write_text("partial")
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_MECHANICAL_COMPLETE,
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        },
    )
    ok, reason = mechanical_complete_valid(OA, cid)
    assert not ok
    assert reason == "tmp_artifacts_present"
    (d / "mechanical_verdict_pre_unblind.json.tmp").unlink(missing_ok=True)
    (d / "mechanical_verdict_pre_unblind.json").unlink(missing_ok=True)
    (d / "mechanical_complete.marker").unlink(missing_ok=True)
    write_case_status(
        OA,
        cid,
        {"case_id": cid, "status": "PENDING", "mechanical_payload_sha256": None, "error": None},
    )


def test_unblind_blocked_at_lt_24():
    with pytest.raises(BatchError) as ei:
        cmd_unblind_outcomes(OA)
    assert ei.value.verdict == "MECHANICAL_UNBLIND_SEPARATION_FAILURE"
    assert "mechanical_complete_count" in str(ei.value)


def test_hashed_v2_files_unchanged():
    contract = json.loads(
        (OA / "results/liquidity_pool_entry_contract_freeze_v2/entry_contract_v2.json").read_text()
    )
    for rel, expected in contract["component_hashes"].items():
        if rel == "effective_strategy_yaml_copy":
            path = OA / "results/liquidity_pool_entry_contract_freeze_v2/effective_strategy_config.yaml"
        else:
            path = OA / rel
        assert sha256_file(path) == expected, rel


def test_plan_no_market_data():
    plan = cmd_plan(OA)
    assert plan["ok"] is True
    assert plan["smoke_cases"][0]["expansion_case_id"] == SMOKE_ASK_CASE_ID
    assert plan["smoke_cases"][1]["expansion_case_id"] == SMOKE_BID_CASE_ID
    assert plan["note"].startswith("No market data")


def test_verify_cmd():
    res = cmd_verify(OA)
    assert res["ok"] is True
    assert res["mechanical_executed_count_before_v4"] == 0
    assert res["outcome_read_count_before_v4"] == 0


def test_smoke_uses_mechanical_api_not_unblind():
    """Smoke path must call run_mechanical_audit and never run_outcome_unblind."""
    calls = {"mech": 0, "unblind": 0}

    def fake_mech(spec, frozen_inputs, output_dir, repo_root=None):
        calls["mech"] += 1
        assert "evidence" in frozen_inputs
        assert frozen_inputs.get("outcome_source") is None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        mech = {
            "case_id": spec.expansion_case_id,
            "mechanical_verdict": "AMBIGUOUS_POOL_CONTEST_NO_TRADE",
            "reaction": "BREAK_THEN_RECLAIM_CONTEST",
            "front_edge": 1.0,
            "back_edge": 0.0,
            "long_branch": {},
            "short_branch": {},
            "room_gate": {},
            "entry_price": None,
            "first_available_ts": None,
            "prefix_parity": {"prefix_status": "EXACT_PREFIX_PARITY"},
            "generated_at": "2026-08-31T00:00:00Z",
        }
        from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
            atomic_write_json,
            atomic_write_text,
            payload_sha256 as psha,
        )

        mech["mechanical_payload_sha256"] = psha(mech)
        atomic_write_json(output_dir / "mechanical_verdict_pre_unblind.json", mech)
        atomic_write_text(
            output_dir / "mechanical_complete.marker", mech["mechanical_payload_sha256"] + "\n"
        )
        atomic_write_json(
            output_dir / "mechanical_blindness_audit.json",
            {"outcomes_read": False, "unblind_invoked": False},
        )
        return {
            "ok": True,
            "verdict": mech["mechanical_verdict"],
            "prefix_status": "EXACT_PREFIX_PARITY",
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        }

    def fake_unblind(*a, **k):
        calls["unblind"] += 1
        raise AssertionError("unblind must not be called")

    fake_bundle = {
        "evidence": {
            "seen_inside": True,
            "arrival_present": True,
            "defense_ok": False,
            "breakout_ok": False,
            "breakout_contested": True,
            "defense_entry": None,
            "breakout_entry": None,
            "defense_first_ts": None,
            "breakout_first_ts": None,
            "attack_eff_count": 0,
            "counter_count": 0,
            "two_sided_count": 1,
        },
        "pool_geometry_rows": [],
        "diagnostics": {"reference_mid": {"ok": True, "mid": 1.0}},
        "query_log": [],
        "market_data_loaded": True,
        "outcomes_read": False,
    }

    with (
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.run_mechanical_audit",
            side_effect=fake_mech,
        ),
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.build_market_evidence_bundle",
            return_value=fake_bundle,
        ),
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_v2.unblind.run_outcome_unblind",
            side_effect=fake_unblind,
        ),
    ):
        # Reset prior status for smoke cases so they re-run under mocks
        for cid in (SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID):
            d = case_dir(OA, cid)
            d.mkdir(parents=True, exist_ok=True)
            for p in d.glob("*"):
                if p.name.endswith(".tmp"):
                    p.unlink()
            write_case_status(
                OA,
                cid,
                {"case_id": cid, "status": "PENDING", "mechanical_payload_sha256": None},
            )
            for name in (
                "mechanical_verdict_pre_unblind.json",
                "mechanical_complete.marker",
            ):
                (d / name).unlink(missing_ok=True)

        res = cmd_smoke(OA, mechanical_only=True)
        assert res["verdict"] == "EXPANSION_BATCH_V2_SMOKE_MECHANICAL_COMPLETE"
        assert calls["mech"] == 2
        assert calls["unblind"] == 0
        assert res["outcome_read_count"] == 0


def test_prefix_failure_stops():
    from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import MechanicalAuditError

    def boom(*a, **k):
        raise MechanicalAuditError("SMOKE_PREFIX_PARITY_FAILURE", "x")

    fake_bundle = {
        "evidence": {
            "seen_inside": True,
            "arrival_present": True,
            "defense_ok": False,
            "breakout_ok": False,
            "breakout_contested": False,
            "defense_entry": None,
            "breakout_entry": None,
            "defense_first_ts": None,
            "breakout_first_ts": None,
            "attack_eff_count": 0,
            "counter_count": 0,
            "two_sided_count": 0,
        },
        "pool_geometry_rows": [],
        "diagnostics": {"reference_mid": {"ok": True, "mid": 1.0}},
        "query_log": [],
        "market_data_loaded": True,
        "outcomes_read": False,
    }
    for cid in (SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID):
        write_case_status(
            OA, cid, {"case_id": cid, "status": "PENDING", "mechanical_payload_sha256": None}
        )
        d = case_dir(OA, cid)
        (d / "mechanical_verdict_pre_unblind.json").unlink(missing_ok=True)
        (d / "mechanical_complete.marker").unlink(missing_ok=True)

    with (
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.run_mechanical_audit",
            side_effect=boom,
        ),
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.build_market_evidence_bundle",
            return_value=fake_bundle,
        ),
        pytest.raises(BatchError) as ei,
    ):
        cmd_smoke(OA, mechanical_only=True)
    assert ei.value.verdict == "SMOKE_PREFIX_PARITY_FAILURE"


def test_hash_failure_stops():
    calls = {"n": 0}

    def fake_verify(repo_root, *, label="check"):
        calls["n"] += 1
        if label.startswith("after_"):
            raise FrozenInputHashMismatch("forced")
        return {"ok": True, "label": label, "checks": {}}

    fake_bundle = {
        "evidence": {
            "seen_inside": True,
            "arrival_present": True,
            "defense_ok": False,
            "breakout_ok": False,
            "breakout_contested": True,
            "defense_entry": None,
            "breakout_entry": None,
            "defense_first_ts": None,
            "breakout_first_ts": None,
            "attack_eff_count": 0,
            "counter_count": 0,
            "two_sided_count": 1,
        },
        "pool_geometry_rows": [],
        "diagnostics": {"reference_mid": {"ok": True, "mid": 1.0}},
        "query_log": [],
        "market_data_loaded": True,
        "outcomes_read": False,
    }

    def fake_mech(spec, frozen_inputs, output_dir, repo_root=None):
        from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
            atomic_write_json,
            atomic_write_text,
            payload_sha256 as psha,
        )

        output_dir = Path(output_dir)
        mech = {
            "case_id": spec.expansion_case_id,
            "mechanical_verdict": "NO_TRADE",
            "generated_at": "2026-08-31T00:00:00Z",
            "long_branch": {},
            "short_branch": {},
            "front_edge": 1,
            "back_edge": 0,
        }
        mech["mechanical_payload_sha256"] = psha(mech)
        atomic_write_json(output_dir / "mechanical_verdict_pre_unblind.json", mech)
        atomic_write_text(
            output_dir / "mechanical_complete.marker", mech["mechanical_payload_sha256"] + "\n"
        )
        atomic_write_json(output_dir / "mechanical_blindness_audit.json", {})
        return {
            "ok": True,
            "verdict": "NO_TRADE",
            "prefix_status": "EXACT_PREFIX_PARITY",
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        }

    for cid in (SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID):
        write_case_status(
            OA, cid, {"case_id": cid, "status": "PENDING", "mechanical_payload_sha256": None}
        )
        d = case_dir(OA, cid)
        (d / "mechanical_verdict_pre_unblind.json").unlink(missing_ok=True)
        (d / "mechanical_complete.marker").unlink(missing_ok=True)

    with (
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.verify_frozen_inputs",
            side_effect=fake_verify,
        ),
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.build_market_evidence_bundle",
            return_value=fake_bundle,
        ),
        patch(
            "orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner.run_mechanical_audit",
            side_effect=fake_mech,
        ),
        pytest.raises(BatchError) as ei,
    ):
        cmd_smoke(OA, mechanical_only=True)
    assert ei.value.verdict == "FROZEN_INPUT_HASH_MISMATCH"
