"""Capability probe: can hashed pipeline run mechanical-only for EXP ASK+BID?"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1 import pipeline as case_pipeline
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import (
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
)


def assess_mechanical_unblind_separation(repo_root: Path) -> dict[str, Any]:
    """Return whether batch can execute without modifying hashed pipeline files."""
    blockers: list[str] = []

    sig = inspect.signature(case_pipeline.run_audit)
    params = set(sig.parameters)
    has_mech_only = bool(params & {"mechanical_only", "skip_unblind", "unblind"})
    if not has_mech_only:
        blockers.append(
            "hashed case_03 pipeline.run_audit has no mechanical_only/skip_unblind parameter; "
            "always continues into unblind phase after mechanical_verdict_pre_unblind.json persist"
        )

    src = inspect.getsource(case_pipeline.run_audit)
    if "UNBLIND" in src or "outcome_comparison" in src or "six_case_summary" in src:
        if "mechanical_only" not in src and "skip_unblind" not in src:
            blockers.append(
                "run_audit source always performs post-mechanical unblind/comparison block; "
                "cannot gate without changing hashed pipeline.py"
            )

    # BID-only case loader
    load_src = inspect.getsource(case_pipeline.load_frozen_bid_case)
    if 'direction"] != "BID"' in load_src or "FROM_ABOVE" in load_src:
        blockers.append(
            f"hashed load_frozen_bid_case requires BID/FROM_ABOVE; "
            f"smoke {SMOKE_ASK_CASE_ID} is ASK/FROM_BELOW — cannot execute without pipeline change"
        )

    # Hardcoded BID selected_pool in run_audit
    if '"side": "BID"' in src or "side\": \"BID\"" in src or 'side": "BID"' in inspect.getsource(
        case_pipeline.run_audit
    ):
        blockers.append(
            "hashed run_audit hardcodes selected_pool side=BID / approach=FROM_ABOVE; "
            "ASK contacts not supported without changing hashed pipeline.py"
        )

    # Case load from CASE sequence freeze, not expansion freeze
    if "load_frozen_bid_case" in src:
        blockers.append(
            "run_audit loads CASE_XX from case-sequence freeze via BidCaseAuditSpec; "
            "no declarative Expansion EXP_* input path without changing hashed pipeline.py"
        )

    ok = len(blockers) == 0
    return {
        "separable_without_hashed_file_change": ok,
        "verdict_if_blocked": None if ok else "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED",
        "smoke_ask_case_id": SMOKE_ASK_CASE_ID,
        "smoke_bid_case_id": SMOKE_BID_CASE_ID,
        "run_audit_parameters": sorted(params),
        "blockers": blockers,
        "allowed_without_change": [
            "hash verification",
            "smoke case selection EXP_01+EXP_03",
            "batch status / resume scaffolding",
            "unblind gate (mechanical_complete_count == 24)",
        ],
        "note": (
            "Batch runner must only orchestrate. Reimplementing ASK reaction logic or "
            "monkeypatching unblind would either change decision semantics or bypass "
            "the fail-closed separation contract."
        ),
        "repo_root": str(repo_root),
    }
