"""Interval timeline, idle-fill, near-simultaneous entries."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _to_ns(ts: pd.Series) -> np.ndarray:
    t = pd.to_datetime(ts, utc=True)
    return t.astype("int64").to_numpy()


def trade_intervals(df: pd.DataFrame) -> list[tuple[int, int, int]]:
    """(start_ns, end_ns, trade_id) half-open [start, end)."""
    if df.empty:
        return []
    s = _to_ns(df["entry_time"])
    e = _to_ns(df["exit_time"])
    ids = df["trade_id"].astype(int).to_numpy()
    out = []
    for a, b, i in zip(s, e, ids):
        if b > a:
            out.append((int(a), int(b), int(i)))
    return out


def single_coin_stats(df: pd.DataFrame, *, label: str, span_start: pd.Timestamp, span_end: pd.Timestamp) -> dict[str, Any]:
    span_start = pd.Timestamp(span_start).tz_convert("UTC")
    span_end = pd.Timestamp(span_end).tz_convert("UTC")
    total_min = (span_end - span_start).total_seconds() / 60.0
    if df.empty:
        return {
            "label": label,
            "trades": 0,
            "total_span_days": total_min / 60.0 / 24.0,
            "time_in_market_pct": 0.0,
            "flat_idle_pct": 100.0,
            "mean_idle_hours": None,
            "median_idle_hours": None,
            "p90_idle_hours": None,
            "max_idle_hours": None,
            "mean_hold_hours": None,
            "median_hold_hours": None,
            "net_pnl_additive": 0.0,
            "net_pnl_per_day": 0.0,
            "trades_per_day": 0.0,
        }
    g = df.sort_values("entry_time").reset_index(drop=True)
    hold = (g["exit_time"] - g["entry_time"]).dt.total_seconds() / 60.0
    tim = float(hold.sum())
    # idle gaps within span: before first, between, after last
    idle_parts = []
    first_entry = g["entry_time"].iloc[0]
    last_exit = g["exit_time"].iloc[-1]
    if first_entry > span_start:
        idle_parts.append((first_entry - span_start).total_seconds() / 60.0)
    if len(g) >= 2:
        gaps = (g["entry_time"].iloc[1:].reset_index(drop=True) - g["exit_time"].iloc[:-1].reset_index(drop=True)).dt.total_seconds() / 60.0
        idle_parts.extend(gaps.astype(float).tolist())
    if last_exit < span_end:
        idle_parts.append((span_end - last_exit).total_seconds() / 60.0)
    idle_arr = np.array(idle_parts, dtype=float) if idle_parts else np.array([0.0])
    # inter-trade gaps only (like prior idle analysis) for mean/median comparable
    if len(g) >= 2:
        inter = (
            g["entry_time"].iloc[1:].reset_index(drop=True)
            - g["exit_time"].iloc[:-1].reset_index(drop=True)
        ).dt.total_seconds() / 60.0
        inter = inter.astype(float).to_numpy()
    else:
        inter = np.array([], dtype=float)

    nets = g["net_return_pct"].astype(float)
    days = total_min / 60.0 / 24.0
    return {
        "label": label,
        "trades": int(len(g)),
        "total_span_days": float(days),
        "time_in_market_pct": float(100.0 * tim / total_min) if total_min else None,
        "flat_idle_pct": float(100.0 * (total_min - tim) / total_min) if total_min else None,
        "mean_idle_hours": float(inter.mean() / 60.0) if len(inter) else None,
        "median_idle_hours": float(np.median(inter) / 60.0) if len(inter) else None,
        "p90_idle_hours": float(np.percentile(inter, 90) / 60.0) if len(inter) else None,
        "max_idle_hours": float(inter.max() / 60.0) if len(inter) else None,
        "mean_hold_hours": float(hold.mean() / 60.0),
        "median_hold_hours": float(hold.median() / 60.0),
        "net_pnl_additive": float(nets.sum()),
        "net_pnl_per_day": float(nets.sum() / days) if days else None,
        "trades_per_day": float(len(g) / days) if days else None,
    }


def timeline_state_stats(
    apt: pd.DataFrame,
    doge: pd.DataFrame,
    *,
    span_start: pd.Timestamp,
    span_end: pd.Timestamp,
) -> dict[str, Any]:
    """Sweep-line % time: APT only / DOGE only / both / neither."""
    t0 = int(pd.Timestamp(span_start).tz_convert("UTC").value)
    t1 = int(pd.Timestamp(span_end).tz_convert("UTC").value)
    events: list[tuple[int, int, str]] = []  # time, delta(+1/-1), symbol
    for a, b, _ in trade_intervals(apt):
        events.append((max(a, t0), +1, "APT"))
        events.append((min(b, t1), -1, "APT"))
    for a, b, _ in trade_intervals(doge):
        events.append((max(a, t0), +1, "DOGE"))
        events.append((min(b, t1), -1, "DOGE"))
    events = [(t, d, s) for t, d, s in events if t0 <= t <= t1]
    events.sort(key=lambda x: (x[0], x[1]))  # exits (-1) before entries (+1) at same ts for half-open

    apt_on = doge_on = 0
    cur = t0
    dur = {"apt_only": 0, "doge_only": 0, "both": 0, "neither": 0}

    def _bucket():
        if apt_on and doge_on:
            return "both"
        if apt_on:
            return "apt_only"
        if doge_on:
            return "doge_only"
        return "neither"

    i = 0
    while i < len(events):
        t = events[i][0]
        if t > cur:
            dur[_bucket()] += t - cur
            cur = t
        # apply all events at t
        while i < len(events) and events[i][0] == t:
            _, d, s = events[i]
            if s == "APT":
                apt_on += d
            else:
                doge_on += d
            i += 1
    if t1 > cur:
        dur[_bucket()] += t1 - cur

    total = sum(dur.values()) or 1
    ns_to_h = 1.0 / (1e9 * 3600)
    return {
        "span_hours": total * ns_to_h,
        "pct_apt_only": 100.0 * dur["apt_only"] / total,
        "pct_doge_only": 100.0 * dur["doge_only"] / total,
        "pct_both_active": 100.0 * dur["both"] / total,
        "pct_both_flat": 100.0 * dur["neither"] / total,
        "hours_apt_only": dur["apt_only"] * ns_to_h,
        "hours_doge_only": dur["doge_only"] * ns_to_h,
        "hours_both_active": dur["both"] * ns_to_h,
        "hours_both_flat": dur["neither"] * ns_to_h,
        "time_any_position_pct": 100.0 * (dur["apt_only"] + dur["doge_only"] + dur["both"]) / total,
    }


def idle_intervals(df: pd.DataFrame, span_start: pd.Timestamp, span_end: pd.Timestamp) -> list[tuple[int, int]]:
    t0 = int(pd.Timestamp(span_start).tz_convert("UTC").value)
    t1 = int(pd.Timestamp(span_end).tz_convert("UTC").value)
    iv = trade_intervals(df)
    if not iv:
        return [(t0, t1)]
    iv = sorted(iv, key=lambda x: x[0])
    out = []
    cur = t0
    for a, b, _ in iv:
        a2, b2 = max(a, t0), min(b, t1)
        if a2 > cur:
            out.append((cur, a2))
        cur = max(cur, b2)
    if cur < t1:
        out.append((cur, t1))
    return out


def active_intervals(df: pd.DataFrame) -> list[tuple[int, int]]:
    return [(a, b) for a, b, _ in trade_intervals(df)]


def intersect_duration(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """Total intersection length in ns."""
    i = j = 0
    total = 0
    aa = sorted(a)
    bb = sorted(b)
    while i < len(aa) and j < len(bb):
        a0, a1 = aa[i]
        b0, b1 = bb[j]
        lo, hi = max(a0, b0), min(a1, b1)
        if hi > lo:
            total += hi - lo
        if a1 < b1:
            i += 1
        else:
            j += 1
    return total


def idle_fill_stats(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    *,
    primary_label: str,
    secondary_label: str,
    span_start: pd.Timestamp,
    span_end: pd.Timestamp,
) -> dict[str, Any]:
    idle = idle_intervals(primary, span_start, span_end)
    active = active_intervals(secondary)
    idle_ns = sum(b - a for a, b in idle)
    filled_ns = intersect_duration(idle, active)
    span_ns = int(pd.Timestamp(span_end).tz_convert("UTC").value) - int(
        pd.Timestamp(span_start).tz_convert("UTC").value
    )
    primary_active_ns = sum(b - a for a, b in active_intervals(primary))
    # combined any-active
    # remaining flat of primary after fill = idle - filled
    remain_ns = idle_ns - filled_ns
    # combined TIM = primary active + filled portion of idle (secondary during primary idle)
    # Actually combined any position = union of both actives
    union_ns = primary_active_ns + sum(b - a for a, b in active) - intersect_duration(
        active_intervals(primary), active
    )
    return {
        "primary": primary_label,
        "secondary": secondary_label,
        "primary_idle_hours": idle_ns / 1e9 / 3600,
        "filled_by_secondary_hours": filled_ns / 1e9 / 3600,
        "remaining_primary_idle_hours": remain_ns / 1e9 / 3600,
        "idle_fill_ratio": float(filled_ns / idle_ns) if idle_ns else None,
        "primary_flat_pct_of_span": 100.0 * idle_ns / span_ns if span_ns else None,
        "filled_pct_of_span": 100.0 * filled_ns / span_ns if span_ns else None,
        "remaining_flat_pct_of_span": 100.0 * remain_ns / span_ns if span_ns else None,
        "combined_any_position_pct": 100.0 * union_ns / span_ns if span_ns else None,
    }


def entry_during_other_active(entries: pd.DataFrame, other_active: pd.DataFrame) -> int:
    if entries.empty or other_active.empty:
        return 0
    iv = active_intervals(other_active)
    n = 0
    for ts in _to_ns(entries["entry_time"]):
        for a, b in iv:
            if a <= ts < b:
                n += 1
                break
    return n


def near_simultaneous(apt: pd.DataFrame, doge: pd.DataFrame) -> pd.DataFrame:
    """For each APT entry, nearest DOGE entry delta; and vice versa unique pairs."""
    if apt.empty or doge.empty:
        return pd.DataFrame()
    ae = _to_ns(apt["entry_time"])
    de = _to_ns(doge["entry_time"])
    rows = []
    for i, t in enumerate(ae):
        j = int(np.searchsorted(de, t))
        cands = []
        if j < len(de):
            cands.append(j)
        if j > 0:
            cands.append(j - 1)
        best = min(cands, key=lambda k: abs(de[k] - t))
        dt_min = abs(de[best] - t) / 1e9 / 60.0
        rows.append(
            {
                "ref_symbol": "APTUSDT",
                "ref_trade_id": int(apt.iloc[i]["trade_id"]),
                "ref_entry_time": apt.iloc[i]["entry_time"],
                "ref_side": apt.iloc[i]["side"],
                "other_symbol": "DOGEUSDT",
                "other_trade_id": int(doge.iloc[best]["trade_id"]),
                "other_entry_time": doge.iloc[best]["entry_time"],
                "other_side": doge.iloc[best]["side"],
                "abs_delta_minutes": float(dt_min),
                "same_direction": str(apt.iloc[i]["side"]) == str(doge.iloc[best]["side"]),
            }
        )
    return pd.DataFrame(rows)


def near_sim_buckets(near: pd.DataFrame) -> pd.DataFrame:
    if near.empty:
        return pd.DataFrame(columns=["bucket", "n", "share_pct", "same_direction_pct"])
    thresholds = [
        ("<=1min", 1.0),
        ("<=5min", 5.0),
        ("<=15min", 15.0),
        ("<=30min", 30.0),
        ("<=60min", 60.0),
    ]
    n = len(near)
    rows = []
    for label, thr in thresholds:
        m = near["abs_delta_minutes"] <= thr
        sub = near.loc[m]
        rows.append(
            {
                "bucket": label,
                "n": int(m.sum()),
                "share_pct_of_apt_entries": float(100.0 * m.mean()),
                "same_direction_pct": float(100.0 * sub["same_direction"].mean()) if len(sub) else None,
            }
        )
    return pd.DataFrame(rows)
