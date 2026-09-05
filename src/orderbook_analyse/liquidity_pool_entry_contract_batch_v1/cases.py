"""Load expansion v3 freeze cases; deterministic smoke selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import (
    EXPECTED_V3_HASH,
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
    V3_FREEZE_FILE,
    V3_FREEZE_REL,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.hashes import FrozenInputHashMismatch


def load_v3_freeze(repo_root: Path) -> dict[str, Any]:
    path = repo_root / V3_FREEZE_REL / V3_FREEZE_FILE
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("expansion_freeze_bundle_sha256") != EXPECTED_V3_HASH:
        raise FrozenInputHashMismatch(
            f"v3 hash {frozen.get('expansion_freeze_bundle_sha256')} != {EXPECTED_V3_HASH}"
        )
    return frozen


def ordered_cases(frozen: dict[str, Any]) -> list[dict[str, Any]]:
    return list(frozen["ordered_cases"])


def case_by_id(frozen: dict[str, Any], case_id: str) -> dict[str, Any]:
    for c in ordered_cases(frozen):
        if c["expansion_case_id"] == case_id:
            return c
    raise KeyError(case_id)


def smoke_selection(frozen: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic: lowest ASK expansion_case_id + lowest BID expansion_case_id.

    Pre-declared: EXP_01 (ASK), EXP_03 (BID). Verified against freeze contents.
    """
    asks = sorted(
        [c for c in ordered_cases(frozen) if c["pool_side"] == "ASK"],
        key=lambda c: c["expansion_case_id"],
    )
    bids = sorted(
        [c for c in ordered_cases(frozen) if c["pool_side"] == "BID"],
        key=lambda c: c["expansion_case_id"],
    )
    if not asks or not bids:
        raise RuntimeError("smoke selection requires ASK and BID cases")
    ask = asks[0]
    bid = bids[0]
    if ask["expansion_case_id"] != SMOKE_ASK_CASE_ID:
        raise RuntimeError(
            f"expected lowest ASK={SMOKE_ASK_CASE_ID}, got {ask['expansion_case_id']}"
        )
    if bid["expansion_case_id"] != SMOKE_BID_CASE_ID:
        raise RuntimeError(
            f"expected lowest BID={SMOKE_BID_CASE_ID}, got {bid['expansion_case_id']}"
        )
    return [ask, bid]


def expansion_case_to_audit_params(case: dict[str, Any]) -> dict[str, Any]:
    """Declarative params only — no outcome fields."""
    return {
        "expansion_case_id": case["expansion_case_id"],
        "source_candidate_id": case["source_candidate_id"],
        "symbol": case["symbol"],
        "reference_ts": case["reference_ts"],
        "pool_id": case["pool_id"],
        "pool_side": case["pool_side"],
        "approach": case["approach"],
        "pool_timeframe": case["pool_timeframe"],
        "pool_lower_edge": case.get("pool_lower_edge"),
        "pool_upper_edge": case.get("pool_upper_edge"),
        "pool_first_available_ts": case["pool_first_available_ts"],
        "event_family_id": case["event_family_id"],
        "audit_window_start": case.get("audit_window_start"),
        "audit_window_end": case.get("audit_window_end"),
        "exposure_status": case["exposure_status"],
        "deterministic_selection_hash": case["deterministic_selection_hash"],
    }
