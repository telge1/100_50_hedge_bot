"""Freeze V2 writer / verifier for checkpoint + episode contract fix.

Does not overwrite V1 freeze. Parent lineage required.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    BUCKET_SEMANTICS_CONTRACT,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance_v2 import (
    CHECKPOINT_CONTRACT_V2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.episode_contract_v2 import (
    EPISODE_CONTRACT_V2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FREEZE_SOURCE_FILES,
    FreezeViolation,
    _file_sha256,
    _payload_sha256,
    verify_freeze as verify_freeze_v1,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_json,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_SRC = PACKAGE_ROOT.parent

PARENT_FREEZE_SHA = "67924037fee3f6e3a399abc9b4854198ce4fa21a77b7877f4001a20641486454"

NO_FIT_V2 = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
    "outcome_used_for_checkpoint_contract": False,
    "outcome_used_for_episode_contract": False,
    "outcome_used_for_deduplication": False,
    "outcome_used_for_entry_timestamp": False,
}

FREEZE_V2_SOURCE_FILES = FREEZE_SOURCE_FILES + (
    "aggressor_efficiency_trapped_vwap_acceptance/bucket_semantics_v2.py",
    "aggressor_efficiency_trapped_vwap_acceptance/edge_acceptance_v2.py",
    "aggressor_efficiency_trapped_vwap_acceptance/episode_contract_v2.py",
    "aggressor_efficiency_trapped_vwap_acceptance/freeze_v2.py",
)

TIMESTAMP_EXECUTION_CONTRACT_V2 = {
    "version": "FROZEN_HIGH_ACCEPTED_TIMESTAMP_EXECUTION_V2",
    "trade_bucket": "half_open_[sec,_sec+1s)_keyed_by_floor_second(trade_ts)",
    "bucket_fully_available_at": "sec+1s (= bucket_close)",
    "checkpoint_ts": "bucket_close",
    "acceptance_first_available_ts_v2": "earliest eligible ACCEPTED bucket_close",
    "earliest_causal_entry_ts_v2": (
        "acceptance_first_available_ts_v2; no execution inside the same bucket "
        "before its close/availability"
    ),
    "ob200_sample_ms": 250,
    "outcome_used_for_entry_timestamp": False,
}


def compute_source_hashes_v2(src_root: Path | None = None) -> dict[str, str]:
    root = src_root or REPO_SRC
    out = {}
    for rel in FREEZE_V2_SOURCE_FILES:
        p = root / rel
        if not p.is_file():
            raise FreezeViolation(f"missing freeze v2 source {p}")
        out[rel] = _file_sha256(p)
    return out


def build_freeze_v2_payloads(*, parent_sha: str = PARENT_FREEZE_SHA) -> dict[str, Any]:
    return {
        "frozen_contract_v2": {
            **NO_FIT_V2,
            "package": "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2",
            "parent_freeze_bundle_sha256": parent_sha,
            "refreeze_reason": "CHECKPOINT_AND_EPISODE_CONTRACT_FIX",
            "thresholds_changed": False,
            "state_definition_changed": False,
            "matching_definition_changed": False,
            "acceptance_evidence_thresholds_changed": False,
            "bucket_semantics": BUCKET_SEMANTICS_CONTRACT,
            "checkpoint_contract": CHECKPOINT_CONTRACT_V2,
            "episode_contract": EPISODE_CONTRACT_V2,
            "timestamp_execution_contract": TIMESTAMP_EXECUTION_CONTRACT_V2,
        },
        "frozen_thresholds_v2": {
            **NO_FIT_V2,
            "note": "V1 TrapAcceptConfig evidence thresholds unchanged; V2 adds coverage labels only",
            "acceptance_evidence_thresholds_changed": False,
        },
        "frozen_rule_manifest_v2": {
            **NO_FIT_V2,
            "rules": [
                "dual_evidence_bucket_semantics",
                "emit_checkpoint_every_1s_including_valid_empty",
                "no_forward_fill_across_source_gap",
                "final_acceptance_not_entry_signal",
                "episode_lifecycle_no_fitted_cooldown",
                "rearm_after_causal_close_only",
            ],
        },
        "frozen_source_manifest_v2": {
            **NO_FIT_V2,
            "files": list(FREEZE_V2_SOURCE_FILES),
            "parent_freeze_bundle_sha256": parent_sha,
            "v1_sources_unchanged_required": True,
        },
    }


def write_freeze_v2(
    output_dir: Path,
    *,
    parent_sha: str = PARENT_FREEZE_SHA,
    src_root: Path | None = None,
) -> dict[str, Any]:
    ensure_outdir(output_dir)
    payloads = build_freeze_v2_payloads(parent_sha=parent_sha)
    source_hashes = compute_source_hashes_v2(src_root)
    hashes = {
        "source_file_sha256": source_hashes,
        "contract_sha256": _payload_sha256(payloads["frozen_contract_v2"]),
        "thresholds_sha256": _payload_sha256(payloads["frozen_thresholds_v2"]),
        "rules_sha256": _payload_sha256(payloads["frozen_rule_manifest_v2"]),
        "sources_manifest_sha256": _payload_sha256(payloads["frozen_source_manifest_v2"]),
        **NO_FIT_V2,
        "parent_freeze_bundle_sha256": parent_sha,
        "refreeze_reason": "CHECKPOINT_AND_EPISODE_CONTRACT_FIX",
        "thresholds_changed": False,
        "state_definition_changed": False,
        "matching_definition_changed": False,
        "acceptance_evidence_thresholds_changed": False,
    }
    hashes["freeze_bundle_sha256"] = _payload_sha256(
        {
            "contract": hashes["contract_sha256"],
            "thresholds": hashes["thresholds_sha256"],
            "rules": hashes["rules_sha256"],
            "sources": hashes["sources_manifest_sha256"],
            "files": source_hashes,
            "parent": parent_sha,
        }
    )
    write_json(output_dir / "frozen_contract_v2.json", payloads["frozen_contract_v2"])
    write_json(output_dir / "frozen_thresholds_v2.json", payloads["frozen_thresholds_v2"])
    write_json(output_dir / "frozen_rule_manifest_v2.json", payloads["frozen_rule_manifest_v2"])
    write_json(output_dir / "frozen_source_manifest_v2.json", payloads["frozen_source_manifest_v2"])
    write_json(output_dir / "frozen_hashes_v2.json", hashes)
    # also copy human-readable contracts
    write_json(output_dir / "checkpoint_contract_v2.json", CHECKPOINT_CONTRACT_V2)
    write_json(output_dir / "episode_contract_v2.json", EPISODE_CONTRACT_V2)
    write_json(output_dir / "timestamp_execution_contract_v2.json", TIMESTAMP_EXECUTION_CONTRACT_V2)
    write_json(output_dir / "bucket_semantics_contract_v2.json", BUCKET_SEMANTICS_CONTRACT)
    return hashes


def verify_freeze_v2(freeze_dir: Path, *, src_root: Path | None = None) -> dict[str, Any]:
    stored_path = freeze_dir / "frozen_hashes_v2.json"
    if not stored_path.is_file():
        raise FreezeViolation(f"missing {stored_path}")
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    current_files = compute_source_hashes_v2(src_root)
    mismatches = []
    for rel, digest in stored.get("source_file_sha256", {}).items():
        cur = current_files.get(rel)
        if cur != digest:
            mismatches.append({"file": rel, "stored": digest, "current": cur})
    payloads = build_freeze_v2_payloads(
        parent_sha=stored.get("parent_freeze_bundle_sha256") or PARENT_FREEZE_SHA
    )
    checks = {
        "contract_sha256": _payload_sha256(payloads["frozen_contract_v2"]),
        "thresholds_sha256": _payload_sha256(payloads["frozen_thresholds_v2"]),
        "rules_sha256": _payload_sha256(payloads["frozen_rule_manifest_v2"]),
    }
    for k, v in checks.items():
        if stored.get(k) != v:
            mismatches.append({"artifact": k, "stored": stored.get(k), "current": v})
    if mismatches:
        raise FreezeViolation(f"freeze v2 hash mismatch: {mismatches}")
    return {
        "ok": True,
        "freeze_bundle_sha256": stored.get("freeze_bundle_sha256"),
        "parent_freeze_bundle_sha256": stored.get("parent_freeze_bundle_sha256"),
        **NO_FIT_V2,
    }


def verify_old_freeze_untouched(old_freeze_dir: Path) -> dict[str, Any]:
    out = verify_freeze_v1(old_freeze_dir)
    if out.get("freeze_bundle_sha256") != PARENT_FREEZE_SHA:
        raise FreezeViolation(
            f"OLD_FROZEN_BUNDLE_TAMPERED or unexpected sha: {out.get('freeze_bundle_sha256')}"
        )
    return out
