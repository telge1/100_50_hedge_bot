"""Group summaries. Descriptive only; pooled slices are flagged as not independent."""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import COMPARE_GROUPS, HORIZONS_S

INSUFFICIENT_N = 10
WARN_N = 20
INSUFFICIENT_CLUSTERS = 3


def _median(series: pd.Series) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.median())


def _mean(series: pd.Series) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.mean())


def _share(mask: pd.Series, n: int) -> float | None:
    if n <= 0:
        return None
    return float(mask.fillna(False).sum()) / float(n)


def sample_flag(n: int, n_clusters: int) -> str:
    if n < INSUFFICIENT_N or n_clusters < INSUFFICIENT_CLUSTERS:
        return "INSUFFICIENT_SAMPLE"
    if n < WARN_N:
        return "SMALL_SAMPLE"
    return "OK"


def cluster_representatives(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["_det"] = pd.to_datetime(work["detection_available_at"], utc=True)
    return work.sort_values(["_det", "episode_id"]).drop_duplicates("movement_cluster_id", keep="first")


def summarize_group(df: pd.DataFrame, *, group: str, batch_id: str, level: str, horizon: int) -> dict[str, Any]:
    n = int(len(df))
    n_cl = int(df["movement_cluster_id"].nunique()) if n and "movement_cluster_id" in df.columns else 0
    n_ok = int(df["path_complete"].fillna(False).sum()) if n and "path_complete" in df.columns else 0
    first = df["first_nonzero_direction"] if n else pd.Series(dtype=object)
    flag = sample_flag(n, n_cl)
    warnings = []
    if batch_id == "pooled":
        warnings.append("POOLED_NOT_INDEPENDENT")
    if flag != "OK":
        warnings.append(flag)
    seq = df["sequence_class"].value_counts().to_dict() if n and "sequence_class" in df.columns else {}
    return {
        "compare_group": group,
        "batch_id": batch_id,
        "level": level,
        "horizon_seconds": int(horizon),
        "n": n,
        "n_complete": n_ok,
        "n_clusters": n_cl,
        "sample_flag": flag,
        "warnings": "|".join(warnings) if warnings else "",
        "n_profit_first": int((first == "PROFIT_FIRST").sum()) if n else 0,
        "n_adverse_first": int((first == "ADVERSE_FIRST").sum()) if n else 0,
        "n_flat_or_ambiguous": int((first == "FLAT_OR_AMBIGUOUS").sum()) if n else 0,
        "share_profit_first": _share(first == "PROFIT_FIRST", n),
        "share_adverse_first": _share(first == "ADVERSE_FIRST", n),
        "median_mae_before_first_profit_pct": _median(df.get("mae_before_first_profit_pct", pd.Series(dtype=float))),
        "median_time_underwater_before_first_profit_ms": _median(df.get("time_underwater_before_first_profit_ms", pd.Series(dtype=float))),
        "median_time_to_first_profit_ms": _median(df.get("time_to_first_profit_ms", pd.Series(dtype=float))),
        "share_plus_0_10_before_minus_0_10": _share(df.get("symmetric_first_0_10", pd.Series(dtype=object)) == "PROFIT", n),
        "share_minus_0_10_before_plus_0_10": _share(df.get("symmetric_first_0_10", pd.Series(dtype=object)) == "ADVERSE", n),
        "share_plus_0_20_before_minus_0_20": _share(df.get("symmetric_first_0_20", pd.Series(dtype=object)) == "PROFIT", n),
        "share_minus_0_20_before_plus_0_20": _share(df.get("symmetric_first_0_20", pd.Series(dtype=object)) == "ADVERSE", n),
        "share_plus_0_50_before_minus_0_50": _share(df.get("symmetric_first_0_50", pd.Series(dtype=object)) == "PROFIT", n),
        "share_minus_0_50_before_plus_0_50": _share(df.get("symmetric_first_0_50", pd.Series(dtype=object)) == "ADVERSE", n),
        "median_mfe_pct": _median(df.get("mfe_pct", pd.Series(dtype=float))),
        "median_mae_pct": _median(df.get("mae_pct", pd.Series(dtype=float))),
        "mean_mfe_pct": _mean(df.get("mfe_pct", pd.Series(dtype=float))),
        "mean_mae_pct": _mean(df.get("mae_pct", pd.Series(dtype=float))),
        "share_mfe_before_mae": _share(df.get("mfe_before_mae", pd.Series(dtype=object)) == True, n),  # noqa: E712
        "share_mae_before_mfe": _share(df.get("mae_before_mfe", pd.Series(dtype=object)) == True, n),  # noqa: E712
        "median_giveback_from_mfe_pct": _median(df.get("giveback_from_mfe_pct", pd.Series(dtype=float))),
        "median_giveback_fraction_of_mfe": _median(df.get("giveback_fraction_of_mfe", pd.Series(dtype=float))),
        "median_retained_profit_at_horizon_pct": _median(df.get("retained_profit_at_horizon_pct", pd.Series(dtype=float))),
        "median_retained_fraction_of_mfe": _median(df.get("retained_fraction_of_mfe", pd.Series(dtype=float))),
        "share_crossed_back_below_zero_after_mfe": _share(df.get("crossed_back_below_zero_after_mfe", pd.Series(dtype=object)) == True, n),  # noqa: E712
        "n_direct_profit_held": int(seq.get("DIRECT_PROFIT_HELD") or 0),
        "n_direct_profit_given_back": int(seq.get("DIRECT_PROFIT_GIVEN_BACK") or 0),
        "n_adverse_then_profit": int(seq.get("ADVERSE_THEN_PROFIT") or 0),
        "n_adverse_no_recovery": int(seq.get("ADVERSE_NO_RECOVERY") or 0),
        "n_chop_around_entry": int(seq.get("CHOP_AROUND_ENTRY") or 0),
        "n_flat_or_insufficient": int(seq.get("FLAT_OR_INSUFFICIENT_DATA") or 0),
    }


def _slice(df: pd.DataFrame, group: str) -> pd.DataFrame:
    if group == "ALIGNED":
        return df
    return df[df["high_conviction_class"] == group]


def build_summaries(horizon_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_batch: list[dict[str, Any]] = []
    by_cluster: list[dict[str, Any]] = []
    by_class: list[dict[str, Any]] = []
    if horizon_df is None or horizon_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    batches = ["batch_1", "batch_2", "pooled"]
    for group in COMPARE_GROUPS:
        scoped = _slice(horizon_df, group)
        for hz in HORIZONS_S:
            hz_all = scoped[scoped["horizon_seconds"] == hz]
            for bid in batches:
                if bid == "pooled":
                    ep = hz_all
                else:
                    ep = hz_all[hz_all["batch_id"] == bid]
                by_batch.append(summarize_group(ep, group=group, batch_id=bid, level="episode", horizon=int(hz)))
                cl = cluster_representatives(ep)
                by_cluster.append(summarize_group(cl, group=group, batch_id=bid, level="cluster", horizon=int(hz)))
        # class table uses 1800s episode + cluster
        hz1800 = scoped[scoped["horizon_seconds"] == 1800]
        for bid in batches:
            ep = hz1800 if bid == "pooled" else hz1800[hz1800["batch_id"] == bid]
            row = summarize_group(ep, group=group, batch_id=bid, level="episode", horizon=1800)
            by_class.append(row)
            by_class.append(summarize_group(cluster_representatives(ep), group=group, batch_id=bid, level="cluster", horizon=1800))
    return pd.DataFrame(by_batch), pd.DataFrame(by_class), pd.DataFrame(by_cluster)
