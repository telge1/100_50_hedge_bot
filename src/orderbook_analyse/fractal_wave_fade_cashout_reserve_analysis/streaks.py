"""SL / losing streak and worst trade-block analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def find_streaks(
    trades: pd.DataFrame,
    *,
    predicate,
    label: str,
) -> dict[str, Any]:
    """Find consecutive streaks where predicate(row) is True; broken when False."""
    df = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    streaks = []
    start = None
    for i, row in df.iterrows():
        ok = bool(predicate(row))
        if ok:
            if start is None:
                start = i
        else:
            if start is not None:
                streaks.append((start, i - 1))
                start = None
    if start is not None:
        streaks.append((start, len(df) - 1))

    dist: dict[int, int] = {}
    for a, b in streaks:
        L = b - a + 1
        key = L if L < 10 else 10  # 10+
        dist[key] = dist.get(key, 0) + 1

    dist_rows = []
    for L in range(1, 10):
        dist_rows.append({"streak_length": L, "occurrences": int(dist.get(L, 0)), "kind": label})
    dist_rows.append({"streak_length": "10+", "occurrences": int(dist.get(10, 0)), "kind": label})

    if not streaks:
        return {
            "max_length": 0,
            "worst": None,
            "distribution": dist_rows,
            "all_streaks": [],
        }

    # worst = longest; tie-break by most negative sum net
    def score(ab):
        a, b = ab
        seg = df.iloc[a : b + 1]
        return (b - a + 1, -float(seg["net_return_pct"].sum()))

    a, b = max(streaks, key=score)
    seg = df.iloc[a : b + 1]
    worst = {
        "kind": label,
        "length": int(b - a + 1),
        "start_trade_id": int(seg.iloc[0]["trade_id"]),
        "end_trade_id": int(seg.iloc[-1]["trade_id"]),
        "start_time": pd.Timestamp(seg.iloc[0]["exit_time"]).isoformat(),
        "end_time": pd.Timestamp(seg.iloc[-1]["exit_time"]).isoformat(),
        "symbols": ",".join(sorted(seg["symbol"].astype(str).unique())),
        "sides": ",".join(sorted(seg["side"].astype(str).unique())),
        "first_signal_tfs": ",".join(sorted(seg["first_signal_tf"].astype(str).unique())),
        "sum_net_return_pct": float(seg["net_return_pct"].sum()),
        "mean_net_return_pct": float(seg["net_return_pct"].mean()),
        "trade_ids": seg["trade_id"].astype(int).tolist(),
        "start_i": int(a),
        "end_i": int(b),
    }
    all_s = []
    for a0, b0 in streaks:
        s = df.iloc[a0 : b0 + 1]
        all_s.append(
            {
                "kind": label,
                "length": int(b0 - a0 + 1),
                "start_trade_id": int(s.iloc[0]["trade_id"]),
                "end_trade_id": int(s.iloc[-1]["trade_id"]),
                "start_time": pd.Timestamp(s.iloc[0]["exit_time"]).isoformat(),
                "end_time": pd.Timestamp(s.iloc[-1]["exit_time"]).isoformat(),
                "sum_net_return_pct": float(s["net_return_pct"].sum()),
            }
        )
    return {
        "max_length": int(worst["length"]),
        "worst": worst,
        "distribution": dist_rows,
        "all_streaks": all_s,
    }


def sl_predicate(row: pd.Series) -> bool:
    return str(row["exit_reason"]) == "SL"


def losing_predicate(row: pd.Series) -> bool:
    return float(row["net_return_pct"]) < 0


def subset_max_sl(trades: pd.DataFrame, mask: pd.Series) -> int:
    sub = trades.loc[mask].sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    return find_streaks(sub, predicate=sl_predicate, label="SL").get("max_length", 0)


def worst_blocks(trades: pd.DataFrame, windows: tuple[int, ...] = (5, 10, 20, 50)) -> pd.DataFrame:
    df = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    nets = df["net_return_pct"].astype(float).to_numpy()
    rows = []
    for w in windows:
        if len(df) < w:
            continue
        roll = np.convolve(nets, np.ones(w), mode="valid")
        i = int(np.argmin(roll))
        seg = df.iloc[i : i + w]
        rows.append(
            {
                "block_size": w,
                "start_trade_id": int(seg.iloc[0]["trade_id"]),
                "end_trade_id": int(seg.iloc[-1]["trade_id"]),
                "start_time": pd.Timestamp(seg.iloc[0]["exit_time"]).isoformat(),
                "end_time": pd.Timestamp(seg.iloc[-1]["exit_time"]).isoformat(),
                "sum_net_return_pct": float(roll[i]),
                "tp_count": int((seg["exit_reason"] == "TP").sum()),
                "sl_count": int((seg["exit_reason"] == "SL").sum()),
                "timeout_count": int((seg["exit_reason"] == "TIMEOUT").sum()),
                "conflict_count": int((seg["exit_reason"] == "HIGHER_TF_CONFLICT").sum()),
                "symbol_mix": ",".join(f"{k}:{v}" for k, v in seg["symbol"].value_counts().items()),
                "side_mix": ",".join(f"{k}:{v}" for k, v in seg["side"].value_counts().items()),
                "first_tf_mix": ",".join(
                    f"{k}:{v}" for k, v in seg["first_signal_tf"].value_counts().items()
                ),
            }
        )
    return pd.DataFrame(rows)


def streak_impact_on_paths(
    trades: pd.DataFrame,
    worst: dict[str, Any],
    paths_by_rate: dict[float, pd.DataFrame],
) -> pd.DataFrame:
    """Active/reserve before/after worst SL streak for each cashout rate."""
    if not worst:
        return pd.DataFrame()
    ids = set(worst["trade_ids"])
    rows = []
    for rate, path in paths_by_rate.items():
        p = path.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
        mask = p["trade_id"].isin(ids)
        seg = p.loc[mask]
        if seg.empty:
            continue
        first = seg.iloc[0]
        last = seg.iloc[-1]
        # before = state before first streak trade
        before_a = float(first["active_before"])
        before_r = float(first["reserve_before"])
        after_a = float(last["active_after"])
        after_r = float(last["reserve_after"])
        peak = before_a
        trough = after_a
        dd_pct = (trough / peak - 1.0) * 100.0 if peak > 0 else 0.0
        rows.append(
            {
                "cashout_rate": rate,
                "cashout_rate_pct": int(round(rate * 100)),
                "streak_start_trade_id": int(worst["start_trade_id"]),
                "streak_end_trade_id": int(worst["end_trade_id"]),
                "streak_length": int(worst["length"]),
                "active_equity_before": before_a,
                "reserve_before": before_r,
                "total_wealth_before": before_a + before_r,
                "active_equity_after": after_a,
                "reserve_after": after_r,
                "total_wealth_after": after_a + after_r,
                "active_change": after_a - before_a,
                "total_change": (after_a + after_r) - (before_a + before_r),
                "drawdown_pct_on_active": dd_pct,
                "sum_net_return_pct": float(worst["sum_net_return_pct"]),
            }
        )
    return pd.DataFrame(rows)
