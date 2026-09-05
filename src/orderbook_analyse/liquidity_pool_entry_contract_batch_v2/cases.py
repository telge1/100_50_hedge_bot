"""Load Expansion binding v4 cases; deterministic smoke selection EXP_01/EXP_03."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    EXPECTED_V4_HASH,
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
    V4_FREEZE_FILE,
    V4_FREEZE_REL,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import FrozenInputHashMismatch
from orderbook_analyse.liquidity_pool_entry_contract_v2.case_spec import (
    CaseSpec,
    case_spec_from_frozen_expansion_case,
)


def load_v4_freeze(repo_root: Path) -> dict[str, Any]:
    path = repo_root / V4_FREEZE_REL / V4_FREEZE_FILE
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("expansion_v4_binding_sha256") != EXPECTED_V4_HASH:
        raise FrozenInputHashMismatch(
            f"v4 hash {frozen.get('expansion_v4_binding_sha256')} != {EXPECTED_V4_HASH}"
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
    """Pre-declared before market access: EXP_01 (ASK) then EXP_03 (BID)."""
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


def case_spec_from_v4_row(row: dict[str, Any]) -> CaseSpec:
    return case_spec_from_frozen_expansion_case(row)


def mechanical_executed_count_before(frozen: dict[str, Any]) -> int:
    return int(frozen.get("mechanical_executed_count_before_v4") or 0)


def outcome_read_count_before(frozen: dict[str, Any]) -> int:
    return int(frozen.get("outcome_read_count_before_v4") or 0)
