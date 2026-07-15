"""Read-only performance profiling + complexity benchmark for the replay path.

Uses only cProfile/pstats (stdlib). Runs deliberately SHORT windows to expose
the hotspot cheaply; it never launches the 1-week or 6-week runs.
"""

from __future__ import annotations

import cProfile
import io
import json
import pstats
import sys
import time
from pathlib import Path

import pandas as pd

from research.regime_scanner.research_runs.baseline_scanner import (
    _indicator_window,
    load_candle_slices,
)
from research.regime_scanner.research_runs.parameters import build_baseline_parameter_set
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.timeframes import ensure_utc_timestamp
from research.regime_scanner.trend_state_machine import run_trend_state_timeline

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results_research_performance"
CANONICAL_WARMUP = "2025-12-27T00:00:00Z"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _run_timeline(params, *, warmup_start: str, start: str, end: str) -> dict:
    slice_5m, _agg15, _agg30 = load_candle_slices(params, warmup_start=warmup_start, end_time=end)
    replay = _indicator_window(
        slice_5m, start_time=start, warmup_start=warmup_start, scanner_cfg=params.regime_scanner
    )
    ind = compute_indicator_frame(replay, config=params.regime_scanner)
    t0 = time.perf_counter()
    snaps, _rt, events = run_trend_state_timeline(
        ind,
        cfg=params.trend_state,
        scanner_cfg=params.regime_scanner,
        start_decision_time=start,
        end_decision_time=end,
    )
    dt = time.perf_counter() - t0
    return {"replay_bars": int(len(replay)), "snapshots": len(snaps), "events": len(events), "seconds": dt}


def profile_window(params, *, name: str, warmup_start: str, start: str, end: str) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"[profile] {name} {start}..{end}")
    pr = cProfile.Profile()
    pr.enable()
    info = _run_timeline(params, warmup_start=warmup_start, start=start, end=end)
    pr.disable()
    pstats_path = RESULTS_DIR / f"{name}.pstats"
    pr.dump_stats(str(pstats_path))
    buf = io.StringIO()
    st = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    st.print_stats(30)
    txt_path = RESULTS_DIR / f"{name}.txt"
    txt_path.write_text(buf.getvalue(), encoding="utf-8")
    # extract top internal-time consumers
    buf2 = io.StringIO()
    pstats.Stats(pr, stream=buf2).sort_stats("tottime").print_stats(12)
    top = buf2.getvalue()
    return {**info, "pstats": str(pstats_path), "txt": str(txt_path), "top_tottime": top}


def complexity_benchmark(params, *, start: str, day_lengths=(1, 2, 4)) -> list[dict]:
    rows: list[dict] = []
    start_ts = ensure_utc_timestamp(start)
    prev = None
    for d in day_lengths:
        end_ts = start_ts + pd.Timedelta(days=d)
        _log(f"[complexity] {d}d window")
        info = _run_timeline(
            params, warmup_start=CANONICAL_WARMUP, start=start, end=end_ts.isoformat()
        )
        ratio = None if prev is None else info["seconds"] / prev["seconds"]
        bar_ratio = None if prev is None else info["replay_bars"] / prev["replay_bars"]
        rows.append(
            {
                "days": d,
                "replay_bars": info["replay_bars"],
                "snapshots": info["snapshots"],
                "runtime_seconds": round(info["seconds"], 3),
                "runtime_per_bar_ms": round(info["seconds"] / max(info["replay_bars"], 1) * 1000, 4),
                "runtime_ratio_vs_prev": None if ratio is None else round(ratio, 3),
                "bar_ratio_vs_prev": None if bar_ratio is None else round(bar_ratio, 3),
            }
        )
        prev = info
    return rows


def main() -> int:
    params = build_baseline_parameter_set(data_source="mysql")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    week_profile = profile_window(
        params,
        name="baseline_week_profile",
        warmup_start=CANONICAL_WARMUP,
        start="2026-03-01T00:00:00Z",
        end="2026-03-03T00:00:00Z",
    )
    mixed_profile = profile_window(
        params,
        name="mixed_sample_profile",
        warmup_start=CANONICAL_WARMUP,
        start="2026-02-01T00:00:00Z",
        end="2026-02-03T00:00:00Z",
    )
    complexity = complexity_benchmark(params, start="2026-03-01T00:00:00Z", day_lengths=(1, 2, 4))

    summary = {
        "note": "Short sub-windows used to expose hotspot cheaply; 1-week/6-week runs not repeated.",
        "warmup_start": CANONICAL_WARMUP,
        "baseline_week_profile": {k: v for k, v in week_profile.items() if k != "top_tottime"},
        "mixed_sample_profile": {k: v for k, v in mixed_profile.items() if k != "top_tottime"},
        "complexity_benchmark": complexity,
        "top_tottime_baseline": week_profile["top_tottime"],
    }
    (RESULTS_DIR / "performance_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "top_tottime_baseline"}, indent=2, default=str))
    _log("\n[profile] TOP tottime (baseline sample):\n" + week_profile["top_tottime"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
