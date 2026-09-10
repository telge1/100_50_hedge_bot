"""Causal baseline helpers for episode_candidate_v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BaselineSlice:
    start_idx: int
    end_idx_exclusive: int  # == current index i (t excluded)
    start_ts: pd.Timestamp
    end_ts: pd.Timestamp  # last included baseline second
    n: int
    valid: bool
    invalid_reason: str | None


def contiguous_suffix_after_gaps(
    state_ts: pd.Series,
    i: int,
    lookback: int,
) -> BaselineSlice:
    """Return contiguous [start, i) suffix inside lookback, resetting after gaps.

    Baseline excludes index i. Gaps are detected as state_ts diffs != 1s.
    """
    if i <= 0:
        return BaselineSlice(0, 0, pd.NaT, pd.NaT, 0, False, "no_history")

    lo = max(0, i - lookback)
    ts = pd.to_datetime(state_ts.iloc[lo:i], utc=True)
    if len(ts) == 0:
        return BaselineSlice(lo, i, pd.NaT, pd.NaT, 0, False, "empty_baseline")

    # Find last gap inside [lo, i)
    diffs = ts.diff().dt.total_seconds()
    gap_pos = np.where((diffs.to_numpy()[1:] != 1.0))[0]
    if len(gap_pos) == 0:
        start_idx = lo
    else:
        # gap between local indices gap_pos[k] and gap_pos[k]+1 → keep after last gap
        last_gap_local = int(gap_pos[-1]) + 1  # first index after gap relative to lo
        start_idx = lo + last_gap_local

    n = i - start_idx
    if n <= 0:
        return BaselineSlice(start_idx, i, pd.NaT, pd.NaT, 0, False, "empty_after_gap_reset")

    start_ts = pd.to_datetime(state_ts.iloc[start_idx], utc=True)
    end_ts = pd.to_datetime(state_ts.iloc[i - 1], utc=True)
    return BaselineSlice(start_idx, i, start_ts, end_ts, n, True, None)


def validate_baseline_length(slice_: BaselineSlice, minimum_seconds: int) -> BaselineSlice:
    if not slice_.valid:
        return slice_
    if slice_.n < minimum_seconds:
        return BaselineSlice(
            slice_.start_idx,
            slice_.end_idx_exclusive,
            slice_.start_ts,
            slice_.end_ts,
            slice_.n,
            False,
            f"warmup_insufficient_n={slice_.n}_min={minimum_seconds}",
        )
    return slice_


@dataclass(frozen=True)
class RobustStats:
    median: float
    mad: float
    q_hi: float
    q_lo: float
    valid: bool
    invalid_reason: str | None


def robust_stats(values: np.ndarray, quantile_hi: float) -> RobustStats:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return RobustStats(np.nan, np.nan, np.nan, np.nan, False, "empty_values")
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    q_hi = float(np.quantile(arr, quantile_hi))
    q_lo = float(np.quantile(arr, 1.0 - quantile_hi))
    return RobustStats(med, mad, q_hi, q_lo, True, None)


def robust_z(x: float, stats: RobustStats, mad_scale: float) -> tuple[float | None, str | None]:
    if not stats.valid or not np.isfinite(x):
        return None, "invalid_input"
    if stats.mad == 0.0 or not np.isfinite(stats.mad):
        return None, "mad_zero_refusal"
    denom = mad_scale * stats.mad
    if denom == 0.0 or not np.isfinite(denom):
        return None, "denom_zero_refusal"
    return float((x - stats.median) / denom), None


def compare_extreme(
    *,
    x: float,
    stats: RobustStats,
    compare: str,
    z_threshold: float,
    mad_scale: float,
) -> dict[str, Any]:
    """Outcome-blind extreme test vs past baseline. Never invents tiny MAD."""
    out: dict[str, Any] = {
        "fired": False,
        "z": None,
        "reason": None,
        "stats_valid": stats.valid,
    }
    if not stats.valid:
        out["reason"] = stats.invalid_reason or "stats_invalid"
        return out
    if not np.isfinite(x):
        out["reason"] = "non_finite_x"
        return out

    z, z_err = robust_z(x, stats, mad_scale)
    out["z"] = z
    if z is None:
        # MAD zero: never invent a tiny denominator for extreme "high"/"low" signals.
        # For stability checks (not_high / not_low), flat baseline + non-worse x is OK.
        if compare == "not_high":
            out["fired"] = bool(np.isfinite(x) and x <= stats.median)
            out["reason"] = "mad_zero_stable_not_high" if out["fired"] else "mad_zero_exceeds_flat_baseline"
            return out
        if compare == "not_low":
            out["fired"] = bool(np.isfinite(x) and x >= stats.median)
            out["reason"] = "mad_zero_stable_not_low" if out["fired"] else "mad_zero_below_flat_baseline"
            return out
        if compare == "high":
            if np.isfinite(stats.q_hi) and x > stats.q_hi and x > stats.median:
                out["fired"] = True
                out["reason"] = "quantile_only_mad_zero"
                return out
            out["reason"] = z_err or "mad_zero_refusal"
            return out
        if compare == "low":
            if np.isfinite(stats.q_lo) and x < stats.q_lo and x < stats.median:
                out["fired"] = True
                out["reason"] = "quantile_only_mad_zero"
                return out
            out["reason"] = z_err or "mad_zero_refusal"
            return out
        if compare in {"high_abs", "high_abs_delta_vs_baseline_median", "low_abs_delta_vs_baseline_median"}:
            out["reason"] = z_err or "mad_zero_refusal"
            return out
        out["reason"] = z_err or "mad_zero_refusal"
        return out

    if compare == "high":
        out["fired"] = bool(z >= z_threshold and x >= stats.q_hi)
        out["reason"] = "ok" if out["fired"] else "below_threshold"
    elif compare == "low":
        out["fired"] = bool(z <= -z_threshold and x <= stats.q_lo)
        out["reason"] = "ok" if out["fired"] else "below_threshold"
    elif compare == "high_abs":
        out["fired"] = bool(abs(z) >= z_threshold and abs(x) >= max(abs(stats.q_hi), abs(stats.q_lo)))
        out["reason"] = "ok" if out["fired"] else "below_threshold"
    elif compare == "high_abs_delta_vs_baseline_median":
        # positive imbalance shift: x - median large positive
        delta = x - stats.median
        dz, derr = robust_z(delta + stats.median, stats, mad_scale)  # z of x already
        out["z"] = z
        out["fired"] = bool(z >= z_threshold and x >= stats.q_hi)
        out["reason"] = "ok" if out["fired"] else (derr or "below_threshold")
    elif compare == "low_abs_delta_vs_baseline_median":
        out["fired"] = bool(z <= -z_threshold and x <= stats.q_lo)
        out["reason"] = "ok" if out["fired"] else "below_threshold"
    elif compare == "not_high":
        out["fired"] = bool(z is not None and z < z_threshold)
        out["reason"] = "ok" if out["fired"] else "is_high"
    elif compare == "not_low":
        out["fired"] = bool(z is not None and z > -z_threshold)
        out["reason"] = "ok" if out["fired"] else "is_low"
    else:
        out["reason"] = f"unknown_compare:{compare}"
    return out


def series_window(arr: np.ndarray, start: int, end: int) -> np.ndarray:
    return arr[start:end]
