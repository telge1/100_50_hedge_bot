"""Integrity tests for liquidity_pool_case_sequence_freeze_v1."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    FreezeError,
    build_frozen_sequence,
    canonical_json_bytes,
    compute_hashes,
    find_unique_six_case_source,
    hashable_sequence_payload,
    sha256_bytes,
    validate_frozen,
    verify_freeze,
    write_freeze,
)

REPO = Path(__file__).resolve().parents[1]


def test_unique_source():
    p = find_unique_six_case_source(REPO)
    assert p.name == "selection_manifest.json"
    assert "liquidity_pool_six_case_wall_trade_reaction_sample_v1" in str(p)


def test_exact_cases_order_next_after_and_no_forbidden(tmp_path: Path):
    out = tmp_path / "freeze"
    res = write_freeze(REPO, out)
    assert res["verdict"] == "CASE_SEQUENCE_FREEZE_V1_COMPLETE"
    frozen = json.loads((out / "frozen_case_sequence_v1.json").read_text())
    validate_frozen(frozen)
    ids = [c["case_id"] for c in frozen["ordered_cases"]]
    assert ids == [f"CASE_{i:02d}" for i in range(1, 7)]
    assert frozen["next_after"]["CASE_02"] == "CASE_03"
    assert frozen["next_after"]["CASE_06"] is None
    blob = json.dumps(frozen)
    for bad in ("pnl", "mfe", "mae", '"verdict"', "winning", "losing"):
        assert bad not in blob.lower() or bad in (
            # allow boolean policy keys only — already excluded from this naive check for verdict
        )


def test_reference_ts_exact_copy_from_cluster_start():
    frozen = build_frozen_sequence(REPO, created_at_utc="2026-08-30T00:00:00Z")
    src = json.loads(find_unique_six_case_source(REPO).read_text())
    by_id = {c["case_id"]: c for c in src["cases"]}
    for c in frozen["ordered_cases"]:
        assert c["reference_ts"] == by_id[c["case_id"]]["cluster_start_ts"]
        assert frozen["reference_ts_policy"]["transformation"] == "EXACT_COPY"


def test_exposure_does_not_reorder():
    frozen = build_frozen_sequence(REPO, created_at_utc="2026-08-30T00:00:00Z")
    ids = [c["case_id"] for c in frozen["ordered_cases"]]
    assert ids == [f"CASE_{i:02d}" for i in range(1, 7)]
    assert frozen["ordered_cases"][0]["exposure_status"] == "PRE_FREEZE_EXPOSED"
    assert frozen["ordered_cases"][1]["exposure_status"] == "PRE_FREEZE_EXPOSED"
    # CASE_03..05 prospective
    for c in frozen["ordered_cases"][2:5]:
        assert c["exposure_status"] == "PROSPECTIVE_UNAUDITED"
    # CASE_06 has deep Einzelfall
    assert frozen["ordered_cases"][5]["case_id"] == "CASE_06"
    assert frozen["ordered_cases"][5]["exposure_status"] == "PRE_FREEZE_EXPOSED"


def test_hash_reproducible_two_runs():
    a = build_frozen_sequence(REPO, created_at_utc="2026-08-30T00:00:00Z")
    b = build_frozen_sequence(REPO, created_at_utc="2099-01-01T00:00:00Z")  # volatile differs
    ha = compute_hashes(a, a["source_manifest"]["sha256"])
    hb = compute_hashes(b, b["source_manifest"]["sha256"])
    assert ha == hb
    assert hashable_sequence_payload(a) == hashable_sequence_payload(b)


def test_verify_ok_and_mutation_fails(tmp_path: Path):
    out = tmp_path / "freeze"
    write_freeze(REPO, out)
    assert verify_freeze(REPO, out)["ok"] is True
    # mutate sequence
    seq_path = out / "frozen_case_sequence_v1.json"
    frozen = json.loads(seq_path.read_text())
    frozen["ordered_cases"][2]["reference_ts"] = "2099-01-01T00:00:00Z"
    seq_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
    with pytest.raises(FreezeError) as ei:
        verify_freeze(REPO, out)
    assert ei.value.verdict == "CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE"


def test_source_file_unchanged_after_freeze(tmp_path: Path):
    src = find_unique_six_case_source(REPO)
    before = src.read_bytes()
    write_freeze(REPO, tmp_path / "freeze")
    after = src.read_bytes()
    assert before == after
    assert sha256_bytes(before) == sha256_bytes(after)


def test_canonical_json_stable():
    obj = {"b": 1, "a": [2, 3]}
    assert canonical_json_bytes(obj) == b'{"a":[2,3],"b":1}'
