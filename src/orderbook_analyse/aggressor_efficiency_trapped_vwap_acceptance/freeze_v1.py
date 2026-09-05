"""Freeze V1 causal edge / disambiguation / trap / acceptance contracts.

outcome_used_for_matching = false
outcome_used_for_thresholds = false
outcome_used_for_state_definition = false
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import JoinThresholds
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import write_json

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_SRC = PACKAGE_ROOT.parent  # orderbook_analyse package root under src/

FREEZE_SOURCE_FILES = (
    "aggressor_efficiency_trapped_vwap_acceptance/contracts.py",
    "aggressor_efficiency_trapped_vwap_acceptance/edge_match.py",
    "aggressor_efficiency_trapped_vwap_acceptance/edge_disambiguation.py",
    "aggressor_efficiency_trapped_vwap_acceptance/edge_catalog.py",
    "aggressor_efficiency_trapped_vwap_acceptance/edge_acceptance.py",
    "aggressor_efficiency_trapped_vwap_acceptance/trapped_vwap.py",
    "aggressor_efficiency_trapped_vwap_acceptance/combined_state.py",
    "aggressor_efficiency_trapped_vwap_acceptance/efficiency.py",
    "aggressor_efficiency_trapped_vwap_acceptance/pipeline.py",
    "aggressor_efficiency_trapped_vwap_acceptance/event_adapter.py",
    "aggressor_efficiency_flip/contracts.py",
)

OUTCOME_HORIZONS_EVAL_S = (10, 30, 60, 180, 300, 900, 1800, 3600)

FREEZE_FLAGS = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
}


class FreezeViolation(RuntimeError):
    """Raised when freeze-relevant sources changed after freeze creation."""


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _payload_sha256(payload: Any) -> str:
    raw = json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_freeze_payloads() -> dict[str, Any]:
    join = JoinThresholds()
    dis = DisambiguationThresholds()
    cfg = TrapAcceptConfig()
    contract = {
        **FREEZE_FLAGS,
        "package": "FROZEN_HIGH_EDGE_FORWARD_OUTCOME_EVALUATION_V1",
        "frozen_logic": [
            "aef_event_definition",
            "flow_contemporaneous_post_windows",
            "buy_ask_sell_bid",
            "edge_eligibility_staleness_reach",
            "trade_touch_zone_cluster",
            "lex_disambiguation",
            "confidence_high_medium_low_none",
            "trap_acceptance_combined_states",
            "causal_decision_checkpoints",
        ],
        "primary_outcome_anchor": "state_available_ts",
        "diagnostic_anchors": ["flow_start", "flow_end"],
        "outcome_horizons_s": list(OUTCOME_HORIZONS_EVAL_S),
        "acceptance_gate_confidence": list(dis.accept_confidence),
        "side_map": {"LONG": "BID", "SHORT": "ASK", "Buy": "ASK", "Sell": "BID"},
    }
    thresholds = {
        "join": join.to_dict(),
        "disambiguation": dis.to_dict(),
        "trap_accept": cfg.to_dict(),
        "outcome_horizons_eval_s": list(OUTCOME_HORIZONS_EVAL_S),
        **FREEZE_FLAGS,
    }
    rules = {
        "lex_order": [
            "correct_side",
            "visible_before_attack",
            "not_stale_coverage_ok",
            "reached_in_directional_path",
            "exact_trade_or_tick_touch",
            "front_edge_in_attack_path",
            "aggressor_notional_at_edge",
            "distance_to_flow_extremum",
            "tie_remains_MULTIPLE_EDGE_AMBIGUOUS",
        ],
        "alignment": {
            "ATTACKER_WINNING": "attack_direction",
            "ATTACKER_TRAPPED_REJECTION": "against_attack_direction",
            "ACCEPTED_ABOVE": "bullish",
            "ACCEPTED_BELOW": "bearish",
            "FAILED_BREAK": "against_break_direction",
            "BREAK_RECLAIMED": "against_break_direction",
            "ABSORPTION_NO_RESOLUTION": "no_invented_direction",
            "MIXED_OR_UNKNOWN": "descriptive_only",
        },
        "confidence_acceptance": {"HIGH": True, "MEDIUM": False, "LOW": False, "NONE": False},
        **FREEZE_FLAGS,
    }
    sources = {
        "files": list(FREEZE_SOURCE_FILES),
        "raw_ob200_root": str(
            Path(
                "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
            )
        ),
        "prior_results": [
            "results/aggressor_efficiency_trapped_vwap_acceptance_v1/",
            "results/causal_pool_edge_join_for_aggressor_trap_acceptance_v1/",
            "results/causal_pool_edge_ambiguity_resolution_v1/",
        ],
    }
    return {
        "frozen_contract": contract,
        "frozen_thresholds": thresholds,
        "frozen_rule_manifest": rules,
        "frozen_source_manifest": sources,
    }


def compute_source_hashes(src_root: Path | None = None) -> dict[str, str]:
    root = src_root or REPO_SRC
    out: dict[str, str] = {}
    for rel in FREEZE_SOURCE_FILES:
        path = root / rel
        if not path.is_file():
            raise FreezeViolation(f"missing freeze source: {path}")
        out[rel] = _file_sha256(path)
    return out


def write_freeze(output_dir: Path, *, src_root: Path | None = None) -> dict[str, Any]:
    payloads = build_freeze_payloads()
    source_hashes = compute_source_hashes(src_root)
    hashes = {
        "source_file_sha256": source_hashes,
        "contract_sha256": _payload_sha256(payloads["frozen_contract"]),
        "thresholds_sha256": _payload_sha256(payloads["frozen_thresholds"]),
        "rules_sha256": _payload_sha256(payloads["frozen_rule_manifest"]),
        "sources_manifest_sha256": _payload_sha256(payloads["frozen_source_manifest"]),
        **FREEZE_FLAGS,
    }
    hashes["freeze_bundle_sha256"] = _payload_sha256(
        {
            "contract": hashes["contract_sha256"],
            "thresholds": hashes["thresholds_sha256"],
            "rules": hashes["rules_sha256"],
            "sources": hashes["sources_manifest_sha256"],
            "files": source_hashes,
        }
    )
    write_json(output_dir / "frozen_contract.json", payloads["frozen_contract"])
    write_json(output_dir / "frozen_thresholds.json", payloads["frozen_thresholds"])
    write_json(output_dir / "frozen_rule_manifest.json", payloads["frozen_rule_manifest"])
    write_json(output_dir / "frozen_source_manifest.json", payloads["frozen_source_manifest"])
    write_json(output_dir / "frozen_hashes.json", hashes)
    return hashes


def verify_freeze(freeze_dir: Path, *, src_root: Path | None = None) -> dict[str, Any]:
    """Recompute hashes; raise FreezeViolation on mismatch."""
    stored_path = freeze_dir / "frozen_hashes.json"
    if not stored_path.is_file():
        raise FreezeViolation(f"missing {stored_path}")
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    current_files = compute_source_hashes(src_root)
    mismatches = []
    for rel, digest in stored.get("source_file_sha256", {}).items():
        cur = current_files.get(rel)
        if cur != digest:
            mismatches.append({"file": rel, "stored": digest, "current": cur})
    payloads = build_freeze_payloads()
    checks = {
        "contract_sha256": _payload_sha256(payloads["frozen_contract"]),
        "thresholds_sha256": _payload_sha256(payloads["frozen_thresholds"]),
        "rules_sha256": _payload_sha256(payloads["frozen_rule_manifest"]),
    }
    for k, v in checks.items():
        if stored.get(k) != v:
            mismatches.append({"artifact": k, "stored": stored.get(k), "current": v})
    if mismatches:
        raise FreezeViolation(f"freeze hash mismatch: {mismatches}")
    return {"ok": True, "freeze_bundle_sha256": stored.get("freeze_bundle_sha256"), **FREEZE_FLAGS}
