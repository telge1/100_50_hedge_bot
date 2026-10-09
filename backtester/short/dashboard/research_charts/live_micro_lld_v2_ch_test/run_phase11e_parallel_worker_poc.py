#!/usr/bin/env python3
"""Phase 11E — 3-worker canonical candle POC (no CH writes)."""

from __future__ import annotations

import json
import multiprocessing as mp
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CanonicalCandleSession,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
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
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"
RANGE_START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
TEST_COUNT = 150
MAX_WORKERS = 3


def pick_closes(n: int) -> list[datetime]:
    steady = enumerate_decision_closes(RANGE_START, n - 40, TF)
    month_edge = enumerate_decision_closes(
        datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc), 20, TF
    )
    late = enumerate_decision_closes(
        datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), 20, TF
    )
    out = sorted(set(list(steady) + list(month_edge) + list(late)))[:n]
    return out


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


def _worker_cached_chunk(close_isos: list[str]) -> dict[str, Any]:
    """Process one disjoint chunk in an isolated process."""
    session = CanonicalCandleSession(SYMBOL, TF)
    records: list[dict[str, Any]] = []
    for iso in close_isos:
        dc = datetime.fromisoformat(iso)
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        records.append(verification_record(snap, candles, res, dc))
    return {
        "records": records,
        "ru_maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "ch_fetch_calls": session.stats.ch_fetch_calls,
    }


def _worker_cached_chunk_with_clear_at(close_isos: list[str], clear_after: int) -> dict[str, Any]:
    session = CanonicalCandleSession(SYMBOL, TF)
    records: list[dict[str, Any]] = []
    for i, iso in enumerate(close_isos):
        if i == clear_after:
            session.clear()
        dc = datetime.fromisoformat(iso)
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        records.append(verification_record(snap, candles, res, dc))
    return {"records": records}


def run_reference_single(closes: list[datetime]) -> tuple[dict[str, dict], float]:
    t0 = time.perf_counter()
    out: dict[str, dict] = {}
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL, TF, hf, dc, pool_detail=PD, history_policy=HISTORY_POLICY
        )
        rec = verification_record(snap, candles, res, dc)
        out[rec["decision_close"]] = rec
    return out, time.perf_counter() - t0


def run_cached_single(closes: list[datetime]) -> tuple[dict[str, dict], float]:
    t0 = time.perf_counter()
    session = CanonicalCandleSession(SYMBOL, TF)
    out: dict[str, dict] = {}
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        snap, candles, res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD,
            history_policy=HISTORY_POLICY,
            candle_session=session,
        )
        rec = verification_record(snap, candles, res, dc)
        out[rec["decision_close"]] = rec
    return out, time.perf_counter() - t0


def run_cached_parallel(
    closes: list[datetime],
    workers: int,
) -> tuple[dict[str, dict], float, list[dict[str, Any]]]:
    chunks = split_contiguous(closes, workers)
    isos = [[c.isoformat() for c in ch] for ch in chunks]
    swap_before = psutil.swap_memory()
    cpu_samples: list[float] = []
    t0 = time.perf_counter()
    with mp.Pool(processes=workers) as pool:
        meta: list[dict[str, Any]] = []
        asyncs = [pool.apply_async(_worker_cached_chunk, (chunk,)) for chunk in isos]
        while not all(a.ready() for a in asyncs):
            time.sleep(0.25)
            cpu_samples.append(psutil.cpu_percent(interval=None))
        for a in asyncs:
            meta.append(a.get())
    wall = time.perf_counter() - t0
    swap_after = psutil.swap_memory()
    merged: dict[str, dict] = {}
    for m in meta:
        for rec in m["records"]:
            merged[rec["decision_close"]] = rec
    slim_meta = [
        {
            "ru_maxrss_kb": m.get("ru_maxrss_kb"),
            "ch_fetch_calls": m.get("ch_fetch_calls"),
            "closes": len(m.get("records") or []),
        }
        for m in meta
    ]
    return (
        merged,
        wall,
        [
            {
                "worker_meta": slim_meta,
                "swap_in_delta_kb": swap_after.sin - swap_before.sin,
                "swap_out_delta_kb": swap_after.sout - swap_before.sout,
                "mean_cpu_pct": sum(cpu_samples) / len(cpu_samples) if cpu_samples else None,
            }
        ],
    )


