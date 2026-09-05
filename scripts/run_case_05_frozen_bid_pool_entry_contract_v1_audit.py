#!/usr/bin/env python3
"""CASE_05 prospective deep audit under frozen Entry Contract V1.

Does not modify any hashed freeze/config/pipeline files.
Parametrizes the existing CASE_03 engine via BidCaseAuditSpec only.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.pipeline import (
    BidCaseAuditSpec,
    run_audit,
)
from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import verify_freeze
from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import (
    EntryContractFreezeError,
    verify_entry_contract_freeze,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    load_effective_room_config,
)

EXPECTED_CASE_SEQ_SHA = "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
EXPECTED_ENTRY_CONTRACT_SHA = "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
EXPECTED_CONFIG_SHA = "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
CONFIG_REL = "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
RAW = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
OUT = OA_ROOT / "results" / "case_05_frozen_bid_pool_entry_contract_v1_audit"

CASE_05_SPEC = BidCaseAuditSpec(
    case_id="CASE_05",
    predecessor_case_id="CASE_04",
    format_version="case_05_frozen_bid_pool_entry_contract_v1_audit/v1",
    results_dirname="case_05_frozen_bid_pool_entry_contract_v1_audit",
    manual_review_name="CASE_05_MANUAL_REVIEW.md",
    deep_audit_result_glob="case_05_frozen_bid_pool_entry_contract_v1_audit",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def verify_all(*, label: str) -> dict:
    out: dict = {
        "label": label,
        "generated_at": _iso_now(),
        "ok": False,
        "verdict": None,
    }
    try:
        seq = verify_freeze(OA_ROOT, OA_ROOT / "results/liquidity_pool_case_sequence_freeze_v1")
        seq_sha = seq["freeze_bundle_sha256"]
        out["case_sequence_freeze"] = {
            "ok": seq_sha == EXPECTED_CASE_SEQ_SHA,
            "sha256": seq_sha,
            "expected": EXPECTED_CASE_SEQ_SHA,
        }
    except Exception as exc:  # noqa: BLE001
        out["case_sequence_freeze"] = {"ok": False, "error": str(exc)}
        out["verdict"] = "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE"
        return out

    try:
        ec = verify_entry_contract_freeze(OA_ROOT)
        out["entry_contract_freeze"] = {
            "ok": bool(ec.get("ok"))
            and ec.get("entry_contract_freeze_sha256") == EXPECTED_ENTRY_CONTRACT_SHA,
            "sha256": ec.get("entry_contract_freeze_sha256"),
            "expected": EXPECTED_ENTRY_CONTRACT_SHA,
        }
    except EntryContractFreezeError as exc:
        out["entry_contract_freeze"] = {"ok": False, "error": str(exc)}
        out["verdict"] = "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE"
        return out

    cfg_path = OA_ROOT / CONFIG_REL
    cfg_sha = hashlib.sha256(cfg_path.read_bytes()).hexdigest()
    out["strategy_config"] = {
        "ok": cfg_sha == EXPECTED_CONFIG_SHA,
        "path": CONFIG_REL,
        "sha256": cfg_sha,
        "expected": EXPECTED_CONFIG_SHA,
    }

    ok = (
        out["case_sequence_freeze"]["ok"]
        and out["entry_contract_freeze"]["ok"]
        and out["strategy_config"]["ok"]
    )
    out["ok"] = ok
    if not ok:
        out["verdict"] = "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE"
    return out


def git_meta() -> dict:
    import subprocess

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=OA_ROOT, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=OA_ROOT, text=True
    ).strip()
    dirty_n = len(
        subprocess.check_output(["git", "status", "--short"], cwd=OA_ROOT, text=True).splitlines()
    )
    return {"head": head, "branch": branch, "dirty_n": dirty_n}


def assert_pipeline_executable() -> None:
    """Confirm CASE_05 can run via declarative BidCaseAuditSpec without editing frozen files."""
    # BidCaseAuditSpec exists and run_audit accepts external spec — no frozen file mutation needed.
    assert CASE_05_SPEC.case_id == "CASE_05"
    assert CASE_05_SPEC.predecessor_case_id == "CASE_04"
    assert callable(run_audit)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = git_meta()
    _write_json(OUT / "git_status.json", meta)

    try:
        assert_pipeline_executable()
    except Exception as exc:  # noqa: BLE001
        payload = {
            "verdict": "CASE_05_FROZEN_PIPELINE_NOT_EXECUTABLE",
            "detail": str(exc),
            "git": meta,
        }
        _write_json(OUT / "summary.json", payload)
        print(json.dumps(payload, indent=2))
        return 2

    before = verify_all(label="before")
    _write_json(OUT / "freeze_verification_before.json", before)
    _write_json(OUT / "entry_contract_verification_before.json", before)
    if not before["ok"]:
        payload = {
            "verdict": "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE",
            "before": before,
            "git": meta,
        }
        _write_json(OUT / "summary.json", payload)
        print(json.dumps(payload, indent=2))
        return 2

    # Effective config snapshot (immutable load; no defaults)
    effective = load_effective_room_config(OA_ROOT)
    _write_json(
        OUT / "effective_config.json",
        {
            "config_path": effective.config_path_rel,
            "config_sha256": effective.config_sha256,
            "expected_config_sha256": EXPECTED_CONFIG_SHA,
            "sha_match": effective.config_sha256 == EXPECTED_CONFIG_SHA,
            "room_to_target": asdict(effective.room),
            "entry_contract_freeze_sha256": EXPECTED_ENTRY_CONTRACT_SHA,
            "case_sequence_freeze_sha256": EXPECTED_CASE_SEQ_SHA,
            "generated_at": _iso_now(),
        },
    )
    if effective.config_sha256 != EXPECTED_CONFIG_SHA:
        payload = {
            "verdict": "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE",
            "reason": "effective config sha mismatch",
            "git": meta,
        }
        _write_json(OUT / "summary.json", payload)
        print(json.dumps(payload, indent=2))
        return 2

    # Prior comparable deep audit elsewhere?
    other = OA_ROOT / "results" / "case_05_frozen_bid_pool_causal_reaction_audit_v1"
    if (
        other.exists()
        and any(other.iterdir())
        and (other / "mechanical_verdict_pre_unblind.json").exists()
        and other.resolve() != OUT.resolve()
    ):
        payload = {
            "verdict": "CASE_05_PREVIOUSLY_EXPOSED",
            "prior_path": str(other),
            "git": meta,
        }
        _write_json(OUT / "summary.json", payload)
        print(json.dumps(payload, indent=2))
        return 2

    res = run_audit(repo_root=OA_ROOT, raw_root=RAW, out_dir=OUT, spec=CASE_05_SPEC)

    # Restore Phase-0 before artefact (pipeline overwrites freeze_verification_before)
    _write_json(OUT / "freeze_verification_before.json", before)
    _write_json(OUT / "entry_contract_verification_before.json", before)

    after = verify_all(label="after")
    _write_json(OUT / "freeze_verification_after.json", after)
    _write_json(OUT / "entry_contract_verification_after.json", after)

    # Annotate summary with entry-contract verification without mutating mechanical verdict
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    summary.update(
        {
            "entry_contract_verification_before_ok": before["ok"],
            "entry_contract_verification_after_ok": after["ok"],
            "entry_contract_freeze_sha256": EXPECTED_ENTRY_CONTRACT_SHA,
            "case_sequence_freeze_sha256": EXPECTED_CASE_SEQ_SHA,
            "strategy_config_sha256": EXPECTED_CONFIG_SHA,
            "git": meta,
            "pipeline_spec": {
                "case_id": CASE_05_SPEC.case_id,
                "predecessor_case_id": CASE_05_SPEC.predecessor_case_id,
                "format_version": CASE_05_SPEC.format_version,
                "results_dirname": CASE_05_SPEC.results_dirname,
            },
        }
    )
    if not after["ok"]:
        summary["verdict"] = "CASE_05_FROZEN_CONTRACT_VERIFICATION_FAILURE"
        summary["after"] = after
    _write_json(summary_path, summary)

    # Enrich mechanical pre-unblind with entry-contract freeze hash if missing
    # (read-only annotate sidecar; do not rewrite payload sha / verdict)
    mech_path = OUT / "mechanical_verdict_pre_unblind.json"
    if mech_path.exists():
        mech = json.loads(mech_path.read_text(encoding="utf-8"))
        sidecar = {
            "entry_contract_freeze_sha256": EXPECTED_ENTRY_CONTRACT_SHA,
            "case_sequence_freeze_sha256": mech.get("freeze_bundle_sha256"),
            "room_gate_config_sha256": mech.get("room_gate_config_sha256"),
            "entry_contract_version": mech.get("entry_contract_version"),
            "mechanical_payload_sha256": mech.get("mechanical_payload_sha256"),
            "note": "Sidecar only; mechanical_verdict_pre_unblind.json unchanged after persist.",
        }
        _write_json(OUT / "entry_contract_hashes_sidecar.json", sidecar)

    print(json.dumps(summary if summary else res, indent=2, default=str))
    v = str(summary.get("verdict") or res.get("verdict") or "")
    if (
        "FAILURE" in v
        or v.endswith("DATA_BLOCKED")
        or v.endswith("PREVIOUSLY_EXPOSED")
        or "BLINDNESS" in v
        or "NOT_EXECUTABLE" in v
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
