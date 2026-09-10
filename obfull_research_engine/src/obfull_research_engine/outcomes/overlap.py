"""Deterministic overlap clustering of outcome windows (dependency mark only)."""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd


def assign_overlap_clusters(rows: list[dict[str, Any]], *, horizon_seconds: int) -> list[dict[str, Any]]:
    """Transitive clustering of [anchor, horizon_end] intervals for one horizon."""
    indexed = [
        (
            i,
            pd.to_datetime(r["outcome_anchor_ts"], utc=True),
            pd.to_datetime(r["horizon_end_ts"], utc=True),
            str(r["episode_id"]),
        )
        for i, r in enumerate(rows)
        if int(r["horizon_seconds"]) == int(horizon_seconds)
    ]
    if not indexed:
        return rows

    indexed.sort(key=lambda x: (x[1], x[2], x[3]))
    clusters: list[list[tuple]] = []
    cur: list[tuple] = []
    cur_end = None
    for item in indexed:
        _i, start, end, _eid = item
        if not cur:
            cur = [item]
            cur_end = end
            continue
        if start <= cur_end:
            cur.append(item)
            if end > cur_end:
                cur_end = end
        else:
            clusters.append(cur)
            cur = [item]
            cur_end = end
    if cur:
        clusters.append(cur)

    # map episode_id -> cluster meta
    meta: dict[str, tuple[str, int]] = {}
    for cl in clusters:
        eids = sorted(x[3] for x in cl)
        raw = f"{horizon_seconds}|" + "|".join(eids)
        cid = hashlib.sha256(raw.encode()).hexdigest()[:16]
        n = len(eids)
        for eid in eids:
            meta[eid] = (cid, n)

    out = []
    for r in rows:
        if int(r["horizon_seconds"]) != int(horizon_seconds):
            out.append(r)
            continue
        cid, n = meta[str(r["episode_id"])]
        nr = dict(r)
        nr["overlap_cluster_id"] = cid
        nr["overlap_count"] = int(n)
        out.append(nr)
    return out


def apply_all_horizons(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, Any]]:
    out = rows
    for h in horizons:
        out = assign_overlap_clusters(out, horizon_seconds=int(h))
    return out


def overlap_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for h, g in df.groupby("horizon_seconds"):
        clusters = g.drop_duplicates("overlap_cluster_id")
        n_ep = len(g)
        overlapping = int((g["overlap_count"] > 1).sum())
        rows.append(
            {
                "horizon_seconds": int(h),
                "n_episodes": n_ep,
                "n_overlap_clusters": int(g["overlap_cluster_id"].nunique()),
                "n_episodes_overlapping": overlapping,
                "pct_episodes_overlapping": round(100.0 * overlapping / max(n_ep, 1), 4),
                "max_overlap_count": int(g["overlap_count"].max()),
                "median_overlap_count": float(g["overlap_count"].median()),
            }
        )
    return pd.DataFrame(rows).sort_values("horizon_seconds").reset_index(drop=True)
