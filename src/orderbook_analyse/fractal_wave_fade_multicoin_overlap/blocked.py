"""Blocked-trade residual hold buckets."""

from __future__ import annotations

import pandas as pd


def blocked_hold_buckets(blocked: pd.DataFrame) -> pd.DataFrame:
    if blocked.empty or "remaining_hold_minutes_at_block" not in blocked.columns:
        return pd.DataFrame(columns=["bucket", "n", "share_pct", "mean_blocked_net"])
    m = blocked["remaining_hold_minutes_at_block"].astype(float)
    nets = blocked["net_return_pct"].astype(float)
    bins = [
        ("<15m", m < 15),
        ("15-30m", (m >= 15) & (m < 30)),
        ("30-60m", (m >= 30) & (m < 60)),
        ("1-3h", (m >= 60) & (m < 180)),
        (">3h", m >= 180),
    ]
    n = len(blocked)
    rows = []
    for label, mask in bins:
        sub_n = int(mask.sum())
        rows.append(
            {
                "bucket": label,
                "n": sub_n,
                "share_pct": float(100.0 * sub_n / n) if n else None,
                "mean_blocked_net": float(nets[mask].mean()) if sub_n else None,
                "sum_blocked_net": float(nets[mask].sum()) if sub_n else 0.0,
            }
        )
    return pd.DataFrame(rows)


def blocked_direction_mix(blocked: pd.DataFrame) -> dict:
    if blocked.empty:
        return {}
    same = (blocked["side"] == blocked["open_side"]).sum()
    opp = (blocked["side"] != blocked["open_side"]).sum()
    n = len(blocked)
    return {
        "n_blocked": int(n),
        "same_direction_as_open": int(same),
        "opposite_direction_as_open": int(opp),
        "same_direction_pct": float(100.0 * same / n) if n else None,
        "sum_blocked_net_pnl": float(blocked["net_return_pct"].astype(float).sum()),
        "mean_blocked_net_pnl": float(blocked["net_return_pct"].astype(float).mean()),
    }
