"""Frozen-input hash verification for batch runner V2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    ENTRY_CONTRACT_V2_FILE,
    ENTRY_CONTRACT_V2_FREEZE_REL,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V1_PREDECESSOR,
    EXPECTED_V2_HASH,
    EXPECTED_V3_HASH,
    EXPECTED_V4_HASH,
    STRATEGY_CONFIG_REL,
    V3_FREEZE_FILE,
    V3_FREEZE_REL,
    V4_FREEZE_FILE,
    V4_FREEZE_REL,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.freeze import (
    EntryContractV2FreezeError,
    verify_entry_contract_v2_freeze,
    verify_expansion_binding_v4,
)


class FrozenInputHashMismatch(RuntimeError):
    verdict = "FROZEN_INPUT_HASH_MISMATCH"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.verdict}: {detail}")


def _bundle_sha_excluding(path: Path, *, hash_key: str, exclude: tuple[str, ...]) -> str:
    frozen = json.loads(path.read_text(encoding="utf-8"))
    stored = frozen.get(hash_key)
    payload = {k: v for k, v in frozen.items() if k not in exclude}
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise FrozenInputHashMismatch(
            f"{path.name} recomputed mismatch stored={stored} recomputed={recomputed}"
        )
    return str(stored)


def verify_v3_v4_membership(repo_root: Path) -> dict[str, Any]:
    v3 = json.loads((repo_root / V3_FREEZE_REL / V3_FREEZE_FILE).read_text(encoding="utf-8"))
    v4 = json.loads((repo_root / V4_FREEZE_REL / V4_FREEZE_FILE).read_text(encoding="utf-8"))
    v3_ids = [c["expansion_case_id"] for c in v3["ordered_cases"]]
    v4_ids = [c["expansion_case_id"] for c in v4["ordered_cases"]]
    hash_eq = all(
        a["deterministic_selection_hash"] == b["deterministic_selection_hash"]
        for a, b in zip(v3["ordered_cases"], v4["ordered_cases"])
    )
    ok = v3_ids == v4_ids and hash_eq and len(v3_ids) == 24
    if not ok:
        raise FrozenInputHashMismatch("v3/v4 membership or order parity failed")
    return {
        "ok": True,
        "case_ids_equal": v3_ids == v4_ids,
        "selection_hashes_equal": hash_eq,
        "count": len(v3_ids),
    }


def verify_frozen_inputs(repo_root: Path, *, label: str = "check") -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "ok": False, "checks": {}}

    try:
        v2 = verify_entry_contract_v2_freeze(repo_root)
    except EntryContractV2FreezeError as exc:
        raise FrozenInputHashMismatch(str(exc)) from exc
    v2_sha = v2.get("entry_contract_v2_freeze_sha256")
    out["checks"]["entry_contract_v2"] = {
        "ok": v2_sha == EXPECTED_V2_HASH,
        "sha256": v2_sha,
        "expected": EXPECTED_V2_HASH,
    }
    if v2_sha != EXPECTED_V2_HASH:
        raise FrozenInputHashMismatch(f"v2 freeze hash {v2_sha} != {EXPECTED_V2_HASH}")

    try:
        v4 = verify_expansion_binding_v4(repo_root)
    except EntryContractV2FreezeError as exc:
        raise FrozenInputHashMismatch(str(exc)) from exc
    v4_sha = v4.get("expansion_v4_binding_sha256")
    out["checks"]["expansion_v4"] = {
        "ok": v4_sha == EXPECTED_V4_HASH,
        "sha256": v4_sha,
        "expected": EXPECTED_V4_HASH,
    }
    if v4_sha != EXPECTED_V4_HASH:
        raise FrozenInputHashMismatch(f"v4 binding hash {v4_sha} != {EXPECTED_V4_HASH}")

    v3_sha = _bundle_sha_excluding(
        repo_root / V3_FREEZE_REL / V3_FREEZE_FILE,
        hash_key="expansion_freeze_bundle_sha256",
        exclude=("expansion_freeze_bundle_sha256", "created_at_utc"),
    )
    out["checks"]["expansion_v3_predecessor"] = {
        "ok": v3_sha == EXPECTED_V3_HASH,
        "sha256": v3_sha,
        "expected": EXPECTED_V3_HASH,
    }
    if v3_sha != EXPECTED_V3_HASH:
        raise FrozenInputHashMismatch(f"v3 predecessor hash {v3_sha} != {EXPECTED_V3_HASH}")

    cfg_sha = sha256_file(repo_root / STRATEGY_CONFIG_REL)
    out["checks"]["strategy_config"] = {
        "ok": cfg_sha == EXPECTED_STRATEGY_CONFIG_HASH,
        "sha256": cfg_sha,
        "expected": EXPECTED_STRATEGY_CONFIG_HASH,
    }
    if cfg_sha != EXPECTED_STRATEGY_CONFIG_HASH:
        raise FrozenInputHashMismatch("strategy config sha mismatch")

    membership = verify_v3_v4_membership(repo_root)
    out["checks"]["v3_v4_membership"] = membership

    contract = json.loads(
        (repo_root / ENTRY_CONTRACT_V2_FREEZE_REL / ENTRY_CONTRACT_V2_FILE).read_text(
            encoding="utf-8"
        )
    )
    pred = contract.get("predecessor_v1_entry_contract_sha256")
    out["checks"]["v1_predecessor"] = {
        "ok": pred == EXPECTED_V1_PREDECESSOR,
        "sha256": pred,
        "expected": EXPECTED_V1_PREDECESSOR,
    }
    if pred != EXPECTED_V1_PREDECESSOR:
        raise FrozenInputHashMismatch("v1 predecessor mismatch")

    # Hashed V2 decision files must be unchanged vs freeze component_hashes
    component_checks = {}
    for rel, expected in (contract.get("component_hashes") or {}).items():
        if rel == "effective_strategy_yaml_copy":
            path = repo_root / ENTRY_CONTRACT_V2_FREEZE_REL / "effective_strategy_config.yaml"
        else:
            path = repo_root / rel
        if not path.is_file():
            raise FrozenInputHashMismatch(f"missing component: {rel}")
        current = sha256_file(path)
        ok = current == expected
        component_checks[rel] = {"ok": ok, "sha256": current, "expected": expected}
        if not ok:
            raise FrozenInputHashMismatch(f"component drift: {rel}")
    out["checks"]["v2_components"] = component_checks
    out["ok"] = True
    return out


def payload_sha256(obj: dict[str, Any]) -> str:
    filtered = {
        k: v
        for k, v in obj.items()
        if k not in ("generated_at", "mechanical_payload_sha256")
    }
    return hashlib.sha256(
        json.dumps(
            filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
        ).encode("utf-8")
    ).hexdigest()
