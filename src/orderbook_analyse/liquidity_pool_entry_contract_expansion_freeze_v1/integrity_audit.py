"""Integrity audit + corrected v2 expansion freeze (post-refill global dedup)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
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
    stratified_select,
    write_csv,
    write_json,
    _assign_expansion_ids,
    _iso,
    _next_after,
    _utc,
    _utc_now,
)

# CASE pipeline window (deep audit), NOT six-case short sample
CASE_PIPELINE_PRE_S = 30
CASE_PIPELINE_MAX_POST_S = 30 * 60  # 1800
DOCUMENTED_OVERLAP_S = 300

V1_RESULTS_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v1"
V1_INTEGRITY_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v1_integrity_audit"
V2_RESULTS_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v2"
V2_SCHEMA = "liquidity_pool_entry_contract_expansion_freeze/v2"
EXPECTED_V1_HASH = "910c1ddd3e76871c3583ed650723e4a9c9a735368bc6beb56045a6a5bbcbbba3"

# Observed CASE audit timings (seconds) — availability only, no outcomes
CASE_RUNTIME_S = {
    "CASE_03": 784.480078,
    "CASE_04": 320.63623,
    "CASE_05": 485.009106,
}
CASE_OB_SECONDS = {
    "CASE_03": 1831,
    "CASE_04": 1831,
    "CASE_05": 1831,
}


class IntegrityAuditError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def audit_window_case_pipeline(ref_ts: str) -> tuple[datetime, datetime]:
    ref = _utc(ref_ts)
    return (
        ref - timedelta(seconds=CASE_PIPELINE_PRE_S),
        ref + timedelta(seconds=CASE_PIPELINE_MAX_POST_S),
    )


def pair_conflict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    da = _utc(a["reference_ts"])
    db = _utc(b["reference_ts"])
    delta = abs((da - db).total_seconds())
    same_pool = a["pool_id"] == b["pool_id"]
    same_fam = a["event_family_id"] == b["event_family_id"]
    same_sym = a["symbol"] == b["symbol"]
    wa0, wa1 = audit_window_case_pipeline(a["reference_ts"])
    wb0, wb1 = audit_window_case_pipeline(b["reference_ts"])
    win_overlap = wa0 <= wb1 and wb0 <= wa1
    viol_fam = same_fam
    viol_time = same_sym and delta <= DOCUMENTED_OVERLAP_S
    return {
        "a": a.get("expansion_case_id") or a["source_candidate_id"],
        "b": b.get("expansion_case_id") or b["source_candidate_id"],
        "source_candidate_a": a["source_candidate_id"],
        "source_candidate_b": b["source_candidate_id"],
        "delta_seconds": delta,
        "same_pool_id": same_pool,
        "same_event_family_id": same_fam,
        "same_symbol": same_sym,
        "overlapping_audit_windows_case_pipeline": win_overlap,
        "audit_window_a": f"{_iso(wa0)}..{_iso(wa1)}",
        "audit_window_b": f"{_iso(wb0)}..{_iso(wb1)}",
        "violates_event_family_rule": viol_fam,
        "violates_same_symbol_le_300s": viol_time,
        "documented_dedup_rule_violated": viol_fam or viol_time,
        "pool_id_a": a["pool_id"],
        "pool_id_b": b["pool_id"],
        "event_family_a": a["event_family_id"],
        "event_family_b": b["event_family_id"],
        "ref_a": a["reference_ts"],
        "ref_b": b["reference_ts"],
    }


def pairwise_audit(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs = [pair_conflict(a, b) for a, b in combinations(cases, 2)]
    violations = [p for p in pairs if p["documented_dedup_rule_violated"]]
    return pairs, violations


def conflicts_with_retained(candidate: dict[str, Any], retained: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits = []
    for prev in retained:
        row = pair_conflict(candidate, prev)
        if row["documented_dedup_rule_violated"]:
            hits.append(row)
    return hits


def select_with_global_dedup_refill(
    eligible: list[dict[str, Any]],
    *,
    target: int = TARGET_COUNT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stratify → dedup → refill with conflict checks against ALL retained after every add."""
    pre_dedup, strat_meta = stratified_select(eligible, target=target)
    trace: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    excluded_log: list[dict[str, Any]] = []
    # process initial stratified picks by hash order; keep only non-conflicting
    for c in sorted(pre_dedup, key=lambda x: x["deterministic_selection_hash"]):
        hits = conflicts_with_retained(c, retained)
        if hits:
            excluded_log.append(
                {
                    "source_candidate_id": c["source_candidate_id"],
                    "phase": "initial_stratified_dedup",
                    "exclusion_reason": "conflicts_with_retained",
                    "conflicts": hits,
                }
            )
            continue
        retained.append(c)
        trace.append({"action": "keep_initial", "id": c["source_candidate_id"]})

    chosen_ids = {c["source_candidate_id"] for c in retained}
    chosen_ids.update(e["source_candidate_id"] for e in excluded_log)
    refill_pool = sorted(
        [c for c in eligible if c["source_candidate_id"] not in chosen_ids],
        key=lambda x: x["deterministic_selection_hash"],
    )
    refill_skipped = 0
    for c in refill_pool:
        if len(retained) >= target:
            break
        hits = conflicts_with_retained(c, retained)
        if hits:
            refill_skipped += 1
            excluded_log.append(
                {
                    "source_candidate_id": c["source_candidate_id"],
                    "phase": "refill_conflict_check",
                    "exclusion_reason": "conflicts_with_retained",
                    "conflicts": hits,
                }
            )
            chosen_ids.add(c["source_candidate_id"])
            continue
        retained.append(c)
        chosen_ids.add(c["source_candidate_id"])
        trace.append({"action": "refill_keep", "id": c["source_candidate_id"]})

    # Final global pairwise check (must be empty)
    _, violations = pairwise_audit(retained)
    if violations:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"post-refill global violations remain: {len(violations)}",
        )
    if len(retained) != target:
        raise ExpansionFreezeError(
            "EXPANSION_STRATIFICATION_NOT_FEASIBLE",
            f"after conflict-aware refill count={len(retained)} need={target}",
        )

    # ASK/BID rebalance check — may deviate if conflict-aware refill depletes a side
    ask = sum(1 for c in retained if c["pool_side"] == "ASK")
    bid = sum(1 for c in retained if c["pool_side"] == "BID")
    deviations = list(strat_meta.get("deviations") or [])
    if ask != bid:
        deviations.append(f"ASK/BID after conflict-aware refill: {ask}/{bid} (target 12/12)")
    # Prefer restoring 12/12 by swapping lower-hash same-side surplus with opposite refill
    if ask != 12 or bid != 12:
        retained, rebalance_trace = _rebalance_ask_bid(retained, eligible, target=target)
        deviations.extend(rebalance_trace)
        ask = sum(1 for c in retained if c["pool_side"] == "ASK")
        bid = sum(1 for c in retained if c["pool_side"] == "BID")
        _, violations = pairwise_audit(retained)
        if violations:
            raise ExpansionFreezeError(
                "EXPANSION_FREEZE_INTEGRITY_FAILURE",
                f"rebalance introduced violations: {len(violations)}",
            )
        if ask != 12 or bid != 12:
            raise ExpansionFreezeError(
                "EXPANSION_STRATIFICATION_NOT_FEASIBLE",
                f"ASK/BID={ask}/{bid} after rebalance",
            )

    meta = {
        "strata_plan": strat_meta["strata_plan"],
        "eligible_strata_before": strat_meta["eligible_strata_before"],
        "selected_strata_after": {
            "ASK": ask,
            "BID": bid,
            "symbols": {
                s: sum(1 for c in retained if c["symbol"] == s)
                for s in sorted({c["symbol"] for c in retained})
            },
            "timeframes": {
                t: sum(1 for c in retained if c["pool_timeframe"] == t)
                for t in sorted({c["pool_timeframe"] for c in retained})
            },
        },
        "deviations": deviations,
        "selection_trace": {
            "initial_kept": len([t for t in trace if t["action"] == "keep_initial"]),
            "refill_kept": len([t for t in trace if t["action"] == "refill_keep"]),
            "refill_skipped_conflicts": refill_skipped,
            "excluded_count": len(excluded_log),
        },
        "excluded_log_sample": excluded_log[:40],
        "post_refill_global_dedup_violations": 0,
        "dedup_after_every_refill": True,
    }
    return sorted(retained, key=lambda x: x["deterministic_selection_hash"]), meta


