"""EMA warmup convergence vs reference history."""

from __future__ import annotations

from typing import Any

from ema_definitions_v1 import EMA_PERIODS, ema_series_for_path, ema_at_index
from validation_config_v1 import (
    EMA200_CONVERGENCE_ABS_TOL,
    EMA200_CONVERGENCE_REL_TOL,
    EMA59_CONVERGENCE_ABS_TOL,
    EMA59_CONVERGENCE_REL_TOL,
    REFERENCE_WARMUP_HOURS,
    WARMUP_HOURS_CANDIDATES,
)


def _within_tol(ref: float, other: float, *, abs_tol: float, rel_tol: float) -> bool:
    diff = abs(ref - other)
    scale = max(abs(ref), abs_tol)
    return diff <= max(abs_tol, rel_tol * scale)


def compare_warmups_at_probes(
    *,
    bar_keys: list[str],
    prices: dict[str, dict[str, float]],
    probe_closes: list[str],
    warmup_hours: int,
    reference_ema_by_probe: dict[str, dict[int, float]] | None = None,
    path: str = "trp_research_charts",
) -> dict[str, Any]:
    """Slice bars to [probe - warmup, probe] per probe; compute EMA on slice; compare to full-series ref."""
    from datetime import timedelta

    from ohlc_loader_v1 import _iso, _parse_utc

    def _bar_dt(key: str):
        return _parse_utc(key)

    closes_full = [float(prices[b]["c"]) for b in bar_keys]
    ref_by_probe: dict[str, dict[int, float]] = reference_ema_by_probe or {}
    rows: list[dict[str, Any]] = []
    all_ok = True

    for probe in probe_closes:
        if probe not in prices:
            rows.append({"probe": probe, "ok": False, "reason": "PROBE_NOT_IN_LOADED_BARS"})
            all_ok = False
            continue
        probe_dt = _parse_utc(probe)
        slice_start = probe_dt - timedelta(hours=warmup_hours)
        slice_keys = [b for b in bar_keys if slice_start <= _bar_dt(b) <= probe_dt]
        if len(slice_keys) < 2:
            rows.append({"probe": probe, "ok": False, "reason": "SLICE_TOO_SHORT", "bars": len(slice_keys)})
            all_ok = False
            continue
        slice_closes = [float(prices[b]["c"]) for b in slice_keys]
        ema_vals: dict[int, float | None] = {}
        for period in EMA_PERIODS:
            series = ema_series_for_path(path, slice_closes, period)
            ema_vals[period] = ema_at_index(series, len(series) - 1)

        if probe not in ref_by_probe:
            full_idx = bar_keys.index(probe)
            full_closes = closes_full[: full_idx + 1]
            ref: dict[int, float] = {}
            for period in EMA_PERIODS:
                series = ema_series_for_path(path, full_closes, period)
                v = ema_at_index(series, len(series) - 1)
                if v is None:
                    ref = {}
                    break
                ref[period] = v
            ref_by_probe[probe] = ref

        ref = ref_by_probe.get(probe) or {}
        row: dict[str, Any] = {
            "probe": probe,
            "warmup_hours": warmup_hours,
            "bars_in_slice": len(slice_keys),
            "slice_start": _iso(slice_start),
        }
        probe_ok = True
        for period in (59, 200):
            ev = ema_vals.get(period)
            rv = ref.get(period)
            if ev is None or rv is None:
                row[f"ema{period}_ok"] = False
                row[f"ema{period}_reason"] = "NOT_WARMED"
                probe_ok = False
                continue
            abs_tol = EMA59_CONVERGENCE_ABS_TOL if period == 59 else EMA200_CONVERGENCE_ABS_TOL
            rel_tol = EMA59_CONVERGENCE_REL_TOL if period == 59 else EMA200_CONVERGENCE_REL_TOL
            ok = _within_tol(rv, ev, abs_tol=abs_tol, rel_tol=rel_tol)
            row[f"ema{period}_ref"] = rv
            row[f"ema{period}_warm"] = ev
            row[f"ema{period}_abs_diff"] = abs(rv - ev)
            row[f"ema{period}_rel_diff"] = abs(rv - ev) / max(abs(rv), abs_tol)
            row[f"ema{period}_ok"] = ok
            if not ok:
                probe_ok = False
        row["ok"] = probe_ok
        if not probe_ok:
            all_ok = False
        rows.append(row)

    return {
        "warmup_hours": warmup_hours,
        "reference_warmup_hours": REFERENCE_WARMUP_HOURS,
        "all_ok": all_ok,
        "rows": rows,
        "reference_by_probe": ref_by_probe,
    }


def summarize_convergence(
    bar_keys: list[str],
    prices: dict[str, dict[str, float]],
    probe_closes: list[str],
) -> dict[str, Any]:
    """Reference = full loaded history from data start; candidates = WARMUP_HOURS_CANDIDATES slices."""
    full: dict[str, Any] | None = None
    ref_ema: dict[str, dict[int, float]] = {}
    candidate_results: list[dict[str, Any]] = []
    for wh in WARMUP_HOURS_CANDIDATES:
        if wh == REFERENCE_WARMUP_HOURS:
            continue
        res = compare_warmups_at_probes(
            bar_keys=bar_keys,
            prices=prices,
            probe_closes=probe_closes,
            warmup_hours=wh,
            reference_ema_by_probe=ref_ema,
        )
        candidate_results.append(res)
    ref_res = compare_warmups_at_probes(
        bar_keys=bar_keys,
        prices=prices,
        probe_closes=probe_closes,
        warmup_hours=REFERENCE_WARMUP_HOURS,
        reference_ema_by_probe=ref_ema,
    )
    ssot72_ok = next((r["all_ok"] for r in candidate_results if r["warmup_hours"] == 72), False)
    return {
        "reference": ref_res,
        "candidates": candidate_results,
        "ssot_price_feed_72h_would_converge": ssot72_ok,
        "all_candidates_converge": all(r["all_ok"] for r in candidate_results),
    }
