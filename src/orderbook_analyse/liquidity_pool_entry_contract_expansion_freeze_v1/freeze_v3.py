"""Expansion freeze v3 — audit-window-independent 24-case sample."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1 import (
    CASE_SEQUENCE_FREEZE_SHA256,
    ENTRY_CONTRACT_FREEZE_SHA256,
    FORBIDDEN_FIELD_SUBSTR,
    SAMPLING_SEED,
    STRATEGY_CONFIG_REL,
    STRATEGY_CONFIG_SHA256,
    TARGET_COUNT,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.coverage import (
    DEFAULT_RAW_ROOT,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze import (
    ExpansionFreezeError,
    OVERLAP_SAME_SYMBOL_S,
    PRIMARY_SOURCE_REL,
    apply_exposure_exclusion,
    build_eligible_universe,
    embargo_policy,
    inventory_candidate_sources,
    load_exposed_cases,
    load_source_rows,
    resolve_unique_pool_contact_source,
    selection_hash,
    write_csv,
    write_json,
    _assign_expansion_ids,
    _iso,
    _next_after,
    _utc,
    _utc_now,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.integrity_audit import (
    CASE_PIPELINE_MAX_POST_S,
    CASE_PIPELINE_PRE_S,
    CASE_RUNTIME_S,
    DOCUMENTED_OVERLAP_S,
    EXPECTED_V1_HASH,
    V2_RESULTS_REL,
    audit_window_case_pipeline,
)

V3_RESULTS_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v3"
V3_SCHEMA = "liquidity_pool_entry_contract_expansion_freeze/v3"
EXPECTED_V2_HASH = "adec08fd23c9c5e84d6df9bcd0150ba1e28101cd069727b0f0d44571a10a25e0"
ASK_TARGET = 12
BID_TARGET = 12


class ExpansionV3Error(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def audit_window_bounds(reference_ts: str) -> tuple[datetime, datetime]:
    """Inclusive deep-audit window: [ref-30s, ref+1800s]."""
    return audit_window_case_pipeline(reference_ts)


def windows_independent(a_ref: str, b_ref: str) -> bool:
    """True iff inclusive intervals share no second.

    Formal: candidate.start > retained.end OR candidate.end < retained.start
    """
    a0, a1 = audit_window_bounds(a_ref)
    b0, b1 = audit_window_bounds(b_ref)
    return a0 > b1 or a1 < b0


def windows_overlap(a_ref: str, b_ref: str) -> bool:
    return not windows_independent(a_ref, b_ref)


def pair_independence_row(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    da = _utc(a["reference_ts"])
    db = _utc(b["reference_ts"])
    delta = abs((da - db).total_seconds())
    same_sym = a["symbol"] == b["symbol"]
    same_fam = a["event_family_id"] == b["event_family_id"]
    wa0, wa1 = audit_window_bounds(a["reference_ts"])
    wb0, wb1 = audit_window_bounds(b["reference_ts"])
    win_ov = same_sym and windows_overlap(a["reference_ts"], b["reference_ts"])
    le300 = same_sym and delta <= DOCUMENTED_OVERLAP_S
    return {
        "a": a.get("expansion_case_id") or a["source_candidate_id"],
        "b": b.get("expansion_case_id") or b["source_candidate_id"],
        "source_candidate_a": a["source_candidate_id"],
        "source_candidate_b": b["source_candidate_id"],
        "symbol_a": a["symbol"],
        "symbol_b": b["symbol"],
        "delta_seconds": delta,
        "same_symbol": same_sym,
        "same_pool_id": a["pool_id"] == b["pool_id"],
        "same_event_family_id": same_fam,
        "audit_window_a_start": _iso(wa0),
        "audit_window_a_end": _iso(wa1),
        "audit_window_b_start": _iso(wb0),
        "audit_window_b_end": _iso(wb1),
        "audit_window_overlap_same_symbol": win_ov,
        "violates_event_family": same_fam,
        "violates_le_300s": le300,
        "violates_independence": same_fam or le300 or win_ov,
        "ref_a": a["reference_ts"],
        "ref_b": b["reference_ts"],
        "pool_id_a": a["pool_id"],
        "pool_id_b": b["pool_id"],
    }


def pairwise_independence(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs = [pair_independence_row(a, b) for a, b in combinations(cases, 2)]
    violations = [p for p in pairs if p["violates_independence"]]
    return pairs, violations


def conflicts_with_retained(candidate: dict[str, Any], retained: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits = []
    for prev in retained:
        row = pair_independence_row(candidate, prev)
        if row["violates_independence"]:
            hits.append(row)
    return hits


def select_independent_24(
    eligible: list[dict[str, Any]],
    *,
    ask_target: int = ASK_TARGET,
    bid_target: int = BID_TARGET,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Hash-ordered per side; keep only if independent of ALL retained (global)."""
    ask_pool = sorted(
        [c for c in eligible if c["pool_side"] == "ASK"],
        key=lambda x: x["deterministic_selection_hash"],
    )
    bid_pool = sorted(
        [c for c in eligible if c["pool_side"] == "BID"],
        key=lambda x: x["deterministic_selection_hash"],
    )

    retained: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []

    def try_fill(side: str, pool: list[dict[str, Any]], need: int) -> None:
        have = sum(1 for c in retained if c["pool_side"] == side)
        for c in pool:
            if have >= need:
                break
            if any(r["source_candidate_id"] == c["source_candidate_id"] for r in retained):
                continue
            hits = conflicts_with_retained(c, retained)
            if hits:
                excluded.append(
                    {
                        "source_candidate_id": c["source_candidate_id"],
                        "pool_side": side,
                        "reference_ts": c["reference_ts"],
                        "phase": f"select_{side}",
                        "n_conflicts": len(hits),
                        "first_conflict_with": hits[0]["a"] if hits[0]["b"] == c["source_candidate_id"] else hits[0]["b"],
                        "first_conflict_reason": (
                            "event_family"
                            if hits[0]["violates_event_family"]
                            else (
                                "le_300s"
                                if hits[0]["violates_le_300s"]
                                else "audit_window_overlap"
                            )
                        ),
                    }
                )
                continue
            retained.append(c)
            have += 1
            trace.append(
                {
                    "action": "keep",
                    "side": side,
                    "id": c["source_candidate_id"],
                    "reference_ts": c["reference_ts"],
                    "hash": c["deterministic_selection_hash"],
                }
            )

    # Primary pass: fill ASK then BID in hash order (each checked globally)
    try_fill("ASK", ask_pool, ask_target)
    try_fill("BID", bid_pool, bid_target)

    # Cross-side refill: if one side short, keep scanning that side against global retained
    ask_n = sum(1 for c in retained if c["pool_side"] == "ASK")
    bid_n = sum(1 for c in retained if c["pool_side"] == "BID")
    if ask_n < ask_target:
        try_fill("ASK", ask_pool, ask_target)
    if bid_n < bid_target:
        try_fill("BID", bid_pool, bid_target)

    ask_n = sum(1 for c in retained if c["pool_side"] == "ASK")
    bid_n = sum(1 for c in retained if c["pool_side"] == "BID")
    if ask_n != ask_target or bid_n != bid_target:
        raise ExpansionV3Error(
            "EXPANSION_V3_INDEPENDENT_SAMPLE_NOT_FEASIBLE",
            f"ASK/BID={ask_n}/{bid_n} need {ask_target}/{bid_target}; "
            f"retained={len(retained)} excluded={len(excluded)}",
        )

    pairs, violations = pairwise_independence(retained)
    if violations:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"post-selection violations={len(violations)}",
        )

    meta = {
        "ask_target": ask_target,
        "bid_target": bid_target,
        "ask_selected": ask_n,
        "bid_selected": bid_n,
        "eligible_ask": len(ask_pool),
        "eligible_bid": len(bid_pool),
        "excluded_count": len(excluded),
        "excluded_sample": excluded[:80],
        "selection_trace_count": len(trace),
        "refill_global_check": True,
        "audit_window": {
            "pre_s": CASE_PIPELINE_PRE_S,
            "post_s": CASE_PIPELINE_MAX_POST_S,
            "inclusive": True,
            "independence_rule": "candidate.start > retained.end OR candidate.end < retained.start",
        },
        "pairwise_checked": len(pairs),
        "pairwise_violations": 0,
    }
    return sorted(retained, key=lambda x: x["deterministic_selection_hash"]), meta


