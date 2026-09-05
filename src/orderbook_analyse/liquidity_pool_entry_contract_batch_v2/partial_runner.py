"""Partial 12/24 mechanical run with concurrency<=2 coordination."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    DEFAULT_RAW_ROOT_REL,
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V2_HASH,
    EXPECTED_V4_HASH,
    STATUS_FAILED_FINAL,
    STATUS_FAILED_RETRYABLE,
    STATUS_PENDING,
    TARGET_MECHANICAL_COMPLETE,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.cases import (
    case_by_id,
    load_v4_freeze,
    ordered_cases,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.coordination import (
    BatchCoordinator,
    CoordinationError,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import (
    FrozenInputHashMismatch,
    verify_frozen_inputs,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.partial_sample import (
    EXPECTED_PARTIAL_12_IDS,
    PARTIAL_CONCURRENCY,
    PartialSampleError,
    build_partial_sample_12_manifest,
    verify_partial_manifest,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner import (
    BatchError,
    _run_one_case,
    cmd_unblind_outcomes,
    init_batch_dirs,
    refresh_status,
    write_batch_manifest,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
    append_jsonl,
    atomic_write_json,
    batch_root,
    case_dir,
    count_mechanical_complete,
    mechanical_complete_valid,
    read_case_status,
    write_case_status,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reset_invalid_complete_for_rerun(repo_root: Path, case_id: str) -> dict[str, Any]:
    """If status claims complete but artifacts invalid, or test-polluted FAILED_FINAL, reset."""
    ok, reason = mechanical_complete_valid(repo_root, case_id)
    if ok:
        return {"case_id": case_id, "action": "keep_complete"}
    st = read_case_status(repo_root, case_id)
    d = case_dir(repo_root, case_id)
    # Remove invalid mechanical artifacts so we do not skip on partial junk
    for name in (
        "mechanical_verdict_pre_unblind.json",
        "mechanical_complete.marker",
    ):
        p = d / name
        if p.is_file():
            # only remove if not a valid complete (already known invalid)
            p.unlink()
    for tmp in d.glob("*.tmp"):
        tmp.unlink(missing_ok=True)
    write_case_status(
        repo_root,
        case_id,
        {
            "case_id": case_id,
            "status": STATUS_PENDING,
            "mechanical_payload_sha256": None,
            "error": None,
            "worker_id": None,
            "worker_pid": None,
            "outcomes_read": False,
            "reset_reason": f"invalid_or_missing_complete:{reason or st.get('status')}",
        },
    )
    return {"case_id": case_id, "action": "reset_pending", "reason": reason or st.get("status")}


def _case_summary_row(repo_root: Path, case_row: dict[str, Any]) -> dict[str, Any]:
    cid = case_row["expansion_case_id"]
    d = case_dir(repo_root, cid)
    st = read_case_status(repo_root, cid)
    mech_path = d / "mechanical_verdict_pre_unblind.json"
    mech = json.loads(mech_path.read_text(encoding="utf-8")) if mech_path.is_file() else {}
    rt_path = d / "case_runtime.json"
    rt = json.loads(rt_path.read_text(encoding="utf-8")) if rt_path.is_file() else {}
    pref_path = d / "prefix_parity.json"
    pref = json.loads(pref_path.read_text(encoding="utf-8")) if pref_path.is_file() else {}
    lb = mech.get("long_branch") or {}
    sb = mech.get("short_branch") or {}
    lb_room = lb.get("room_gate") or {}
    sb_room = sb.get("room_gate") or {}
    geom = (rt.get("diagnostics") or {}).get("geometry") or {}
    defense_dir = geom.get("defense_trade_direction")
    breakout_dir = geom.get("breakout_trade_direction")

    def _branch_for_dir(direction: str | None) -> dict[str, Any]:
        if direction == "LONG":
            return lb
        if direction == "SHORT":
            return sb
        return {}

    defense_b = _branch_for_dir(defense_dir)
    breakout_b = _branch_for_dir(breakout_dir)
    trade = mech.get("mechanical_verdict") or st.get("mechanical_verdict")
    blockers = []
    if not (lb.get("microstructure_gate_passed") or sb.get("microstructure_gate_passed")):
        blockers.append("micro_both_fail")
    if lb.get("microstructure_gate_passed") and not (lb_room.get("gate_passed")):
        blockers.append("long_room_fail")
    if sb.get("microstructure_gate_passed") and not (sb_room.get("gate_passed")):
        blockers.append("short_room_fail")

    room_only_block = False
    # Micro pass on a branch but room fails; no other trade branch
    for br, room in ((lb, lb_room), (sb, sb_room)):
        if br.get("microstructure_gate_passed") and not room.get("gate_passed"):
            if room.get("gate_reason") in (
                "INSUFFICIENT_TARGET_DISTANCE",
                "MIN_TARGET_DISTANCE_NOT_MET",
                "TARGET_DISTANCE_BELOW_MIN",
            ) or (
                isinstance(room.get("raw_target_distance_pct"), (int, float))
                and float(room["raw_target_distance_pct"]) < 0.5
                and room.get("gate_passed") is False
            ):
                room_only_block = True

    # Detect 50bps-only block from gate_reason text if present
    for room in (lb_room, sb_room):
        gr = str(room.get("gate_reason") or "")
        if "0.5" in gr or "50" in gr or "MIN_TARGET" in gr or "INSUFFICIENT" in gr.upper():
            if room.get("gate_passed") is False:
                room_only_block = True

    return {
        "case_id": cid,
        "pool_side": case_row.get("pool_side") or mech.get("pool_side"),
        "approach": case_row.get("approach") or mech.get("approach"),
        "status": st.get("status"),
        "reaction": mech.get("reaction"),
        "mechanical_verdict": trade,
        "defense_direction": defense_dir,
        "breakout_direction": breakout_dir,
        "long_micro_passed": bool(lb.get("microstructure_gate_passed")),
        "short_micro_passed": bool(sb.get("microstructure_gate_passed")),
        "long_micro_reason": lb.get("microstructure_gate_reason"),
        "short_micro_reason": sb.get("microstructure_gate_reason"),
        "long_room_passed": bool(lb_room.get("gate_passed")),
        "short_room_passed": bool(sb_room.get("gate_passed")),
        "long_room_reason": lb_room.get("gate_reason"),
        "short_room_reason": sb_room.get("gate_reason"),
        "long_target_pool_id": lb_room.get("target_pool_id"),
        "short_target_pool_id": sb_room.get("target_pool_id"),
        "long_room_bps": lb_room.get("raw_target_distance_bps"),
        "short_room_bps": sb_room.get("raw_target_distance_bps"),
        "defense_micro_passed": bool(defense_b.get("microstructure_gate_passed")),
        "breakout_micro_passed": bool(breakout_b.get("microstructure_gate_passed")),
        "prefix_status": pref.get("prefix_status") or st.get("prefix_status"),
        "payload_sha": mech.get("mechanical_payload_sha256") or st.get("mechanical_payload_sha256"),
        "elapsed_s": st.get("elapsed_s") or rt.get("elapsed_s"),
        "peak_rss_mb": st.get("peak_rss_mb") or rt.get("peak_rss_mb"),
        "n_queries": rt.get("n_queries"),
        "worker_id": st.get("worker_id") or rt.get("worker_id"),
        "skipped_resume": False,
        "blockers": blockers,
        "room_50bps_block_candidate": room_only_block,
        "outcomes_read": False,
    }


def _aggregate(rows: list[dict[str, Any]], *, wallclock_s: float, max_workers_observed: int) -> dict[str, Any]:
    trade_like = [
        r
        for r in rows
        if r.get("mechanical_verdict")
        and "NO_TRADE" not in str(r.get("mechanical_verdict"))
        and "CONTEST" not in str(r.get("mechanical_verdict"))
        and str(r.get("mechanical_verdict")).endswith("CANDIDATE")
    ]
    # Also count CLEAR_*_CANDIDATE
    trade_cands = [
        r
        for r in rows
        if "CANDIDATE" in str(r.get("mechanical_verdict") or "")
        and "NO_TRADE" not in str(r.get("mechanical_verdict") or "")
    ]
    ask_trades = [r for r in trade_cands if r.get("pool_side") == "ASK"]
    bid_trades = [r for r in trade_cands if r.get("pool_side") == "BID"]

    defense_pass = sum(1 for r in rows if r.get("defense_micro_passed"))
    breakout_pass = sum(1 for r in rows if r.get("breakout_micro_passed"))
    micro_pass_any = sum(
        1 for r in rows if r.get("long_micro_passed") or r.get("short_micro_passed")
    )
    micro_fail_both = sum(
        1 for r in rows if not r.get("long_micro_passed") and not r.get("short_micro_passed")
    )
    room_pass_any = sum(
        1 for r in rows if r.get("long_room_passed") or r.get("short_room_passed")
    )
    room_fail_when_micro = sum(
        1
        for r in rows
        if (r.get("long_micro_passed") and not r.get("long_room_passed"))
        or (r.get("short_micro_passed") and not r.get("short_room_passed"))
    )
    room_only = [r for r in rows if r.get("room_50bps_block_candidate")]
    contest = sum(
        1
        for r in rows
        if "CONTEST" in str(r.get("mechanical_verdict") or "")
        or "CONTEST" in str(r.get("reaction") or "")
        or "NO_CONFIRMATION" in str(r.get("long_micro_reason") or "")
        or "NO_CONFIRMATION" in str(r.get("short_micro_reason") or "")
    )
    seq_est = sum(float(r.get("elapsed_s") or 0.0) for r in rows)
    speedup = (seq_est / wallclock_s) if wallclock_s > 0 else None
    return {
        "trade_candidates_total": len(trade_cands),
        "trade_candidates_ask": len(ask_trades),
        "trade_candidates_bid": len(bid_trades),
        "defense_micro_pass_count": defense_pass,
        "breakout_micro_pass_count": breakout_pass,
        "micro_pass_any_branch": micro_pass_any,
        "micro_fail_both_branches": micro_fail_both,
        "room_pass_any_branch": room_pass_any,
        "room_fail_on_micro_pass_branch": room_fail_when_micro,
        "blocked_only_by_50bps_room_gate_candidates": [
            r["case_id"] for r in room_only
        ],
        "contest_or_no_confirmation_count": contest,
        "technical_errors": [
            r["case_id"] for r in rows if r.get("status") not in ("MECHANICAL_COMPLETE",)
        ],
        "max_concurrent_workers_observed": max_workers_observed,
        "sum_case_elapsed_s": seq_est,
        "wallclock_s": wallclock_s,
        "wallclock_speedup_vs_sequential_sum": speedup,
    }


def cmd_partial_12(
    repo_root: Path,
    *,
    mechanical_only: bool = True,
    raw_root: Path | None = None,
    concurrency: int = PARTIAL_CONCURRENCY,
) -> dict[str, Any]:
    if not mechanical_only:
        raise BatchError(
            "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
            "--partial-12 requires --mechanical-only",
        )
    if concurrency < 1 or concurrency > 2:
        raise BatchError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"concurrency={concurrency} exceeds allowed max 2",
        )

    hashes_before = verify_frozen_inputs(repo_root, label="partial12_before")
    frozen = load_v4_freeze(repo_root)
    init_batch_dirs(repo_root, frozen)
    write_batch_manifest(repo_root, frozen, mode="partial_12")

    manifest = build_partial_sample_12_manifest(
        repo_root,
        entry_contract_v2_sha=EXPECTED_V2_HASH,
        expansion_v4_sha=EXPECTED_V4_HASH,
        strategy_config_sha=EXPECTED_STRATEGY_CONFIG_HASH,
    )
    verify_partial_manifest(manifest)
    case_ids = list(manifest["case_ids"])
    if tuple(case_ids) != EXPECTED_PARTIAL_12_IDS:
        raise BatchError("PARALLEL_BATCH_COORDINATION_FAILURE", "manifest ids mismatch")

    resets = []
    for cid in case_ids:
        resets.append(_reset_invalid_complete_for_rerun(repo_root, cid))

    # Resume inventory
    skippable = []
    todo = []
    for cid in case_ids:
        ok, _ = mechanical_complete_valid(repo_root, cid)
        if ok:
            skippable.append(cid)
        else:
            todo.append(cid)

    raw = Path(raw_root) if raw_root else repo_root / DEFAULT_RAW_ROOT_REL
    if not raw.is_dir():
        raise BatchError("EXPANSION_BATCH_V2_PARTIAL_RETRYABLE", f"raw_root missing: {raw}")

    coord = BatchCoordinator(repo_root, max_concurrency=concurrency)
    coord.clear_stop()
    log_path = batch_root(repo_root) / "run_log.jsonl"
    append_jsonl(
        log_path,
        {
            "ts": _utc_now(),
            "event": "partial12_start",
            "case_ids": case_ids,
            "todo": todo,
            "skippable": skippable,
            "concurrency": concurrency,
            "manifest_sha": manifest["partial_sample_12_manifest_sha256"],
        },
    )

    results_by_id: dict[str, Any] = {}
    retries_used: dict[str, int] = {}
    max_workers_observed = 0
    observed_lock = threading.Lock()
    stop_fatal: list[str] = []

    def _on_signal(signum, frame):  # noqa: ANN001
        coord.request_stop(f"signal_{signum}")
        for cid in case_ids:
            st = read_case_status(repo_root, cid)
            if st.get("status") == "RUNNING":
                wid = st.get("worker_id") or "unknown"
                coord.mark_retryable_interrupted(cid, worker_id=str(wid))

    prev_int = signal.signal(signal.SIGINT, _on_signal)
    prev_term = signal.signal(signal.SIGTERM, _on_signal)

    t_wall0 = time.perf_counter()
    try:
        def worker_loop(worker_id: str) -> list[dict[str, Any]]:
            nonlocal max_workers_observed
            local: list[dict[str, Any]] = []
            pid = os.getpid()
            while not coord.stop_requested():
                with observed_lock:
                    # approximate concurrent by counting RUNNING
                    running_n = sum(
                        1
                        for c in case_ids
                        if read_case_status(repo_root, c).get("status") == "RUNNING"
                    )
                    max_workers_observed = max(max_workers_observed, running_n, 1 if local else 0)

                reservation = coord.try_reserve(case_ids, worker_id=worker_id, worker_pid=pid)
                if reservation is None:
                    # Idle: either no work or at concurrency cap / stop
                    # Exit if all sample cases complete or failed-final / stop
                    remaining = []
                    for cid in case_ids:
                        ok, _ = mechanical_complete_valid(repo_root, cid)
                        if ok:
                            continue
                        st = read_case_status(repo_root, cid)
                        if st.get("status") in (STATUS_PENDING, STATUS_FAILED_RETRYABLE):
                            remaining.append(cid)
                        elif st.get("status") == "RUNNING":
                            remaining.append(cid)
                    if not remaining or coord.stop_requested():
                        break
                    time.sleep(0.25)
                    continue

                cid = reservation.case_id
                try:
                    coord.assert_owned(cid, worker_id=worker_id, worker_pid=pid)
                    case_row = case_by_id(frozen, cid)
                    # Post-reserve hash check before heavy work
                    verify_frozen_inputs(repo_root, label=f"before_{cid}")
                    res = _run_one_case(
                        repo_root=repo_root,
                        case_row=case_row,
                        raw_root=raw,
                        log_path=log_path,
                        hashes=hashes_before,
                        already_reserved=True,
                        worker_id=worker_id,
                        query_audit_append=coord.append_query_audit,
                        skip_post_hash_verify=False,
                        technical_failure_verdict="EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
                    )
                    local.append(res)
                    results_by_id[cid] = res
                except BatchError as exc:
                    if exc.verdict in (
                        "FROZEN_INPUT_HASH_MISMATCH",
                        "BATCH_PREFIX_PARITY_FAILURE",
                        "OUTCOME_BLINDNESS_VIOLATION",
                        "PARALLEL_BATCH_COORDINATION_FAILURE",
                    ):
                        coord.request_stop(exc.verdict)
                        stop_fatal.append(exc.verdict)
                        raise
                    # technical retryable: one controlled retry
                    n = retries_used.get(cid, 0)
                    if n < 1 and exc.verdict == "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE":
                        retries_used[cid] = n + 1
                        st = read_case_status(repo_root, cid)
                        st["status"] = STATUS_FAILED_RETRYABLE
                        st["error"] = f"retry_scheduled:{exc}"
                        st["worker_pid"] = None
                        write_case_status(repo_root, cid, st)
                        append_jsonl(
                            log_path,
                            {
                                "ts": _utc_now(),
                                "event": "retry_scheduled",
                                "case_id": cid,
                                "attempt": n + 1,
                            },
                        )
                        continue
                    coord.request_stop(exc.verdict)
                    stop_fatal.append(exc.verdict)
                    raise
                except FrozenInputHashMismatch as exc:
                    coord.request_stop("FROZEN_INPUT_HASH_MISMATCH")
                    stop_fatal.append("FROZEN_INPUT_HASH_MISMATCH")
                    raise BatchError("FROZEN_INPUT_HASH_MISMATCH", exc.detail) from exc
                except CoordinationError as exc:
                    coord.request_stop(exc.verdict)
                    stop_fatal.append(exc.verdict)
                    raise BatchError(exc.verdict, str(exc)) from exc
            return local

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="lp_batch") as ex:
            futs = [
                ex.submit(worker_loop, f"worker-{i+1}")
                for i in range(concurrency)
            ]
            errors: list[BaseException] = []
            for fut in as_completed(futs):
                try:
                    fut.result()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
            if errors:
                # Prefer classified BatchError
                for e in errors:
                    if isinstance(e, BatchError):
                        raise e
                raise BatchError(
                    "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
                    str(errors[0]),
                ) from errors[0]
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    wallclock_s = time.perf_counter() - t_wall0

    # Build per-case summaries for all 12
    rows = []
    resume_skipped = []
    for cid in case_ids:
        ok, _ = mechanical_complete_valid(repo_root, cid)
        row = _case_summary_row(repo_root, case_by_id(frozen, cid))
        if cid in skippable and cid not in results_by_id:
            row["skipped_resume"] = True
            resume_skipped.append(cid)
        elif results_by_id.get(cid, {}).get("skipped"):
            row["skipped_resume"] = True
            resume_skipped.append(cid)
        rows.append(row)
        if not ok:
            raise BatchError(
                "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
                f"case {cid} not MECHANICAL_COMPLETE after partial run: {row.get('status')}",
            )

    # Untouched outside sample
    all_ids = [c["expansion_case_id"] for c in ordered_cases(frozen)]
    outside = [c for c in all_ids if c not in case_ids]
    untouched = []
    for cid in outside:
        st = read_case_status(repo_root, cid)
        untouched.append(
            {
                "case_id": cid,
                "status": st.get("status"),
                "mechanical_complete": mechanical_complete_valid(repo_root, cid)[0],
            }
        )
        # Must not be newly completed outside sample in this task
        if mechanical_complete_valid(repo_root, cid)[0]:
            raise BatchError(
                "PARALLEL_BATCH_COORDINATION_FAILURE",
                f"outside-sample case unexpectedly complete: {cid}",
            )

    n_complete_sample = count_mechanical_complete(repo_root, case_ids)
    n_complete_all = count_mechanical_complete(repo_root, all_ids)
    if n_complete_sample != 12:
        raise BatchError(
            "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
            f"expected 12/12 sample complete, got {n_complete_sample}",
        )
    if n_complete_all != 12:
        raise BatchError(
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            f"expected exactly 12/24 global complete, got {n_complete_all}",
        )

    hashes_after = verify_frozen_inputs(repo_root, label="partial12_after")
    aggregates = _aggregate(
        rows, wallclock_s=wallclock_s, max_workers_observed=max(max_workers_observed, concurrency)
    )

    # Prefix 12/12
    prefix_ok = all(r.get("prefix_status") == "EXACT_PREFIX_PARITY" for r in rows)
    if not prefix_ok:
        raise BatchError("BATCH_PREFIX_PARITY_FAILURE", "prefix parity not 12/12")

    # Unblind gate at 12/24
    unblind_block = None
    try:
        cmd_unblind_outcomes(repo_root)
    except BatchError as exc:
        unblind_block = {"verdict": exc.verdict, "detail": str(exc)}

    status = refresh_status(repo_root, frozen)
    report = {
        "verdict": "EXPANSION_BATCH_V2_PARTIAL_12_MECHANICAL_COMPLETE",
        "ok": True,
        "partial_sample_case_ids": case_ids,
        "partial_sample_12_manifest_sha256": manifest["partial_sample_12_manifest_sha256"],
        "n_ask": 6,
        "n_bid": 6,
        "resume_skipped": resume_skipped,
        "resets_before_run": resets,
        "cases": rows,
        "aggregates": aggregates,
        "mechanical_complete_count_sample": n_complete_sample,
        "mechanical_complete_count_global": n_complete_all,
        "target_mechanical_complete": TARGET_MECHANICAL_COMPLETE,
        "outside_sample_untouched": untouched,
        "outcome_read_count": 0,
        "unblind_blocked": unblind_block,
        "concurrency_max": concurrency,
        "max_concurrent_workers_observed": aggregates["max_concurrent_workers_observed"],
        "hashes_before": hashes_before,
        "hashes_after": hashes_after,
        "batch_status": status,
        "wallclock_s": wallclock_s,
        "note": "STOP at exactly 12/24 MECHANICAL_COMPLETE; no unblind; no remaining-12 execution",
    }
    atomic_write_json(batch_root(repo_root) / "partial_12_report.json", report)
    atomic_write_json(
        batch_root(repo_root) / "partial_12_case_summaries.json",
        {"cases": rows, "aggregates": aggregates},
    )
    append_jsonl(
        log_path,
        {"ts": _utc_now(), "event": "partial12_complete", "verdict": report["verdict"]},
    )
    return report
