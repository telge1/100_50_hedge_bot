"""V1 CASE_03/04/05 mechanical regression from stored artifacts (no new queries)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_v2.decision import (
    MicroEvidence,
    resolve_mechanical_decision,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.geometry import resolve_geometry
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    load_effective_room_config,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import PoolCandidate

CASE_ARTIFACTS = (
    (
        "CASE_03",
        "results/case_03_frozen_bid_pool_causal_reaction_audit_v1/mechanical_verdict_pre_unblind.json",
    ),
    (
        "CASE_04",
        "results/case_04_frozen_bid_pool_causal_reaction_audit_v1/mechanical_verdict_pre_unblind.json",
    ),
    (
        "CASE_05",
        "results/case_05_frozen_bid_pool_entry_contract_v1_audit/mechanical_verdict_pre_unblind.json",
    ),
)

# Decision fields compared (exclude contract_version / hashes / pure metadata)
COMPARE_KEYS = (
    "mechanical_verdict",
    "reaction",
    "candidate_direction",
    "mechanical_trade_verdict",
)


def _evidence_from_v1_mech(mech: dict[str, Any]) -> MicroEvidence:
    """Rebuild MicroEvidence from stored V1 BID mechanical artifact (no outcome files)."""
    long_b = mech["long_branch"]
    short_b = mech["short_branch"]
    # V1 BID: defense=LONG, breakout=SHORT
    defense_ok = bool(long_b.get("eligible"))
    # V1 stores short eligible sometimes True even when contested (CASE_05)
    breakout_contested = bool(short_b.get("contested"))
    breakout_ok = bool(short_b.get("eligible")) and not breakout_contested
    # When contested, V1 still may mark eligible True — treat contested as breakout_ok False
    if breakout_contested:
        breakout_ok = False
    return MicroEvidence(
        seen_inside=True,
        arrival_present=True,
        defense_ok=defense_ok,
        breakout_ok=breakout_ok,
        breakout_contested=breakout_contested,
        defense_entry=long_b.get("entry_price"),
        breakout_entry=short_b.get("entry_price") or mech.get("entry_price"),
        defense_first_ts=long_b.get("first_available_ts"),
        breakout_first_ts=short_b.get("first_available_ts") or mech.get("first_available_ts"),
        attack_eff_count=0,
        counter_count=0,
        two_sided_count=0,
    )


def _pools_stub_for_room(mech: dict[str, Any]) -> list[PoolCandidate]:
    """Minimal pools so room recompute is skippable for contest NO_TRADE paths.

    Contest paths do not require TRADE; room may still be evaluated on contested branch.
    Use empty pool list — room becomes TARGET_NOT_OBSERVED or distance fail; we only
    compare COMPARE_KEYS which exclude room details for regression.
    """
    return []


def run_v1_regression(repo_root: Path) -> dict[str, Any]:
    effective = load_effective_room_config(repo_root)
    results = []
    all_ok = True
    for case_id, rel in CASE_ARTIFACTS:
        path = repo_root / rel
        mech = json.loads(path.read_text(encoding="utf-8"))
        # Never open outcome_comparison.json
        geom = resolve_geometry(
            pool_side="BID",
            approach="FROM_ABOVE",
            lower=float(mech.get("back_edge") or 0) or 1.0,
            upper=float(mech.get("front_edge") or 0) or 2.0,
        )
        # Prefer edges from frozen_case_input if present alongside
        parent = path.parent
        fin = parent / "frozen_case_input.json"
        if fin.is_file():
            fi = json.loads(fin.read_text(encoding="utf-8"))
            lo = float(fi.get("lower") or fi.get("back_edge"))
            hi = float(fi.get("upper") or fi.get("front_edge"))
            geom = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=lo, upper=hi)

        evidence = _evidence_from_v1_mech(mech)
        decision = resolve_mechanical_decision(
            geom=geom,
            evidence=evidence,
            pools=_pools_stub_for_room(mech),
            effective=effective,
        )
        expected = {k: mech.get(k) for k in COMPARE_KEYS}
        # V1 may store entry_price instead of mechanical_entry_price for older artifacts
        got = {
            "mechanical_verdict": decision.mechanical_verdict,
            "reaction": decision.reaction,
            "candidate_direction": decision.candidate_direction,
            "mechanical_trade_verdict": decision.mechanical_trade_verdict,
        }
        mismatches = [k for k in COMPARE_KEYS if expected[k] != got[k]]
        ok = not mismatches
        all_ok = all_ok and ok
        results.append(
            {
                "case_id": case_id,
                "ok": ok,
                "expected": expected,
                "got": got,
                "mismatches": mismatches,
                "source_artifact": rel,
                "outcomes_read": False,
            }
        )
    return {
        "ok": all_ok,
        "verdict": "V1_REGRESSION_OK" if all_ok else "ENTRY_CONTRACT_V2_REGRESSION_FAILURE",
        "cases": results,
    }
