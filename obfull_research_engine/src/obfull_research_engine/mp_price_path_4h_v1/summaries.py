"""Summaries in percent (no bps columns)."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable


def _f(v: Any) -> float | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"p25": None, "p50": None, "p75": None, "p90": None, "mean": None}
    xs = sorted(vals)

    def q(p: float) -> float:
        pos = p * (len(xs) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return {
        "p25": q(0.25),
        "p50": q(0.5),
        "p75": q(0.75),
        "p90": q(0.90),
        "mean": sum(xs) / len(xs),
    }


def sample_flag(n: int) -> str:
    if n < 10:
        return "VERY_LOW_SAMPLE"
    if n < 30:
        return "LOW_SAMPLE"
    if n < 50:
        return "LIMITED_SAMPLE"
    return "ANALYZABLE_SAMPLE"


def summarize_horizons(
    horizon_rows: list[dict[str, Any]],
    *,
    group_name: str,
    event_ids: set[str],
    horizon_min: int,
) -> dict[str, Any]:
    rows = [
        r
        for r in horizon_rows
        if r["event_id"] in event_ids and int(r["horizon_min"]) == int(horizon_min)
    ]
    complete = [r for r in rows if r.get("is_complete")]
    censored = len(rows) - len(complete)
    mfes = [_f(r["mfe_pct"]) for r in complete]
    maes = [_f(r["mae_pct"]) for r in complete]
    closes = [_f(r["close_return_pct"]) for r in complete]
    mae_bf = [_f(r.get("mae_before_mfe_peak_exclusive_pct")) for r in complete]
    ttm = [_f(r.get("time_to_mfe_minutes")) for r in complete]
    mfes = [x for x in mfes if x is not None]
    maes = [x for x in maes if x is not None]
    closes = [x for x in closes if x is not None]
    mae_bf = [x for x in mae_bf if x is not None]
    ttm = [x for x in ttm if x is not None]
    qm, qa, qc, qb, qt = (
        _quantiles(mfes),
        _quantiles(maes),
        _quantiles(closes),
        _quantiles(mae_bf),
        _quantiles(ttm),
    )
    pos_rate = (
        sum(1 for x in closes if x > 0) / len(closes) if closes else None
    )
    return {
        "group": group_name,
        "horizon_min": horizon_min,
        "event_count": len(rows),
        "complete_count": len(complete),
        "censored_count": censored,
        "sample_flag": sample_flag(len(complete)),
        "mean_mfe_pct": qm["mean"],
        "median_mfe_pct": qm["p50"],
        "q25_mfe_pct": qm["p25"],
        "q75_mfe_pct": qm["p75"],
        "mean_mae_pct": qa["mean"],
        "median_mae_pct": qa["p50"],
        "q25_mae_pct": qa["p25"],
        "q75_mae_pct": qa["p75"],
        "median_mae_before_mfe_peak_pct": qb["p50"],
        "q75_mae_before_mfe_peak_pct": qb["p75"],
        "q90_mae_before_mfe_peak_pct": qb["p90"],
        "mean_close_return_pct": qc["mean"],
        "median_close_return_pct": qc["p50"],
        "positive_close_rate": pos_rate,
        "median_time_to_mfe_minutes": qt["p50"],
    }


def summarize_targets(
    target_rows: list[dict[str, Any]],
    *,
    group_name: str,
    event_ids: set[str],
    target_pct: float,
) -> dict[str, Any]:
    rows = [
        r
        for r in target_rows
        if r["event_id"] in event_ids and abs(float(r["target_pct"]) - float(target_pct)) < 1e-12
    ]
    reached = [r for r in rows if r.get("target_reached")]
    mae = [_f(r.get("mae_before_target_exclusive_pct")) for r in reached]
    mae = [x for x in mae if x is not None]
    mins = [_f(r.get("minutes_to_target")) for r in reached]
    mins = [x for x in mins if x is not None]
    q = _quantiles(mae)
    qm = _quantiles(mins)
    amb = sum(1 for r in reached if r.get("intrabar_order_ambiguous"))
    return {
        "group": group_name,
        "target_pct": target_pct,
        "event_count": len(rows),
        "reached_count": len(reached),
        "reach_rate": (len(reached) / len(rows)) if rows else None,
        "sample_flag": sample_flag(len(rows)),
        "median_minutes_to_target": qm["p50"],
        "median_mae_before_target_pct": q["p50"],
        "q75_mae_before_target_pct": q["p75"],
        "q90_mae_before_target_pct": q["p90"],
        "intrabar_ambiguous_rate": (amb / len(reached)) if reached else None,
    }


def summarize_first_hit(
    hit_rows: list[dict[str, Any]],
    *,
    group_name: str,
    event_ids: set[str],
    horizon_min: int,
    target_pct: float,
    stop_pct: float,
) -> dict[str, Any]:
    rows = [
        r
        for r in hit_rows
        if r["event_id"] in event_ids
        and int(r["horizon_min"]) == horizon_min
        and abs(float(r["target_pct"]) - target_pct) < 1e-12
        and abs(float(r["stop_pct"]) - stop_pct) < 1e-12
    ]
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[str(r.get("result"))] += 1
    n = len(rows)
    return {
        "group": group_name,
        "horizon_min": horizon_min,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "event_count": n,
        "sample_flag": sample_flag(n),
        "target_first": counts.get("TARGET_FIRST", 0),
        "stop_first": counts.get("STOP_FIRST", 0),
        "neither": counts.get("NEITHER", 0),
        "ambiguous": counts.get("AMBIGUOUS", 0),
        "censored": counts.get("CENSORED", 0),
        "target_first_rate": (counts.get("TARGET_FIRST", 0) / n) if n else None,
        "stop_first_rate": (counts.get("STOP_FIRST", 0) / n) if n else None,
    }
