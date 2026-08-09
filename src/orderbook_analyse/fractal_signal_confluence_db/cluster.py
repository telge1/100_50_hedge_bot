"""A-priori confluence clustering + conflict detection."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db import (
    PAIR_WINDOW_MIN,
    SIGNAL_TFS,
    TF_BAR_MIN,
    TF_RANK,
)


def pair_window(tf_a: str, tf_b: str) -> int:
    a, b = sorted([tf_a, tf_b], key=lambda x: TF_RANK[x])
    base = PAIR_WINDOW_MIN.get((a, b))
    if base is None:
        # fallback larger TF bar
        base = TF_BAR_MIN[b]
    # sensitivity: max(base, 1 bar of larger TF)
    return int(max(base, TF_BAR_MIN[b]))


def combo_label(tfs: list[str]) -> str:
    s = sorted(set(tfs), key=lambda x: TF_RANK[x])
    if len(s) == 1:
        return f"{s[0]}_only"
    return "+".join(s)


def confluence_class(tf_count: int) -> str:
    if tf_count <= 1:
        return "SINGLE"
    if tf_count == 2:
        return "DOUBLE"
    if tf_count == 3:
        return "TRIPLE"
    return "QUAD"


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def build_same_side_clusters(signals: pd.DataFrame) -> list[dict[str, Any]]:
    """Union-find clusters of same-side signals linked by pair windows."""
    if signals.empty:
        return []
    df = signals.sort_values("confirmation_available_at").reset_index(drop=True)
    n = len(df)
    times = pd.to_datetime(df["confirmation_available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    sides = df["side"].astype(str).to_numpy()
    tfs = df["signal_tf"].astype(str).to_numpy()
    uf = _UF(n)

    # For each signal, scan forward within max window (240m + 4h bar = 480)
    max_look = np.timedelta64(480, "m")
    for i in range(n):
        t_lim = times[i] + max_look
        j = i + 1
        while j < n and times[j] <= t_lim:
            if sides[j] == sides[i]:
                w = pair_window(tfs[i], tfs[j])
                dt_min = float((times[j] - times[i]) / np.timedelta64(1, "m"))
                if dt_min <= w:
                    uf.union(i, j)
            j += 1

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    clusters = []
    for _, idxs in groups.items():
        sub = df.iloc[idxs].sort_values("confirmation_available_at")
        tfs_u = sorted(sub["signal_tf"].astype(str).unique(), key=lambda x: TF_RANK[x])
        first = sub.iloc[0]
        last = sub.iloc[-1]
        highest = tfs_u[-1]
        lowest = tfs_u[0]
        clusters.append(
            {
                "side": str(first["side"]),
                "cluster_start": pd.Timestamp(first["confirmation_available_at"]),
                "cluster_end": pd.Timestamp(last["confirmation_available_at"]),
                "participating_tfs": tfs_u,
                "tf_count": len(tfs_u),
                "confluence_class": confluence_class(len(tfs_u)),
                "combo": combo_label(tfs_u),
                "highest_tf": highest,
                "lowest_tf": lowest,
                "tier_a_count": int(sub["is_tier_a"].astype(bool).sum()),
                "q4_count": int(sub["is_q4"].astype(bool).sum()),
                "first_signal_tf": str(first["signal_tf"]),
                "last_signal_tf": str(last["signal_tf"]),
                "n_raw_signals": int(len(sub)),
                "signal_indices": idxs,
                "rows": sub,
            }
        )
    clusters.sort(key=lambda c: c["cluster_start"])
    return clusters


def detect_conflicts(signals: pd.DataFrame) -> list[dict[str, Any]]:
    """Opposite-side pairs within pair window (higher vs lower TF)."""
    if signals.empty:
        return []
    df = signals.sort_values("confirmation_available_at").reset_index(drop=True)
    n = len(df)
    times = pd.to_datetime(df["confirmation_available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    sides = df["side"].astype(str).to_numpy()
    tfs = df["signal_tf"].astype(str).to_numpy()
    conflicts = []
    max_look = np.timedelta64(480, "m")
    seen = set()
    for i in range(n):
        t_lim = times[i] + max_look
        j = i + 1
        while j < n and times[j] <= t_lim:
            if sides[j] != sides[i]:
                w = pair_window(tfs[i], tfs[j])
                dt = float((times[j] - times[i]) / np.timedelta64(1, "m"))
                if dt <= w:
                    # order by TF rank
                    if TF_RANK[tfs[i]] >= TF_RANK[tfs[j]]:
                        hi, lo = i, j
                    else:
                        hi, lo = j, i
                    key = (min(i, j), max(i, j))
                    if key in seen:
                        j += 1
                        continue
                    seen.add(key)
                    hi_side = sides[hi]
                    lo_side = sides[lo]
                    if hi_side == "SHORT" and lo_side == "LONG":
                        ctype = "HIGHER_TF_SHORT_LOWER_LONG"
                    else:
                        ctype = "HIGHER_TF_LONG_LOWER_SHORT"
                    conflicts.append(
                        {
                            "conflict_type": ctype,
                            "highest_tf": tfs[hi],
                            "lower_tf": tfs[lo],
                            "highest_side": hi_side,
                            "lower_side": lo_side,
                            "dt_min": dt,
                            "higher_tier_a": bool(df.iloc[hi]["is_tier_a"]),
                            "lower_tier_a": bool(df.iloc[lo]["is_tier_a"]),
                            "higher_row": df.iloc[hi],
                            "lower_row": df.iloc[lo],
                            "t_higher": pd.Timestamp(times[hi]),
                            "t_lower": pd.Timestamp(times[lo]),
                        }
                    )
            j += 1
    return conflicts
