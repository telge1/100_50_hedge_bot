"""Group metrics for higher-TF Stoch context analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import K_BUCKETS


def sample_flag(n: int) -> str:
    if n < 50:
        return "VERY_SMALL"
    if n < 100:
        return "SMALL"
    if n < 250:
        return "MEDIUM"
    return "LARGE"


def summarize_group(df: pd.DataFrame, **meta) -> dict[str, Any]:
    row: dict[str, Any] = {**meta}
    if df is None or df.empty:
        row.update({"n": 0, "sample_flag": "VERY_SMALL"})
        return row
    nets = df["net_return_pct"].astype(float).to_numpy()
    n = len(nets)
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    tp = int((df["exit_reason"] == "TP").sum())
    sl = int((df["exit_reason"] == "SL").sum())
    pf = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    row.update(
        {
            "n": n,
            "sample_flag": sample_flag(n),
            "tp": tp,
            "sl": sl,
            "timeout": int((df["exit_reason"] == "TIMEOUT").sum()),
            "conflict": int((df["exit_reason"] == "HIGHER_TF_CONFLICT").sum()),
            "tp_rate": float(tp / n),
            "win_rate": float(np.mean(nets > 0)),
            "expectancy": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "profit_factor": pf,
            "cumulative_additive_net": float(np.sum(nets)),
            "max_drawdown_additive": float(dd.min()) if len(dd) else 0.0,
            "avg_holding_minutes": float(df["holding_minutes"].astype(float).mean())
            if "holding_minutes" in df.columns
            else None,
            "upgrade_rate": float((df["upgrade_count"].astype(float) > 0).mean())
            if "upgrade_count" in df.columns
            else None,
        }
    )
    return row


def with_deltas(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for a, b, name in (
        ("expectancy", "expectancy", "delta_expectancy_vs_baseline"),
        ("tp_rate", "tp_rate", "delta_tp_rate_vs_baseline"),
        ("profit_factor", "profit_factor", "delta_pf_vs_baseline"),
    ):
        va, vb = row.get(a), baseline.get(b)
        out[name] = (float(va) - float(vb)) if va is not None and vb is not None else None
    return out


def k_bucket(k: float | None) -> str | None:
    if k is None or not np.isfinite(k):
        return None
    for lo, hi, lab in K_BUCKETS:
        if lo <= float(k) < hi:
            return lab
    return None


def groupby_stats(
    df: pd.DataFrame,
    keys: list[str],
    *,
    baseline: dict[str, Any] | None = None,
    min_n: int = 1,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows = []
    for key, g in df.groupby(keys, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        meta = {k: v for k, v in zip(keys, key)}
        sm = summarize_group(g, **meta)
        if baseline is not None:
            sm = with_deltas(sm, baseline)
        if sm["n"] >= min_n:
            rows.append(sm)
    return pd.DataFrame(rows)