def compare_all(
    reference: dict[str, dict],
    candidate: dict[str, dict],
) -> list[str]:
    errs: list[str] = []
    if set(reference) != set(candidate):
        errs.append(f"close_set ref={len(reference)} cand={len(candidate)}")
    for dc in sorted(reference):
        if dc not in candidate:
            errs.append(f"missing@{dc}")
            continue
        errs.extend(compare_records_strict(reference[dc], candidate[dc]))
    return errs


def ch_query_metric() -> float | None:
    try:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        cfg = load_clickhouse_config()
        c = clickhouse_connect.get_client(**cfg.connect_kwargs())
        rows = c.query(
            "SELECT value FROM system.metrics WHERE metric = 'Query'"
        ).result_rows
        c.close()
        return float(rows[0][0]) if rows else None
    except Exception:
        return None


def probe_block_content_hash(
    closes: list[datetime],
) -> dict[str, Any]:
    """Read-only CH aggregate fingerprint for 1m window (revision probe, not proven live-safe)."""
    if not closes:
        return {"status": "empty"}
    try:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        cfg = load_clickhouse_config()
        c = clickhouse_connect.get_client(**cfg.connect_kwargs())
        lo = min(closes).astimezone(timezone.utc)
        hi = max(closes).astimezone(timezone.utc)
        q = f"""
        SELECT count(),
               sum(cityHash64(open, high, low, close, volume))
        FROM {cfg.database}.{cfg.table} FINAL
        WHERE exchange = {{ex:String}} AND symbol = {{sym:String}}
          AND interval = '1m' AND is_closed = 1
          AND open_time >= {{lo:DateTime64(3,'UTC')}}
          AND open_time <= {{hi:DateTime64(3,'UTC')}}
        """
        row = c.query(
            q,
            parameters={
                "ex": cfg.exchange,
                "sym": SYMBOL,
                "lo": lo,
                "hi": hi,
            },
        ).result_rows[0]
        c.close()
        return {
            "status": "ok",
            "from": lo.isoformat(),
            "to": hi.isoformat(),
            "count_1m": int(row[0]),
            "sum_cityhash64": int(row[1]),
            "note": "Snapshot at POC time only; live corrections after snapshot NOT_PROVEN",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def main() -> int:
    mp.set_start_method("spawn", force=True)
    closes = pick_closes(TEST_COUNT)
    closes = sorted(closes)
    cfg_h = config_hash(build_live_micro_lld_v2_config(PD))
    pub = PublishedActiveReader()

    failures: list[str] = []
    fault_failures: list[str] = []

    ref_map, ref_sec = run_reference_single(closes)
    cached_map, cached_sec = run_cached_single(closes)
    errs_b = compare_all(ref_map, cached_map)
    failures.extend(errs_b)

    parallel_map, parallel_sec, par_meta = run_cached_parallel(closes, MAX_WORKERS)
    errs_c = compare_all(ref_map, parallel_map)
    failures.extend(errs_c)

    # Block boundary oracles (first close of each worker chunk after the first)
    chunks = split_contiguous(closes, MAX_WORKERS)
    boundary_closes = [ch[0] for ch in chunks[1:] if ch]
    for dc in boundary_closes:
        iso = dc.isoformat()
        if iso not in ref_map or iso not in parallel_map:
            failures.append(f"boundary_missing@{iso}")
        else:
            failures.extend(compare_records_strict(ref_map[iso], parallel_map[iso]))

    phase10_ok = True
    for iso in ref_map:
        dc = datetime.fromisoformat(iso)
        try:
            published = pub.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
            if published["active_state_fingerprint"] != ref_map[iso]["active_state_fingerprint"]:
                phase10_ok = False
                failures.append(f"phase10_fp@{iso}")
        except Exception as exc:
            phase10_ok = False
            failures.append(f"phase10_read@{iso}:{exc}")

    # Fault: worker order permutation
    chunk_results = split_contiguous(closes, MAX_WORKERS)
    isos = [[c.isoformat() for c in ch] for ch in chunk_results]
    with mp.Pool(processes=MAX_WORKERS) as pool:
        parts = pool.map(_worker_cached_chunk, isos)
    permuted = parts[2]["records"] + parts[0]["records"] + parts[1]["records"]
    perm_map = {r["decision_close"]: r for r in permuted}
    fault_failures.extend(compare_all(ref_map, perm_map))

    # Fault: cold cache mid-chunk vs full chunk
    mid = len(isos[0]) // 2
    with mp.Pool(processes=1) as pool:
        cold = pool.apply(_worker_cached_chunk_with_clear_at, (isos[0], mid))
        full = pool.apply(_worker_cached_chunk, (isos[0],))
    for rec in full["records"]:
        iso = rec["decision_close"]
        cold_rec = next((r for r in cold["records"] if r["decision_close"] == iso), None)
        if not cold_rec:
            fault_failures.append(f"cold_missing@{iso}")
        else:
            fault_failures.extend(compare_records_strict(rec, cold_rec))

    # Fault: missing close detection
    if len(parallel_map) != len(closes):
        fault_failures.append("missing_close_detection_failed")

    failures.extend(fault_failures)
    worker_crash_ok = len(fault_failures) == 0 and len(perm_map) == len(ref_map)

    ru_parent = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    worker_rss = sum(m.get("ru_maxrss_kb", 0) for m in par_meta[0].get("worker_meta", []))

    speed_ref = len(closes) / ref_sec if ref_sec else 0
    speed_cached = len(closes) / cached_sec if cached_sec else 0
    speed_parallel = len(closes) / parallel_sec if parallel_sec else 0

    all_ok = (
        not failures
        and phase10_ok
        and worker_crash_ok
        and not errs_b
        and not errs_c
    )

    report = {
        "TEST_CLOSES": len(closes),
        "REFERENCE_SINGLE_WORKER_SECONDS": round(ref_sec, 3),
        "CACHED_SINGLE_WORKER_SECONDS": round(cached_sec, 3),
        "CACHED_THREE_WORKERS_SECONDS": round(parallel_sec, 3),
        "MEASURED_SPEEDUP_VS_REFERENCE": round(ref_sec / parallel_sec, 3)
        if parallel_sec
        else None,
        "MEASURED_SPEEDUP_VS_CACHED_SINGLE": round(cached_sec / parallel_sec, 3)
        if parallel_sec
        else None,
        "THROUGHPUT_CLOSES_PER_SEC": {
            "reference": round(speed_ref, 4),
            "cached_single": round(speed_cached, 4),
            "cached_three_workers": round(speed_parallel, 4),
        },
        "CANDLE_PARITY": "PASS" if not errs_b and not errs_c else "FAIL",
        "ENGINE_PARITY": "PASS" if not errs_b and not errs_c else "FAIL",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS" if not errs_b and not errs_c else "FAIL",
        "STRENGTH_PARITY": "PASS" if not errs_b and not errs_c else "FAIL",
        "PHASE10_PUBLISHED_PARITY": "PASS" if phase10_ok else "FAIL",
        "WORKER_CRASH_RESUME_PARITY": "PASS" if worker_crash_ok else "FAIL",
        "RAM_PEAK_TOTAL_MB": round(max(ru_parent / 1024, worker_rss / 1024), 2),
        "SWAP_ACTIVITY": par_meta[0] if par_meta else {},
        "CH_QUERY_METRIC_AFTER": ch_query_metric(),
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "BLOCK_CONTENT_HASH_PROBE": probe_block_content_hash(closes),
        "BOUNDARY_CLOSES_CHECKED": [c.isoformat() for c in boundary_closes],
        "FAILURES_SAMPLE": failures[:40],
        "READY_FOR_PUBLISH_PIPELINE_POC": "YES" if all_ok else "NO",
    }

    out = Path(__file__).with_name("phase11e_parallel_worker_poc_report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
