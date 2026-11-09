"""Multi-close runner with per-close rolling history_from_close (Phase 9)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..identity import build_live_micro_lld_v2_config, config_hash
from .cleanup import cleanup_before_retry
from .extract import ActiveStateSnapshot, build_active_state_at_decision_close, compare_active_sets
from .faults import FaultInjector, SimulatedCrash
from .pool_catalog import compare_catalog_to_engine, load_active_pool_catalog, write_active_pool_catalog
from .publish_guard import LineIdentity
from .published_reader import PublishedActiveReader
from .reader import ActiveSnapshotReader
from .publisher import publish_validated_snapshot
from .rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
    assert_rolling_coverage,
    rolling_history_from_close,
)
from .runner import CloseTiming, RunnerResult, compare_pipeline_results, enumerate_decision_closes
from .writer import snapshot_id_for_state

__all__ = [
    "RunnerResult",
    "CloseTiming",
    "compare_pipeline_results",
    "enumerate_decision_closes",
    "run_rolling_controlled_pipeline",
    "rolling_prefix_oracle_check",
    "analyze_rolling_window_transitions",
]


def build_rolling_line_identity(state: ActiveStateSnapshot) -> LineIdentity:
    cfg = build_live_micro_lld_v2_config(state.pool_detail)
    return LineIdentity(
        symbol=state.symbol.upper(),
        timeframe=state.timeframe,
        pool_detail=state.pool_detail,
        config_hash=config_hash(cfg),
        engine_version=state.engine_version,
        candle_load_start=state.candle_load_start,
        history_from_close=state.history_from_close,
        history_policy=state.history_policy,
    )


def process_rolling_close(
    *,
    symbol: str,
    timeframe: str,
    decision_close: datetime,
    pool_detail: int,
    pipeline_line_id: str,
    test_run_id: str,
    history_policy: str = HISTORY_POLICY_ROLLING_4M,
    rolling_months: int = 4,
    faults: FaultInjector | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    dc = decision_close.astimezone(timezone.utc)
    history_from = assert_rolling_coverage(dc, months=rolling_months)
    state, _candles, result = build_active_state_at_decision_close(
        symbol,
        timeframe,
        history_from,
        dc,
        pool_detail=pool_detail,
        history_policy=history_policy,
    )
    line = build_rolling_line_identity(state)
    t_eng = time.perf_counter() - t0
    t_pub = time.perf_counter()
    pub = publish_validated_snapshot(
        state,
        pipeline_line_id=pipeline_line_id,
        test_run_id=test_run_id,
        line=line,
        faults=faults,
    )
    pub_s = time.perf_counter() - t_pub
    sid = pub.get("snapshot_id") or snapshot_id_for_state(state)
    cat = write_active_pool_catalog(
        state, result.pools, snapshot_id=sid, pipeline_line_id=pipeline_line_id
    )
    total = time.perf_counter() - t0
    return {
        "decision_close": dc.isoformat(),
        "snapshot_id": sid,
        "publish_id": pub.get("publish_id"),
        "status": pub["status"],
        "catalog": cat,
        "history_from_close": history_from.isoformat(),
        "active_state_fingerprint": state.active_state_fingerprint,
        "timing": CloseTiming(
            decision_close=dc.isoformat(),
            load_seconds=t_eng,
            engine_seconds=t_eng,
            write_seconds=pub_s,
            validate_publish_seconds=pub_s,
            total_seconds=total,
        ),
        "state": state,
        "engine_result": result,
    }


def run_rolling_controlled_pipeline(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_closes: list[datetime],
    pipeline_line_id: str,
    fault_schedule: dict[int, FaultInjector] | None = None,
    max_attempts_per_close: int = 3,
) -> RunnerResult:
    fault_schedule = fault_schedule or {}
    cfg_h = config_hash(build_live_micro_lld_v2_config(pool_detail))
    test_run_id = str(uuid.uuid4())
    pub_reader = PublishedActiveReader()
    result = RunnerResult(pipeline_line_id=pipeline_line_id, test_run_id=test_run_id)

    for idx, dc in enumerate(decision_closes):
        dc_utc = dc.astimezone(timezone.utc)
        try:
            already = pub_reader.load_published(
                pipeline_line_id, dc_utc, config_hash=cfg_h
            )
        except Exception:
            already = None
        if already:
            result.closes_processed.append(dc_utc.isoformat())
            result.snapshot_ids[dc_utc.isoformat()] = already["snapshot_id"]
            result.fingerprints[dc_utc.isoformat()] = already["active_state_fingerprint"]
            continue

        attempt = 0
        while attempt < max_attempts_per_close:
            attempt += 1
            faults = fault_schedule.get(idx) if attempt == 1 else None
            try:
                row = process_rolling_close(
                    symbol=symbol,
                    timeframe=timeframe,
                    decision_close=dc_utc,
                    pool_detail=pool_detail,
                    pipeline_line_id=pipeline_line_id,
                    test_run_id=test_run_id,
                    faults=faults,
                )
                result.closes_processed.append(dc_utc.isoformat())
                result.snapshot_ids[dc_utc.isoformat()] = row["snapshot_id"]
                result.fingerprints[dc_utc.isoformat()] = row["active_state_fingerprint"]
                if row.get("timing"):
                    result.timings.append(row["timing"])
                break
            except SimulatedCrash:
                try:
                    hf = rolling_history_from_close(dc_utc, 4)
                    st, _, _ = build_active_state_at_decision_close(
                        symbol,
                        timeframe,
                        hf,
                        dc_utc,
                        pool_detail=pool_detail,
                        history_policy=HISTORY_POLICY_ROLLING_4M,
                    )
                    cleanup_before_retry(st)
                except Exception:
                    pass
                if attempt >= max_attempts_per_close:
                    result.errors.append(f"{dc_utc.isoformat()}: simulated crash exhausted")
                    result.stopped_early = True
                    return result
                continue
            except Exception as exc:
                result.errors.append(f"{dc_utc.isoformat()}: {exc}")
                result.stopped_early = True
                return result

    return result


def rolling_prefix_oracle_check(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    pipeline_line_id: str,
    oracle_closes: list[datetime],
) -> dict[str, Any]:
    cfg_h = config_hash(build_live_micro_lld_v2_config(pool_detail))
    reader = PublishedActiveReader()
    reports = []
    attr_reports = []
    all_ok = True
    for dc in oracle_closes:
        dc_utc = dc.astimezone(timezone.utc)
        pub = reader.load_published(pipeline_line_id, dc_utc, config_hash=cfg_h)
        hf = rolling_history_from_close(dc_utc, 4)
        fresh, _, result = build_active_state_at_decision_close(
            symbol,
            timeframe,
            hf,
            dc_utc,
            pool_detail=pool_detail,
            history_policy=HISTORY_POLICY_ROLLING_4M,
        )
        cmp = compare_active_sets(fresh, pub["pool_ids"], pub["sides"])
        catalog = load_active_pool_catalog(pub["snapshot_id"])
        attr = compare_catalog_to_engine(
            catalog, result.pools, set(fresh.active_pool_ids)
        )
        reports.append(
            {
                "decision_close": dc_utc.isoformat(),
                "pass": cmp["pass"],
                "missing": cmp["missing"],
                "extra": cmp["extra"],
            }
        )
        attr_reports.append({"decision_close": dc_utc.isoformat(), **attr})
        all_ok = all_ok and cmp["pass"] and attr["pass"]
    return {"pass": all_ok, "active_reports": reports, "attribute_reports": attr_reports}


def extended_oracle_check(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    pipeline_line_id: str,
    oracle_closes: list[datetime],
) -> dict[str, Any]:
    """Active IDs + bounds/strength via pool catalog fingerprints."""
    base = rolling_prefix_oracle_check(
        symbol=symbol,
        timeframe=timeframe,
        pool_detail=pool_detail,
        pipeline_line_id=pipeline_line_id,
        oracle_closes=oracle_closes,
    )
    cfg_h = config_hash(build_live_micro_lld_v2_config(pool_detail))
    reader = PublishedActiveReader()
    bounds_ok = True
    strength_ok = True
    future_pools = 0
    detail = []
    for dc in oracle_closes:
        dc_utc = dc.astimezone(timezone.utc)
        pub = reader.load_published(pipeline_line_id, dc_utc, config_hash=cfg_h)
        hf = rolling_history_from_close(dc_utc, 4)
        fresh, _, result = build_active_state_at_decision_close(
            symbol,
            timeframe,
            hf,
            dc_utc,
            pool_detail=pool_detail,
            history_policy=HISTORY_POLICY_ROLLING_4M,
        )
        by_id = {p.pool_id: p for p in result.pools if p.pool_id in fresh.active_pool_ids}
        catalog = load_active_pool_catalog(pub["snapshot_id"])
        for row in catalog:
            p = by_id.get(row["pool_id"])
            if not p:
                continue
            if p.known_at.astimezone(timezone.utc) > dc_utc:
                future_pools += 1
            if abs(float(p.top_price) - float(row["top_price"])) > 1e-9:
                bounds_ok = False
            if abs(float(p.strength) - float(row["strength"])) > 1e-9:
                strength_ok = False
        detail.append({"decision_close": dc_utc.isoformat(), "catalog_rows": len(catalog)})
    return {
        **base,
        "bounds_parity": bounds_ok,
        "strength_parity": strength_ok,
        "future_pool_count": future_pools,
        "detail": detail,
        "pass": base["pass"] and bounds_ok and strength_ok and future_pools == 0,
    }


def audit_catalog_completeness(
    pipeline_line_id: str,
    *,
    config_hash_value: str,
    sample_limit: int = 500,
) -> dict[str, Any]:
    reader = PublishedActiveReader()
    snap_reader = ActiveSnapshotReader()
    rows = reader.list_published_summary(pipeline_line_id, config_hash=config_hash_value)
    incomplete = []
    for row in rows[:sample_limit]:
        sid = row["snapshot_id"]
        meta = snap_reader.load_meta(sid)
        cat = load_active_pool_catalog(sid)
        if len(cat) != meta["active_pool_count"]:
            incomplete.append(sid)
    return {
        "published_count": len(rows),
        "sampled": min(len(rows), sample_limit),
        "incomplete_catalog": incomplete,
        "complete": len(incomplete) == 0,
    }


def analyze_rolling_window_transitions(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_closes: list[datetime],
) -> dict[str, Any]:
    """Compare adjacent rolling runs; detect changes only from window shift via fixed-start control."""
    transitions = []
    for i in range(1, len(decision_closes)):
        t_prev, t_curr = decision_closes[i - 1], decision_closes[i]
        hf_prev = rolling_history_from_close(t_prev, 4)
        hf_curr = rolling_history_from_close(t_curr, 4)
        snap_prev, _, _ = build_active_state_at_decision_close(
            symbol, timeframe, hf_prev, t_prev, pool_detail=pool_detail,
            history_policy=HISTORY_POLICY_ROLLING_4M,
        )
        snap_curr, _, _ = build_active_state_at_decision_close(
            symbol, timeframe, hf_curr, t_curr, pool_detail=pool_detail,
            history_policy=HISTORY_POLICY_ROLLING_4M,
        )
        ids_prev = set(snap_prev.active_pool_ids)
        ids_curr = set(snap_curr.active_pool_ids)
        # fixed-start control: hold history_from at hf_prev, advance end to t_curr
        snap_fixed, _, _ = build_active_state_at_decision_close(
            symbol, timeframe, hf_prev, t_curr, pool_detail=pool_detail,
            history_policy="FIXED_START_CONTROL",
        )
        ids_fixed = set(snap_fixed.active_pool_ids)
        roll_only = (ids_curr - ids_prev) - (ids_fixed - ids_prev)
        transitions.append(
            {
                "from": t_prev.isoformat(),
                "to": t_curr.isoformat(),
                "added": len(ids_curr - ids_prev),
                "removed": len(ids_prev - ids_curr),
                "history_from_shifted": hf_prev.isoformat() != hf_curr.isoformat(),
                "rolling_window_only_ids_sample": sorted(roll_only)[:10],
                "rolling_window_only_count": len(roll_only),
            }
        )
    return {"transitions": transitions}