def _rebalance_ask_bid(
    retained: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    *,
    target: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministically restore ASK/BID 12/12 without creating conflicts."""
    notes: list[str] = []
    cur = list(retained)
    chosen = {c["source_candidate_id"] for c in cur}

    def counts() -> tuple[int, int]:
        return (
            sum(1 for c in cur if c["pool_side"] == "ASK"),
            sum(1 for c in cur if c["pool_side"] == "BID"),
        )

    ask_n, bid_n = counts()
    if ask_n == 12 and bid_n == 12:
        return cur, notes

    surplus_side = "ASK" if ask_n > 12 else "BID"
    deficit_side = "BID" if surplus_side == "ASK" else "ASK"
    # drop highest-hash surplus members first (deterministic), then refill deficit
    surplus = sorted(
        [c for c in cur if c["pool_side"] == surplus_side],
        key=lambda x: x["deterministic_selection_hash"],
        reverse=True,
    )
    need = abs(ask_n - 12)
    dropped: list[str] = []
    for c in surplus[:need]:
        cur = [x for x in cur if x["source_candidate_id"] != c["source_candidate_id"]]
        chosen.discard(c["source_candidate_id"])
        dropped.append(c["source_candidate_id"])
    notes.append(f"dropped_surplus_{surplus_side}:{','.join(dropped)}")

    refill = sorted(
        [
            c
            for c in eligible
            if c["source_candidate_id"] not in chosen and c["pool_side"] == deficit_side
        ],
        key=lambda x: x["deterministic_selection_hash"],
    )
    added: list[str] = []
    for c in refill:
        if len(cur) >= target and counts()[0 if deficit_side == "ASK" else 1] >= 12:
            break
        if sum(1 for x in cur if x["pool_side"] == deficit_side) >= 12:
            break
        if conflicts_with_retained(c, cur):
            continue
        cur.append(c)
        chosen.add(c["source_candidate_id"])
        added.append(c["source_candidate_id"])
    notes.append(f"added_deficit_{deficit_side}:{','.join(added)}")
    # trim if over target (should not happen)
    cur = sorted(cur, key=lambda x: x["deterministic_selection_hash"])[:target]
    return cur, notes


def reconstruct_v1_bug_trace(repo_root: Path) -> dict[str, Any]:
    """Reproduce v1 select→dedup→blind-refill path that reintroduces conflicts."""
    from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze import (
        dedup_selected,
    )

    inventory = inventory_candidate_sources(repo_root)
    resolution = resolve_unique_pool_contact_source(repo_root, inventory)
    source = resolution["source"]
    raw_rows = load_source_rows(repo_root, source["path_relative"])
    eligible_all, _ = build_eligible_universe(
        raw_rows,
        source_rel=source["path_relative"],
        source_sha256=source["sha256"],
        raw_root=repo_root / DEFAULT_RAW_ROOT,
    )
    eligible, _ = apply_exposure_exclusion(
        eligible_all, load_exposed_cases(repo_root), embargo_policy()
    )
    pre_dedup, _ = stratified_select(eligible, target=TARGET_COUNT)
    pre_conflicts = [pair_conflict(a, b) for a, b in combinations(pre_dedup, 2)]
    pre_viol = [p for p in pre_conflicts if p["documented_dedup_rule_violated"]]
    deduped, groups = dedup_selected(pre_dedup)
    post_dedup_viol = [
        p
        for a, b in combinations(deduped, 2)
        for p in [pair_conflict(a, b)]
        if p["documented_dedup_rule_violated"]
    ]
    chosen = {c["source_candidate_id"] for c in deduped}
    refill = sorted(
        [c for c in eligible if c["source_candidate_id"] not in chosen],
        key=lambda x: x["deterministic_selection_hash"],
    )
    refill_adds = []
    final = list(deduped)
    for c in refill:
        if len(final) >= TARGET_COUNT:
            break
        hits = conflicts_with_retained(c, final)
        refill_adds.append(
            {
                "source_candidate_id": c["source_candidate_id"],
                "reference_ts": c["reference_ts"],
                "pool_side": c["pool_side"],
                "pool_id": c["pool_id"],
                "would_conflict_with_retained": hits,
                "blindly_appended": True,
            }
        )
        final.append(c)
    final = sorted(final, key=lambda x: x["deterministic_selection_hash"])[:TARGET_COUNT]
    final_viol = [
        p
        for a, b in combinations(final, 2)
        for p in [pair_conflict(a, b)]
        if p["documented_dedup_rule_violated"]
    ]
    return {
        "pre_dedup_count": len(pre_dedup),
        "pre_dedup_violations": pre_viol,
        "after_dedup_count": len(deduped),
        "after_dedup_violations": post_dedup_viol,
        "dedup_groups": groups,
        "refill_adds": refill_adds,
        "final_count": len(final),
        "final_violations": final_viol,
        "root_cause": (
            "dedup_selected correctly removed conflicting candidates; "
            "blind refill re-appended the exact excluded candidates "
            "(next-lowest selection hashes) without conflict checks against retained cases; "
            "no second global dedup after refill"
        ),
        "implementation_bugs": [
            "refill appends without conflicts_with_retained check",
            "no global pairwise re-check after refill",
            "excluded-by-dedup candidates remain eligible for refill",
            "operator <=300 is correct in code; failure is refill, not threshold",
            "event_family_id populated from market_arrival_cluster_id (non-empty, distinct for EXP_09/12)",
            "timezone parsing uses trailing Z → UTC (correct)",
            "temporal dedup is global across ASK/BID, not stratum-local (correct); refill undoes it",
        ],
    }


def run_integrity_audit(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    v1_dir = root / V1_RESULTS_REL
    frozen_path = v1_dir / "frozen_expansion_cases_v1.json"
    if not frozen_path.is_file():
        raise IntegrityAuditError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "v1 freeze missing")

    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    stored = frozen.get("expansion_freeze_bundle_sha256")
    if stored != EXPECTED_V1_HASH:
        raise IntegrityAuditError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"v1 hash mismatch expected={EXPECTED_V1_HASH} got={stored}",
        )
    # recompute hash excluding volatile fields
    payload = {
        k: v
        for k, v in frozen.items()
        if k not in ("expansion_freeze_bundle_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise IntegrityAuditError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"v1 recomputed sha mismatch stored={stored} recomputed={recomputed}",
        )

    cases = frozen["ordered_cases"]
    pairs, violations = pairwise_audit(cases)
    bug_trace = reconstruct_v1_bug_trace(root)

    audit_windows = {
        "case_pipeline": {
            "pre_s": CASE_PIPELINE_PRE_S,
            "max_post_s": CASE_PIPELINE_MAX_POST_S,
            "total_grid_seconds": CASE_PIPELINE_PRE_S + CASE_PIPELINE_MAX_POST_S + 1,
            "source": "case_03_frozen_bid_pool_causal_reaction_audit_v1/__init__.py PRE_S, MAX_POST_S",
            "dynamic_extension": False,
            "note": (
                "load_start=ref-PRE_S, load_end=ref+MAX_POST_S; "
                "first_available_ts / back-cross searched inside this fixed window; "
                "window is not dynamically extended beyond MAX_POST_S"
            ),
        },
        "six_case_short_sample_window_misused_in_v1_coverage": {
            "pre_s": 30,
            "max_post_s": 300,
            "total_seconds": 331,
            "note": "v1 coverage estimate incorrectly used six-case MAX_POST_START_S=300",
        },
        "observed_n_ob_seconds": CASE_OB_SECONDS,
        "observed_elapsed_s": CASE_RUNTIME_S,
        "discrepancy_explanation": (
            "Freeze coverage estimated 331s (=30+300+1) from six-case short sample constants; "
            "CASE deep-audit pipeline uses MAX_POST_S=1800 → 1831 Raw-OB200 seconds "
            "(30+1800+1). CASE_03/04/05 all report n_ob_seconds=1831."
        ),
        "revised_runtime_estimate": {
            "per_case_ob_seconds": 1831,
            "observed_runtimes_s": CASE_RUNTIME_S,
            "min_s": min(CASE_RUNTIME_S.values()),
            "median_s": sorted(CASE_RUNTIME_S.values())[1],
            "max_s": max(CASE_RUNTIME_S.values()),
            "est_total_24_min_s": 24 * min(CASE_RUNTIME_S.values()),
            "est_total_24_median_s": 24 * sorted(CASE_RUNTIME_S.values())[1],
            "est_total_24_max_s": 24 * max(CASE_RUNTIME_S.values()),
            "est_total_24_min_min": round(24 * min(CASE_RUNTIME_S.values()) / 60, 1),
            "est_total_24_median_min": round(24 * sorted(CASE_RUNTIME_S.values())[1] / 60, 1),
            "est_total_24_max_min": round(24 * max(CASE_RUNTIME_S.values()) / 60, 1),
            "disk_per_case_mb_revised": 80,
            "disk_total_24_mb_revised": 80 * 24,
            "basis": "CASE_03/04/05 observed elapsed_s and n_ob_seconds=1831",
        },
    }

    out_dir = root / V1_INTEGRITY_REL
    out_dir.mkdir(parents=True, exist_ok=True)

    detail_0912 = next(
        (p for p in pairs if {p["a"], p["b"]} == {"EXP_09", "EXP_12"}), None
    )
    detail_1017 = next(
        (p for p in pairs if {p["a"], p["b"]} == {"EXP_10", "EXP_17"}), None
    )

    if violations:
        verdict = "EXPANSION_FREEZE_V1_DEDUP_INTEGRITY_FAILURE"
    else:
        verdict = "EXPANSION_FREEZE_V1_DEDUP_INTEGRITY_CONFIRMED"

    audit = {
        "schema_version": "liquidity_pool_entry_contract_expansion_freeze_v1_integrity_audit/v1",
        "created_at_utc": _utc_now(),
        "verdict": verdict,
        "v1_freeze_path": V1_RESULTS_REL,
        "v1_expansion_freeze_bundle_sha256": stored,
        "v1_hash_verified": True,
        "pair_count": len(pairs),
        "violation_count": len(violations),
        "violations": violations,
        "detail_EXP_09_12": detail_0912,
        "detail_EXP_10_17": detail_1017,
        "v1_bug_trace": bug_trace,
        "audit_windows": audit_windows,
        "documented_dedup_rules": {
            "same_event_family_id": "retain first deterministic hash only",
            "same_symbol_delta_le_300s": "retain first deterministic hash only",
            "comparison_operator": "<= 300 (inclusive)",
        },
        "v1_not_overwritten": True,
    }
    write_json(out_dir / "integrity_audit.json", audit)
    write_csv(out_dir / "pairwise_conflicts.csv", pairs)
    write_csv(out_dir / "dedup_violations.csv", violations)
    write_json(out_dir / "v1_bug_trace.json", bug_trace)
    write_json(out_dir / "audit_window_analysis.json", audit_windows)

    report = _integrity_report(audit)
    (out_dir / "INTEGRITY_AUDIT_REPORT.md").write_text(report, encoding="utf-8")

    result: dict[str, Any] = {
        "verdict": verdict,
        "out_dir": str(out_dir),
        "violation_count": len(violations),
        "v1_hash": stored,
    }
    if violations:
        v2 = build_expansion_freeze_v2(root, v1_audit=audit)
        result["v2"] = v2
    return result


def build_expansion_freeze_v2(
    repo_root: Path,
    *,
    v1_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Corrected freeze: same source/eligibility/exposure/seed/strata; post-refill global dedup."""
    root = repo_root
    out_dir = root / V2_RESULTS_REL
    out_dir.mkdir(parents=True, exist_ok=True)

    inventory = inventory_candidate_sources(root)
    resolution = resolve_unique_pool_contact_source(root, inventory)
    source = resolution["source"]
    source_rel = source["path_relative"]
    source_sha = source["sha256"]
    if source_rel != PRIMARY_SOURCE_REL:
        raise ExpansionFreezeError(
            "EXPANSION_CANDIDATE_SOURCE_NOT_UNAMBIGUOUS",
            f"expected {PRIMARY_SOURCE_REL}, got {source_rel}",
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
    if len(eligible) < TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_ELIGIBLE_UNIVERSE_TOO_SMALL",
            f"post-exposure eligible={len(eligible)}",
        )

    selected, strat_meta = select_with_global_dedup_refill(eligible, target=TARGET_COUNT)
    pairs, violations = pairwise_audit(selected)
    if violations:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"v2 still has {len(violations)} pairwise violations",
        )

    ordered_cases = _assign_expansion_ids(selected)
    next_after = _next_after(ordered_cases)

    frozen_payload = {
        "schema_version": V2_SCHEMA,
        "created_at_utc": _utc_now(),
        "supersedes": {
            "path": V1_RESULTS_REL,
            "expansion_freeze_bundle_sha256": EXPECTED_V1_HASH,
            "reason": "EXPANSION_FREEZE_V1_DEDUP_INTEGRITY_FAILURE",
        },
        "source_manifest": {
            "path_relative": source_rel,
            "sha256": source_sha,
        },
        "sampling_seed": SAMPLING_SEED,
        "eligibility_policy": {
            "required_fields": [
                "source_candidate_id",
                "symbol",
                "reference_ts",
                "pool_id",
                "pool_side",
                "approach",
                "pool_first_available_ts",
            ],
            "causal_rules": [
                "pool_first_available_ts <= reference_ts",
                "raw_ob200_coverage >= 85% audit window",
                "public_trades_canonical assumed for v2 monitor window",
                "lld_packs 5m/15m/30m/1h via chart backend",
            ],
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
            "same_event_family": "first deterministic hash wins",
            "same_symbol_overlap_s": OVERLAP_SAME_SYMBOL_S,
            "comparison": "delta_seconds <= 300 inclusive",
            "nested_tf_event_family": "market_arrival_cluster_id",
            "refill_rule": "every refill candidate checked against ALL retained; final global pairwise must be empty",
        },
        "stratification_policy": strat_meta["strata_plan"],
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
                "exposure_status": c["exposure_status"],
                "deterministic_selection_hash": c["deterministic_selection_hash"],
            }
            for c in ordered_cases
        ],
        "next_after": next_after,
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "entry_contract_freeze_sha256": ENTRY_CONTRACT_FREEZE_SHA256,
        "strategy_config_sha256": STRATEGY_CONFIG_SHA256,
    }
    hash_payload = {k: v for k, v in frozen_payload.items() if k != "created_at_utc"}
    bundle_sha = sha256_bytes(canonical_json_bytes(hash_payload))
    frozen_payload["expansion_freeze_bundle_sha256"] = bundle_sha

    # v1 → v2 replacement map
    v1_path = root / V1_RESULTS_REL / "frozen_expansion_cases_v1.json"
    v1_cases = json.loads(v1_path.read_text(encoding="utf-8"))["ordered_cases"]
    v1_ids = {c["source_candidate_id"] for c in v1_cases}
    v2_ids = {c["source_candidate_id"] for c in ordered_cases}
    removed = [c for c in v1_cases if c["source_candidate_id"] not in v2_ids]
    added = [c for c in ordered_cases if c["source_candidate_id"] not in v1_ids]
    replacement = {
        "removed_from_v1": [
            {
                "expansion_case_id_v1": c["expansion_case_id"],
                "source_candidate_id": c["source_candidate_id"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "reason": "dedup_conflict_with_retained_after_corrected_global_dedup",
            }
            for c in removed
        ],
        "added_in_v2": [
            {
                "expansion_case_id_v2": c["expansion_case_id"],
                "source_candidate_id": c["source_candidate_id"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "reason": "deterministic_conflict_aware_refill",
            }
            for c in added
        ],
        "retained_same_candidate": sorted(v1_ids & v2_ids),
    }

    write_json(out_dir / "frozen_expansion_cases_v2.json", frozen_payload)
    write_csv(out_dir / "eligible_universe.csv", eligible)
    write_csv(out_dir / "excluded_exposed_cases.csv", exposure_excluded)
    write_csv(
        out_dir / "stratum_counts.csv",
        [
            {
                "stratum": "ASK",
                "eligible": strat_meta["eligible_strata_before"]["ASK"],
                "selected": strat_meta["selected_strata_after"]["ASK"],
            },
            {
                "stratum": "BID",
                "eligible": strat_meta["eligible_strata_before"]["BID"],
                "selected": strat_meta["selected_strata_after"]["BID"],
            },
        ],
    )
    write_csv(out_dir / "pairwise_conflicts_v2.csv", pairs)
    write_json(out_dir / "v1_to_v2_replacement.json", replacement)
    write_json(
        out_dir / "selection_audit.json",
        {
            "source_resolution": resolution,
            "stratification": strat_meta,
            "pairwise_violation_count": 0,
            "sampling_seed": SAMPLING_SEED,
            "same_as_v1_except_dedup_refill": True,
        },
    )
    write_json(
        out_dir / "outcome_blindness_audit.json",
        {
            "outcome_fields_read_for_selection": False,
            "outcome_fields_read_for_sorting": False,
            "verified_cases_have_no_outcome_fields": all(
                not any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR)
                for c in frozen_payload["ordered_cases"]
                for k in c
            ),
        },
    )
    revised = (v1_audit or {}).get("audit_windows", {}).get("revised_runtime_estimate")
    write_json(
        out_dir / "coverage_estimate_revised.json",
        {
            "per_case_raw_ob200_seconds": CASE_PIPELINE_PRE_S + CASE_PIPELINE_MAX_POST_S + 1,
            "case_pipeline_window": {
                "pre_s": CASE_PIPELINE_PRE_S,
                "max_post_s": CASE_PIPELINE_MAX_POST_S,
            },
            "revised_runtime_estimate": revised
            or {
                "min_s": min(CASE_RUNTIME_S.values()),
                "median_s": sorted(CASE_RUNTIME_S.values())[1],
                "max_s": max(CASE_RUNTIME_S.values()),
            },
        },
    )

    manifest = {
        "schema_version": V2_SCHEMA,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V2",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "supersedes_v1_sha256": EXPECTED_V1_HASH,
        "source_manifest_sha256": source_sha,
        "selected_count": TARGET_COUNT,
        "created_at_utc": frozen_payload["created_at_utc"],
        "files": [
            "frozen_expansion_cases_v2.json",
            "freeze_manifest.json",
            "eligible_universe.csv",
            "excluded_exposed_cases.csv",
            "stratum_counts.csv",
            "pairwise_conflicts_v2.csv",
            "v1_to_v2_replacement.json",
            "selection_audit.json",
            "outcome_blindness_audit.json",
            "coverage_estimate_revised.json",
            "EXPANSION_FREEZE_REPORT.md",
        ],
    }
    write_json(out_dir / "freeze_manifest.json", manifest)
    (out_dir / "EXPANSION_FREEZE_REPORT.md").write_text(
        _v2_report(frozen_payload, replacement, bundle_sha),
        encoding="utf-8",
    )
    return {
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V2",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "out_dir": str(out_dir),
        "selected_count": len(ordered_cases),
        "replacement": replacement,
    }


def verify_expansion_freeze_v2(
    repo_root: Path | None = None,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = root / V2_RESULTS_REL
    frozen_path = out_dir / "frozen_expansion_cases_v2.json"
    if not frozen_path.is_file():
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "v2 missing")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    stored = frozen["expansion_freeze_bundle_sha256"]
    payload = {
        k: v
        for k, v in frozen.items()
        if k not in ("expansion_freeze_bundle_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"sha mismatch stored={stored} recomputed={recomputed}",
        )
    if sha256_file(root / frozen["source_manifest"]["path_relative"]) != frozen["source_manifest"]["sha256"]:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "source changed")
    if frozen["case_sequence_freeze_sha256"] != CASE_SEQUENCE_FREEZE_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "case seq sha")
    if frozen["entry_contract_freeze_sha256"] != ENTRY_CONTRACT_FREEZE_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "entry contract sha")
    if frozen["strategy_config_sha256"] != STRATEGY_CONFIG_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "strategy sha")
    if sha256_file(root / STRATEGY_CONFIG_REL) != STRATEGY_CONFIG_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "yaml drift")

    cases = frozen["ordered_cases"]
    if len(cases) != TARGET_COUNT:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "count != 24")
    ask = sum(1 for c in cases if c["pool_side"] == "ASK")
    bid = sum(1 for c in cases if c["pool_side"] == "BID")
    if ask != 12 or bid != 12:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", f"ASK/BID {ask}/{bid}")
    pairs, violations = pairwise_audit(cases)
    if violations:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"pairwise violations={len(violations)}",
        )
    if len({c["event_family_id"] for c in cases}) != len(cases):
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "dup families")
    for c in cases:
        expected = selection_hash(
            source_sha256=frozen["source_manifest"]["sha256"],
            candidate_id=c["source_candidate_id"],
            pool_id=c["pool_id"],
            reference_ts=c["reference_ts"],
            seed=frozen["sampling_seed"],
        )
        if c["deterministic_selection_hash"] != expected:
            raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", f"hash {c['expansion_case_id']}")
    next_after = frozen["next_after"]
    for i, c in enumerate(cases[:-1]):
        if next_after[c["expansion_case_id"]] != cases[i + 1]["expansion_case_id"]:
            raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "next_after")
    if next_after[cases[-1]["expansion_case_id"]] is not None:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "terminal next_after")

    if mutate:
        tampered = dict(payload)
        tampered["selected_count"] = 99
        bad = sha256_bytes(canonical_json_bytes(tampered))
        if bad == stored:
            raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "mutation failed")
        return {
            "ok": True,
            "mutation_detected": True,
            "original_sha256": stored,
            "mutated_sha256": bad,
            "pairwise_checked": len(pairs),
            "violations": 0,
        }

    rebuild = build_expansion_freeze_v2(root)
    if rebuild["expansion_freeze_bundle_sha256"] != stored:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "second run mismatch")
    return {
        "ok": True,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V2",
        "expansion_freeze_bundle_sha256": stored,
        "pairwise_checked": len(pairs),
        "violations": 0,
    }