def list_v2_audit_window_overlaps(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / V2_RESULTS_REL / "frozen_expansion_cases_v2.json"
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("expansion_freeze_bundle_sha256") != EXPECTED_V2_HASH:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"v2 hash mismatch expected={EXPECTED_V2_HASH}",
        )
    cases = frozen["ordered_cases"]
    pairs, _ = pairwise_independence(cases)
    return [p for p in pairs if p["audit_window_overlap_same_symbol"]]


def build_expansion_freeze_v3(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = root / V3_RESULTS_REL
    out_dir.mkdir(parents=True, exist_ok=True)

    # Verify predecessors untouched
    v1_path = root / "results/liquidity_pool_entry_contract_expansion_freeze_v1/frozen_expansion_cases_v1.json"
    v2_path = root / V2_RESULTS_REL / "frozen_expansion_cases_v2.json"
    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    if v1.get("expansion_freeze_bundle_sha256") != EXPECTED_V1_HASH:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "v1 hash drifted")
    if v2.get("expansion_freeze_bundle_sha256") != EXPECTED_V2_HASH:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "v2 hash drifted")

    v2_overlaps = list_v2_audit_window_overlaps(root)

    inventory = inventory_candidate_sources(root)
    resolution = resolve_unique_pool_contact_source(root, inventory)
    source = resolution["source"]
    source_rel = source["path_relative"]
    source_sha = source["sha256"]
    if source_rel != PRIMARY_SOURCE_REL:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"source path changed: {source_rel}",
        )

    raw_rows = load_source_rows(root, source_rel)
    eligible_all, _ = build_eligible_universe(
        raw_rows,
        source_rel=source_rel,
        source_sha256=source_sha,
        raw_root=root / DEFAULT_RAW_ROOT,
    )
    embargo = embargo_policy()
    exposed = load_exposed_cases(root)
    eligible, exposure_excluded = apply_exposure_exclusion(eligible_all, exposed, embargo)

    try:
        selected, sel_meta = select_independent_24(eligible)
    except ExpansionV3Error:
        raise
    except Exception as exc:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", str(exc)) from exc

    ordered_cases = _assign_expansion_ids(selected)
    # attach audit window fields for freeze (causal metadata only)
    for c in ordered_cases:
        w0, w1 = audit_window_bounds(c["reference_ts"])
        c["audit_window_start"] = _iso(w0)
        c["audit_window_end"] = _iso(w1)
        c["audit_window_pre_s"] = CASE_PIPELINE_PRE_S
        c["audit_window_post_s"] = CASE_PIPELINE_MAX_POST_S

    next_after = _next_after(ordered_cases)
    pairs, violations = pairwise_independence(ordered_cases)
    if violations:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"final pairwise violations={len(violations)}",
        )

    # replacement vs v2
    v2_by_cand = {c["source_candidate_id"]: c for c in v2["ordered_cases"]}
    v3_ids = {c["source_candidate_id"] for c in ordered_cases}
    v2_ids = set(v2_by_cand)
    removed = [v2_by_cand[i] for i in sorted(v2_ids - v3_ids)]
    added = [c for c in ordered_cases if c["source_candidate_id"] not in v2_ids]
    retained_same = sorted(v2_ids & v3_ids)

    replacement_rows = []
    for c in removed:
        replacement_rows.append(
            {
                "change": "removed_from_v2",
                "expansion_case_id_v2": c["expansion_case_id"],
                "expansion_case_id_v3": "",
                "source_candidate_id": c["source_candidate_id"],
                "pool_side": c["pool_side"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "reason": "audit_window_overlap_or_displaced_by_independence_selection",
            }
        )
    for c in added:
        replacement_rows.append(
            {
                "change": "added_in_v3",
                "expansion_case_id_v2": "",
                "expansion_case_id_v3": c["expansion_case_id"],
                "source_candidate_id": c["source_candidate_id"],
                "pool_side": c["pool_side"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "reason": "deterministic_independence_refill",
            }
        )

    frozen_payload = {
        "schema_version": V3_SCHEMA,
        "created_at_utc": _utc_now(),
        "supersedes": {
            "v2_path": V2_RESULTS_REL,
            "v2_expansion_freeze_bundle_sha256": EXPECTED_V2_HASH,
            "v1_expansion_freeze_bundle_sha256": EXPECTED_V1_HASH,
            "reason": "audit_window_independence_same_symbol",
        },
        "source_manifest": {
            "path_relative": source_rel,
            "sha256": source_sha,
        },
        "sampling_seed": SAMPLING_SEED,
        "eligibility_policy": {
            "unchanged_from_v1_v2": True,
            "forbidden_for_eligibility": [
                "acceptance",
                "micro_pass",
                "room_pass",
                "contest",
                "outcome",
                "return",
                "pnl",
                "manual_quality",
            ],
        },
        "exposure_embargo": embargo,
        "dedup_policy": {
            "same_event_family": "reject",
            "same_symbol_overlap_s": OVERLAP_SAME_SYMBOL_S,
            "comparison_le_300s": "delta_seconds <= 300 inclusive",
            "audit_window_independence": {
                "pre_s": CASE_PIPELINE_PRE_S,
                "post_s": CASE_PIPELINE_MAX_POST_S,
                "inclusive": True,
                "rule": "candidate.start > retained.end OR candidate.end < retained.start",
                "applies_to": "same_symbol",
            },
            "refill_rule": "every candidate checked against ALL retained; final 276-pair check empty",
        },
        "stratification_policy": {
            "primary": {"ASK": ASK_TARGET, "BID": BID_TARGET},
            "selection_order": "per_side_hash_asc_then_global_independence_check",
        },
        "selected_count": len(ordered_cases),
        "ordered_cases": [
            {
                "expansion_case_id": c["expansion_case_id"],
                "source_candidate_id": c["source_candidate_id"],
                "symbol": c["symbol"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "pool_side": c["pool_side"],
                "approach": c["approach"],
                "pool_timeframe": c["pool_timeframe"],
                "pool_lower_edge": c.get("pool_lower_edge"),
                "pool_upper_edge": c.get("pool_upper_edge"),
                "pool_first_available_ts": c["pool_first_available_ts"],
                "event_family_id": c["event_family_id"],
                "audit_window_start": c["audit_window_start"],
                "audit_window_end": c["audit_window_end"],
                "audit_window_pre_s": c["audit_window_pre_s"],
                "audit_window_post_s": c["audit_window_post_s"],
                "exposure_status": c["exposure_status"],
                "deterministic_selection_hash": c["deterministic_selection_hash"],
            }
            for c in ordered_cases
        ],
        "next_after": next_after,
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "entry_contract_freeze_sha256": ENTRY_CONTRACT_FREEZE_SHA256,
        "strategy_config_sha256": STRATEGY_CONFIG_SHA256,
        "predecessor_v2_expansion_freeze_bundle_sha256": EXPECTED_V2_HASH,
    }
    hash_payload = {k: v for k, v in frozen_payload.items() if k != "created_at_utc"}
    bundle_sha = sha256_bytes(canonical_json_bytes(hash_payload))
    frozen_payload["expansion_freeze_bundle_sha256"] = bundle_sha

    write_json(out_dir / "frozen_expansion_cases_v3.json", frozen_payload)
    write_csv(out_dir / "eligible_universe.csv", eligible)
    write_csv(out_dir / "audit_window_conflicts.csv", v2_overlaps)
    write_csv(out_dir / "v2_to_v3_replacements.csv", replacement_rows)
    write_csv(
        out_dir / "stratum_counts.csv",
        [
            {
                "stratum": "ASK",
                "eligible": sel_meta["eligible_ask"],
                "selected": sel_meta["ask_selected"],
            },
            {
                "stratum": "BID",
                "eligible": sel_meta["eligible_bid"],
                "selected": sel_meta["bid_selected"],
            },
        ],
    )
    write_csv(out_dir / "excluded_exposed_cases.csv", exposure_excluded)
    write_csv(out_dir / "pairwise_independence_v3.csv", pairs)

    runtimes = sorted(CASE_RUNTIME_S.values())
    coverage = {
        "audit_window": {
            "pre_s": CASE_PIPELINE_PRE_S,
            "post_s": CASE_PIPELINE_MAX_POST_S,
            "inclusive": True,
            "seconds_per_case": CASE_PIPELINE_PRE_S + CASE_PIPELINE_MAX_POST_S + 1,
        },
        "independence": "no same-symbol audit-window overlap among 24 cases",
        "raw_ob200_seconds_per_case": 1831,
        "public_trades": "orderbook_analysis.public_trades_canonical SELECT-only",
        "lld_packs": ["5m", "15m", "30m", "1h"],
        "runtime_estimate_s": {
            "min_per_case": runtimes[0],
            "median_per_case": runtimes[1],
            "max_per_case": runtimes[2],
            "total_24_min": 24 * runtimes[0],
            "total_24_median": 24 * runtimes[1],
            "total_24_max": 24 * runtimes[2],
            "total_24_min_min": round(24 * runtimes[0] / 60, 1),
            "total_24_median_min": round(24 * runtimes[1] / 60, 1),
            "total_24_max_min": round(24 * runtimes[2] / 60, 1),
        },
        "disk_estimate_mb": {"per_case": 80, "total_24": 1920},
        "batch_note": (
            "Independent windows reduce same-hour OB/trade cache sharing across selected cases; "
            "segment-level reuse still possible across non-overlapping hours."
        ),
        "basis": "CASE_03/04/05 observed elapsed_s; n_ob_seconds=1831",
    }
    write_json(out_dir / "coverage_estimate.json", coverage)

    selection_audit = {
        "v2_predecessor_hash": EXPECTED_V2_HASH,
        "v2_audit_window_overlap_pairs": v2_overlaps,
        "v2_overlap_count": len(v2_overlaps),
        "source_resolution": resolution,
        "selection_meta": sel_meta,
        "replacement": {
            "removed_count": len(removed),
            "added_count": len(added),
            "retained_same_candidate_count": len(retained_same),
            "retained_same_candidates": retained_same,
        },
        "sampling_seed": SAMPLING_SEED,
        "outcome_blind": True,
    }
    write_json(out_dir / "selection_audit.json", selection_audit)
    write_json(
        out_dir / "outcome_blindness_audit.json",
        {
            "outcome_fields_read_for_selection": False,
            "outcome_fields_read_for_sorting": False,
            "micro_room_used_for_selection": False,
            "verified_cases_have_no_outcome_fields": all(
                not any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR)
                for c in frozen_payload["ordered_cases"]
                for k in c
            ),
        },
    )

    manifest = {
        "schema_version": V3_SCHEMA,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V3",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "predecessor_v2_expansion_freeze_bundle_sha256": EXPECTED_V2_HASH,
        "source_manifest_sha256": source_sha,
        "selected_count": TARGET_COUNT,
        "created_at_utc": frozen_payload["created_at_utc"],
        "files": [
            "eligible_universe.csv",
            "frozen_expansion_cases_v3.json",
            "audit_window_conflicts.csv",
            "v2_to_v3_replacements.csv",
            "stratum_counts.csv",
            "freeze_manifest.json",
            "selection_audit.json",
            "outcome_blindness_audit.json",
            "coverage_estimate.json",
            "EXPANSION_FREEZE_REPORT.md",
        ],
    }
    write_json(out_dir / "freeze_manifest.json", manifest)
    (out_dir / "EXPANSION_FREEZE_REPORT.md").write_text(
        _v3_report(frozen_payload, v2_overlaps, replacement_rows, bundle_sha, coverage),
        encoding="utf-8",
    )

    return {
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V3",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "out_dir": str(out_dir),
        "selected_count": len(ordered_cases),
        "v2_overlap_count": len(v2_overlaps),
        "removed_count": len(removed),
        "added_count": len(added),
    }


def verify_expansion_freeze_v3(
    repo_root: Path | None = None,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = root / V3_RESULTS_REL
    frozen_path = out_dir / "frozen_expansion_cases_v3.json"
    if not frozen_path.is_file():
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "v3 freeze missing")

    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    stored = frozen["expansion_freeze_bundle_sha256"]
    payload = {
        k: v
        for k, v in frozen.items()
        if k not in ("expansion_freeze_bundle_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"sha mismatch stored={stored} recomputed={recomputed}",
        )

    # predecessors unchanged
    v1 = json.loads(
        (root / "results/liquidity_pool_entry_contract_expansion_freeze_v1/frozen_expansion_cases_v1.json").read_text(
            encoding="utf-8"
        )
    )
    v2 = json.loads(
        (root / V2_RESULTS_REL / "frozen_expansion_cases_v2.json").read_text(encoding="utf-8")
    )
    if v1["expansion_freeze_bundle_sha256"] != EXPECTED_V1_HASH:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "v1 mutated")
    if v2["expansion_freeze_bundle_sha256"] != EXPECTED_V2_HASH:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "v2 mutated")
    if frozen.get("predecessor_v2_expansion_freeze_bundle_sha256") != EXPECTED_V2_HASH:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "predecessor hash missing")

    if sha256_file(root / frozen["source_manifest"]["path_relative"]) != frozen["source_manifest"]["sha256"]:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "source changed")
    if frozen["case_sequence_freeze_sha256"] != CASE_SEQUENCE_FREEZE_SHA256:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "case seq sha")
    if frozen["entry_contract_freeze_sha256"] != ENTRY_CONTRACT_FREEZE_SHA256:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "entry contract sha")
    if frozen["strategy_config_sha256"] != STRATEGY_CONFIG_SHA256:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "strategy sha")
    if sha256_file(root / STRATEGY_CONFIG_REL) != STRATEGY_CONFIG_SHA256:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "yaml drift")

    cases = frozen["ordered_cases"]
    if len(cases) != TARGET_COUNT:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", f"count={len(cases)}")
    ask = sum(1 for c in cases if c["pool_side"] == "ASK")
    bid = sum(1 for c in cases if c["pool_side"] == "BID")
    if ask != 12 or bid != 12:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", f"ASK/BID={ask}/{bid}")

    pairs, violations = pairwise_independence(cases)
    if len(pairs) != 276:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", f"pairs={len(pairs)}")
    if violations:
        raise ExpansionV3Error(
            "EXPANSION_V3_INTEGRITY_FAILURE",
            f"independence violations={len(violations)}",
        )
    if len({c["event_family_id"] for c in cases}) != len(cases):
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "dup event_family")

    for c in cases:
        for k in c:
            if any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR):
                raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", f"forbidden field {k}")
        expected = selection_hash(
            source_sha256=frozen["source_manifest"]["sha256"],
            candidate_id=c["source_candidate_id"],
            pool_id=c["pool_id"],
            reference_ts=c["reference_ts"],
            seed=frozen["sampling_seed"],
        )
        if c["deterministic_selection_hash"] != expected:
            raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", f"hash {c['expansion_case_id']}")
        if c.get("audit_window_pre_s") != CASE_PIPELINE_PRE_S or c.get("audit_window_post_s") != CASE_PIPELINE_MAX_POST_S:
            raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "audit window semantics")

    next_after = frozen["next_after"]
    for i, c in enumerate(cases[:-1]):
        if next_after[c["expansion_case_id"]] != cases[i + 1]["expansion_case_id"]:
            raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "next_after")
    if next_after[cases[-1]["expansion_case_id"]] is not None:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "terminal next_after")

    # exposure check
    eligible, excluded = apply_exposure_exclusion(
        [
            {
                "source_candidate_id": c["source_candidate_id"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "market_arrival_cluster_id": c["event_family_id"],
                "event_family_id": c["event_family_id"],
            }
            for c in cases
        ],
        load_exposed_cases(root),
        frozen["exposure_embargo"],
    )
    if len(eligible) != len(cases):
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "exposed case in sample")

    if mutate:
        tampered = dict(payload)
        tampered["selected_count"] = 99
        bad = sha256_bytes(canonical_json_bytes(tampered))
        if bad == stored:
            raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "mutation failed")
        return {
            "ok": True,
            "mutation_detected": True,
            "original_sha256": stored,
            "mutated_sha256": bad,
            "pairwise_checked": len(pairs),
            "violations": 0,
        }

    rebuild = build_expansion_freeze_v3(root)
    if rebuild["expansion_freeze_bundle_sha256"] != stored:
        raise ExpansionV3Error("EXPANSION_V3_INTEGRITY_FAILURE", "second run hash mismatch")

    return {
        "ok": True,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V3",
        "expansion_freeze_bundle_sha256": stored,
        "pairwise_checked": len(pairs),
        "violations": 0,
    }


