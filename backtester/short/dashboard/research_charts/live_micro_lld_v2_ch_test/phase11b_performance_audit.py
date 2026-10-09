"""
Phase 11B — read-only performance & parallel scaling audit.
No ClickHouse inserts; engine path uses original run_live_micro_lld_v2.
"""

from __future__ import annotations

import cProfile
import io
import json
import multiprocessing as mp
import pstats
import resource
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
    extract_active_from_result,
    run_engine_prefix,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    candle_sequence_fingerprint,
    config_hash,
    engine_input_document,
    engine_input_hash,
)
from research_charts.live_micro_lld_v2_ch_test.serialize import pool_fingerprint, pool_to_ch_row

SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
POOL_DETAIL = 10
# Mid Phase-10 window (stable rolling 4m coverage)
FIRST_CLOSE = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
PROFILE_CLOSES = 24
PARALLEL_CLOSES_PER_WORKER = 10
PARALLEL_WORKERS = (1, 2, 3, 4)


def _iso_closes(count: int, offset: int = 0) -> list[datetime]:
    all_c = enumerate_decision_closes(FIRST_CLOSE, count + offset, TIMEFRAME)
    return all_c[offset : offset + count]


def _close_work(decision_close: datetime) -> dict[str, Any]:
    """Single close: load → engine → snapshot/fingerprint prep (no CH)."""
    dc = decision_close.astimezone(timezone.utc)
    hf = rolling_history_from_close(dc, 4)
    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)

    t0 = time.perf_counter()
    candles, meta = _load_candles_for_window(SYMBOL, TIMEFRAME, hf, dc, POOL_DETAIL)
    t_load = time.perf_counter() - t0

    t1 = time.perf_counter()
    result = run_engine_prefix(candles, cfg)
    t_engine = time.perf_counter() - t1

    t2 = time.perf_counter()
    active_ids, active_sides, known = extract_active_from_result(result, decision_close=dc)
    prefix_fp = candle_sequence_fingerprint(candles)
    input_doc = engine_input_document(
        cfg=cfg,
        symbol=meta["symbol"],
        timeframe=meta["timeframe"],
        from_close=meta["from_close"],
        to_close=dc,
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
        history_policy="ROLLING_4M",
    )
    _ = engine_input_hash(input_doc)
    _ = active_state_fingerprint(active_ids, active_sides)
    active_set = set(active_ids)
    catalog_prep = 0
    for p in result.pools:
        if p.pool_id not in active_set:
            continue
        ch = pool_to_ch_row(p, run_id="bench")
        _ = "|".join(str(x) for x in pool_fingerprint(p))
        catalog_prep += 1
    t_extract = time.perf_counter() - t2

    packed = meta.get("packed") or {}
    timings_ms = (packed.get("timings_ms") or {}) if isinstance(packed, dict) else {}

    return {
        "decision_close": dc.isoformat(),
        "history_from_close": hf.isoformat(),
        "prefix_candle_count": len(candles),
        "active_pools": len(active_ids),
        "catalog_rows_prepared": catalog_prep,
        "load_seconds": t_load,
        "engine_seconds": t_engine,
        "extract_seconds": t_extract,
        "total_seconds": t_load + t_engine + t_extract,
        "candle_timings_ms": timings_ms,
        "candle_cache": packed.get("cache") if isinstance(packed, dict) else None,
    }


def _worker_run(closes_iso: list[str]) -> dict[str, Any]:
    rows = []
    for s in closes_iso:
        dc = datetime.fromisoformat(s)
        rows.append(_close_work(dc))
    return {
        "closes": len(rows),
        "total_seconds": sum(r["total_seconds"] for r in rows),
        "rows": rows,
    }


