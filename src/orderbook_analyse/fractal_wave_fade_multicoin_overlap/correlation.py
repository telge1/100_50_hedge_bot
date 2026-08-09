"""Signal coincidence / direction agreement."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_multicoin_overlap.intervals import _to_ns


def signal_correlation(apt: pd.DataFrame, doge: pd.DataFrame) -> pd.DataFrame:
    buckets = [5, 15, 30, 60]
    if apt.empty or doge.empty:
        return pd.DataFrame()
    ae = _to_ns(apt["entry_time"])
    de = _to_ns(doge["entry_time"])
    asides = apt["side"].astype(str).to_numpy()
    dsides = doge["side"].astype(str).to_numpy()

    rows = []
    for thr_min in buckets:
        thr = thr_min * 60 * 1e9
        # APT entries with ≥1 DOGE entry within thr
        apt_hit = 0
        ll = ls = sl = ss = 0
        matched_pairs = 0
        for i, t in enumerate(ae):
            j0 = int(np.searchsorted(de, t - thr, side="left"))
            j1 = int(np.searchsorted(de, t + thr, side="right"))
            if j1 > j0:
                apt_hit += 1
                # nearest for direction
                j = int(np.searchsorted(de, t))
                cands = [k for k in (j - 1, j) if 0 <= k < len(de) and abs(de[k] - t) <= thr]
                if not cands:
                    continue
                best = min(cands, key=lambda k: abs(de[k] - t))
                matched_pairs += 1
                a, d = asides[i], dsides[best]
                if a == "LONG" and d == "LONG":
                    ll += 1
                elif a == "LONG" and d == "SHORT":
                    ls += 1
                elif a == "SHORT" and d == "LONG":
                    sl += 1
                else:
                    ss += 1

        doge_hit = 0
        for t in de:
            j0 = int(np.searchsorted(ae, t - thr, side="left"))
            j1 = int(np.searchsorted(ae, t + thr, side="right"))
            if j1 > j0:
                doge_hit += 1

        # Jaccard-like: |pairs near| / (|A|+|D| - approx)
        # use: apt_hit / n_apt as coincidence from APT view
        agree = ll + ss
        disagree = ls + sl
        rows.append(
            {
                "bucket_minutes": thr_min,
                "apt_entries": int(len(apt)),
                "doge_entries": int(len(doge)),
                "apt_with_nearby_doge": apt_hit,
                "apt_coincidence_pct": float(100.0 * apt_hit / len(apt)),
                "doge_with_nearby_apt": doge_hit,
                "doge_coincidence_pct": float(100.0 * doge_hit / len(doge)),
                "direction_pairs": matched_pairs,
                "long_long": ll,
                "short_short": ss,
                "long_short": ls,
                "short_long": sl,
                "same_direction_pct": float(100.0 * agree / matched_pairs) if matched_pairs else None,
                "opposite_direction_pct": float(100.0 * disagree / matched_pairs) if matched_pairs else None,
                "jaccard_proxy": float(apt_hit / (len(apt) + len(doge) - apt_hit)) if (len(apt) + len(doge) - apt_hit) else None,
            }
        )
    return pd.DataFrame(rows)
