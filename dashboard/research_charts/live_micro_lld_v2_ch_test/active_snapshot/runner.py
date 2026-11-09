"""Controlled multi-close runner with publish gating and resume support."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..identity import build_live_micro_lld_v2_config, config_hash
from ...service import _timeframe_seconds
from .cleanup import cleanup_before_retry
from .extract import (
    ActiveStateSnapshot,
    build_active_state_at_decision_close,
    compare_active_sets,
)
from .faults import FaultInjector, SimulatedCrash
from .publish_guard import LineIdentity
from .published_reader import PublishedActiveReader
from .publisher import publish_validated_snapshot
from .writer import snapshot_id_for_state


@dataclass
class CloseTiming:
    decision_close: str
    load_seconds: float = 0.0
    engine_seconds: float = 0.0
    write_seconds: float = 0.0
    validate_publish_seconds: float = 0.0
    total_seconds: float = 0.0


@dataclass
class RunnerResult:
    pipeline_line_id: str
    test_run_id: str
    closes_processed: list[str] = field(default_factory=list)
    snapshot_ids: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    timings: list[CloseTiming] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_early: bool = False


def enumerate_decision_closes(
    first_close: datetime,
    count: int,
    timeframe: str,
) -> list[datetime]:
    step = _timeframe_seconds(timeframe)
    base = first_close.astimezone(timezone.utc)
    return [base + timedelta(seconds=step * i) for i in range(int(count))]


def build_line_identity(
    symbol: str,
    timeframe: str,
    pool_detail: int,
    history_from_close: datetime,
    candle_load_start: datetime,
) -> LineIdentity:
    cfg = build_live_micro_lld_v2_config(pool_detail)
    return LineIdentity(
        symbol=symbol.upper(),
        timeframe=timeframe,
        pool_detail=int(pool_detail),
        config_hash=config_hash(cfg),
        engine_version="live_micro_lld_v2@trp",
        candle_load_start=candle_load_start.astimezone(timezone.utc),
        history_from_close=history_from_close.astimezone(timezone.utc),
    )


def _compute_state(
    symbol: str,
    timeframe: str,
    history_from: datetime,
    decision_close: datetime,
    pool_detail: int,
) -> ActiveStateSnapshot:
    state, _, _ = build_active_state_at_decision_close(
        symbol, timeframe, history_from, decision_close, pool_detail=pool_detail
    )
    return state


def process_close_with_publish(
    *,
    symbol: str,
    timeframe: str,
    history_from: datetime,
    decision_close: datetime,
    pool_detail: int,
    pipeline_line_id: str,
    test_run_id: str,
    line: LineIdentity,
    faults: FaultInjector | None = None,
    on_crash: Callable[[SimulatedCrash, ActiveStateSnapshot | None], None] | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    state: ActiveStateSnapshot | None = None
    try:
        t_load = time.perf_counter()
        state = _compute_state(symbol, timeframe, history_from, decision_close, pool_detail)
        load_s = time.perf_counter() - t_load
        t_pub = time.perf_counter()
        pub = publish_validated_snapshot(
            state,
            pipeline_line_id=pipeline_line_id,
            test_run_id=test_run_id,
            line=line,
            faults=faults,
        )
        pub_s = time.perf_counter() - t_pub
        total = time.perf_counter() - t0
        sid = pub.get("snapshot_id") or snapshot_id_for_state(state)
        return {
            "decision_close": decision_close.isoformat(),
            "snapshot_id": sid,
            "publish_id": pub.get("publish_id"),
            "status": pub["status"],
            "active_state_fingerprint": state.active_state_fingerprint,
            "prefix_candle_fingerprint": state.prefix_candle_fingerprint,
            "timing": CloseTiming(
                decision_close=decision_close.isoformat(),
                load_seconds=load_s,
                engine_seconds=load_s,
                write_seconds=pub_s * 0.5,
                validate_publish_seconds=pub_s * 0.5,
                total_seconds=total,
            ),
        }
    except SimulatedCrash as crash:
        if on_crash:
            on_crash(crash, state)
        elif state is not None:
            cleanup_before_retry(state)
        raise


def run_controlled_pipeline(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    history_from: datetime,
    candle_load_start: datetime,
    decision_closes: list[datetime],
    pipeline_line_id: str,
    fault_schedule: dict[int, FaultInjector] | None = None,
    max_attempts_per_close: int = 3,
) -> RunnerResult:
    """
    Process closes in order. Stops on first non-recoverable error.
    fault_schedule maps close index -> FaultInjector for first attempt only.
    """
    fault_schedule = fault_schedule or {}
    line = build_line_identity(symbol, timeframe, pool_detail, history_from, candle_load_start)
    test_run_id = str(uuid.uuid4())
    pub_reader = PublishedActiveReader()
    result = RunnerResult(pipeline_line_id=pipeline_line_id, test_run_id=test_run_id)

    for idx, dc in enumerate(decision_closes):
        dc_utc = dc.astimezone(timezone.utc)
        try:
            already = pub_reader.load_published(
                pipeline_line_id, dc_utc, config_hash=line.config_hash
            )
        except Exception:
            already = None
        if already:
            result.closes_processed.append(dc_utc.isoformat())
            result.snapshot_ids[dc_utc.isoformat()] = already["snapshot_id"]
            result.fingerprints[dc_utc.isoformat()] = already["active_state_fingerprint"]
            continue

        attempt = 0
        last_err: Exception | None = None
        while attempt < max_attempts_per_close:
            attempt += 1
            faults = fault_schedule.get(idx) if attempt == 1 else None
            try:
                row = process_close_with_publish(
                    symbol=symbol,
                    timeframe=timeframe,
                    history_from=history_from,
                    decision_close=dc_utc,
                    pool_detail=pool_detail,
                    pipeline_line_id=pipeline_line_id,
                    test_run_id=test_run_id,
                    line=line,
                    faults=faults,
                    on_crash=lambda c, st: cleanup_before_retry(st) if st else None,
                )
                result.closes_processed.append(dc_utc.isoformat())
                result.snapshot_ids[dc_utc.isoformat()] = row["snapshot_id"]
                result.fingerprints[dc_utc.isoformat()] = row["active_state_fingerprint"]
                if row.get("timing"):
                    result.timings.append(row["timing"])
                break
            except SimulatedCrash as crash:
                last_err = crash
                if attempt >= max_attempts_per_close:
                    result.errors.append(f"{dc_utc.isoformat()}: {crash}")
                    result.stopped_early = True
                    return result
                continue
            except Exception as exc:
                result.errors.append(f"{dc_utc.isoformat()}: {exc}")
                result.stopped_early = True
                return result

    return result


def compare_pipeline_results(
    full: RunnerResult,
    resume: RunnerResult,
    expected_closes: list[datetime],
) -> dict[str, Any]:
    exp_keys = sorted(c.astimezone(timezone.utc).isoformat() for c in expected_closes)
    full_keys = sorted(full.snapshot_ids.keys())
    resume_keys = sorted(resume.snapshot_ids.keys())
    missing_full = sorted(set(exp_keys) - set(full_keys))
    missing_resume = sorted(set(exp_keys) - set(resume_keys))
    extra_full = sorted(set(full_keys) - set(exp_keys))
    extra_resume = sorted(set(resume_keys) - set(exp_keys))

    fp_mismatch = []
    sid_mismatch = []
    for k in exp_keys:
        if k not in full.snapshot_ids or k not in resume.snapshot_ids:
            continue
        if full.snapshot_ids[k] != resume.snapshot_ids[k]:
            sid_mismatch.append(k)
        if full.fingerprints[k] != resume.fingerprints[k]:
            fp_mismatch.append(k)

    dup_full = len(full_keys) != len(set(full_keys))
    dup_resume = len(resume_keys) != len(set(resume_keys))

    return {
        "missing_full": missing_full,
        "missing_resume": missing_resume,
        "extra_full": extra_full,
        "extra_resume": extra_resume,
        "snapshot_id_mismatch": sid_mismatch,
        "fingerprint_mismatch": fp_mismatch,
        "duplicate_logical_full": dup_full,
        "duplicate_logical_resume": dup_resume,
        "pass": (
            not missing_full
            and not missing_resume
            and not extra_full
            and not extra_resume
            and not sid_mismatch
            and not fp_mismatch
            and not dup_full
            and not dup_resume
        ),
    }


def prefix_oracle_check(
    *,
    symbol: str,
    timeframe: str,
    history_from: datetime,
    pool_detail: int,
    pipeline_line_id: str,
    config_hash_value: str,
    oracle_closes: list[datetime],
) -> dict[str, Any]:
    reader = PublishedActiveReader()
    reports = []
    all_ok = True
    for dc in oracle_closes:
        dc_utc = dc.astimezone(timezone.utc)
        pub = reader.load_published(pipeline_line_id, dc_utc, config_hash=config_hash_value)
        fresh, _, _ = build_active_state_at_decision_close(
            symbol, timeframe, history_from, dc_utc, pool_detail=pool_detail
        )
        cmp = compare_active_sets(
            fresh, pub["pool_ids"], pub["sides"]
        )
        reports.append(
            {
                "decision_close": dc_utc.isoformat(),
                "pass": cmp["pass"],
                "missing": cmp["missing"],
                "extra": cmp["extra"],
            }
        )
        all_ok = all_ok and cmp["pass"]
    return {"pass": all_ok, "reports": reports}
