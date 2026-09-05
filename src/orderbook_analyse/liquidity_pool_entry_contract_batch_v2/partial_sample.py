"""Deterministic 12/24 partial sample selection for Expansion batch V2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.cases import (
    case_by_id,
    load_v4_freeze,
    ordered_cases,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
    atomic_write_json,
    batch_root,
)

# Exact frozen selection — must not be altered by outcomes / runtime / room.
EXPECTED_PARTIAL_ASK_IDS = (
    "EXP_01",
    "EXP_02",
    "EXP_05",
    "EXP_06",
    "EXP_08",
    "EXP_11",
)
EXPECTED_PARTIAL_BID_IDS = (
    "EXP_03",
    "EXP_04",
    "EXP_07",
    "EXP_09",
    "EXP_10",
    "EXP_17",
)
EXPECTED_PARTIAL_12_IDS = tuple(
    sorted(EXPECTED_PARTIAL_ASK_IDS + EXPECTED_PARTIAL_BID_IDS)
)
PARTIAL_CONCURRENCY = 2
FORBIDDEN_OUTCOME_FIELDS = (
    "outcome",
    "pnl",
    "mfe",
    "mae",
    "forward_return",
    "evidence_class",
    "unblind",
)


class PartialSampleError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def select_partial_12_cases(frozen: dict[str, Any]) -> dict[str, Any]:
    """First 6 ASK + first 6 BID by expansion_case_id from frozen v4 ordered set."""
    asks = sorted(
        [c for c in ordered_cases(frozen) if c["pool_side"] == "ASK"],
        key=lambda c: c["expansion_case_id"],
    )
    bids = sorted(
        [c for c in ordered_cases(frozen) if c["pool_side"] == "BID"],
        key=lambda c: c["expansion_case_id"],
    )
    ask6 = asks[:6]
    bid6 = bids[:6]
    ask_ids = tuple(c["expansion_case_id"] for c in ask6)
    bid_ids = tuple(c["expansion_case_id"] for c in bid6)
    if ask_ids != EXPECTED_PARTIAL_ASK_IDS:
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"ASK selection {ask_ids} != {EXPECTED_PARTIAL_ASK_IDS}",
        )
    if bid_ids != EXPECTED_PARTIAL_BID_IDS:
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"BID selection {bid_ids} != {EXPECTED_PARTIAL_BID_IDS}",
        )
    selected = ask6 + bid6
    # Stable execution order: sorted by expansion_case_id
    selected_sorted = sorted(selected, key=lambda c: c["expansion_case_id"])
    ids = [c["expansion_case_id"] for c in selected_sorted]
    if tuple(ids) != EXPECTED_PARTIAL_12_IDS:
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"combined ids {ids} != {list(EXPECTED_PARTIAL_12_IDS)}",
        )
    return {
        "ask_case_ids": list(ask_ids),
        "bid_case_ids": list(bid_ids),
        "case_ids": ids,
        "cases": selected_sorted,
    }


def _case_row_public(row: dict[str, Any]) -> dict[str, Any]:
    """Persist selection fields only — no outcome fields."""
    return {
        "expansion_case_id": row["expansion_case_id"],
        "source_candidate_id": row["source_candidate_id"],
        "symbol": row["symbol"],
        "reference_ts": row["reference_ts"],
        "pool_id": row["pool_id"],
        "pool_side": row["pool_side"],
        "approach": row["approach"],
        "pool_timeframe": row["pool_timeframe"],
        "pool_lower_edge": row.get("pool_lower_edge"),
        "pool_upper_edge": row.get("pool_upper_edge"),
        "pool_first_available_ts": row["pool_first_available_ts"],
        "event_family_id": row["event_family_id"],
        "audit_window_start": row.get("audit_window_start"),
        "audit_window_end": row.get("audit_window_end"),
        "exposure_status": row["exposure_status"],
        "deterministic_selection_hash": row["deterministic_selection_hash"],
    }


def build_partial_sample_12_manifest(
    repo_root: Path,
    *,
    entry_contract_v2_sha: str,
    expansion_v4_sha: str,
    strategy_config_sha: str,
) -> dict[str, Any]:
    frozen = load_v4_freeze(repo_root)
    sel = select_partial_12_cases(frozen)
    cases_public = [_case_row_public(c) for c in sel["cases"]]
    blob = json.dumps(cases_public, sort_keys=True, default=str).lower()
    for key in FORBIDDEN_OUTCOME_FIELDS:
        if f'"{key}"' in blob and key not in ("unblind",):
            raise PartialSampleError(
                "OUTCOME_BLINDNESS_VIOLATION",
                f"forbidden outcome field in manifest selection: {key}",
            )

    n_ask = sum(1 for c in cases_public if c["pool_side"] == "ASK")
    n_bid = sum(1 for c in cases_public if c["pool_side"] == "BID")
    if n_ask != 6 or n_bid != 6:
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"expected 6/6 ASK/BID, got {n_ask}/{n_bid}",
        )

    payload = {
        "format_version": "liquidity_pool_entry_contract_batch_partial_12/v1",
        "selection_rule": "first_6_ASK_and_first_6_BID_by_expansion_case_id",
        "selection_immutable": True,
        "selection_not_conditioned_on": [
            "mechanical_verdict",
            "room_gate",
            "outcome",
            "runtime",
        ],
        "entry_contract_v2_freeze_sha256": entry_contract_v2_sha,
        "expansion_v4_binding_sha256": expansion_v4_sha,
        "strategy_config_sha256": strategy_config_sha,
        "concurrency_max": PARTIAL_CONCURRENCY,
        "ask_case_ids": sel["ask_case_ids"],
        "bid_case_ids": sel["bid_case_ids"],
        "case_ids": sel["case_ids"],
        "n_ask": n_ask,
        "n_bid": n_bid,
        "n_total": 12,
        "cases": cases_public,
        "outcomes_included": False,
        "mechanical_only": True,
    }
    digest = sha256_bytes(canonical_json_bytes(payload))
    payload["partial_sample_12_manifest_sha256"] = digest

    out_path = batch_root(repo_root) / "partial_sample_12_manifest.json"
    atomic_write_json(out_path, payload)
    return payload


def load_partial_manifest(repo_root: Path) -> dict[str, Any]:
    path = batch_root(repo_root) / "partial_sample_12_manifest.json"
    if not path.is_file():
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE", "missing partial_sample_12_manifest.json"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def verify_partial_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    stored = manifest.get("partial_sample_12_manifest_sha256")
    payload = {k: v for k, v in manifest.items() if k != "partial_sample_12_manifest_sha256"}
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise PartialSampleError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"manifest sha mismatch stored={stored} recomputed={recomputed}",
        )
    if tuple(manifest.get("ask_case_ids") or ()) != EXPECTED_PARTIAL_ASK_IDS:
        raise PartialSampleError("PARALLEL_BATCH_COORDINATION_FAILURE", "ASK ids drift")
    if tuple(manifest.get("bid_case_ids") or ()) != EXPECTED_PARTIAL_BID_IDS:
        raise PartialSampleError("PARALLEL_BATCH_COORDINATION_FAILURE", "BID ids drift")
    if tuple(manifest.get("case_ids") or ()) != EXPECTED_PARTIAL_12_IDS:
        raise PartialSampleError("PARALLEL_BATCH_COORDINATION_FAILURE", "combined ids drift")
    if int(manifest.get("n_ask") or 0) != 6 or int(manifest.get("n_bid") or 0) != 6:
        raise PartialSampleError("PARALLEL_BATCH_COORDINATION_FAILURE", "6/6 count drift")
    if manifest.get("outcomes_included") is not False:
        raise PartialSampleError("OUTCOME_BLINDNESS_VIOLATION", "outcomes_included must be false")
    return {"ok": True, "partial_sample_12_manifest_sha256": stored}
