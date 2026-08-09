"""Streak statistics for TRUE_SL and NON_WINNER."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _streaks(reasons: list[str], *, count_as: set[str]) -> list[dict[str, Any]]:
    """Return list of streaks with start_idx, end_idx, length (0-based into reasons)."""
    out = []
    i = 0
    n = len(reasons)
    while i < n:
        if reasons[i] not in count_as:
            i += 1
            continue
        j = i
        while j < n and reasons[j] in count_as:
            j += 1
        out.append({"start_i": i, "end_i": j - 1, "length": j - i})
        i = j
    return out


def streak_summary(reasons: list[str], *, count_as: set[str], label: str) -> dict[str, Any]:
    streaks = _streaks(reasons, count_as=count_as)
    lengths = [s["length"] for s in streaks] if streaks else [0]
    arr = np.array(lengths, dtype=int) if streaks else np.array([0])
    # distribution counts for length k among streaks (and also singles)
    dist = {}
    for L in range(1, int(arr.max()) + 1 if streaks else 2):
        dist[L] = int((arr == L).sum()) if streaks else 0

    def n_ge(k: int) -> int:
        return int(sum(1 for L in lengths if L >= k)) if streaks else 0

    sorted_desc = sorted(streaks, key=lambda s: s["length"], reverse=True)
    return {
        "label": label,
        "n_streaks": int(len(streaks)),
        "max_streak": int(arr.max()) if streaks else 0,
        "second_max": int(sorted_desc[1]["length"]) if len(sorted_desc) > 1 else 0,
        "third_max": int(sorted_desc[2]["length"]) if len(sorted_desc) > 2 else 0,
        "mean_streak": float(arr.mean()) if streaks else 0.0,
        "median_streak": float(np.median(arr)) if streaks else 0.0,
        "n_ge_2": n_ge(2),
        "n_ge_3": n_ge(3),
        "n_ge_4": n_ge(4),
        "n_ge_5": n_ge(5),
        "n_ge_6": n_ge(6),
        "n_ge_7": n_ge(7),
        "n_ge_8": n_ge(8),
        "n_ge_10": n_ge(10),
        "distribution": dist,
        "top_streaks": sorted_desc[:10],
    }


def distribution_frame(base_dist: dict, be_dist: dict) -> pd.DataFrame:
    keys = sorted(set(base_dist) | set(be_dist))
    return pd.DataFrame(
        {
            "streak_length": keys,
            "baseline_count": [base_dist.get(k, 0) for k in keys],
            "be50_count": [be_dist.get(k, 0) for k in keys],
        }
    )


def detail_top_streaks(
    cmp: pd.DataFrame,
    base_reasons: list[str],
    be_reasons: list[str],
    top: list[dict[str, Any]],
) -> pd.DataFrame:
    rows = []
    for rank, s in enumerate(top, start=1):
        a, b = s["start_i"], s["end_i"]
        sub = cmp.iloc[a : b + 1]
        base_seq = "-".join(base_reasons[a : b + 1])
        be_seq = "-".join(be_reasons[a : b + 1])
        # BE50 true SL length within same trade window
        be_sl_streaks = _streaks(be_reasons[a : b + 1], count_as={"SL"})
        be_max_sl = max((x["length"] for x in be_sl_streaks), default=0)
        sl_to_be = int((sub["change_class"] == "SL_TO_BE").sum())
        rows.append(
            {
                "rank": rank,
                "start_i": a,
                "end_i": b,
                "start_trade_id": int(sub.iloc[0]["trade_id"]),
                "end_trade_id": int(sub.iloc[-1]["trade_id"]),
                "start_time": sub.iloc[0]["entry_time"],
                "end_time": sub.iloc[-1]["exit_time_baseline"],
                "baseline_length": s["length"],
                "baseline_seq": base_seq,
                "be50_seq": be_seq,
                "be50_max_true_sl_in_window": be_max_sl,
                "sl_to_be": sl_to_be,
                "trades": int(len(sub)),
                "symbols": ",".join(sorted(sub["symbol"].unique())),
                "side_mix": ";".join(f"{k}:{int(v)}" for k, v in sub["side"].value_counts().items()),
                "baseline_cum_net": float(sub["baseline_net_pct"].sum()),
                "be50_cum_net": float(sub["be50_net_pct"].sum()),
            }
        )
    return pd.DataFrame(rows)
