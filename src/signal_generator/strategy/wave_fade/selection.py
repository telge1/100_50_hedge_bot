"""Selection helpers: Tier-A prepare + global event sort (FIRST_CLUSTER / GLOBAL_SINGLE)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from signal_generator.strategy.wave_fade.clusters import build_same_side_clusters
from signal_generator.strategy.wave_fade.parameters import TF_RANK


def _ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def event_sort_key(row: pd.Series) -> tuple:
    """Deterministic global event ordering (freeze global_engine.event_sort_key)."""
    tf = str(row["signal_tf"])
    return (
        _ts_utc(row["entry_time"]).value,
        _ts_utc(row["confirmation_available_at"]).value,
        -int(TF_RANK[tf]),
        str(row["symbol"]),
    )


def prepare_signal_events(
    sig_valid: pd.DataFrame,
    *,
    tier_a_only: bool,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[int, int]]:
    """Filter signals, build frozen clusters (freeze engine.prepare_signal_events)."""
    df = sig_valid.copy()
    if tier_a_only:
        df = df[df["is_tier_a"].astype(bool)].copy().reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    clusters = build_same_side_clusters(df)
    sig_to_cluster: dict[int, int] = {}
    for ci, c in enumerate(clusters):
        for _, row in c["rows"].iterrows():
            sig_to_cluster[int(row["signal_id"])] = ci
    return df, clusters, sig_to_cluster


def first_cluster_entry_signal_ids(clusters: list[dict[str, Any]]) -> set[int]:
    """FIRST_CLUSTER_ENTRY: first row of each cluster by confirmation time."""
    out: set[int] = set()
    for c in clusters:
        first = c["rows"].iloc[0]
        out.add(int(first["signal_id"]))
    return out
