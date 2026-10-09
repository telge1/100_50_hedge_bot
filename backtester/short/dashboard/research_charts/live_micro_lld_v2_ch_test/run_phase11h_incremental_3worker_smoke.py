#!/usr/bin/env python3
"""Phase 11H — incremental CanonicalCandleSession, 3-worker smoke (read-only, no CH writes)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CandleDataGapError,
    CandleRevisionConflictError,
    CanonicalCandleSession,
    candles_equal,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    assemble_active_state_from_candles,
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
    compare_records_strict,
    verification_record,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.run_phase11d_canonical_candle_poc import (
    _pick_closes,
)

SYMBOL = "DOGEUSDT"
TF = "5m"
PD10 = 10
PD1 = 1
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"
WORKER_COUNT = 3
# Smoke scope: fixed reproducible window (subset of Phase 11G sequence).
TEST_COUNT = 50


def _run_dir() -> Path:
    raw = os.environ.get("PHASE11H_RUN_DIR", "").strip()
    if not raw:
        raise SystemExit("PHASE11H_RUN_DIR is required")
    return Path(raw).resolve()


def _write_status(run_dir: Path, state: str, **extra: Any) -> None:
    payload = {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    (run_dir / "status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def pick_closes_smoke(n: int) -> list[datetime]:
    """Sorted contiguous closes for worker split (same universe as 11G, truncated)."""
    all_closes = sorted(set(_pick_closes(max(n, TEST_COUNT))))[:n]
    return all_closes


def split_contiguous(closes: list[datetime], workers: int) -> list[list[datetime]]:
    workers = max(1, min(workers, len(closes)))
    n = len(closes)
    base, rem = divmod(n, workers)
    chunks: list[list[datetime]] = []
    start = 0
    for w in range(workers):
        size = base + (1 if w < rem else 0)
        chunks.append(closes[start : start + size])
        start += size
    return chunks


def preflight_resource_check() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    ok = True
    reasons: list[str] = []
    if vm.available < 2 * 1024**3:
        ok = False
        reasons.append(f"available_ram_gb<{2} ({vm.available / 1e9:.2f})")
    if load1 > cores * 0.95:
        ok = False
        reasons.append(f"load1>{cores * 0.95:.1f} ({load1:.2f})")
    if sw.percent > 97 and vm.available < 4 * 1024**3:
        ok = False
        reasons.append(f"swap_critical percent={sw.percent}")
    return {
        "ok": ok,
        "reasons": reasons,
        "cores": cores,
        "load1": load1,
        "ram_total_gb": round(vm.total / 1e9, 2),
        "ram_available_gb": round(vm.available / 1e9, 2),
        "swap_percent": sw.percent,
    }


def _worker_incremental_chunk(close_isos: list[str]) -> dict[str, Any]:
    t0 = time.perf_counter()
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    records: list[dict[str, Any]] = []
    for iso in close_isos:
        dc = datetime.fromisoformat(iso)
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD10,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        records.append(verification_record(snap, candles, res, dc))
    wall = time.perf_counter() - t0
    return {
        "records": records,
        "wall_seconds": wall,
        "ru_maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "ch_fetch_calls": session.stats.ch_fetch_calls,
        "incremental_bucket_recomputations": session.stats.incremental_bucket_recomputations,
        "full_5m_rebuilds": session.stats.full_5m_rebuilds,
        "full_aggregation_seconds": session.stats.full_aggregation_seconds,
        "incremental_aggregation_seconds": session.stats.incremental_aggregation_seconds,
    }


def build_reference_map(closes: list[datetime]) -> tuple[dict[str, dict], float]:
    t0 = time.perf_counter()
    out: dict[str, dict] = {}
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL, TF, hf, dc, pool_detail=PD10, history_policy=HISTORY_POLICY
        )
        rec = verification_record(snap, candles, res, dc)
        out[rec["decision_close"]] = rec
    return out, time.perf_counter() - t0


def build_full_cache_map(closes: list[datetime]) -> tuple[dict[str, dict], float, CanonicalCandleSession]:
    t0 = time.perf_counter()
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=False)
    out: dict[str, dict] = {}
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD10,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        rec = verification_record(snap, candles, res, dc)
        out[rec["decision_close"]] = rec
    return out, time.perf_counter() - t0, session


def build_incremental_single_map(
    closes: list[datetime],
) -> tuple[dict[str, dict], float, CanonicalCandleSession]:
    t0 = time.perf_counter()
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    out: dict[str, dict] = {}
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD10,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        rec = verification_record(snap, candles, res, dc)
        out[rec["decision_close"]] = rec
    return out, time.perf_counter() - t0, session


def compare_maps(reference: dict[str, dict], candidate: dict[str, dict]) -> list[str]:
    errs: list[str] = []
    if set(reference) != set(candidate):
        errs.append(f"close_set ref={len(reference)} cand={len(candidate)}")
    for dc in sorted(reference):
        if dc not in candidate:
            errs.append(f"missing@{dc}")
            continue
        errs.extend(compare_records_strict(reference[dc], candidate[dc]))
    return errs


def reference_candle_parity_fast(closes: list[datetime], session: CanonicalCandleSession) -> list[str]:
    errs: list[str] = []
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
        cand_c, _ = session.load_for_window(hf, dc, PD10)
        if not candles_equal(ref_c, cand_c):
            errs.append(f"candles@{dc.isoformat()}")
    return errs


def test_gap_and_revision() -> tuple[bool, bool, list[str]]:
    failures: list[str] = []
    gap_ok = rev_ok = True
    try:
        probe = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        closes = pick_closes_smoke(12)
        dc = closes[5]
        hf = assert_rolling_coverage(dc)
        probe.load_for_window(hf, dc, PD10)
        cfg = build_live_micro_lld_v2_config(PD10)
        warmup = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
        load_start = max(0, int(hf.timestamp()) - warmup * 300)
        src_lo = load_start - probe._need_1m_per_5m * 60
        probe.check_1m_gap_free(src_lo, int(dc.timestamp()))
        u0 = min(probe._1m_by_unix)
        probe._1m_by_unix.pop(u0, None)
        try:
            probe.check_1m_gap_free(src_lo, int(dc.timestamp()))
            gap_ok = False
        except CandleDataGapError:
            pass
    except Exception as exc:
        gap_ok = False
        failures.append(f"gap:{exc}")

    try:
        from data.models import Candle

        rev = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        dc1 = pick_closes_smoke(12)[6]
        hf1 = assert_rolling_coverage(dc1)
        rev.load_for_window(hf1, dc1, PD10)
        u_pick = min(rev._1m_by_unix)
        c0 = rev._1m_by_unix[u_pick]
        o = float(c0.open) * 1.01
        c1 = Candle(
            timestamp=c0.timestamp,
            open=o,
            high=max(float(c0.high), o) + 0.001,
            low=min(float(c0.low), o) - 0.001,
            close=float(c0.close),
            volume=float(c0.volume),
            symbol=c0.symbol,
            timeframe=c0.timeframe,
        )
        try:
            rev.ingest_1m_closed([c1])
            rev_ok = False
        except CandleRevisionConflictError:
            pass
    except Exception as exc:
        rev_ok = False
        failures.append(f"revision:{exc}")
    return gap_ok, rev_ok, failures


def test_out_of_order_rebuild() -> tuple[bool, list[str]]:
    failures: list[str] = []
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    closes = _pick_closes(min(30, TEST_COUNT))
    rebuilds_before = session.stats.full_5m_rebuilds
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
        cand_c, _ = session.load_for_window(hf, dc, PD10)
        if not candles_equal(ref_c, cand_c):
            failures.append(f"oo_order@{dc.isoformat()}")
    rebuilds = session.stats.full_5m_rebuilds - rebuilds_before
    ok = not failures and rebuilds >= 1
    if rebuilds < 1:
        failures.append("expected_full_5m_rebuild_on_out_of_order")
    return ok, failures


def test_pd_shared_cache() -> tuple[bool, list[str]]:
    failures: list[str] = []
    shared = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    sample = pick_closes_smoke(TEST_COUNT)[::11][:3]
    for pd in (PD1, PD10):
        cfg_pd = build_live_micro_lld_v2_config(pd)
        for dc in sample:
            hf = assert_rolling_coverage(dc)
            ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, pd)
            cand_c, cand_m = shared.load_for_window(hf, dc, pd)
            if not candles_equal(ref_c, cand_c):
                failures.append(f"pd{pd}@{dc.isoformat()}")
            ref_snap, _, _ = assemble_active_state_from_candles(
                ref_c,
                {**cand_m, "config": cfg_pd, "pool_detail": pd},
                history_from_close=hf,
                decision_close=dc,
                pool_detail=pd,
                cfg=cfg_pd,
                history_policy=HISTORY_POLICY,
                timeframe=TF,
            )
            cand_snap, _, _ = assemble_active_state_from_candles(
                cand_c,
                cand_m,
                history_from_close=hf,
                decision_close=dc,
                pool_detail=pd,
                cfg=cfg_pd,
                history_policy=HISTORY_POLICY,
                timeframe=TF,
            )
            if ref_snap.engine_input_hash != cand_snap.engine_input_hash:
                failures.append(f"pd{pd}_eih@{dc.isoformat()}")
    return not failures, failures


def monitor_pool(
    pool: mp.pool.Pool,
    asyncs: list[Any],
    metrics_path: Path,
    stop: list[bool],
) -> None:
    samples: list[dict[str, Any]] = []
    while not stop[0]:
        if all(a.ready() for a in asyncs):
            break
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        samples.append(
            {
                "t": time.time(),
                "cpu_percent": cpu,
                "ram_used_gb": round(mem.used / 1e9, 3),
            }
        )
        _write_json(metrics_path, {"samples": samples[-120:]})
        time.sleep(1.0)
    _write_json(metrics_path, {"samples": samples})


def main() -> int:
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_status(run_dir, "STARTING", phase="11H")

    pre = preflight_resource_check()
    _write_json(run_dir / "preflight.json", pre)
    if not pre["ok"]:
        _write_status(run_dir, "ABORTED", reason="preflight_failed", preflight=pre)
        (run_dir / "exit_code").write_text("2", encoding="utf-8")
        return 2

    mp.set_start_method("spawn", force=True)
    closes = pick_closes_smoke(TEST_COUNT)
    chunks = split_contiguous(closes, WORKER_COUNT)
    closes_per_worker = [len(c) for c in chunks]
    cfg_h = config_hash(build_live_micro_lld_v2_config(PD10))
    pub = PublishedActiveReader()

    failures: list[str] = []
    wall_t0 = time.perf_counter()
    cpu_peak = 0.0
    ram_peak_mb = 0.0

    _write_status(
        run_dir,
        "RUNNING",
        worker_count=WORKER_COUNT,
        test_closes=len(closes),
        closes_per_worker=closes_per_worker,
    )

    ref_map, ref_sec = build_reference_map(closes)
    full_map, full_sec, full_session = build_full_cache_map(closes)
    inc_single_map, inc_single_sec, inc_single_session = build_incremental_single_map(closes)

    errs_ref_full = compare_maps(ref_map, full_map)
    errs_ref_inc_single = compare_maps(ref_map, inc_single_map)
    failures.extend(errs_ref_full)
    failures.extend(errs_ref_inc_single)

    candle_errs_full = reference_candle_parity_fast(closes[::5], full_session)
    candle_errs_inc = reference_candle_parity_fast(closes[::5], inc_single_session)
    failures.extend(candle_errs_full)
    failures.extend(candle_errs_inc)

    isos = [[c.isoformat() for c in ch] for ch in chunks]
    worker_seconds: list[float] = []
    ch_fetch_total = full_session.stats.ch_fetch_calls + inc_single_session.stats.ch_fetch_calls
    bucket_recomp = inc_single_session.stats.incremental_bucket_recomputations
    full_rebuilds = inc_single_session.stats.full_5m_rebuilds

    par_meta: list[dict[str, Any]] = []
    parallel_map: dict[str, dict] = {}
    parallel_wall = 0.0

    with mp.Pool(processes=WORKER_COUNT) as pool:
        asyncs = [pool.apply_async(_worker_incremental_chunk, (chunk,)) for chunk in isos]
        stop_flag = [False]
        metrics_path = run_dir / "resource_metrics.json"
        import threading

        mon = threading.Thread(target=monitor_pool, args=(pool, asyncs, metrics_path, stop_flag))
        mon.start()
        par_t0 = time.perf_counter()
        while not all(a.ready() for a in asyncs):
            cpu_peak = max(cpu_peak, psutil.cpu_percent(interval=None))
            ram_peak_mb = max(ram_peak_mb, psutil.virtual_memory().used / 1e6)
            time.sleep(0.25)
        for a in asyncs:
            par_meta.append(a.get())
        parallel_wall = time.perf_counter() - par_t0
        stop_flag[0] = True
        mon.join(timeout=5)

    for m in par_meta:
        worker_seconds.append(round(m["wall_seconds"], 3))
        ch_fetch_total += int(m.get("ch_fetch_calls") or 0)
        bucket_recomp += int(m.get("incremental_bucket_recomputations") or 0)
        full_rebuilds += int(m.get("full_5m_rebuilds") or 0)
        for rec in m["records"]:
            parallel_map[rec["decision_close"]] = rec

    errs_parallel = compare_maps(ref_map, parallel_map)
    errs_single_vs_parallel = compare_maps(inc_single_map, parallel_map)
    failures.extend(errs_parallel)
    failures.extend(errs_single_vs_parallel)

    chunks_boundary = [ch[0] for ch in chunks[1:] if ch]
    for dc in chunks_boundary:
        iso = dc.isoformat()
        if iso in ref_map and iso in parallel_map:
            failures.extend(compare_records_strict(ref_map[iso], parallel_map[iso]))
        else:
            failures.append(f"boundary_missing@{iso}")

    phase10_ok = True
    for iso in list(ref_map.keys())[::7]:
        dc = datetime.fromisoformat(iso)
        try:
            published = pub.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
            if published["active_state_fingerprint"] != ref_map[iso]["active_state_fingerprint"]:
                phase10_ok = False
                failures.append(f"phase10_fp@{iso}")
        except Exception as exc:
            phase10_ok = False
            failures.append(f"phase10_read@{iso}:{exc}")

    gap_ok, rev_ok, aux_fail = test_gap_and_revision()
    failures.extend(aux_fail)
    oo_ok, oo_fail = test_out_of_order_rebuild()
    failures.extend(oo_fail)
    pd_ok, pd_fail = test_pd_shared_cache()
    failures.extend(pd_fail)

    session_isolation_ok = not errs_single_vs_parallel and not errs_parallel

    wall_total = time.perf_counter() - wall_t0
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    ram_peak_mb = max(ram_peak_mb, ru * 1024 / 1e6)
    for m in par_meta:
        ram_peak_mb = max(ram_peak_mb, (m.get("ru_maxrss_kb") or 0) / 1024)

    parity_ok = (
        not errs_ref_full
        and not errs_ref_inc_single
        and not errs_parallel
        and not candle_errs_full
        and not candle_errs_inc
        and phase10_ok
        and gap_ok
        and rev_ok
        and oo_ok
        and pd_ok
        and session_isolation_ok
        and not failures
    )

    results = {
        "PHASE": "11H",
        "RUN_ID": os.environ.get("PHASE11H_RUN_ID", ""),
        "WORKER_COUNT": WORKER_COUNT,
        "TEST_CLOSES_TOTAL": len(closes),
        "TEST_CLOSES_PER_WORKER": closes_per_worker,
        "CLOSE_SEQUENCE": "sorted subset of Phase11G _pick_closes",
        "REFERENCE_CANDLE_PARITY": "PASS" if not candle_errs_full and not candle_errs_inc else "FAIL",
        "FULL_CACHE_PARITY": "PASS" if not errs_ref_full else "FAIL",
        "INCREMENTAL_CACHE_PARITY": "PASS"
        if not errs_ref_inc_single and not errs_parallel
        else "FAIL",
        "ENGINE_POOL_PARITY": "PASS"
        if not errs_ref_full and not errs_parallel
        else "FAIL",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS"
        if not errs_ref_full and not errs_parallel
        else "FAIL",
        "STRENGTH_PARITY": "PASS"
        if not errs_ref_full and not errs_parallel
        else "FAIL",
        "PHASE10_PUBLISHED_PARITY": "PASS" if phase10_ok else "FAIL",
        "PD1_PD10_SHARED_CACHE_PARITY": "PASS" if pd_ok else "FAIL",
        "GAP_DETECTION": "PASS" if gap_ok else "FAIL",
        "REVISION_CONFLICT_DETECTION": "PASS" if rev_ok else "FAIL",
        "OUT_OF_ORDER_REBUILD": "PASS" if oo_ok else "FAIL",
        "SESSION_ISOLATION": "PASS" if session_isolation_ok else "FAIL",
        "TOTAL_WALL_SECONDS": round(wall_total, 3),
        "WORKER_SECONDS": worker_seconds,
        "PARALLEL_WALL_SECONDS": round(parallel_wall, 3),
        "REFERENCE_BUILD_SECONDS": round(ref_sec, 3),
        "FULL_CACHE_BUILD_SECONDS": round(full_sec, 3),
        "INCREMENTAL_SINGLE_SECONDS": round(inc_single_sec, 3),
        "CH_FETCH_CALLS": ch_fetch_total,
        "INCREMENTAL_BUCKET_RECOMPUTATIONS": bucket_recomp,
        "FULL_5M_REBUILDS": full_rebuilds,
        "RAM_PEAK_MB": round(ram_peak_mb, 2),
        "CPU_PEAK": round(cpu_peak, 2),
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "READY_FOR_NEXT_PHASE": "YES" if parity_ok else "NO",
        "FAILURES_SAMPLE": failures[:40],
    }

    _write_json(run_dir / "results.json", results)
    final_state = "PASS" if parity_ok else "FAIL"
    _write_status(run_dir, final_state, results_summary=final_state)
    (run_dir / "exit_code").write_text("0" if parity_ok else "1", encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0 if parity_ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        run_dir = Path(os.environ.get("PHASE11H_RUN_DIR", "."))
        _write_status(run_dir, "ABORTED", reason="keyboard_interrupt")
        (run_dir / "exit_code").write_text("130", encoding="utf-8")
        raise
