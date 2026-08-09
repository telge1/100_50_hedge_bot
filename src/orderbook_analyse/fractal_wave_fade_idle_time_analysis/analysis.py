"""Orchestrate idle-time analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orderbook_analyse.fractal_wave_fade_idle_time_analysis import (
    AUDIT_VERSION,
    OUT_DIR_DEFAULT,
    REF_TRADES,
)
from orderbook_analyse.fractal_wave_fade_idle_time_analysis.compute import (
    basic_stats,
    bucket_stats,
    build_gaps,
    by_halfyear,
    by_month,
    capacity_assessment,
    cumulative_within,
    load_trades,
    longest_idle,
    time_budget,
)
from orderbook_analyse.fractal_wave_fade_idle_time_analysis.export import write_results


def run_analysis(
    *,
    trades_path: Path = REF_TRADES,
    out_dir: Path = OUT_DIR_DEFAULT,
) -> dict[str, Any]:
    trades = load_trades(trades_path)
    gaps = build_gaps(trades)
    stats = basic_stats(gaps)
    buckets = bucket_stats(gaps)
    cum = cumulative_within(gaps)
    longest = longest_idle(gaps, 25)
    half = by_halfyear(gaps, trades)
    monthly = by_month(gaps, trades)
    budget = time_budget(trades, gaps)
    capacity = capacity_assessment(budget, cum)

    payload = {
        "audit_version": AUDIT_VERSION,
        "trades_path": str(trades_path),
        "n_trades": int(len(trades)),
        "gaps": gaps,
        "stats": stats,
        "buckets": buckets,
        "cumulative": cum,
        "longest": longest,
        "halfyear": half,
        "monthly": monthly,
        "budget": budget,
        "capacity": capacity,
        "out_dir": out_dir,
    }
    write_results(payload, out_dir)
    return payload
