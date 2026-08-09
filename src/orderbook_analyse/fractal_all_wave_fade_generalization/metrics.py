"""Metrics helpers with bootstrap CIs."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization import (
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    FEE_PCT,
    MIN_SAMPLE,
    VERY_SMALL,
)


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def bootstrap_ci(
    values: np.ndarray,
    *,
    stat: str = "median",
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None, float | None]:
    v = values[np.isfinite(values)]
    if len(v) == 0:
        return None, None, None
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot, dtype=float)
    n = len(v)
    for i in range(n_boot):
        sample = v[rng.integers(0, n, n)]
        stats[i] = float(np.median(sample) if stat == "median" else np.mean(sample))
    point = float(np.median(v) if stat == "median" else np.mean(v))
    lo, hi = np.quantile(stats, [0.025, 0.975])
    return point, float(lo), float(hi)


def summarize_net(
    sub: pd.DataFrame,
    *,
    horizon: int,
    **meta: Any,
) -> dict[str, Any]:
    col = f"dir_ret_{horizon}m"
    n = int(len(sub))
    row: dict[str, Any] = {**meta, "n": n, "horizon_min": horizon, "sample_flag": sample_flag(n)}
    if n == 0 or col not in sub.columns:
        return row
    r = sub[col].astype(float)
    valid = r.notna()
    rv = r[valid].to_numpy(dtype=float)
    nv = int(len(rv))
    row["n_valid"] = nv
    if nv == 0:
        return row
    net = rv - FEE_PCT
    hit = float((rv > 0).mean())
    row["hit_rate"] = hit
    row["median_gross"] = float(np.median(rv))
    row["mean_gross"] = float(np.mean(rv))
    row["median_net"] = float(np.median(net))
    row["mean_net"] = float(np.mean(net))
    row["positive_net_fraction"] = float((net > 0).mean())
    wins = net[net > 0]
    losses = net[net < 0]
    row["win_loss_ratio"] = (
        float(len(wins) / len(losses)) if len(losses) else (float("inf") if len(wins) else None)
    )
    # binomial-ish hit CI (normal approx / Wilson-lite via bootstrap on hits)
    _, hit_lo, hit_hi = bootstrap_ci((rv > 0).astype(float), stat="mean")
    row["hit_rate_ci95_lo"] = hit_lo
    row["hit_rate_ci95_hi"] = hit_hi
    med, mlo, mhi = bootstrap_ci(net, stat="median")
    row["median_net_ci95_lo"] = mlo
    row["median_net_ci95_hi"] = mhi
    mean, elo, ehi = bootstrap_ci(net, stat="mean")
    row["mean_net_ci95_lo"] = elo
    row["mean_net_ci95_hi"] = ehi
    if f"dir_fav_{horizon}m" in sub.columns:
        fav = sub.loc[valid, f"dir_fav_{horizon}m"].astype(float)
        row["median_fav"] = float(fav.median()) if fav.notna().any() else None
    if f"dir_adv_{horizon}m" in sub.columns:
        adv = sub.loc[valid, f"dir_adv_{horizon}m"].astype(float)
        row["median_adv"] = float(adv.median()) if adv.notna().any() else None
    return row
