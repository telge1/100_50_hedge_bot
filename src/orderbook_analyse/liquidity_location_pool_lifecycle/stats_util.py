"""Wilson intervals and cohort aggregation."""

from __future__ import annotations

import math
from typing import Any, Iterable

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


def rate_block(mask: pd.Series, label: str) -> dict[str, Any]:
    n = int(mask.shape[0])
    s = int(mask.sum()) if n else 0
    lo, hi = wilson_interval(s, n)
    return {
        f"{label}_n": n,
        f"{label}_count": s,
        f"{label}_rate": (s / n) if n else None,
        f"{label}_wilson_lo": lo,
        f"{label}_wilson_hi": hi,
    }


def summarize_cohorts(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    # Primary variant for headline rates: acceptance=2, reclaim=6, reaction=0.5
    prim = outcomes[
        (outcomes["acceptance_bars"] == 2)
        & (outcomes["reclaim_horizon_bars"] == 6)
        & (
            outcomes["reaction_atr_mult"].isna()
            | (outcomes["reaction_atr_mult"] == 0.5)
            | (outcomes["swept"] == True)  # noqa: E712
        )
    ].copy()
    if prim.empty:
        prim = outcomes.copy()

    rows: list[dict[str, Any]] = []
    group_cols = ["symbol", "timeframe", "side", "entity_kind"]
    if "pool_count_bucket" in prim.columns:
        group_cols.append("pool_count_bucket")
    for keys, g in prim.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(group_cols, keys))
        row["n"] = len(g)
        row.update(rate_block(g["touched"].astype(bool), "touch"))
        row.update(rate_block(g["swept"].astype(bool), "sweep"))
        row.update(rate_block(g["defended"].astype(bool), "defended"))
        row.update(rate_block(g["swept_reclaimed"].astype(bool), "sweep_reclaim"))
        row.update(rate_block(g["consumed_accepted"].astype(bool), "consumed_accepted"))
        for col in ("minutes_to_touch", "minutes_to_sweep", "mfe_frac", "mae_frac"):
            if col in g.columns:
                row[f"median_{col}"] = float(g[col].median()) if g[col].notna().any() else None
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_sensitivity(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    keys = ["timeframe", "acceptance_bars", "reclaim_horizon_bars", "reaction_atr_mult", "entity_kind"]
    for k, g in outcomes.groupby(keys, dropna=False):
        if not isinstance(k, tuple):
            k = (k,)
        row = dict(zip(keys, k))
        row["n"] = len(g)
        row.update(rate_block(g["touched"].astype(bool), "touch"))
        row.update(rate_block(g["swept"].astype(bool), "sweep"))
        row.update(rate_block(g["defended"].astype(bool), "defended"))
        row.update(rate_block(g["swept_reclaimed"].astype(bool), "sweep_reclaim"))
        row.update(rate_block(g["consumed_accepted"].astype(bool), "consumed_accepted"))
        rows.append(row)
    return pd.DataFrame(rows)


def quality_by_symbol(outcomes: pd.DataFrame, summaries: Iterable[dict[str, Any]]) -> pd.DataFrame:
    rows = list(summaries)
    if outcomes.empty and not rows:
        return pd.DataFrame()
    if rows:
        return pd.DataFrame(rows)
    q = []
    for sym, g in outcomes.groupby("symbol"):
        q.append(
            {
                "symbol": sym,
                "n_outcome_rows": len(g),
                "n_entities": g["entity_id"].nunique(),
                "touch_rate": float(g["touched"].mean()),
                "sweep_rate": float(g["swept"].mean()),
            }
        )
    return pd.DataFrame(q)
