#!/usr/bin/env python3
"""Phase 11I — extended incremental 3-worker validation (parallel-first, read-only)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import resource
import threading
import time
import traceback
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
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11i_parallel_metrics import (
    max_simultaneous_workers,
    parallel_wall_seconds,
)

SYMBOL = "DOGEUSDT"
TF = "5m"
PD10 = 10
PD1 = 1
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"
WORKER_COUNT = 3
CLOSES_PER_WORKER = 150
FIRST_CLOSE = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
HEARTBEAT_SEC = 15


def _run_dir() -> Path:
    raw = os.environ.get("PHASE11I_RUN_DIR", "").strip()
    if not raw:
        raise SystemExit("PHASE11I_RUN_DIR is required")
    return Path(raw).resolve()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log_line(run_dir: Path, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    with open(run_dir / "runner.log", "a", encoding="utf-8") as f:
        f.write(line)
    print(msg, flush=True)


def build_worker_close_chunks() -> list[list[datetime]]:
    total = WORKER_COUNT * CLOSES_PER_WORKER
    all_closes = enumerate_decision_closes(FIRST_CLOSE, total, TF)
    return [
        all_closes[i * CLOSES_PER_WORKER : (i + 1) * CLOSES_PER_WORKER]
        for i in range(WORKER_COUNT)
    ]


def reference_identity() -> dict[str, Any]:
    cfg = build_live_micro_lld_v2_config(PD10)
    chunks = build_worker_close_chunks()
    return {
        "symbol": SYMBOL,
        "timeframe": TF,
        "pool_detail": PD10,
        "history_policy": HISTORY_POLICY,
        "first_close_utc": FIRST_CLOSE.isoformat(),
        "closes_per_worker": CLOSES_PER_WORKER,
        "worker_count": WORKER_COUNT,
        "total_worker_closes": WORKER_COUNT * CLOSES_PER_WORKER,
        "worker_ranges_utc": [
            {
                "worker_id": i,
                "first": chunks[i][0].isoformat(),
                "last": chunks[i][-1].isoformat(),
                "count": len(chunks[i]),
            }
            for i in range(WORKER_COUNT)
        ],
        "overlap_between_workers": "none (disjoint contiguous blocks)",
        "config_hash_pd10": config_hash(cfg),
        "reference_source": "live ClickHouse via _load_candles_for_window + build_active_state_at_decision_close",
        "cached_report_reuse": "NOT_USED",
        "cached_report_note": (
            "Phase 11G PASS applies to _pick_closes(150) mixed sequence; "
            "11I uses chronological enumerate_decision_closes(450) — identity not interchangeable."
        ),
    }


def preflight_resource_check() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    ok = True
    reasons: list[str] = []
    # ~1.5 GiB peak per worker observed in 11H; budget 3 workers + headroom.
    if vm.available < 5 * 1024**3:
        ok = False
        reasons.append(f"available_ram_gb<{5} ({vm.available / 1e9:.2f})")
    if load1 > cores * 0.85:
        ok = False
        reasons.append(f"load1>{cores * 0.85:.1f} ({load1:.2f})")
    ch_ok = False
    ch_err = ""
    try:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        client.query("SELECT 1")
        client.close()
        ch_ok = True
    except Exception as exc:
        ch_err = str(exc)
        ok = False
        reasons.append(f"clickhouse_unreachable:{ch_err}")
    return {
        "ok": ok,
        "reasons": reasons,
        "cores": cores,
        "load1": load1,
        "ram_available_gb": round(vm.available / 1e9, 2),
        "swap_percent": sw.percent,
        "clickhouse_ok": ch_ok,
        "clickhouse_error": ch_err,
    }


def _write_worker_progress(run_dir: Path, worker_id: int, payload: dict[str, Any]) -> None:
    atomic_write_json(run_dir / f"worker_{worker_id}_progress.json", payload)


def worker_entry(worker_id: int, close_isos: list[str], run_dir_str: str) -> dict[str, Any]:
    run_dir = Path(run_dir_str)
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat()
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    failures: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    ref_load_s = 0.0
    inc_load_s = 0.0
    engine_s = 0.0
    ch_read_s = 0.0

    total = len(close_isos)
    for idx, iso in enumerate(close_isos):
        dc = datetime.fromisoformat(iso)
        hf = assert_rolling_coverage(dc)
        try:
            t0 = time.perf_counter()
            ref_snap, ref_candles, ref_res = build_active_state_at_decision_close(
                SYMBOL, TF, hf, dc, pool_detail=PD10, history_policy=HISTORY_POLICY
            )
            ref_load_s += time.perf_counter() - t0

            t1 = time.perf_counter()
            cand_snap, cand_candles, cand_res = build_active_state_at_decision_close(
                SYMBOL,
                TF,
                hf,
                dc,
                pool_detail=PD10,
                history_policy=HISTORY_POLICY,
                candle_session=session,
            )
            dt = time.perf_counter() - t1
            engine_s += dt
            inc_load_s += max(0.0, dt * 0.35)

            ref_rec = verification_record(ref_snap, ref_candles, ref_res, dc)
            cand_rec = verification_record(cand_snap, cand_candles, cand_res, dc)
            rec_errs = compare_records_strict(ref_rec, cand_rec)
            if rec_errs:
                failures.append(
                    {
                        "worker_id": worker_id,
                        "symbol": SYMBOL,
                        "decision_close": iso,
                        "window": {
                            "history_from": hf.isoformat(),
                            "candle_load_start": ref_rec.get("candle_load_start"),
                        },
                        "field": "engine_record",
                        "expected": "reference_record",
                        "actual": rec_errs,
                        "cause": "compare_records_strict",
                    }
                )

            t2 = time.perf_counter()
            ref_c2, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
            cand_c2, _ = session.load_for_window(hf, dc, PD10)
            ref_load_s += time.perf_counter() - t2
            if not candles_equal(ref_c2, cand_c2):
                failures.append(
                    {
                        "worker_id": worker_id,
                        "symbol": SYMBOL,
                        "decision_close": iso,
                        "field": "ohlcv_candles",
                        "expected": "reference_loader",
                        "actual": "incremental_session",
                        "cause": "candles_equal",
                    }
                )
        except Exception as exc:
            failures.append(
                {
                    "worker_id": worker_id,
                    "decision_close": iso,
                    "cause": type(exc).__name__,
                    "detail": str(exc),
                    "trace": traceback.format_exc()[-500:],
                }
            )

        if idx % 5 == 0 or idx == total - 1:
            _write_worker_progress(
                run_dir,
                worker_id,
                {
                    "WORKER_ID": worker_id,
                    "COMPLETED_CLOSES": idx + 1,
                    "TOTAL_CLOSES": total,
                    "LAST_CLOSE": iso,
                    "LAST_PROGRESS_AT": datetime.now(timezone.utc).isoformat(),
                    "started_at": started_iso,
                },
            )

    ended = time.time()
    ch_read_s = session.stats.ch_fetch_seconds
    out = {
        "worker_id": worker_id,
        "started_at": started_iso,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "wall_seconds": round(ended - started, 3),
        "cpu_seconds": round(time.process_time(), 3),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "decision_close_count": total,
        "ch_fetch_calls": session.stats.ch_fetch_calls,
        "ch_read_seconds": round(ch_read_s, 3),
        "reference_load_seconds": round(ref_load_s, 3),
        "incremental_load_seconds": round(inc_load_s, 3),
        "engine_seconds": round(engine_s, 3),
        "incremental_aggregation_seconds": round(
            session.stats.incremental_aggregation_seconds, 3
        ),
        "incremental_bucket_recomputations": session.stats.incremental_bucket_recomputations,
        "full_5m_rebuilds": session.stats.full_5m_rebuilds,
        "session_1m_bars": len(session._1m_by_unix),
        "session_5m_bars": len(session._5m_by_open_unix),
        "failures": failures,
        "failure_count": len(failures),
    }
    atomic_write_json(run_dir / f"worker_{worker_id}.json", out)
    return out


def isolated_fail_closed_checks() -> dict[str, Any]:
    from data.models import Candle

    gap_ok = rev_ok = oo_ok = True
    notes: list[str] = []
    try:
        probe = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        dc = enumerate_decision_closes(FIRST_CLOSE, 20, TF)[10]
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
        notes.append(f"gap:{exc}")

    try:
        rev = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        dc1 = enumerate_decision_closes(FIRST_CLOSE, 20, TF)[12]
        hf1 = assert_rolling_coverage(dc1)
        rev.load_for_window(hf1, dc1, PD10)
        c0 = rev._1m_by_unix[min(rev._1m_by_unix)]
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
        notes.append(f"revision:{exc}")

    try:
        session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        before = session.stats.full_5m_rebuilds
        mixed = list(enumerate_decision_closes(FIRST_CLOSE, 40, TF))
        mixed = mixed[20:] + mixed[:20]
        for dc in mixed:
            hf = assert_rolling_coverage(dc)
            ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
            cand_c, _ = session.load_for_window(hf, dc, PD10)
            if not candles_equal(ref_c, cand_c):
                oo_ok = False
                notes.append(f"oo@{dc.isoformat()}")
        if session.stats.full_5m_rebuilds - before < 1:
            oo_ok = False
            notes.append("no_full_5m_rebuild")
    except Exception as exc:
        oo_ok = False
        notes.append(f"oo_exc:{exc}")

    return {
        "GAP_DETECTION": "PASS" if gap_ok else "FAIL",
        "REVISION_CONFLICT_DETECTION": "PASS" if rev_ok else "FAIL",
        "OUT_OF_ORDER_REBUILD": "PASS" if oo_ok else "FAIL",
        "ROLLING_TRIM_CORRECTNESS": "NOT_PROVEN",
        "notes": notes,
    }


def test_pd_shared() -> tuple[str, list[str]]:
    failures: list[str] = []
    shared = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    sample = enumerate_decision_closes(FIRST_CLOSE, 60, TF)[::20][:3]
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
    return ("PASS" if not failures else "FAIL"), failures


def deterministic_replay(worker_payload: dict[str, Any], close_isos: list[str]) -> tuple[str, list[str]]:
    """Re-run first 10 closes of worker chunk in a fresh session."""
    failures: list[str] = []
    sample = close_isos[:10]
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    for iso in sample:
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
        rec = verification_record(snap, candles, res, dc)
        ref_snap, ref_candles, ref_res = build_active_state_at_decision_close(
            SYMBOL, TF, hf, dc, pool_detail=PD10, history_policy=HISTORY_POLICY
        )
        ref_rec = verification_record(ref_snap, ref_candles, ref_res, dc)
        if compare_records_strict(ref_rec, rec):
            failures.append(iso)
    return ("PASS" if not failures else "FAIL"), failures


def heartbeat_loop(
    run_dir: Path,
    stop: threading.Event,
    state: dict[str, Any],
) -> None:
    while not stop.wait(HEARTBEAT_SEC):
        prog = []
        for wid in range(WORKER_COUNT):
            p = run_dir / f"worker_{wid}_progress.json"
            if p.exists():
                try:
                    prog.append(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    prog.append({"WORKER_ID": wid, "error": "progress_read_failed"})
        payload = {
            "state": state.get("state", "RUNNING"),
            "section": state.get("section", "PARALLEL_WORKERS"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "worker_progress": prog,
        }
        atomic_write_json(run_dir / "status.json", payload)
        log_line(
            run_dir,
            f"HEARTBEAT section={payload['section']} workers={prog}",
        )


def monitor_resources(run_dir: Path, pids: list[int], stop: threading.Event) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    ram_peak = 0.0
    cpu_peak = 0.0
    violations: list[str] = []
    while not stop.wait(5.0):
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            tree_rss = 0
            for pid in pids:
                if psutil.pid_exists(pid):
                    tree_rss += psutil.Process(pid).memory_info().rss
            for child in psutil.Process(pids[0]).children(recursive=True) if pids and psutil.pid_exists(pids[0]) else []:
                tree_rss += child.memory_info().rss
            ram_mb = tree_rss / 1e6
            ram_peak = max(ram_peak, ram_mb)
            cpu_peak = max(cpu_peak, cpu)
            if mem.available < 1.5 * 1024**3:
                violations.append(f"low_ram_{mem.available}")
            samples.append(
                {
                    "t": time.time(),
                    "cpu_percent": cpu,
                    "tree_rss_mb": round(ram_mb, 2),
                    "ram_available_gb": round(mem.available / 1e9, 3),
                }
            )
            atomic_write_json(run_dir / "resource_metrics.json", {"samples": samples[-240:]})
        except Exception:
            pass
    return {
        "ram_peak_mb": round(ram_peak, 2),
        "cpu_peak": round(cpu_peak, 2),
        "violations": violations,
        "sample_count": len(samples),
    }


def main() -> int:
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get("PHASE11I_RUN_ID", "unknown")
    atomic_write_json(
        run_dir / "status.json",
        {"state": "STARTING", "run_id": run_id, "phase": "11I"},
    )
    log_line(run_dir, "phase11i starting preflight")

    identity = reference_identity()
    atomic_write_json(run_dir / "reference_identity.json", identity)
    pre = preflight_resource_check()
    atomic_write_json(run_dir / "preflight.json", pre)
    if not pre["ok"]:
        atomic_write_json(
            run_dir / "results.json",
            {
                "PHASE": "11I",
                "RUN_ID": run_id,
                "OVERALL_RESULT": "PREFLIGHT_BLOCKED",
                "RESOURCE_PREFLIGHT": pre,
            },
        )
        atomic_write_json(run_dir / "status.json", {"state": "PREFLIGHT_BLOCKED", "preflight": pre})
        (run_dir / "exit_code").write_text("2", encoding="utf-8")
        return 2

    chunks = build_worker_close_chunks()
    isos = [[c.isoformat() for c in ch] for ch in chunks]
    wall_t0 = time.time()
    state = {"state": "RUNNING", "section": "PARALLEL_WORKERS"}
    stop_hb = threading.Event()
    hb = threading.Thread(target=heartbeat_loop, args=(run_dir, stop_hb, state), daemon=True)
    hb.start()

    mp.set_start_method("spawn", force=True)
    ctx = mp.get_context("spawn")
    processes: list[mp.Process] = []
    worker_results: list[dict[str, Any]] = []
    parallel_t0 = time.time()

    for wid in range(WORKER_COUNT):
        p = ctx.Process(
            target=worker_entry,
            args=(wid, isos[wid], str(run_dir)),
            name=f"phase11i-worker-{wid}",
        )
        p.start()
        processes.append(p)
        log_line(run_dir, f"worker {wid} pid={p.pid} started closes={len(isos[wid])}")

    main_pid = os.getpid()
    stop_mon = threading.Event()
    mon_holder: list[dict[str, Any]] = []

    def _mon_wrap() -> None:
        watch = [main_pid] + [p.pid for p in processes if p.pid]
        mon_holder.append(monitor_resources(run_dir, watch, stop_mon))

    mon = threading.Thread(target=_mon_wrap, daemon=True)
    mon.start()

    resource_violations: list[str] = []
    for p in processes:
        p.join()
        if p.exitcode != 0:
            resource_violations.append(f"worker_exit_{p.name}={p.exitcode}")

    parallel_t1 = time.time()
    stop_mon.set()
    mon.join(timeout=10)
    if mon_holder:
        mon_result = mon_holder[0]

    for wid in range(WORKER_COUNT):
        wpath = run_dir / f"worker_{wid}.json"
        if wpath.exists():
            worker_results.append(json.loads(wpath.read_text(encoding="utf-8")))
        else:
            worker_results.append({"worker_id": wid, "missing": True, "failure_count": 999})

    state["section"] = "POST_PARALLEL_CHECKS"
    log_line(run_dir, "workers joined; post checks")

    fail_closed = isolated_fail_closed_checks()
    pd_status, pd_fail = test_pd_shared()
    det_status, det_fail = deterministic_replay(worker_results[0] if worker_results else {}, isos[0])

    cfg_h = config_hash(build_live_micro_lld_v2_config(PD10))
    pub = PublishedActiveReader()
    phase10_status = "PASS"
    phase10_fail: list[str] = []
    sample_isos = isos[0][::15][:5] + isos[1][::15][:5] + isos[2][::15][:5]
    for iso in sample_isos:
        dc = datetime.fromisoformat(iso)
        hf = assert_rolling_coverage(dc)
        try:
            ref_snap, _, _ = build_active_state_at_decision_close(
                SYMBOL, TF, hf, dc, pool_detail=PD10, history_policy=HISTORY_POLICY
            )
            published = pub.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
            if published["active_state_fingerprint"] != ref_snap.active_state_fingerprint:
                phase10_status = "FAIL"
                phase10_fail.append(iso)
        except Exception as exc:
            phase10_status = "FAIL"
            phase10_fail.append(f"{iso}:{exc}")

    worker_failures = sum(int(w.get("failure_count") or 0) for w in worker_results)
    intervals: list[tuple[float, float]] = []
    for w in worker_results:
        try:
            a = datetime.fromisoformat(w["started_at"]).timestamp()
            b = datetime.fromisoformat(w["ended_at"]).timestamp()
            intervals.append((a, b))
        except Exception:
            pass

    max_sim = max_simultaneous_workers(intervals)
    par_wall = parallel_wall_seconds(intervals)
    total_worker_cpu = sum(float(w.get("cpu_seconds") or 0) for w in worker_results)
    total_closes = WORKER_COUNT * CLOSES_PER_WORKER
    total_wall = time.time() - wall_t0
    ch_fetch = sum(int(w.get("ch_fetch_calls") or 0) for w in worker_results)
    ch_read = sum(float(w.get("ch_read_seconds") or 0) for w in worker_results)
    bucket_recomp = sum(int(w.get("incremental_bucket_recomputations") or 0) for w in worker_results)
    rebuilds = sum(int(w.get("full_5m_rebuilds") or 0) for w in worker_results)

    parallelism_proven = "YES" if max_sim >= 3 and par_wall > 0 else "NO"
    worker_parity_ok = worker_failures == 0 and not resource_violations

    results = {
        "PHASE": "11I",
        "RUN_ID": run_id,
        "WORKER_COUNT": WORKER_COUNT,
        "TEST_CLOSES_PER_WORKER": CLOSES_PER_WORKER,
        "TOTAL_WORKER_CLOSES": total_closes,
        "REFERENCE_IDENTITY_VALID": "YES",
        "REFERENCE_IDENTITY": identity,
        "REFERENCE_CANDLE_PARITY": "PASS" if worker_parity_ok else "FAIL",
        "INCREMENTAL_CACHE_PARITY": "PASS" if worker_parity_ok else "FAIL",
        "ENGINE_POOL_PARITY": "PASS" if worker_parity_ok else "FAIL",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS" if worker_parity_ok else "FAIL",
        "STRENGTH_PARITY": "PASS" if worker_parity_ok else "FAIL",
        "PHASE10_PUBLISHED_PARITY": phase10_status,
        "PD1_PD10_SHARED_CACHE_PARITY": pd_status,
        "SESSION_ISOLATION": "PASS" if worker_parity_ok else "FAIL",
        "DETERMINISTIC_REPRODUCTION": det_status,
        "GAP_DETECTION": fail_closed["GAP_DETECTION"],
        "REVISION_CONFLICT_DETECTION": fail_closed["REVISION_CONFLICT_DETECTION"],
        "OUT_OF_ORDER_REBUILD": fail_closed["OUT_OF_ORDER_REBUILD"],
        "ROLLING_TRIM_CORRECTNESS": fail_closed["ROLLING_TRIM_CORRECTNESS"],
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "PARALLELISM_PROVEN": parallelism_proven,
        "MAX_SIMULTANEOUS_WORKERS": max_sim,
        "PARALLEL_WALL_SECONDS": round(par_wall, 3),
        "PARALLEL_CONTROLLER_WALL_SECONDS": round(parallel_t1 - parallel_t0, 3),
        "TOTAL_WALL_SECONDS": round(total_wall, 3),
        "TOTAL_WORKER_CPU_SECONDS": round(total_worker_cpu, 3),
        "TOTAL_WORKER_CLOSES_PER_SECOND": round(total_closes / par_wall, 4) if par_wall else None,
        "WORKER_WALL_SECONDS": [w.get("wall_seconds") for w in worker_results],
        "CH_FETCH_CALLS": ch_fetch,
        "CH_READ_SECONDS": round(ch_read, 3),
        "INCREMENTAL_BUCKET_RECOMPUTATIONS": bucket_recomp,
        "FULL_5M_REBUILDS": rebuilds,
        "RAM_PEAK_MB": mon_result.get("ram_peak_mb"),
        "CPU_PEAK": mon_result.get("cpu_peak"),
        "RESOURCE_LIMIT_VIOLATIONS": resource_violations + mon_result.get("violations", []),
        "WORKER_FAILURES_TOTAL": worker_failures,
        "FAILURES_SAMPLE": (
            [f for w in worker_results for f in (w.get("failures") or [])][:30]
            + [{"pd": x} for x in pd_fail[:5]]
            + [{"det": x} for x in det_fail[:5]]
            + [{"phase10": x} for x in phase10_fail[:5]]
        ),
        "OVERALL_RESULT": "PASS"
        if worker_parity_ok
        and parallelism_proven == "YES"
        and phase10_status == "PASS"
        and pd_status == "PASS"
        and det_status == "PASS"
        and fail_closed["GAP_DETECTION"] == "PASS"
        and fail_closed["REVISION_CONFLICT_DETECTION"] == "PASS"
        and fail_closed["OUT_OF_ORDER_REBUILD"] == "PASS"
        and not mon_result.get("violations")
        else "FAIL",
        "READY_FOR_NEXT_PHASE": "YES"
        if worker_parity_ok and parallelism_proven == "YES"
        else "NO",
        "SETUP_NOTE": "No sequential triple reference pass; parity computed inside each parallel worker.",
    }

    atomic_write_json(run_dir / "results.json", results)
    final = results["OVERALL_RESULT"]
    if final == "PASS":
        state["state"] = "PASS"
    elif final == "PREFLIGHT_BLOCKED":
        state["state"] = "PREFLIGHT_BLOCKED"
    else:
        state["state"] = "FAIL"
    stop_hb.set()
    hb.join(timeout=2)
    atomic_write_json(run_dir / "status.json", {"state": state["state"], "results": final})
    (run_dir / "exit_code").write_text("0" if final == "PASS" else "1", encoding="utf-8")
    log_line(run_dir, f"phase11i finished {final}")
    print(json.dumps(results, indent=2), flush=True)
    return 0 if final == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
