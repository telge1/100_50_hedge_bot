"""Hash verification for expansion batch — frozen inputs only."""

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
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import (
    ENTRY_CONTRACT_FREEZE_REL,
    EXPECTED_CASE_SEQUENCE_HASH,
    EXPECTED_ENTRY_CONTRACT_HASH,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V3_HASH,
    STRATEGY_CONFIG_REL,
    V3_FREEZE_FILE,
    V3_FREEZE_REL,
)
from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import (
    EntryContractFreezeError,
    INTEGRATED_PATHS,
    verify_entry_contract_freeze,
)
from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import verify_freeze


class FrozenInputHashMismatch(RuntimeError):
    verdict = "FROZEN_INPUT_HASH_MISMATCH"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.verdict}: {detail}")


def _v3_bundle_sha(repo_root: Path) -> str:
    path = repo_root / V3_FREEZE_REL / V3_FREEZE_FILE
    frozen = json.loads(path.read_text(encoding="utf-8"))
    stored = frozen.get("expansion_freeze_bundle_sha256")
    payload = {
        k: v
        for k, v in frozen.items()
        if k not in ("expansion_freeze_bundle_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise FrozenInputHashMismatch(
            f"v3 recomputed mismatch stored={stored} recomputed={recomputed}"
        )
    return str(stored)


def verify_frozen_inputs(repo_root: Path, *, label: str = "check") -> dict[str, Any]:
    """Verify v3 / entry-contract / strategy / component hashes. Raises on mismatch."""
    out: dict[str, Any] = {"label": label, "ok": False, "checks": {}}

    v3_sha = _v3_bundle_sha(repo_root)
    out["checks"]["v3_freeze"] = {
        "ok": v3_sha == EXPECTED_V3_HASH,
        "sha256": v3_sha,
        "expected": EXPECTED_V3_HASH,
    }
    if v3_sha != EXPECTED_V3_HASH:
        raise FrozenInputHashMismatch(f"v3 freeze hash {v3_sha} != {EXPECTED_V3_HASH}")

    seq = verify_freeze(repo_root, repo_root / "results/liquidity_pool_case_sequence_freeze_v1")
    seq_sha = seq["freeze_bundle_sha256"]
    out["checks"]["case_sequence"] = {
        "ok": seq_sha == EXPECTED_CASE_SEQUENCE_HASH,
        "sha256": seq_sha,
        "expected": EXPECTED_CASE_SEQUENCE_HASH,
    }
    if seq_sha != EXPECTED_CASE_SEQUENCE_HASH:
        raise FrozenInputHashMismatch(f"case sequence hash mismatch")

    try:
        ec = verify_entry_contract_freeze(repo_root)
    except EntryContractFreezeError as exc:
        raise FrozenInputHashMismatch(str(exc)) from exc
    ec_sha = ec.get("entry_contract_freeze_sha256")
    out["checks"]["entry_contract"] = {
        "ok": ec_sha == EXPECTED_ENTRY_CONTRACT_HASH,
        "sha256": ec_sha,
        "expected": EXPECTED_ENTRY_CONTRACT_HASH,
    }
    if ec_sha != EXPECTED_ENTRY_CONTRACT_HASH:
        raise FrozenInputHashMismatch(f"entry contract hash mismatch")

    cfg_sha = sha256_file(repo_root / STRATEGY_CONFIG_REL)
    out["checks"]["strategy_config"] = {
        "ok": cfg_sha == EXPECTED_STRATEGY_CONFIG_HASH,
        "sha256": cfg_sha,
        "expected": EXPECTED_STRATEGY_CONFIG_HASH,
        "path": STRATEGY_CONFIG_REL,
    }
    if cfg_sha != EXPECTED_STRATEGY_CONFIG_HASH:
        raise FrozenInputHashMismatch(f"strategy config hash mismatch")

    # Component hashes from frozen entry contract bundle
    contract_path = repo_root / ENTRY_CONTRACT_FREEZE_REL / "entry_contract_v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    component_checks = {}
    for rel in INTEGRATED_PATHS.values():
        current = sha256_file(repo_root / rel)
        expected = contract["component_hashes"][rel]
        ok = current == expected
        component_checks[rel] = {"ok": ok, "sha256": current, "expected": expected}
        if not ok:
            raise FrozenInputHashMismatch(f"component drift: {rel}")
    out["checks"]["components"] = component_checks
    out["ok"] = True
    return out


def payload_sha256(obj: dict[str, Any], *, exclude: tuple[str, ...] = ("generated_at",)) -> str:
    filtered = {k: v for k, v in obj.items() if k not in exclude and k != "mechanical_payload_sha256"}
    return hashlib.sha256(
        json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
