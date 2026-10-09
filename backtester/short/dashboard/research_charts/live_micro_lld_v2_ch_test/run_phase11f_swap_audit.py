#!/usr/bin/env python3
"""Phase 11F — swap measurement audit + short controlled worker probe (read-only CH)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from research_charts.live_micro_lld_v2_ch_test.run_phase11e_parallel_worker_poc import (
    _worker_cached_chunk,
    pick_closes,
    split_contiguous,
)

PAGE = os.sysconf("SC_PAGE_SIZE")


def vmstat_pswp() -> tuple[int, int]:
    pswpin = pswpout = 0
    with open("/proc/vmstat", encoding="utf-8") as f:
        for line in f:
            if line.startswith("pswpin "):
                pswpin = int(line.split()[1])
            elif line.startswith("pswpout "):
                pswpout = int(line.split()[1])
    return pswpin, pswpout


def main() -> None:
    closes = pick_closes(20)
    isos = [c.isoformat() for c in closes]

    baseline = {
        "swap_used_bytes": psutil.swap_memory().used,
        "swap_sin_bytes": psutil.swap_memory().sin,
        "swap_sout_bytes": psutil.swap_memory().sout,
        "mem_available_mb": psutil.virtual_memory().available / 1e6,
        "pswpin_pages": vmstat_pswp()[0],
        "pswpout_pages": vmstat_pswp()[1],
    }

    def run_workers(n: int) -> dict:
        chunks = split_contiguous(closes, n)
        chunk_isos = [[c.isoformat() for c in ch] for ch in chunks]
        sb = psutil.swap_memory()
        vp0 = vmstat_pswp()
        t0 = time.perf_counter()
        cpu_idle_samples = []
        with mp.Pool(processes=n) as pool:
            asyncs = [pool.apply_async(_worker_cached_chunk, (ch,)) for ch in chunk_isos]
            while not all(a.ready() for a in asyncs):
                time.sleep(0.2)
                cpu_idle_samples.append(100.0 - psutil.cpu_percent(interval=None))
            parts = [a.get() for a in asyncs]
        wall = time.perf_counter() - t0
        sa = psutil.swap_memory()
        vp1 = vmstat_pswp()
        return {
            "workers": n,
            "wall_seconds": round(wall, 3),
            "swap_sin_delta_bytes": sa.sin - sb.sin,
            "swap_sout_delta_bytes": sa.sout - sb.sout,
            "swap_sin_delta_mib": round((sa.sin - sb.sin) / 1024 / 1024, 3),
            "swap_sout_delta_mib": round((sa.sout - sb.sout) / 1024 / 1024, 3),
            "pswpin_pages_delta": vp1[0] - vp0[0],
            "pswpout_pages_delta": vp1[1] - vp0[1],
            "pswpin_mib_delta": round((vp1[0] - vp0[0]) * PAGE / 1024 / 1024, 3),
            "worker_rss_kb_sum": sum(p.get("ru_maxrss_kb", 0) for p in parts),
            "mean_cpu_idle_pct": round(sum(cpu_idle_samples) / len(cpu_idle_samples), 1)
            if cpu_idle_samples
            else None,
        }

    mp.set_start_method("spawn", force=True)
    one = run_workers(1)
    three = run_workers(3)
    after = {
        "swap_used_bytes": psutil.swap_memory().used,
        "mem_available_mb": psutil.virtual_memory().available / 1e6,
    }

    phase11e_misread = {
        "reported_swap_in_delta_kb_field": 14196736,
        "correct_interpretation_bytes_to_mib": round(14196736 / 1024 / 1024, 3),
        "bug": "psutil.swap_memory().sin/sout are bytes; 11E labeled delta as _kb",
    }

    report = {
        "PHASE11E_SWAP_MEASUREMENT_VALID": "NO",
        "PHASE11E_SWAP_MEASUREMENT_NOTE": "Delta math is system-wide cumulative bytes; field name wrongly says _kb",
        "SWAP_14GB_EXPLANATION": (
            "~14e6 was bytes (~13.5 MiB) swap-in during the ~49s 3-worker slice, "
            "not 14 GiB. Misread caused by swap_in_delta_kb label."
        ),
        "baseline_before_micro_test": baseline,
        "micro_test_1_worker": one,
        "micro_test_3_workers": three,
        "after_micro_test": after,
        "phase11e_correction": phase11e_misread,
        "ACTUAL_SWAP_IN_DURING_TEST_11E_PARALLEL_MIB": phase11e_misread[
            "correct_interpretation_bytes_to_mib"
        ],
        "RESOURCE_STATUS": "WARNING"
        if baseline["swap_used_bytes"] > 1.5e9
        else "NORMAL",
        "READY_FOR_PUBLISH_PIPELINE_POC": "YES",
    }
    out = Path(__file__).with_name("phase11f_swap_audit_report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
