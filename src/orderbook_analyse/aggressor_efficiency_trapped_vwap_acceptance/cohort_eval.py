"""Descriptive cohort statistics — no threshold fitting."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Optional


def _finite(xs: list[float]) -> list[float]:
    return [x for x in xs if x is not None and isinstance(x, (int, float)) and math.isfinite(x)]


def _quantile(xs: list[float], q: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - pos) + ys[hi] * (pos - lo)


def sample_size_label(n: int) -> str:
    if n < 10:
        return "VERY_SMALL_N"
    if n < 30:
        return "SMALL_N"
    if n < 100:
        return "EXPLORATORY"
    return "RESEARCH_DESCRIPTIVE"


def summarize_values(values: list[Optional[float]], *, n_total: int) -> dict[str, Any]:
    xs = _finite([v for v in values if v is not None])
    n = len(xs)
    mean = sum(xs) / n if n else None
    var = sum((x - mean) ** 2 for x in xs) / n if n and mean is not None else None
    std = math.sqrt(var) if var is not None else None
    pos = sum(1 for x in xs if x > 0) / n if n else None
    return {
        "n_total": n_total,
        "n_complete": n,
        "n_directional": n,
        "mean": mean,
        "median": _quantile(xs, 0.5),
        "std": std,
        "q10": _quantile(xs, 0.10),
        "q25": _quantile(xs, 0.25),
        "q50": _quantile(xs, 0.50),
        "q75": _quantile(xs, 0.75),
        "q90": _quantile(xs, 0.90),
        "positive_rate": pos,
        "sample_size_label": sample_size_label(n),
    }


def cohort_horizon_stats(
    rows: list[dict[str, Any]],
    *,
    cohort_key: str,
    cohort_value: Any,
    horizon_s: int,
    metric: str = "state_aligned_return_bps",
    require_directional: bool = True,
    anchor: str = "state_available",
) -> dict[str, Any]:
    subset = [
        r
        for r in rows
        if r.get(cohort_key) == cohort_value
        and r.get("horizon_s") == horizon_s
        and r.get("anchor") == anchor
        and r.get("outcome_coverage_complete")
        and (not require_directional or r.get("include_in_directional_hit_rate"))
    ]
    # For non-directional cohorts, use raw_return_bps descriptively
    if not require_directional:
        subset = [
            r
            for r in rows
            if r.get(cohort_key) == cohort_value
            and r.get("horizon_s") == horizon_s
            and r.get("anchor") == anchor
            and r.get("outcome_coverage_complete")
        ]
        values = [r.get("raw_return_bps") for r in subset]
        mfe = _finite([r.get("MFE_bps") for r in subset])
        mae = _finite([r.get("MAE_bps") for r in subset])
        base = summarize_values(values, n_total=len(subset))
        base.update(
            {
                "cohort_key": cohort_key,
                "cohort_value": cohort_value,
                "horizon_s": horizon_s,
                "metric": "raw_return_bps",
                "median_MFE": _quantile(mfe, 0.5),
                "median_MAE": _quantile(mae, 0.5),
                "MFE_MAE_ratio": (
                    (_quantile(mfe, 0.5) / _quantile(mae, 0.5))
                    if mfe and mae and _quantile(mae, 0.5) not in (None, 0)
                    else None
                ),
                "directional_analysis": False,
            }
        )
        return base

    values = [r.get(metric) for r in subset]
    mfe = _finite([r.get("MFE_bps") for r in subset])
    mae = _finite([r.get("MAE_bps") for r in subset])
    base = summarize_values(values, n_total=len(subset))
    base.update(
        {
            "cohort_key": cohort_key,
            "cohort_value": cohort_value,
            "horizon_s": horizon_s,
            "metric": metric,
            "median_MFE": _quantile(mfe, 0.5),
            "median_MAE": _quantile(mae, 0.5),
            "MFE_MAE_ratio": (
                (_quantile(mfe, 0.5) / _quantile(mae, 0.5))
                if mfe and mae and _quantile(mae, 0.5) not in (None, 0)
                else None
            ),
            "directional_analysis": True,
        }
    )
    return base


def leave_one_out(
    rows: list[dict[str, Any]],
    *,
    event_ids: list[str],
    horizon_s: int = 300,
    metric: str = "state_aligned_return_bps",
    anchor: str = "state_available",
) -> list[dict[str, Any]]:
    """Sensitivity for small HIGH cohort."""
    out: list[dict[str, Any]] = []
    base_rows = [
        r
        for r in rows
        if r.get("event_id") in set(event_ids)
        and r.get("horizon_s") == horizon_s
        and r.get("anchor") == anchor
        and r.get("outcome_coverage_complete")
        and r.get("include_in_directional_hit_rate")
    ]
    full_vals = _finite([r.get(metric) for r in base_rows])
    full_med = _quantile(full_vals, 0.5)
    full_mean = sum(full_vals) / len(full_vals) if full_vals else None
    for eid in event_ids:
        left = [r for r in base_rows if r.get("event_id") != eid]
        vals = _finite([r.get(metric) for r in left])
        med = _quantile(vals, 0.5)
        mean = sum(vals) / len(vals) if vals else None
        out.append(
            {
                "excluded_event_id": eid,
                "horizon_s": horizon_s,
                "n": len(vals),
                "median": med,
                "mean": mean,
                "full_median": full_med,
                "full_mean": full_mean,
                "median_delta": (med - full_med) if med is not None and full_med is not None else None,
                "mean_delta": (mean - full_mean) if mean is not None and full_mean is not None else None,
            }
        )
    return out


def information_stack_label(feat: dict[str, Any]) -> str:
    conf = feat.get("edge_match_confidence_class")
    acc = feat.get("final_acceptance_state")
    trap = feat.get("final_trap_label")
    has_high = conf == "HIGH"
    has_acc = acc not in {None, "UNKNOWN_EDGE", "UNKNOWN_DATA"}
    has_trap = trap in {"TRAP_CONFIRMED", "TEMPORARY_UNDERWATER", "VWAP_RECLAIMED"}
    if has_high and has_acc and has_trap:
        return "efficiency_high_edge_trap_acceptance"
    if has_high and has_trap:
        return "efficiency_high_edge_trap"
    if has_high and has_acc:
        return "efficiency_high_edge_acceptance"
    if has_high:
        return "efficiency_high_edge"
    return "efficiency_only"
