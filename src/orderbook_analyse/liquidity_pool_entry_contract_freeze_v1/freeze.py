"""Build / verify liquidity pool entry contract freeze v1."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    load_effective_room_config,
    repo_root_from,
)

FORMAT_VERSION = "liquidity_pool_entry_contract_freeze/v1"
CASE_SEQUENCE_FREEZE_SHA256 = (
    "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
)
EFFECTIVE_FROM_CASE = "CASE_05"
RESULTS_DIR_REL = "results/liquidity_pool_entry_contract_freeze_v1"

INTEGRATED_PATHS = {
    "strategy_yaml": "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml",
    "config_loader": (
        "src/orderbook_analyse/liquidity_pool_min_target_distance_config_v1/config.py"
    ),
    "gate_logic": (
        "src/orderbook_analyse/liquidity_pool_min_target_distance_config_v1/gate.py"
    ),
    "entry_contract": (
        "src/orderbook_analyse/case_03_frozen_bid_pool_causal_reaction_audit_v1/entry_contract.py"
    ),
    "audit_pipeline": (
        "src/orderbook_analyse/case_03_frozen_bid_pool_causal_reaction_audit_v1/pipeline.py"
    ),
}

MECH_SCHEMA_FIELDS = (
    "microstructure_gate_passed",
    "microstructure_gate_reason",
    "candidate_direction",
    "mechanical_entry_price",
    "room_gate_enabled",
    "room_gate_config_path",
    "room_gate_config_sha256",
    "min_target_distance_pct",
    "min_target_distance_bps",
    "target_pool_id",
    "target_pool_first_available_ts",
    "target_edge",
    "target_price",
    "raw_target_distance_pct",
    "raw_target_distance_bps",
    "room_after_cost_11bps",
    "room_after_cost_15bps",
    "room_after_cost_20bps",
    "overlap_detected",
    "target_causally_available",
    "room_gate_passed",
    "room_gate_reason",
    "mechanical_trade_verdict",
    "entry_contract_version",
)


class EntryContractFreezeError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mechanical_verdict_schema() -> dict[str, Any]:
    return {
        "format": "mechanical_verdict_pre_unblind.json",
        "entry_contract_version": "liquidity_pool_entry_contract/v1",
        "required_fields": list(MECH_SCHEMA_FIELDS),
        "entry_rule": (
            "microstructure_gate_passed AND room_gate_passed -> TRADE_*; else NO_TRADE"
        ),
    }


def build_entry_contract_bundle(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or repo_root_from()
    out_dir = root / RESULTS_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)

    effective = load_effective_room_config(root)
    yaml_src = root / INTEGRATED_PATHS["strategy_yaml"]
    yaml_dst = out_dir / "effective_strategy_config.yaml"
    shutil.copy2(yaml_src, yaml_dst)

    component_hashes = {
        rel: sha256_file(root / rel) for rel in INTEGRATED_PATHS.values()
    }
    component_hashes["effective_strategy_yaml_copy"] = sha256_file(yaml_dst)

    entry_contract_v1 = {
        "format_version": FORMAT_VERSION,
        "entry_contract_version": "liquidity_pool_entry_contract/v1",
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "effective_from_case": EFFECTIVE_FROM_CASE,
        "min_target_distance_pct": effective.room.min_target_distance_pct,
        "min_target_distance_bps": effective.room.min_target_distance_bps,
        "room_gate_config_path": effective.config_path_rel,
        "room_gate_config_sha256": effective.config_sha256,
        "exposure": {
            "CASE_01": "PRE_CONTRACT_EXPOSED",
            "CASE_02": "PRE_CONTRACT_EXPOSED",
            "CASE_03": "PRE_CONTRACT_EXPOSED",
            "CASE_04": "PRE_CONTRACT_EXPOSED",
            "CASE_05": "PROSPECTIVE_UNAUDITED",
            "CASE_06": "PRE_CONTRACT_EXPOSED",
        },
        "decision_order": [
            "frozen_case_load",
            "causal_pool_determination",
            "edge_reaction_classification",
            "aggressor_wall_confirmation",
            "mechanical_candidate_direction",
            "first_available_ts_and_mechanical_entry_price",
            "next_causal_opposing_pool_tp_direction",
            "min_target_distance_config_v1",
            "final_trade_no_trade",
        ],
        "entry_rule": {
            "requires": ["microstructure_gate_passed", "room_gate_passed"],
            "otherwise": "NO_TRADE",
        },
        "target_as_of_rule": "pool.available_at <= first_available_ts",
        "canonical_json": "UTF-8 JSON sort_keys=True separators=(',', ':')",
        "component_hashes": component_hashes,
        "mechanical_verdict_schema": mechanical_verdict_schema(),
        "generated_at": _utc_now(),
    }
    bundle_sha = sha256_bytes(canonical_json_bytes(entry_contract_v1))
    entry_contract_v1["entry_contract_freeze_sha256"] = bundle_sha

    manifest = {
        "format_version": FORMAT_VERSION,
        "verdict": "LIQUIDITY_POOL_ENTRY_CONTRACT_V1_FROZEN",
        "entry_contract_freeze_sha256": bundle_sha,
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "files": {
            "entry_contract_v1.json": "entry contract bundle",
            "effective_strategy_config.yaml": "frozen strategy YAML copy",
            "INTEGRATION_REPORT.md": "human report",
        },
        "component_paths": INTEGRATED_PATHS,
        "generated_at": entry_contract_v1["generated_at"],
    }

    (out_dir / "entry_contract_v1.json").write_text(
        json.dumps(entry_contract_v1, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "freeze_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    report = _integration_report(root, entry_contract_v1, effective)
    (out_dir / "INTEGRATION_REPORT.md").write_text(report, encoding="utf-8")

    return {
        "verdict": "LIQUIDITY_POOL_ENTRY_CONTRACT_V1_FROZEN",
        "entry_contract_freeze_sha256": bundle_sha,
        "out_dir": str(out_dir),
    }


def verify_entry_contract_freeze(
    repo_root: Path | None = None,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    root = repo_root or repo_root_from()
    out_dir = root / RESULTS_DIR_REL
    contract_path = out_dir / "entry_contract_v1.json"
    manifest_path = out_dir / "freeze_manifest.json"
    if not contract_path.is_file() or not manifest_path.is_file():
        raise EntryContractFreezeError(
            "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE", "freeze artefacts missing"
        )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    stored_sha = contract.get("entry_contract_freeze_sha256")
    recomputed = sha256_bytes(
        canonical_json_bytes({k: v for k, v in contract.items() if k != "entry_contract_freeze_sha256"})
    )
    if recomputed != stored_sha:
        raise EntryContractFreezeError(
            "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE",
            f"bundle sha mismatch stored={stored_sha} recomputed={recomputed}",
        )

    for rel in INTEGRATED_PATHS.values():
        current = sha256_file(root / rel)
        if current != contract["component_hashes"][rel]:
            raise EntryContractFreezeError(
                "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE",
                f"component changed: {rel}",
            )

    yaml_copy = out_dir / "effective_strategy_config.yaml"
    if sha256_file(yaml_copy) != contract["component_hashes"]["effective_strategy_yaml_copy"]:
        raise EntryContractFreezeError(
            "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE", "effective_strategy_config.yaml mismatch"
        )

    if contract["case_sequence_freeze_sha256"] != CASE_SEQUENCE_FREEZE_SHA256:
        raise EntryContractFreezeError(
            "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE", "case sequence freeze sha mismatch"
        )

    if mutate:
        tampered = dict(contract)
        tampered["min_target_distance_pct"] = 0.99
        bad_sha = sha256_bytes(
            canonical_json_bytes({k: v for k, v in tampered.items() if k != "entry_contract_freeze_sha256"})
        )
        if bad_sha == stored_sha:
            raise EntryContractFreezeError(
                "ENTRY_CONTRACT_FREEZE_INTEGRITY_FAILURE", "mutation test did not change sha"
            )
        return {
            "ok": True,
            "mutation_detected": True,
            "original_sha256": stored_sha,
            "mutated_sha256": bad_sha,
        }

    return {
        "ok": True,
        "verdict": "LIQUIDITY_POOL_ENTRY_CONTRACT_V1_FROZEN",
        "entry_contract_freeze_sha256": stored_sha,
    }


def _integration_report(root: Path, contract: dict[str, Any], effective) -> str:
    return "\n".join(
        [
            "# Liquidity Pool Entry Contract Freeze v1",
            "",
            f"Generated: {contract['generated_at']}",
            "",
            "## Verdict",
            "",
            "**LIQUIDITY_POOL_ENTRY_CONTRACT_V1_FROZEN**",
            "",
            "## Config",
            "",
            f"- Path: `{effective.config_path_rel}`",
            f"- SHA256: `{effective.config_sha256}`",
            f"- min_target_distance_pct: {effective.room.min_target_distance_pct}",
            "",
            "## Entry contract freeze SHA256",
            "",
            f"`{contract['entry_contract_freeze_sha256']}`",
            "",
            "## Canonical serialization",
            "",
            "UTF-8 JSON with `sort_keys=True`, `separators=(',', ':')`, `ensure_ascii=True`.",
            "",
            "## CASE_05",
            "",
            "Not started — effective from CASE_05 prospective only.",
            "",
        ]
    )
