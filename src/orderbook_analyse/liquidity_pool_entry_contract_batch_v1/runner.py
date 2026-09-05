"""Batch orchestration — resume, smoke, unblind gate. No decision changes."""

from __future__ import annotations

import json
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v1 import (
    CONCURRENCY,
    EXPECTED_ENTRY_CONTRACT_HASH,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V3_HASH,
    FORMAT_VERSION,
    RESULTS_DIR_REL,
    STATUS_FAILED_FINAL,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_PENDING,
    STATUS_RUNNING,
    TARGET_MECHANICAL_COMPLETE,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.capability import (
    assess_mechanical_unblind_separation,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.cases import (
    expansion_case_to_audit_params,
    load_v3_freeze,
    ordered_cases,
    smoke_selection,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.hashes import (
    FrozenInputHashMismatch,
    verify_frozen_inputs,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.status import (
    append_jsonl,
    atomic_write_json,
    batch_root,
    build_batch_status,
    case_dir,
    count_mechanical_complete,
    mechanical_complete_valid,
    read_case_status,
    write_case_status,
)


class BatchError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_batch_dirs(repo_root: Path, frozen: dict[str, Any]) -> Path:
    root = batch_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases").mkdir(exist_ok=True)
    for c in ordered_cases(frozen):
        case_dir(repo_root, c["expansion_case_id"]).mkdir(parents=True, exist_ok=True)
        st = read_case_status(repo_root, c["expansion_case_id"])
        if st.get("status") == STATUS_PENDING and st.get("updated_at_utc") is None:
            write_case_status(repo_root, c["expansion_case_id"], st)
        elif not (case_dir(repo_root, c["expansion_case_id"]) / "case_status.json").is_file():
            write_case_status(repo_root, c["expansion_case_id"], st)
    return root


def write_batch_manifest(repo_root: Path, frozen: dict[str, Any], *, mode: str) -> dict[str, Any]:
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_or_updated_at_utc": _utc_now(),
        "mode": mode,
        "results_dir": RESULTS_DIR_REL,
        "concurrency": CONCURRENCY,
        "v3_freeze_sha256": EXPECTED_V3_HASH,
        "entry_contract_sha256": EXPECTED_ENTRY_CONTRACT_HASH,
        "strategy_config_sha256": EXPECTED_STRATEGY_CONFIG_HASH,
        "case_count": len(ordered_cases(frozen)),
        "smoke_cases": [c["expansion_case_id"] for c in smoke_selection(frozen)],
        "mechanical_unblind_separation": "required",
        "unblind_requires_mechanical_complete_count": TARGET_MECHANICAL_COMPLETE,
        "no_outcome_in_mechanical": True,
        "no_decision_changes": True,
    }
    atomic_write_json(batch_root(repo_root) / "batch_manifest.json", manifest)
    return manifest


def refresh_status(repo_root: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    status = build_batch_status(repo_root, ordered_cases(frozen))
    atomic_write_json(batch_root(repo_root) / "batch_status.json", status)
    return status


def cmd_verify(repo_root: Path) -> dict[str, Any]:
    before = verify_frozen_inputs(repo_root, label="verify")
    cap = assess_mechanical_unblind_separation(repo_root)
    return {"ok": True, "hashes": before, "capability": cap}


def cmd_plan(repo_root: Path) -> dict[str, Any]:
    verify_frozen_inputs(repo_root, label="plan_before")
    frozen = load_v3_freeze(repo_root)
    init_batch_dirs(repo_root, frozen)
    smoke = smoke_selection(frozen)
    cap = assess_mechanical_unblind_separation(repo_root)
    manifest = write_batch_manifest(repo_root, frozen, mode="plan")
    status = refresh_status(repo_root, frozen)
    plan = {
        "ok": True,
        "verdict": "PLAN_READY",
        "smoke_cases": [expansion_case_to_audit_params(c) for c in smoke],
        "all_case_ids": [c["expansion_case_id"] for c in ordered_cases(frozen)],
        "capability": cap,
        "manifest": manifest,
        "status_counts": status["counts"],
        "note": "No market data accessed in --plan",
    }
    atomic_write_json(batch_root(repo_root) / "plan.json", plan)
    verify_frozen_inputs(repo_root, label="plan_after")
    return plan


def cmd_status(repo_root: Path) -> dict[str, Any]:
    frozen = load_v3_freeze(repo_root)
    status = refresh_status(repo_root, frozen)
    return {"ok": True, "status": status}


def cmd_unblind_outcomes(repo_root: Path) -> dict[str, Any]:
    """Fail-closed: require all 24 mechanical complete. Never auto-unblind."""
    verify_frozen_inputs(repo_root, label="unblind_gate")
    frozen = load_v3_freeze(repo_root)
    ids = [c["expansion_case_id"] for c in ordered_cases(frozen)]
    n = count_mechanical_complete(repo_root, ids)
    if n != TARGET_MECHANICAL_COMPLETE:
        raise BatchError(
            "BATCH_UNBLIND_BLOCKED",
            f"mechanical_complete_count={n} != {TARGET_MECHANICAL_COMPLETE}",
        )
    raise BatchError(
        "BATCH_UNBLIND_NOT_IMPLEMENTED_IN_THIS_TASK",
        "unblind deferred; this smoke task must not open outcomes",
    )


def _mark_interrupted(repo_root: Path, case_id: str) -> None:
    st = read_case_status(repo_root, case_id)
    if st.get("status") == STATUS_RUNNING:
        st["status"] = "FAILED_RETRYABLE"
        st["error"] = "interrupted_SIGINT_SIGTERM"
        st["worker_pid"] = None
        write_case_status(repo_root, case_id, st)


def cmd_smoke(repo_root: Path, *, mechanical_only: bool = True) -> dict[str, Any]:
    """Smoke EXP_01 + EXP_03. Stops before market data if separation blocked."""
    if not mechanical_only:
        raise BatchError("EXPANSION_BATCH_V1_SMOKE_FAILED", "--smoke requires --mechanical-only")

    hashes_before = verify_frozen_inputs(repo_root, label="smoke_before")
    frozen = load_v3_freeze(repo_root)
    init_batch_dirs(repo_root, frozen)
    write_batch_manifest(repo_root, frozen, mode="smoke")
    smoke = smoke_selection(frozen)
    smoke_ids = [c["expansion_case_id"] for c in smoke]

    log_path = batch_root(repo_root) / "run_log.jsonl"
    append_jsonl(
        log_path,
        {
            "ts": _utc_now(),
            "event": "smoke_start",
            "cases": smoke_ids,
            "mechanical_only": True,
        },
    )

    cap = assess_mechanical_unblind_separation(repo_root)
    atomic_write_json(batch_root(repo_root) / "capability_probe.json", cap)

    if not cap["separable_without_hashed_file_change"]:
        # Initialize case statuses as PENDING; do not touch market data
        for c in smoke:
            cid = c["expansion_case_id"]
            st = read_case_status(repo_root, cid)
            # leave PENDING unless already further along
            if st.get("status") not in (
                STATUS_MECHANICAL_COMPLETE,
                STATUS_FAILED_FINAL,
            ):
                st["status"] = STATUS_PENDING
                st["error"] = "blocked:BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED"
                write_case_status(repo_root, cid, st)
            append_jsonl(
                log_path,
                {
                    "ts": _utc_now(),
                    "event": "smoke_case_blocked",
                    "case_id": cid,
                    "pool_side": c["pool_side"],
                    "params": expansion_case_to_audit_params(c),
                },
            )

        status = refresh_status(repo_root, frozen)
        hashes_after = verify_frozen_inputs(repo_root, label="smoke_after_blocked")
        report = {
            "verdict": "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED",
            "ok": False,
            "smoke_cases": smoke_ids,
            "cases_executed": [],
            "market_data_accessed": False,
            "outcomes_read": False,
            "mechanical_complete_count": status["mechanical_complete_count"],
            "target_mechanical_complete": TARGET_MECHANICAL_COMPLETE,
            "capability": cap,
            "hashes_before": hashes_before,
            "hashes_after": hashes_after,
            "batch_status": status,
            "concurrency": CONCURRENCY,
            "note": (
                "Smoke aborted before Raw-OB / public-trades / LLD access. "
                "Hashed Entry Contract pipeline cannot run EXP ASK contacts or "
                "mechanical-only without modifying gehashte pipeline.py."
            ),
        }
        atomic_write_json(batch_root(repo_root) / "smoke_report.json", report)
        append_jsonl(log_path, {"ts": _utc_now(), "event": "smoke_blocked", "verdict": report["verdict"]})
        return report

    # --- Future path: if separation becomes available without hashed-file change ---
    # Would run sequential mechanical-only for smoke cases with resume/hash gates.
    raise BatchError(
        "EXPANSION_BATCH_V1_SMOKE_FAILED",
        "capability claimed separable but executor not wired in this task",
    )


def cmd_resume(repo_root: Path) -> dict[str, Any]:
    """Resume scaffolding: skip valid MECHANICAL_COMPLETE; no run-all in this task."""
    verify_frozen_inputs(repo_root, label="resume")
    frozen = load_v3_freeze(repo_root)
    status = refresh_status(repo_root, frozen)
    skippable = []
    for c in ordered_cases(frozen):
        cid = c["expansion_case_id"]
        ok, reason = mechanical_complete_valid(repo_root, cid)
        if ok:
            skippable.append(cid)
    return {
        "ok": True,
        "verdict": "RESUME_STATUS_ONLY",
        "skippable_mechanical_complete": skippable,
        "status": status,
        "note": "Full --run-all not started in this task; resume inspect only",
    }
