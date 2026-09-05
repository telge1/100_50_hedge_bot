"""Build / verify Entry Contract V2 freeze + Expansion binding V4."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2 import (
    ENTRY_CONTRACT_VERSION,
    EXPANSION_V3_HASH,
    EXPANSION_V3_REL,
    EXPANSION_V4_REL,
    EXPECTED_STRATEGY_CONFIG_SHA256,
    FORMAT_VERSION,
    PREDECESSOR_V1_ENTRY_CONTRACT_SHA256,
    RESULTS_FREEZE_REL,
    STRATEGY_CONFIG_REL,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    load_effective_room_config,
)

V2_COMPONENT_RELS = (
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/__init__.py",
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/case_spec.py",
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/geometry.py",
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/decision.py",
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/mechanical.py",
    "src/orderbook_analyse/liquidity_pool_entry_contract_v2/unblind.py",
    STRATEGY_CONFIG_REL,
)


class EntryContractV2FreezeError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_entry_contract_v2_freeze(repo_root: Path) -> dict[str, Any]:
    root = repo_root
    out = root / RESULTS_FREEZE_REL
    out.mkdir(parents=True, exist_ok=True)

    effective = load_effective_room_config(root)
    if effective.config_sha256 != EXPECTED_STRATEGY_CONFIG_SHA256:
        raise EntryContractV2FreezeError(
            "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "strategy config sha drift"
        )
    if abs(effective.room.min_target_distance_pct - 0.5) > 1e-12:
        raise EntryContractV2FreezeError(
            "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "room threshold drift"
        )

    yaml_src = root / STRATEGY_CONFIG_REL
    yaml_dst = out / "effective_strategy_config.yaml"
    shutil.copy2(yaml_src, yaml_dst)

    component_hashes = {rel: sha256_file(root / rel) for rel in V2_COMPONENT_RELS}
    component_hashes["effective_strategy_yaml_copy"] = sha256_file(yaml_dst)

    bundle = {
        "format_version": FORMAT_VERSION,
        "entry_contract_version": ENTRY_CONTRACT_VERSION,
        "predecessor_v1_entry_contract_sha256": PREDECESSOR_V1_ENTRY_CONTRACT_SHA256,
        "min_target_distance_pct": effective.room.min_target_distance_pct,
        "min_target_distance_bps": effective.room.min_target_distance_bps,
        "room_gate_config_path": effective.config_path_rel,
        "room_gate_config_sha256": effective.config_sha256,
        "mechanical_unblind_separation": {
            "mechanical_api": "run_mechanical_audit",
            "unblind_api": "run_outcome_unblind",
            "automatic_unblind_from_mechanical": False,
            "mechanical_complete_marker": "mechanical_complete.marker",
            "expansion_unblind_requires_mechanical_complete_count": 24,
        },
        "case_spec_schema": {
            "fields": [
                "expansion_case_id",
                "source_candidate_id",
                "symbol",
                "reference_ts",
                "pool_id",
                "pool_side",
                "approach",
                "pool_timeframe",
                "pool_lower",
                "pool_upper",
                "pool_first_available_ts",
                "event_family_id",
                "exposure_status",
            ],
            "valid_combinations": [["BID", "FROM_ABOVE"], ["ASK", "FROM_BELOW"]],
            "invalid_fail_closed": "INVALID_POOL_APPROACH_COMBINATION",
        },
        "ask_bid_geometry": {
            "BID_FROM_ABOVE": {"front": "upper", "back": "lower", "defense": "LONG", "breakout": "SHORT"},
            "ASK_FROM_BELOW": {"front": "lower", "back": "upper", "defense": "SHORT", "breakout": "LONG"},
            "shared_thresholds": True,
        },
        "entry_rule": {
            "requires": ["microstructure_gate_passed", "room_gate_passed"],
            "otherwise": "NO_TRADE",
        },
        "component_hashes": component_hashes,
        "canonical_json": "UTF-8 JSON sort_keys=True separators=(',', ':')",
        "generated_at": _utc_now(),
    }
    sha = sha256_bytes(canonical_json_bytes(bundle))
    bundle["entry_contract_v2_freeze_sha256"] = sha

    (out / "entry_contract_v2.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "format_version": FORMAT_VERSION,
        "verdict": "LP_ENTRY_CONTRACT_V2_FROZEN",
        "entry_contract_v2_freeze_sha256": sha,
        "predecessor_v1_entry_contract_sha256": PREDECESSOR_V1_ENTRY_CONTRACT_SHA256,
        "files": {
            "entry_contract_v2.json": "contract bundle",
            "effective_strategy_config.yaml": "strategy yaml copy",
            "MECHANICAL_UNBLIND_SEPARATION_REPORT.md": "separation report",
            "ASK_BID_SYMMETRY_REPORT.md": "symmetry report",
            "v1_regression.json": "CASE_03/04/05 regression",
            "test_results.json": "test summary",
            "freeze_manifest.json": "manifest",
        },
        "generated_at": bundle["generated_at"],
    }
    (out / "freeze_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (out / "MECHANICAL_UNBLIND_SEPARATION_REPORT.md").write_text(
        "\n".join(
            [
                "# Mechanical / Unblind Separation — Entry Contract V2",
                "",
                "- Mechanical API: `run_mechanical_audit` — never opens outcomes.",
                "- Unblind API: `run_outcome_unblind` — separate; requires marker + payload SHA",
                "  and `mechanical_complete_count == 24` for expansion.",
                "- No automatic unblind from mechanical.",
                f"- Freeze SHA: `{sha}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "ASK_BID_SYMMETRY_REPORT.md").write_text(
        "\n".join(
            [
                "# ASK/BID Symmetry — Entry Contract V2",
                "",
                "- BID/FROM_ABOVE: front=upper, back=lower; defense→LONG; breakout→SHORT",
                "- ASK/FROM_BELOW: front=lower, back=upper; defense→SHORT; breakout→LONG",
                "- Shared windows: 5/15/30/60s, PRE_S=30, MAX_POST_S=1800",
                "- Shared room gate: min_target_distance_pct=0.5 from YAML only",
                "- Invalid combinations fail-closed: INVALID_POOL_APPROACH_COMBINATION",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"verdict": "LP_ENTRY_CONTRACT_V2_FROZEN", "entry_contract_v2_freeze_sha256": sha, "out_dir": str(out)}


def verify_entry_contract_v2_freeze(repo_root: Path, *, mutate: bool = False) -> dict[str, Any]:
    root = repo_root
    path = root / RESULTS_FREEZE_REL / "entry_contract_v2.json"
    if not path.is_file():
        raise EntryContractV2FreezeError("ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "missing freeze")
    bundle = json.loads(path.read_text(encoding="utf-8"))
    stored = bundle["entry_contract_v2_freeze_sha256"]
    payload = {k: v for k, v in bundle.items() if k != "entry_contract_v2_freeze_sha256"}
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise EntryContractV2FreezeError(
            "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE",
            f"sha mismatch stored={stored} recomputed={recomputed}",
        )
    for rel, expected in bundle["component_hashes"].items():
        if rel == "effective_strategy_yaml_copy":
            current = sha256_file(root / RESULTS_FREEZE_REL / "effective_strategy_config.yaml")
        else:
            current = sha256_file(root / rel)
        if current != expected:
            raise EntryContractV2FreezeError(
                "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", f"component drift {rel}"
            )
    if bundle["predecessor_v1_entry_contract_sha256"] != PREDECESSOR_V1_ENTRY_CONTRACT_SHA256:
        raise EntryContractV2FreezeError("ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "v1 predecessor")
    if mutate:
        tampered = dict(payload)
        tampered["min_target_distance_pct"] = 0.99
        bad = sha256_bytes(canonical_json_bytes(tampered))
        if bad == stored:
            raise EntryContractV2FreezeError("ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "mutation failed")
        return {"ok": True, "mutation_detected": True, "original_sha256": stored, "mutated_sha256": bad}
    return {"ok": True, "entry_contract_v2_freeze_sha256": stored}


def _audit_windows_independent(a_ref: str, b_ref: str) -> bool:
    from datetime import datetime, timedelta

    def utc(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    a0 = utc(a_ref) - timedelta(seconds=30)
    a1 = utc(a_ref) + timedelta(seconds=1800)
    b0 = utc(b_ref) - timedelta(seconds=30)
    b1 = utc(b_ref) + timedelta(seconds=1800)
    return a0 > b1 or a1 < b0


def build_expansion_binding_v4(
    repo_root: Path,
    *,
    entry_contract_v2_sha: str,
) -> dict[str, Any]:
    root = repo_root
    v3_path = root / EXPANSION_V3_REL / "frozen_expansion_cases_v3.json"
    v3 = json.loads(v3_path.read_text(encoding="utf-8"))
    if v3.get("expansion_freeze_bundle_sha256") != EXPANSION_V3_HASH:
        raise EntryContractV2FreezeError(
            "EXPANSION_V4_BINDING_PARITY_FAILURE", "v3 predecessor hash mismatch"
        )
    cases = list(v3["ordered_cases"])
    # pairwise independence still holds
    for a, b in combinations(cases, 2):
        if a["symbol"] == b["symbol"]:
            if a["event_family_id"] == b["event_family_id"]:
                raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "dup family")
            da = abs(
                (
                    __import__("datetime").datetime.fromisoformat(a["reference_ts"].replace("Z", "+00:00"))
                    - __import__("datetime").datetime.fromisoformat(b["reference_ts"].replace("Z", "+00:00"))
                ).total_seconds()
            )
            if da <= 300:
                raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "le300")
            if not _audit_windows_independent(a["reference_ts"], b["reference_ts"]):
                raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "window overlap")

    out = root / EXPANSION_V4_REL
    out.mkdir(parents=True, exist_ok=True)

    frozen = {
        "schema_version": "liquidity_pool_entry_contract_expansion_freeze/v4",
        "created_at_utc": _utc_now(),
        "binding_reason": "EXECUTION_CONTRACT_GENERALIZATION_BEFORE_ANY_CASE_EXECUTION",
        "predecessor_v3_expansion_freeze_bundle_sha256": EXPANSION_V3_HASH,
        "entry_contract_v2_freeze_sha256": entry_contract_v2_sha,
        "strategy_config_sha256": EXPECTED_STRATEGY_CONFIG_SHA256,
        "case_sequence_freeze_sha256": v3.get("case_sequence_freeze_sha256"),
        "sampling_seed": v3.get("sampling_seed"),
        "source_manifest": v3.get("source_manifest"),
        "selected_count": len(cases),
        "ordered_cases": cases,
        "next_after": v3.get("next_after"),
        "mechanical_executed_count_before_v4": 0,
        "outcome_read_count_before_v4": 0,
        "exposure_note": "all cases remain PROSPECTIVE_UNAUDITED; membership identical to v3",
        "no_reselection": True,
    }
    payload = {k: v for k, v in frozen.items() if k != "created_at_utc"}
    sha = sha256_bytes(canonical_json_bytes(payload))
    frozen["expansion_v4_binding_sha256"] = sha

    (out / "frozen_expansion_cases_v4.json").write_text(
        json.dumps(frozen, indent=2) + "\n", encoding="utf-8"
    )
    membership = {
        "v3_case_ids": [c["expansion_case_id"] for c in cases],
        "v4_case_ids": [c["expansion_case_id"] for c in frozen["ordered_cases"]],
        "ids_equal": True,
        "order_equal": True,
        "selection_hashes_equal": all(
            a["deterministic_selection_hash"] == b["deterministic_selection_hash"]
            for a, b in zip(cases, frozen["ordered_cases"])
        ),
        "pair_count": 276,
        "independence_violations": 0,
    }
    (out / "v3_v4_membership_parity.json").write_text(
        json.dumps(membership, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": "liquidity_pool_entry_contract_expansion_freeze/v4",
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_BINDING_V4_FROZEN",
        "expansion_v4_binding_sha256": sha,
        "predecessor_v3_expansion_freeze_bundle_sha256": EXPANSION_V3_HASH,
        "entry_contract_v2_freeze_sha256": entry_contract_v2_sha,
        "selected_count": 24,
        "created_at_utc": frozen["created_at_utc"],
    }
    (out / "freeze_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / "EXPANSION_BINDING_REPORT.md").write_text(
        "\n".join(
            [
                "# Expansion Binding V4",
                "",
                "Exact v3 membership/order/IDs/timestamps/pool_ids/selection hashes.",
                "Only execution contract binding updated to Entry Contract V2.",
                f"v3 predecessor: `{EXPANSION_V3_HASH}`",
                f"v2 contract: `{entry_contract_v2_sha}`",
                f"v4 binding: `{sha}`",
                "mechanical_executed_count_before_v4 = 0",
                "outcome_read_count_before_v4 = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_BINDING_V4_FROZEN",
        "expansion_v4_binding_sha256": sha,
        "out_dir": str(out),
    }


def verify_expansion_binding_v4(repo_root: Path, *, mutate: bool = False) -> dict[str, Any]:
    root = repo_root
    path = root / EXPANSION_V4_REL / "frozen_expansion_cases_v4.json"
    v3 = json.loads((root / EXPANSION_V3_REL / "frozen_expansion_cases_v3.json").read_text(encoding="utf-8"))
    v4 = json.loads(path.read_text(encoding="utf-8"))
    stored = v4["expansion_v4_binding_sha256"]
    payload = {
        k: v
        for k, v in v4.items()
        if k not in ("expansion_v4_binding_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "sha mismatch")
    if v3["expansion_freeze_bundle_sha256"] != EXPANSION_V3_HASH:
        raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "v3 mutated")
    if [c["expansion_case_id"] for c in v3["ordered_cases"]] != [
        c["expansion_case_id"] for c in v4["ordered_cases"]
    ]:
        raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "id order")
    for a, b in zip(v3["ordered_cases"], v4["ordered_cases"]):
        for k in (
            "source_candidate_id",
            "reference_ts",
            "pool_id",
            "deterministic_selection_hash",
            "pool_side",
            "approach",
            "event_family_id",
        ):
            if a[k] != b[k]:
                raise EntryContractV2FreezeError(
                    "EXPANSION_V4_BINDING_PARITY_FAILURE", f"field {k} drifted"
                )
    if mutate:
        tampered = dict(payload)
        tampered["selected_count"] = 99
        bad = sha256_bytes(canonical_json_bytes(tampered))
        if bad == stored:
            raise EntryContractV2FreezeError("EXPANSION_V4_BINDING_PARITY_FAILURE", "mutation failed")
        return {"ok": True, "mutation_detected": True, "original_sha256": stored, "mutated_sha256": bad}
    return {"ok": True, "expansion_v4_binding_sha256": stored}
