"""Batch orchestration V2 — smoke EXP_01/EXP_03 via run_mechanical_audit only."""

from __future__ import annotations

import json
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    CONCURRENCY,
    DEFAULT_RAW_ROOT_REL,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V2_HASH,
    EXPECTED_V4_HASH,
    FORMAT_VERSION,
    RESULTS_DIR_REL,
    SMOKE_ASK_CASE_ID,
    SMOKE_BID_CASE_ID,
    STATUS_FAILED_FINAL,
    STATUS_FAILED_RETRYABLE,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_PENDING,
    STATUS_RUNNING,
    TARGET_MECHANICAL_COMPLETE,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.cases import (
    case_spec_from_v4_row,
    load_v4_freeze,
    mechanical_executed_count_before,
    ordered_cases,
    outcome_read_count_before,
    smoke_selection,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import (
    FrozenInputHashMismatch,
    verify_frozen_inputs,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.market_evidence import (
    MarketEvidenceError,
    build_market_evidence_bundle,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
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
from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
    MechanicalAuditError,
    run_mechanical_audit,
)


class BatchError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _peak_rss_mb() -> float:
    # Linux: ru_maxrss is KB
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def init_batch_dirs(repo_root: Path, frozen: dict[str, Any]) -> Path:
    root = batch_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases").mkdir(exist_ok=True)
    for c in ordered_cases(frozen):
        d = case_dir(repo_root, c["expansion_case_id"])
        d.mkdir(parents=True, exist_ok=True)
        st = read_case_status(repo_root, c["expansion_case_id"])
        if not (d / "case_status.json").is_file():
            write_case_status(repo_root, c["expansion_case_id"], st)
    return root


def write_batch_manifest(repo_root: Path, frozen: dict[str, Any], *, mode: str) -> dict[str, Any]:
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_or_updated_at_utc": _utc_now(),
        "mode": mode,
        "results_dir": RESULTS_DIR_REL,
        "concurrency": CONCURRENCY,
        "entry_contract_v2_freeze_sha256": EXPECTED_V2_HASH,
        "expansion_v4_binding_sha256": EXPECTED_V4_HASH,
        "strategy_config_sha256": EXPECTED_STRATEGY_CONFIG_HASH,
        "case_count": len(ordered_cases(frozen)),
        "smoke_cases": [c["expansion_case_id"] for c in smoke_selection(frozen)],
        "mechanical_api": "run_mechanical_audit",
        "unblind_api": "run_outcome_unblind",
        "automatic_unblind_from_mechanical": False,
        "unblind_requires_mechanical_complete_count": TARGET_MECHANICAL_COMPLETE,
        "no_outcome_in_mechanical": True,
        "no_decision_reimplementation_in_runner": True,
    }
    atomic_write_json(batch_root(repo_root) / "batch_manifest.json", manifest)
    return manifest


def refresh_status(repo_root: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    status = build_batch_status(repo_root, ordered_cases(frozen))
    atomic_write_json(batch_root(repo_root) / "batch_status.json", status)
    return status


def cmd_verify(repo_root: Path) -> dict[str, Any]:
    before = verify_frozen_inputs(repo_root, label="verify")
    frozen = load_v4_freeze(repo_root)
    return {
        "ok": True,
        "hashes": before,
        "mechanical_executed_count_before_v4": mechanical_executed_count_before(frozen),
        "outcome_read_count_before_v4": outcome_read_count_before(frozen),
    }


def cmd_plan(repo_root: Path) -> dict[str, Any]:
    verify_frozen_inputs(repo_root, label="plan_before")
    frozen = load_v4_freeze(repo_root)
    init_batch_dirs(repo_root, frozen)
    smoke = smoke_selection(frozen)
    manifest = write_batch_manifest(repo_root, frozen, mode="plan")
    status = refresh_status(repo_root, frozen)
    plan = {
        "ok": True,
        "verdict": "PLAN_READY",
        "smoke_cases": [
            {
                "expansion_case_id": c["expansion_case_id"],
                "pool_side": c["pool_side"],
                "approach": c["approach"],
                "symbol": c["symbol"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
            }
            for c in smoke
        ],
        "all_case_ids": [c["expansion_case_id"] for c in ordered_cases(frozen)],
        "manifest": manifest,
        "status_counts": status["counts"],
        "note": "No market data accessed in --plan",
    }
    atomic_write_json(batch_root(repo_root) / "plan.json", plan)
    verify_frozen_inputs(repo_root, label="plan_after")
    return plan


def cmd_status(repo_root: Path) -> dict[str, Any]:
    frozen = load_v4_freeze(repo_root)
    status = refresh_status(repo_root, frozen)
    return {"ok": True, "status": status}


def cmd_unblind_outcomes(repo_root: Path) -> dict[str, Any]:
    """Fail-closed: require all 24 mechanical complete. Never auto-unblind."""
    verify_frozen_inputs(repo_root, label="unblind_gate")
    frozen = load_v4_freeze(repo_root)
    ids = [c["expansion_case_id"] for c in ordered_cases(frozen)]
    n = count_mechanical_complete(repo_root, ids)
    if n != TARGET_MECHANICAL_COMPLETE:
        raise BatchError(
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            f"mechanical_complete_count={n} != {TARGET_MECHANICAL_COMPLETE}",
        )
    # Even at 24, this smoke task must not open outcomes.
    raise BatchError(
        "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
        "unblind deferred; smoke must not open outcomes",
    )


def _run_one_case(
    *,
    repo_root: Path,
    case_row: dict[str, Any],
    raw_root: Path,
    log_path: Path,
    hashes: dict[str, Any],
    already_reserved: bool = False,
    worker_id: str | None = None,
    query_audit_append=None,
    skip_post_hash_verify: bool = False,
    technical_failure_verdict: str = "EXPANSION_BATCH_V2_SMOKE_FAILED",
) -> dict[str, Any]:
    """Execute one mechanical case via run_mechanical_audit.

    If already_reserved=True, status must already be RUNNING for this worker.
    query_audit_append: optional callable(dict) for lock-safe query audit append.
    """
    cid = case_row["expansion_case_id"]
    out = case_dir(repo_root, cid)
    ok_skip, reason = mechanical_complete_valid(repo_root, cid)
    if ok_skip:
        append_jsonl(
            log_path,
            {"ts": _utc_now(), "event": "skip_complete", "case_id": cid, "reason": reason},
        )
        return {
            "case_id": cid,
            "skipped": True,
            "ok": True,
            "mechanical_payload_sha256": read_case_status(repo_root, cid).get(
                "mechanical_payload_sha256"
            ),
        }

    if not already_reserved:
        st = read_case_status(repo_root, cid)
        st["status"] = STATUS_RUNNING
        st["started_at_utc"] = _utc_now()
        st["error"] = None
        st["worker_pid"] = os.getpid()
        st["worker_id"] = worker_id
        st["outcomes_read"] = False
        write_case_status(repo_root, cid, st)

    t0 = time.perf_counter()
    rss0 = _peak_rss_mb()
    query_log: list[dict[str, Any]] = []
    try:
        spec = case_spec_from_v4_row(case_row)
        if cid == SMOKE_ASK_CASE_ID and spec.pool_side != "ASK":
            raise BatchError("EXP_ASK_REAL_DATA_EXECUTION_BLOCKED", "ASK case loaded as non-ASK")
        if cid == SMOKE_BID_CASE_ID and spec.pool_side != "BID":
            raise BatchError("EXP_BID_REAL_DATA_EXECUTION_BLOCKED", "BID case loaded as non-BID")

        append_jsonl(
            log_path,
            {
                "ts": _utc_now(),
                "event": "case_start",
                "case_id": cid,
                "pool_side": spec.pool_side,
                "approach": spec.approach,
                "worker_id": worker_id,
            },
        )

        bundle = build_market_evidence_bundle(
            case_spec=spec,
            repo_root=repo_root,
            raw_root=raw_root,
            out_dir=out,
            query_log=query_log,
        )
        for q in query_log:
            row = {"case_id": cid, **q}
            if query_audit_append is not None:
                query_audit_append(row)
            else:
                append_jsonl(batch_root(repo_root) / "query_audit.jsonl", row)

        frozen_inputs = {
            "evidence": bundle["evidence"],
            "pool_geometry_rows": bundle["pool_geometry_rows"],
            "hashes": {
                "expansion_freeze_sha256": EXPECTED_V4_HASH,
                "entry_contract_v2_freeze_sha256": EXPECTED_V2_HASH,
            },
            "forbid_outcome_unblind": True,
        }
        # Public V2 mechanical API only — never call run_outcome_unblind here.
        res = run_mechanical_audit(spec, frozen_inputs, out, repo_root=repo_root)

        # Enrich blindness audit with market_data flag (non-decision metadata)
        blindness_path = out / "mechanical_blindness_audit.json"
        if blindness_path.is_file():
            blindness = json.loads(blindness_path.read_text(encoding="utf-8"))
            blindness["market_data_loaded"] = True
            blindness["outcomes_read"] = False
            blindness["unblind_invoked"] = False
            atomic_write_json(blindness_path, blindness)

        mech_path = out / "mechanical_verdict_pre_unblind.json"
        mech = json.loads(mech_path.read_text(encoding="utf-8"))
        # Attach non-decision runtime meta without rewriting decision fields used in payload SHA
        runtime = {
            "elapsed_s": time.perf_counter() - t0,
            "peak_rss_mb": max(rss0, _peak_rss_mb()),
            "n_queries": len(query_log),
            "market_data_loaded": True,
            "outcomes_read": False,
            "worker_id": worker_id,
            "diagnostics": {
                k: bundle["diagnostics"].get(k)
                for k in (
                    "arrival_ts",
                    "first_back_cross_ts",
                    "aggressor_counts",
                    "wall_flags",
                    "geometry",
                    "reference_mid",
                )
            },
        }
        atomic_write_json(out / "case_runtime.json", runtime)

        elapsed = runtime["elapsed_s"]
        peak = runtime["peak_rss_mb"]
        st = read_case_status(repo_root, cid)
        st["status"] = STATUS_MECHANICAL_COMPLETE
        st["finished_at_utc"] = _utc_now()
        st["elapsed_s"] = elapsed
        st["peak_rss_mb"] = peak
        st["mechanical_payload_sha256"] = res["mechanical_payload_sha256"]
        st["prefix_status"] = res.get("prefix_status")
        st["worker_pid"] = None
        st["worker_id"] = worker_id
        st["error"] = None
        st["market_data_loaded"] = True
        st["outcomes_read"] = False
        st["mechanical_verdict"] = mech.get("mechanical_verdict")
        write_case_status(repo_root, cid, st)

        # Post-case hash gate
        if not skip_post_hash_verify:
            verify_frozen_inputs(repo_root, label=f"after_{cid}")

        append_jsonl(
            log_path,
            {
                "ts": _utc_now(),
                "event": "case_complete",
                "case_id": cid,
                "mechanical_verdict": mech.get("mechanical_verdict"),
                "prefix_status": res.get("prefix_status"),
                "payload_sha": res["mechanical_payload_sha256"],
                "elapsed_s": elapsed,
                "worker_id": worker_id,
            },
        )
        return {
            "case_id": cid,
            "skipped": False,
            "ok": True,
            "pool_side": spec.pool_side,
            "approach": spec.approach,
            "mechanical_verdict": mech.get("mechanical_verdict"),
            "reaction": mech.get("reaction"),
            "prefix_status": res.get("prefix_status"),
            "mechanical_payload_sha256": res["mechanical_payload_sha256"],
            "elapsed_s": elapsed,
            "peak_rss_mb": peak,
            "n_queries": len(query_log),
            "long_branch": mech.get("long_branch"),
            "short_branch": mech.get("short_branch"),
            "room_gate": mech.get("room_gate"),
            "entry_price": mech.get("entry_price"),
            "first_available_ts": mech.get("first_available_ts"),
            "reference_mid": bundle["diagnostics"].get("reference_mid"),
            "selected_pool": {
                "pool_id": spec.pool_id,
                "lower": spec.pool_lower,
                "upper": spec.pool_upper,
                "front_edge": mech.get("front_edge"),
                "back_edge": mech.get("back_edge"),
            },
            "outcomes_read": False,
            "market_data_loaded": True,
            "worker_id": worker_id,
        }
    except MechanicalAuditError as exc:
        st = read_case_status(repo_root, cid)
        st["status"] = (
            STATUS_FAILED_FINAL
            if exc.verdict in ("SMOKE_PREFIX_PARITY_FAILURE",)
            else STATUS_FAILED_RETRYABLE
        )
        st["error"] = f"{exc.verdict}:{exc}"
        st["finished_at_utc"] = _utc_now()
        st["elapsed_s"] = time.perf_counter() - t0
        st["peak_rss_mb"] = max(rss0, _peak_rss_mb())
        st["worker_pid"] = None
        write_case_status(repo_root, cid, st)
        append_jsonl(
            log_path,
            {"ts": _utc_now(), "event": "case_failed", "case_id": cid, "verdict": exc.verdict},
        )
        if exc.verdict == "SMOKE_PREFIX_PARITY_FAILURE":
            raise BatchError(
                "BATCH_PREFIX_PARITY_FAILURE"
                if technical_failure_verdict == "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE"
                else "SMOKE_PREFIX_PARITY_FAILURE",
                str(exc),
            ) from exc
        raise BatchError(technical_failure_verdict, str(exc)) from exc
    except MarketEvidenceError as exc:
        st = read_case_status(repo_root, cid)
        st["status"] = STATUS_FAILED_FINAL
        st["error"] = f"{exc.verdict}:{exc}"
        st["finished_at_utc"] = _utc_now()
        st["elapsed_s"] = time.perf_counter() - t0
        st["peak_rss_mb"] = max(rss0, _peak_rss_mb())
        st["worker_pid"] = None
        write_case_status(repo_root, cid, st)
        raise BatchError(exc.verdict, str(exc)) from exc
    except FrozenInputHashMismatch as exc:
        st = read_case_status(repo_root, cid)
        st["status"] = STATUS_FAILED_FINAL
        st["error"] = f"FROZEN_INPUT_HASH_MISMATCH:{exc.detail}"
        st["finished_at_utc"] = _utc_now()
        st["worker_pid"] = None
        write_case_status(repo_root, cid, st)
        raise BatchError("FROZEN_INPUT_HASH_MISMATCH", exc.detail) from exc
    except BatchError:
        raise
    except Exception as exc:
        st = read_case_status(repo_root, cid)
        st["status"] = STATUS_FAILED_RETRYABLE
        st["error"] = f"FAILED_RETRYABLE:{type(exc).__name__}:{exc}"
        st["finished_at_utc"] = _utc_now()
        st["elapsed_s"] = time.perf_counter() - t0
        st["peak_rss_mb"] = max(rss0, _peak_rss_mb())
        st["worker_pid"] = None
        write_case_status(repo_root, cid, st)
        append_jsonl(
            log_path,
            {
                "ts": _utc_now(),
                "event": "case_failed",
                "case_id": cid,
                "error": str(exc),
            },
        )
        raise BatchError(technical_failure_verdict, str(exc)) from exc


def cmd_smoke(
    repo_root: Path,
    *,
    mechanical_only: bool = True,
    raw_root: Path | None = None,
) -> dict[str, Any]:
    if not mechanical_only:
        raise BatchError("EXPANSION_BATCH_V2_SMOKE_FAILED", "--smoke requires --mechanical-only")

    hashes_before = verify_frozen_inputs(repo_root, label="smoke_before")
    frozen = load_v4_freeze(repo_root)
    if mechanical_executed_count_before(frozen) != 0:
        raise BatchError(
            "EXPANSION_BATCH_V2_SMOKE_FAILED",
            f"mechanical_executed_count_before_v4={mechanical_executed_count_before(frozen)} != 0",
        )
    if outcome_read_count_before(frozen) != 0:
        raise BatchError(
            "EXPANSION_BATCH_V2_SMOKE_FAILED",
            f"outcome_read_count_before_v4={outcome_read_count_before(frozen)} != 0",
        )

    init_batch_dirs(repo_root, frozen)
    write_batch_manifest(repo_root, frozen, mode="smoke")
    smoke = smoke_selection(frozen)
    smoke_ids = [c["expansion_case_id"] for c in smoke]
    if smoke_ids != [SMOKE_ASK_CASE_ID, SMOKE_BID_CASE_ID]:
        raise BatchError(
            "EXPANSION_BATCH_V2_SMOKE_FAILED",
            f"smoke selection {smoke_ids} != [{SMOKE_ASK_CASE_ID}, {SMOKE_BID_CASE_ID}]",
        )

    raw = Path(raw_root) if raw_root else repo_root / DEFAULT_RAW_ROOT_REL
    if not raw.is_dir():
        raise BatchError("EXPANSION_BATCH_V2_SMOKE_FAILED", f"raw_root missing: {raw}")

    log_path = batch_root(repo_root) / "run_log.jsonl"
    append_jsonl(
        log_path,
        {
            "ts": _utc_now(),
            "event": "smoke_start",
            "cases": smoke_ids,
            "mechanical_only": True,
            "concurrency": CONCURRENCY,
            "raw_root": str(raw),
        },
    )

    executed: list[dict[str, Any]] = []
    # concurrency=1: EXP_01 fully completes before EXP_03 starts
    for case_row in smoke:
        result = _run_one_case(
            repo_root=repo_root,
            case_row=case_row,
            raw_root=raw,
            log_path=log_path,
            hashes=hashes_before,
        )
        executed.append(result)

    status = refresh_status(repo_root, frozen)
    hashes_after = verify_frozen_inputs(repo_root, label="smoke_after")
    all_ids = [c["expansion_case_id"] for c in ordered_cases(frozen)]
    n_complete = count_mechanical_complete(repo_root, all_ids)

    report = {
        "verdict": "EXPANSION_BATCH_V2_SMOKE_MECHANICAL_COMPLETE",
        "ok": True,
        "smoke_cases": smoke_ids,
        "cases_executed": executed,
        "market_data_accessed": True,
        "outcomes_read": False,
        "outcome_read_count": 0,
        "mechanical_complete_count": n_complete,
        "target_mechanical_complete": TARGET_MECHANICAL_COMPLETE,
        "concurrency": CONCURRENCY,
        "hashes_before": hashes_before,
        "hashes_after": hashes_after,
        "batch_status": status,
        "unblind_blocked_at_2_of_24": n_complete < TARGET_MECHANICAL_COMPLETE,
        "remaining_cases": TARGET_MECHANICAL_COMPLETE - n_complete,
        "note": "STOP after EXP_01 and EXP_03; no full batch; no unblind",
    }
    atomic_write_json(batch_root(repo_root) / "smoke_report.json", report)
    append_jsonl(
        log_path,
        {"ts": _utc_now(), "event": "smoke_complete", "verdict": report["verdict"]},
    )
    return report


def cmd_resume(repo_root: Path) -> dict[str, Any]:
    verify_frozen_inputs(repo_root, label="resume")
    frozen = load_v4_freeze(repo_root)
    status = refresh_status(repo_root, frozen)
    skippable = []
    for c in ordered_cases(frozen):
        cid = c["expansion_case_id"]
        ok, _ = mechanical_complete_valid(repo_root, cid)
        if ok:
            skippable.append(cid)
    return {
        "ok": True,
        "verdict": "RESUME_STATUS_ONLY",
        "skippable_mechanical_complete": skippable,
        "status": status,
        "note": "Full --run-all not started in this task; resume inspect only",
    }
