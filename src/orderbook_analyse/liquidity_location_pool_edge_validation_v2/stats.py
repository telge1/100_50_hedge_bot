"""Shared helpers: rates, buckets, Wilson, block bootstrap."""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def rate_row(mask: pd.Series, label: str) -> dict[str, Any]:
    n = int(len(mask))
    s = int(mask.fillna(False).astype(bool).sum()) if n else 0
    lo, hi = wilson_interval(s, n)
    return {
        f"{label}_n": n,
        f"{label}_count": s,
        f"{label}_rate": (s / n) if n else None,
        f"{label}_wilson_lo": lo,
        f"{label}_wilson_hi": hi,
    }


def distance_atr_bucket(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "unknown"
    a = abs(float(x))
    if a < 0.25:
        return "0-0.25"
    if a < 0.5:
        return "0.25-0.5"
    if a < 1.0:
        return "0.5-1"
    if a < 2.0:
        return "1-2"
    if a < 3.0:
        return "2-3"
    return ">3"


def distance_pct_bucket(dist_price: float | None, center: float | None) -> str:
    if dist_price is None or center is None or center == 0:
        return "unknown"
    pct = abs(float(dist_price) / float(center)) * 100.0
    if pct < 0.05:
        return "0-0.05%"
    if pct < 0.10:
        return "0.05-0.10%"
    if pct < 0.25:
        return "0.10-0.25%"
    if pct < 0.50:
        return "0.25-0.50%"
    if pct < 1.0:
        return "0.50-1.0%"
    return ">1.0%"


def age_at_touch_bucket(bars: float | None, touched: bool) -> str:
    if not touched:
        return "untouched"
    if bars is None or (isinstance(bars, float) and math.isnan(bars)):
        return "unknown"
    b = int(bars)
    if b <= 1:
        return "0-1"
    if b <= 3:
        return "2-3"
    if b <= 6:
        return "4-6"
    if b <= 12:
        return "7-12"
    if b <= 24:
        return "13-24"
    return ">24"


def touch_timing(bars: float | None, touched: bool) -> str:
    if not touched:
        return "untouched"
    if bars is None or (isinstance(bars, float) and math.isnan(bars)):
        return "unknown"
    if int(bars) <= 0:
        return "immediate_touch"
    return "delayed_touch"


def component_bucket(n: int | float | None) -> str:
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "1"
    k = int(n)
    if k <= 1:
        return "1"
    if k == 2:
        return "2"
    if k == 3:
        return "3"
    if k <= 5:
        return "4-5"
    return "6+"


def summarize_group(
    g: pd.DataFrame,
    *,
    group_cols: dict[str, Any],
    dest_col: str | None = "first_destination_60m",
) -> dict[str, Any]:
    row = dict(group_cols)
    row["n"] = len(g)
    for name, col in [
        ("touch", "touched"),
        ("sweep", "swept"),
        ("defended", "defended"),
        ("sweep_reclaim", "swept_reclaimed"),
        ("consumed_accepted", "consumed_accepted"),
    ]:
        if col in g.columns:
            row.update(rate_row(g[col], name))
    if "minutes_to_touch" in g.columns and g["minutes_to_touch"].notna().any():
        row["median_minutes_to_touch"] = float(g["minutes_to_touch"].median())
    else:
        row["median_minutes_to_touch"] = None
    if "bars_to_touch" in g.columns and g["bars_to_touch"].notna().any():
        row["median_bars_to_touch"] = float(g.loc[g["touched"].astype(bool), "bars_to_touch"].median()) if g["touched"].any() else None
    if dest_col and dest_col in g.columns and g[dest_col].notna().any():
        mode = g[dest_col].dropna().mode()
        row["top_first_destination"] = None if mode.empty else str(mode.iloc[0])
        row["top_first_destination_share"] = float((g[dest_col] == row["top_first_destination"]).mean()) if row["top_first_destination"] else None
    return row


def block_bootstrap_rate(
    df: pd.DataFrame,
    *,
    success_col: str,
    block_cols: list[str],
    n_boot: int = 400,
    seed: int = 42,
) -> dict[str, Any]:
    """Resample whole blocks (e.g. utc_day or symbol+utc_day) with replacement."""
    if df.empty or success_col not in df.columns:
        return {"n": 0, "rate": None, "boot_lo": None, "boot_hi": None, "n_boot": n_boot}
    work = df.copy()
    work["_success"] = work[success_col].fillna(False).astype(bool)
    work["_block"] = work[block_cols].astype(str).agg("|".join, axis=1)
    blocks = work.groupby("_block", sort=False)
    block_ids = list(blocks.groups.keys())
    if not block_ids:
        return {"n": len(work), "rate": float(work["_success"].mean()), "boot_lo": None, "boot_hi": None, "n_boot": n_boot}
    rng = np.random.default_rng(seed)
    rates = []
    for _ in range(n_boot):
        chosen = rng.choice(block_ids, size=len(block_ids), replace=True)
        parts = [blocks.get_group(b) for b in chosen]
        sample = pd.concat(parts, ignore_index=True)
        rates.append(float(sample["_success"].mean()) if len(sample) else float("nan"))
    arr = np.asarray(rates, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        lo = hi = None
    else:
        lo, hi = float(np.quantile(arr, 0.025)), float(np.quantile(arr, 0.975))
    return {
        "n": int(len(work)),
        "rate": float(work["_success"].mean()),
        "boot_lo": lo,
        "boot_hi": hi,
        "n_boot": n_boot,
        "n_blocks": len(block_ids),
    }
