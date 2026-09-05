"""Independent episode clustering for overlapping same-side pools."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float, gap_frac: float) -> bool:
    if a_hi < b_lo:
        gap = b_lo - a_hi
    elif b_hi < a_lo:
        gap = a_lo - b_hi
    else:
        return True
    mid = (a_lo + a_hi + b_lo + b_hi) / 4.0
    if mid <= 0:
        return False
    return (gap / mid) <= gap_frac


def assign_episodes(
    df: pd.DataFrame,
    *,
    gap_pct: float = 0.001,  # 0.10% as fraction
    time_proximity_bars_max: int = 12,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Union-find episodes within symbol×side×timeframe by price overlap + time proximity.

    Uses known_at order. Shared first-touch bar forces same episode.
    """
    out = df.copy()
    out["episode_id"] = None
    episode_rows: list[dict[str, Any]] = []

    for (sym, side, tf), g in out.groupby(["symbol", "side", "timeframe"], sort=False):
        idx = list(g.index)
        n = len(idx)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        rows = g.loc[idx]
        lowers = rows["lower_price"].to_numpy(float)
        uppers = rows["upper_price"].to_numpy(float)
        known = pd.to_datetime(rows["known_at_ts"]).to_numpy()
        touch_i = rows["first_touch_index"].to_numpy()

        for i in range(n):
            for j in range(i + 1, n):
                # time proximity on known_at (bar-equivalent via minutes rough: use index if both have analysis)
                dt = abs((known[i] - known[j]) / np.timedelta64(1, "m"))
                # map TF to minutes
                tfm = 5 if str(tf).startswith("5") else 15 if str(tf).startswith("15") else 30
                bar_dt = dt / tfm
                same_touch = (
                    not (pd.isna(touch_i[i]) or pd.isna(touch_i[j]))
                    and int(touch_i[i]) == int(touch_i[j])
                )
                if same_touch or (
                    bar_dt <= time_proximity_bars_max
                    and _overlap(lowers[i], uppers[i], lowers[j], uppers[j], gap_pct)
                ):
                    union(i, j)

        roots: dict[int, list[int]] = {}
        for i in range(n):
            r = find(i)
            roots.setdefault(r, []).append(i)

        for r, members in roots.items():
            member_idx = [idx[i] for i in members]
            sub = out.loc[member_idx]
            eid = f"ep:{sym}:{tf}:{side}:{int(pd.Timestamp(sub['known_at_ts'].min()).timestamp())}:{len(members)}"
            out.loc[member_idx, "episode_id"] = eid
            # episode-level outcome: any defended / all swept etc. — use first-touch leader (earliest known)
            leader = sub.sort_values("known_at_ts").iloc[0]
            episode_rows.append(
                {
                    "episode_id": eid,
                    "symbol": sym,
                    "timeframe": tf,
                    "side": side,
                    "n_members": len(members),
                    "member_ids": "|".join(sub["entity_id"].astype(str)),
                    "known_at": str(sub["known_at_ts"].min()),
                    "utc_day": leader.get("utc_day"),
                    "n_components_max": int(sub["n_components"].max()),
                    "component_bucket_max": str(sub.loc[sub["n_components"].idxmax(), "component_bucket"]),
                    "distance_atr_min": float(sub["distance_from_price_atr"].abs().min())
                    if sub["distance_from_price_atr"].notna().any()
                    else None,
                    "touched": bool(sub["touched"].astype(bool).any()),
                    "swept": bool(sub["swept"].astype(bool).any()),
                    "defended": bool(sub["defended"].astype(bool).any())
                    and not bool(sub["swept"].astype(bool).all()),
                    "swept_reclaimed": bool(sub["swept_reclaimed"].astype(bool).any()),
                    "consumed_accepted": bool(sub["consumed_accepted"].astype(bool).any()),
                    "multi_6plus": bool((sub["n_components"] >= 6).any()),
                    "multi_pool": bool((sub["n_components"] >= 2).any()),
                    "overlaps_ema200": bool(sub.get("overlaps_ema200", False).astype(bool).any())
                    if "overlaps_ema200" in sub
                    else False,
                    "bullish_stack": bool(sub.get("bullish_stack", False).astype(bool).any())
                    if "bullish_stack" in sub
                    else False,
                    "bearish_stack": bool(sub.get("bearish_stack", False).astype(bool).any())
                    if "bearish_stack" in sub
                    else False,
                    "touch_timing": "immediate_touch"
                    if (sub["touch_timing"] == "immediate_touch").any()
                    else (
                        "delayed_touch"
                        if (sub["touch_timing"] == "delayed_touch").any()
                        else "untouched"
                    ),
                    "temporal_split": leader.get("temporal_split"),
                    "approach_regime": leader.get("approach_regime"),
                }
            )

    return out, pd.DataFrame(episode_rows)