def profile_closes(n: int) -> dict[str, Any]:
    closes = _iso_closes(n)
    rows = [_close_work(dc) for dc in closes]
    totals = [r["total_seconds"] for r in rows]
    loads = [r["load_seconds"] for r in rows]
    engines = [r["engine_seconds"] for r in rows]
    extracts = [r["extract_seconds"] for r in rows]

    # Adjacent overlap: history_from shift + candle count delta
    transitions = []
    for i in range(1, len(rows)):
        transitions.append(
            {
                "history_from_shifted": rows[i]["history_from_close"]
                != rows[i - 1]["history_from_close"],
                "prefix_count_delta": rows[i]["prefix_candle_count"]
                - rows[i - 1]["prefix_candle_count"],
                "cache": rows[i].get("candle_cache"),
            }
        )

    # Cached-prefix simulation (variant B feasibility, not engine change)
    dc_last = closes[-1]
    hf_last = rolling_history_from_close(dc_last, 4)
    t0 = time.perf_counter()
    candles_big, meta_big = _load_candles_for_window(
        SYMBOL, TIMEFRAME, hf_last, dc_last, POOL_DETAIL
    )
    big_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    _ = run_engine_prefix(candles_big, build_live_micro_lld_v2_config(POOL_DETAIL))
    big_engine = time.perf_counter() - t1

    # cProfile on 3 closes
    pr = cProfile.Profile()
    pr.enable()
    for dc in closes[:3]:
        _close_work(dc)
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumtime")
    ps.print_stats(25)
    profile_text = s.getvalue()

    sum_total = sum(totals)
    return {
        "close_count": n,
        "per_close": rows,
        "transitions": transitions,
        "aggregates": {
            "total_seconds_sum": sum_total,
            "mean_total": statistics.mean(totals),
            "mean_load": statistics.mean(loads),
            "mean_engine": statistics.mean(engines),
            "mean_extract": statistics.mean(extracts),
            "p95_total": sorted(totals)[int(0.95 * len(totals)) - 1],
            "load_pct_of_total": 100 * sum(loads) / sum_total,
            "engine_pct_of_total": 100 * sum(engines) / sum_total,
            "extract_pct_of_total": 100 * sum(extracts) / sum_total,
            "mean_prefix_candles": statistics.mean(r["prefix_candle_count"] for r in rows),
        },
        "reload_analysis": {
            "each_close_full_window_reload": True,
            "note": "_load_candles_for_window called per close with rolling history_from",
            "mean_cache_field": statistics.mode(
                [r.get("candle_cache") or "miss" for r in rows]
            )
            if rows
            else None,
            "history_from_shifts_in_sample": sum(
                1 for t in transitions if t["history_from_shifted"]
            ),
        },
        "variant_b_upper_bound_one_big_run": {
            "last_close": dc_last.isoformat(),
            "big_load_seconds": big_load,
            "big_engine_seconds": big_engine,
            "mean_per_close_total_in_sample": statistics.mean(totals),
            "theoretical_amortized_if_only_end_mattered": (big_load + big_engine) / n,
        },
        "cprofile_top": profile_text,
    }


def parallel_benchmark() -> dict[str, Any]:
    base_closes = _iso_closes(PARALLEL_CLOSES_PER_WORKER)
    iso_base = [c.isoformat() for c in base_closes]
    proc = psutil.Process()
    rss_start = proc.memory_info().rss
    swap_before = psutil.swap_memory()
    results = {}

    for nw in PARALLEL_WORKERS:
        # Same workload per worker (synthetic scale test — identical DOGE closes)
        work = [iso_base for _ in range(nw)]
        t0 = time.perf_counter()
        cpu_samples = []

        with mp.Pool(processes=nw) as pool:
            asyncs = [pool.apply_async(_worker_run, (chunk,)) for chunk in work]
            while not all(a.ready() for a in asyncs):
                time.sleep(0.2)
                cpu_samples.append(psutil.cpu_percent(interval=None))
            out = [a.get() for a in asyncs]
        wall = time.perf_counter() - t0
        rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        swap_after = psutil.swap_memory()
        total_closes = nw * PARALLEL_CLOSES_PER_WORKER
        throughput = total_closes / wall
        per_worker = [o["total_seconds"] for o in out]
        results[str(nw)] = {
            "workers": nw,
            "closes_per_worker": PARALLEL_CLOSES_PER_WORKER,
            "total_closes": total_closes,
            "wall_seconds": round(wall, 3),
            "throughput_closes_per_sec": round(throughput, 4),
            "throughput_per_worker_closes_per_sec": round(throughput / nw, 4),
            "worker_cpu_seconds_sum": round(sum(per_worker), 3),
            "efficiency_vs_1_worker": None,
            "mean_cpu_pct_during": round(statistics.mean(cpu_samples), 1) if cpu_samples else None,
            "rss_max_kb_self": rss_peak,
            "swap_in_delta_kb": swap_after.sin - swap_before.sin,
            "swap_out_delta_kb": swap_after.sout - swap_before.sout,
        }
        if nw == 1:
            baseline_throughput = throughput
        else:
            results[str(nw)]["efficiency_vs_1_worker"] = round(
                throughput / baseline_throughput, 3
            )

    return {
        "baseline_rss_bytes": rss_start,
        "swap_before": {"sin": swap_before.sin, "sout": swap_before.sout},
        "worker_runs": results,
        "synthetic_scale_test": True,
        "note": "Each worker repeats the same 10 DOGE closes (not multi-coin).",
    }


def ch_readonly_metrics() -> dict[str, Any]:
    try:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        cfg = load_clickhouse_config()
        c = clickhouse_connect.get_client(**cfg.connect_kwargs())
        metrics = c.query(
            "SELECT metric, value FROM system.metrics WHERE metric IN "
            "('Query', 'Merge', 'BackgroundPoolTask', 'MemoryTracking')"
        ).result_rows
        c.close()
        return {"system_metrics": {m: float(v) for m, v in metrics}}
    except Exception as exc:
        return {"error": str(exc)}