def _integrity_report(audit: dict[str, Any]) -> str:
    lines = [
        "# Expansion Freeze v1 Integrity Audit",
        "",
        f"Generated: {audit['created_at_utc']}",
        "",
        "## Verdict",
        "",
        f"**{audit['verdict']}**",
        "",
        f"- v1 hash verified: `{audit['v1_expansion_freeze_bundle_sha256']}`",
        f"- Pairs checked: {audit['pair_count']}",
        f"- Documented-rule violations: {audit['violation_count']}",
        f"- v1 not overwritten: {audit['v1_not_overwritten']}",
        "",
        "## Violations",
        "",
    ]
    for v in audit["violations"]:
        lines.append(
            f"- `{v['a']}`/`{v['b']}` delta={v['delta_seconds']}s "
            f"same_pool={v['same_pool_id']} same_fam={v['same_event_family_id']} "
            f"time_rule={v['violates_same_symbol_le_300s']}"
        )
    lines.extend(
        [
            "",
            "## Root cause",
            "",
            audit["v1_bug_trace"]["root_cause"],
            "",
            "## Audit windows",
            "",
            audit["audit_windows"]["discrepancy_explanation"],
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _v2_report(frozen: dict[str, Any], replacement: dict[str, Any], bundle_sha: str) -> str:
    lines = [
        "# Liquidity Pool Entry Contract Expansion Freeze v2",
        "",
        f"Generated: {frozen['created_at_utc']}",
        "",
        "## Verdict",
        "",
        "**LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN_V2**",
        "",
        f"Supersedes v1 `{EXPECTED_V1_HASH}` due to dedup integrity failure.",
        "",
        f"Bundle SHA256: `{bundle_sha}`",
        "",
        "## Replacements",
        "",
        f"- Removed: {len(replacement['removed_from_v1'])}",
        f"- Added: {len(replacement['added_in_v2'])}",
        f"- Retained same candidate: {len(replacement['retained_same_candidate'])}",
        "",
        "## Cases",
        "",
    ]
    for c in frozen["ordered_cases"]:
        lines.append(
            f"- `{c['expansion_case_id']}` {c['pool_side']} {c['reference_ts']} "
            f"`{c['pool_id']}` cand=`{c['source_candidate_id']}`"
        )
    return "\n".join(lines) + "\n"