def _v3_report(
    frozen: dict[str, Any],
    v2_overlaps: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    bundle_sha: str,
    coverage: dict[str, Any],
) -> str:
    lines = [
        "# Liquidity Pool Entry Contract Expansion Freeze v3",
        "",
        f"Generated: {frozen['created_at_utc']}",
        "",
        "## Verdict",
        "",
        "**LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V3**",
        "",
        "Outcome-blind correction: same-symbol deep-audit windows must be non-overlapping.",
        "",
        f"Predecessor v2: `{EXPECTED_V2_HASH}`",
        f"Bundle SHA256: `{bundle_sha}`",
        "",
        "## v2 audit-window overlaps corrected",
        "",
    ]
    for p in v2_overlaps:
        lines.append(
            f"- `{p['a']}`/`{p['b']}` delta={p['delta_seconds']}s "
            f"windows overlap on {p['symbol_a']}"
        )
    lines.extend(
        [
            "",
            f"Replacements: removed={sum(1 for r in replacements if r['change']=='removed_from_v2')} "
            f"added={sum(1 for r in replacements if r['change']=='added_in_v3')}",
            "",
            "## Frozen cases (24)",
            "",
        ]
    )
    for c in frozen["ordered_cases"]:
        lines.append(
            f"- `{c['expansion_case_id']}` {c['pool_side']} {c['reference_ts']} "
            f"win=[{c['audit_window_start']},{c['audit_window_end']}] "
            f"`{c['pool_id']}`"
        )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- OB seconds/case: {coverage['raw_ob200_seconds_per_case']}",
            f"- Runtime median×24: {coverage['runtime_estimate_s']['total_24_median_min']} min",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