def main() -> None:
    mp.set_start_method("spawn", force=True)
    system = {
        "cpu_count": psutil.cpu_count(logical=True),
        "loadavg_1m": psutil.getloadavg()[0],
        "mem_available_mb": round(psutil.virtual_memory().available / 1e6, 1),
        "swap_used_mb": round(psutil.swap_memory().used / 1e6, 1),
    }
    phase10_ref = {
        "total_seconds": 4035.41,
        "closes": 2016,
        "avg_per_close": 1.9773,
    }

    print("Phase 11B: profiling closes...", flush=True)
    prof = profile_closes(PROFILE_CLOSES)

    print("Phase 11B: parallel benchmark...", flush=True)
    par = parallel_benchmark()

    ag = prof["aggregates"]
    # Phase 10 included CH publish+catalog; estimate CH as residual vs measured in-memory path
    bench_mean = ag["mean_total"]
    ch_est_pct = max(
        0.0,
        100 * (phase10_ref["avg_per_close"] - bench_mean) / phase10_ref["avg_per_close"],
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_baseline": system,
        "phase10_reference": phase10_ref,
        "profile": prof,
        "parallel": par,
        "clickhouse_during_benchmark": ch_readonly_metrics(),
        "architecture_notes": {
            "A": "Full prefix engine per close (current)",
            "B": "Same engine; shared candle buffer / incremental slice from cached 1m",
            "C": "Incremental engine state — not in original API; feasibility only",
        },
        "estimates": {},
    }

    # Scaling from measured throughput (use 1-worker parallel bench as conservative)
    w1 = par["worker_runs"]["1"]["throughput_closes_per_sec"]
    closes_3m = 25920  # 90d * 288
    for workers in (1, 2, 3, 4):
        tp = par["worker_runs"][str(workers)]["throughput_closes_per_sec"]
        report["estimates"][f"doge_5m_pd10_3m_hours_{workers}w"] = round(
            closes_3m / tp / 3600, 2
        )
    report["estimates"]["doge_5m_pd10_3m_hours_phase10_linear"] = round(
        closes_3m * phase10_ref["avg_per_close"] / 3600, 2
    )

    report["summary"] = {
        "CURRENT_BOTTLENECK": "run_live_micro_lld_v2 on ~35k 5m bars per close",
        "ENGINE_TIME_PERCENT": round(ag["engine_pct_of_total"], 1),
        "CANDLE_LOADING_PERCENT": round(ag["load_pct_of_total"], 1),
        "EXTRACT_FINGERPRINT_PERCENT": round(ag["extract_pct_of_total"], 1),
        "CH_WRITE_PERCENT_ESTIMATED": round(ch_est_pct, 1),
        "WORKERS_1_THROUGHPUT": par["worker_runs"]["1"]["throughput_closes_per_sec"],
        "WORKERS_2_THROUGHPUT": par["worker_runs"]["2"]["throughput_closes_per_sec"],
        "WORKERS_3_THROUGHPUT": par["worker_runs"]["3"]["throughput_closes_per_sec"],
        "WORKERS_4_THROUGHPUT": par["worker_runs"]["4"]["throughput_closes_per_sec"],
        "RECOMMENDED_CONCURRENT_WORKERS": None,
        "BEST_OPTIMIZATION_OPPORTUNITY": "B — candle load + 1m→5m aggregation reuse across adjacent closes",
        "POTENTIAL_SPEEDUP": None,
    }

    w4_eff = par["worker_runs"]["4"]["efficiency_vs_1_worker"]
    w3_eff = par["worker_runs"]["3"]["efficiency_vs_1_worker"]
    if w3_eff and w3_eff >= 2.5 and system["mem_available_mb"] > 8000:
        rec = 3
    elif w4_eff and w4_eff >= 3.0:
        rec = 4
    else:
        rec = min(3, psutil.cpu_count(logical=True) - 1)
    report["summary"]["RECOMMENDED_CONCURRENT_WORKERS"] = rec

    load_eng = (ag["load_pct_of_total"] + ag["engine_pct_of_total"]) / 100
    report["summary"]["POTENTIAL_SPEEDUP"] = (
        f"parallel ~{par['worker_runs'][str(rec)]['efficiency_vs_1_worker']}x; "
        f"candle cache (B) up to ~{round(100*ag['load_pct_of_total']/load_eng, 2)} of load+engine if 1m reuse"
    )

    h3_1w = report["estimates"]["doge_5m_pd10_3m_hours_1w"]
    h3_rec = report["estimates"][f"doge_5m_pd10_3m_hours_{rec}w"]
    report["summary"]["ESTIMATED_10_COINS_3_MONTHS"] = (
        f"~{round(h3_rec * 10 / 24, 1)} days wall @ {rec} workers (10× DOGE synthetic, 5m PD10 only)"
    )
    report["summary"]["RECOMMENDATION"] = (
        "Run monthly backfill with 2–3 parallel pipeline workers per coin; "
        "prioritize candle/aggregation caching (variant B) before touching engine."
    )

    out = Path(__file__).with_name("phase11b_performance_audit.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
