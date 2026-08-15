"""Event clustering: contiguous burst buckets → anchor = max liq; cooldown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.config import COOLDOWN_BARS


@dataclass
class EventCluster:
    symbol: str
    side: str
    burst: str
    sequence_id: int
    start_i: int
    end_i: int
    anchor_i: int
    anchor_liq: float
    member_indices: list[int]


def cluster_bursts(
    df: pd.DataFrame,
    *,
    burst: str,
    side: str,
    cooldown: int = COOLDOWN_BARS,
) -> list[EventCluster]:
    """Cluster contiguous True flags for burst_side; dedupe with cooldown after anchor."""
    key = f"{burst}_{side}"
    if key not in df.columns:
        return []
    flags = df[key].to_numpy(dtype=bool)
    liq_col = "long_liq_usd" if side == "long" else "short_liq_usd"
    liq = df[liq_col].to_numpy(dtype=float)
    seq = df["sequence_id"].to_numpy(dtype=int)
    n = len(df)

    # raw contiguous clusters within same sequence
    raw: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1] and seq[j + 1] == seq[i]:
            # also require contiguous 5m
            dt = (df["bucket_start"].iloc[j + 1] - df["bucket_start"].iloc[j]).total_seconds()
            if dt != 300:
                break
            j += 1
        raw.append((i, j))
        i = j + 1

    clusters: list[EventCluster] = []
    last_anchor = -10**9
    for a, b in raw:
        # skip if overlaps cooldown from previous accepted anchor
        members = list(range(a, b + 1))
        anchor = max(members, key=lambda k: liq[k] if np.isfinite(liq[k]) else -np.inf)
        if anchor - last_anchor <= cooldown:
            continue
        clusters.append(
            EventCluster(
                symbol=str(df["symbol"].iloc[anchor]),
                side=side,
                burst=burst,
                sequence_id=int(seq[anchor]),
                start_i=a,
                end_i=b,
                anchor_i=anchor,
                anchor_liq=float(liq[anchor]),
                member_indices=members,
            )
        )
        last_anchor = anchor
    return clusters


def clusters_to_rows(clusters: list[EventCluster], df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for c in clusters:
        rows.append(
            {
                "symbol": c.symbol,
                "side": c.side,
                "burst": c.burst,
                "sequence_id": c.sequence_id,
                "cluster_start": str(df["bucket_start"].iloc[c.start_i]),
                "cluster_end": str(df["bucket_start"].iloc[c.end_i]),
                "anchor_bucket": str(df["bucket_start"].iloc[c.anchor_i]),
                "anchor_index": c.anchor_i,
                "anchor_liq_usd": c.anchor_liq,
                "n_members": len(c.member_indices),
            }
        )
    return rows
