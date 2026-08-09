"""Equity / drawdown / streak / period metrics for strategy backtest."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import START_EQUITY, UNIT_SIZE


def trades_frame(trades: list[dict[str, Any]]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    df = df.sort_values(["exit_time", "entry_time"]).reset_index(drop=True)
    return df


def summarize_trades(df: pd.DataFrame, **meta) -> dict[str, Any]:
    row: dict[str, Any] = {**meta}
    if df is None or df.empty:
        row.update({"trades": 0, "n": 0})
        return row
    nets = df["net_return"].astype(float).to_numpy()
    n = len(nets)
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    be = int(np.sum(np.abs(nets) <= 1e-12))
    eq = START_EQUITY + np.cumsum(nets) * UNIT_SIZE
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min()) if len(dd) else 0.0
    max_dd_pct = float((dd / peak).min() * 100.0) if len(dd) and np.all(peak > 0) else None
    row.update(
        {
            "trades": n,
            "n": n,
            "wins": int(len(wins)),
            "losses": int(len(losses)),
            "breakeven": be,
            "win_rate": float(np.mean(nets > 0)),
            "mean_net_return": float(np.mean(nets)),
            "median_net_return": float(np.median(nets)),
            "expectancy": float(np.mean(nets)),
            "average_winner": float(np.mean(wins)) if len(wins) else None,
            "average_loser": float(np.mean(losses)) if len(losses) else None,
            "payoff_ratio": (
                float(np.mean(wins) / abs(np.mean(losses)))
                if len(wins) and len(losses) and np.mean(losses) != 0
                else None
            ),
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "cumulative_net": float(np.sum(nets)),
            "final_equity": float(eq[-1]),
            "peak_equity": float(peak.max()),
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "recovery_factor": (
                float(np.sum(nets) / abs(max_dd)) if max_dd < 0 else None
            ),
            "median_hold_min": float(df["holding_time_min"].median()),
            "mean_hold_min": float(df["holding_time_min"].mean()),
            "tp_count": int((df["exit_reason"] == "TP").sum()),
            "sl_count": int((df["exit_reason"] == "SL").sum()),
            "conflict_count": int((df["exit_reason"] == "HIGHER_TF_CONFLICT").sum()),
            "timeout_count": int((df["exit_reason"] == "TIMEOUT").sum()),
            "end_of_data_count": int((df["exit_reason"] == "END_OF_DATA").sum()),
            "ambiguous_sl_first": int(df.get("ambiguous_sl_first", pd.Series(dtype=bool)).astype(bool).sum())
            if "ambiguous_sl_first" in df.columns
            else 0,
            "upgrade_rate": float((df["number_of_upgrades"] > 0).mean()),
        }
    )
    # streaks
    max_w = max_l = cur_w = cur_l = 0
    lose_streaks = []
    for x in nets:
        if x > 0:
            cur_w += 1
            if cur_l:
                lose_streaks.append(cur_l)
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif x < 0:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)
        else:
            cur_w = cur_l = 0
    if cur_l:
        lose_streaks.append(cur_l)
    row["max_consecutive_wins"] = int(max_w)
    row["max_consecutive_losses"] = int(max_l)
    row["median_losing_streak"] = float(np.median(lose_streaks)) if lose_streaks else 0.0
    for w in (10, 20):
        if n >= w:
            roll = np.convolve(nets, np.ones(w), mode="valid")
            row[f"worst_rolling_{w}_trade_return"] = float(roll.min())
        else:
            row[f"worst_rolling_{w}_trade_return"] = None
    return row


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["exit_time", "net_return", "equity", "drawdown", "drawdown_pct"])
    nets = df["net_return"].astype(float).to_numpy()
    eq = START_EQUITY + np.cumsum(nets) * UNIT_SIZE
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return pd.DataFrame(
        {
            "exit_time": df["exit_time"].values,
            "symbol": df["symbol"].values if "symbol" in df.columns else None,
            "net_return": nets,
            "equity": eq,
            "drawdown": dd,
            "drawdown_pct": np.where(peak > 0, dd / peak * 100.0, np.nan),
        }
    )


def drawdown_episodes(eq_df: pd.DataFrame, top_n: int = 5) -> list[dict[str, Any]]:
    if eq_df.empty:
        return []
    eq = eq_df["equity"].to_numpy(dtype=float)
    times = pd.to_datetime(eq_df["exit_time"], utc=True)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    episodes = []
    in_dd = False
    start = 0
    peak_i = 0
    for i in range(len(eq)):
        if dd[i] < 0 and not in_dd:
            in_dd = True
            start = i
            # peak index = last time at peak before dd
            peak_i = i
            while peak_i > 0 and peak[peak_i] == peak[i]:
                if eq[peak_i] == peak[i]:
                    break
                peak_i -= 1
        if in_dd and (dd[i] == 0 or i == len(eq) - 1):
            end = i
            trough = start + int(np.argmin(dd[start : end + 1]))
            depth = float(dd[trough])
            dur = (times.iloc[end] - times.iloc[start]).total_seconds() / 86400.0
            recovered = bool(dd[end] == 0)
            ttr = (
                (times.iloc[end] - times.iloc[trough]).total_seconds() / 86400.0
                if recovered
                else None
            )
            episodes.append(
                {
                    "start": times.iloc[start],
                    "trough": times.iloc[trough],
                    "end": times.iloc[end],
                    "depth": depth,
                    "depth_pct": float(dd[trough] / peak[trough] * 100.0) if peak[trough] else None,
                    "duration_days": dur,
                    "time_to_recovery_days": ttr,
                    "recovered": recovered,
                }
            )
            in_dd = False
    episodes.sort(key=lambda e: e["depth"])
    out = []
    for rank, e in enumerate(episodes[:top_n], 1):
        out.append({"rank": rank, **e})
    return out


def monthly_performance(df: pd.DataFrame, symbol: str) -> list[dict[str, Any]]:
    if df.empty:
        return []
    x = df.copy()
    x["month"] = x["exit_time"].dt.strftime("%Y-%m")
    rows = []
    for mo, g in x.groupby("month"):
        sm = summarize_trades(g, symbol=symbol, period=mo, period_type="month")
        rows.append(
            {
                "symbol": symbol,
                "month": mo,
                "trades": sm["trades"],
                "net_return": sm["cumulative_net"],
                "expectancy": sm["expectancy"],
                "profit_factor": sm["profit_factor"],
                "win_rate": sm["win_rate"],
            }
        )
    return rows


def monthly_meta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    nets = [r["net_return"] for r in rows if r.get("net_return") is not None]
    pos = sum(1 for v in nets if v > 0)
    neg = sum(1 for v in nets if v < 0)
    return {
        "positive_month_share": pos / len(nets) if nets else None,
        "negative_month_share": neg / len(nets) if nets else None,
        "best_month": max(rows, key=lambda r: r["net_return"] or -1e18)["month"] if rows else None,
        "best_month_net": max((r["net_return"] or -1e18) for r in rows) if rows else None,
        "worst_month": min(rows, key=lambda r: r["net_return"] or 1e18)["month"] if rows else None,
        "worst_month_net": min((r["net_return"] or 1e18) for r in rows) if rows else None,
        "trades_per_month": float(np.mean([r["trades"] for r in rows])) if rows else None,
    }


def yearly_performance(df: pd.DataFrame, symbol: str) -> list[dict[str, Any]]:
    if df.empty:
        return []
    x = df.copy()
    x["year"] = x["exit_time"].dt.year.astype(str)
    rows = []
    for yr, g in x.groupby("year"):
        sm = summarize_trades(g, symbol=symbol, period=yr, period_type="year")
        rows.append(
            {
                "symbol": symbol,
                "year": yr,
                "trades": sm["trades"],
                "expectancy": sm["expectancy"],
                "profit_factor": sm["profit_factor"],
                "net": sm["cumulative_net"],
            }
        )
    return rows


def half_split(df: pd.DataFrame, symbol: str) -> list[dict[str, Any]]:
    if df.empty or len(df) < 4:
        return []
    mid = df["exit_time"].quantile(0.5)
    out = []
    for label, g in (("first_half", df[df["exit_time"] <= mid]), ("second_half", df[df["exit_time"] > mid])):
        sm = summarize_trades(g, symbol=symbol, period=label)
        out.append(
            {
                "symbol": symbol,
                "period": label,
                "trades": sm["trades"],
                "expectancy": sm["expectancy"],
                "profit_factor": sm["profit_factor"],
                "net": sm["cumulative_net"],
            }
        )
    return out


def overlap_stats(
    doge_trades: list[dict],
    btc_trades: list[dict],
) -> dict[str, Any]:
    """Fraction of timeline with 0/1/2 opens using entry/exit intervals."""
    intervals = []
    for t in doge_trades + btc_trades:
        a = pd.Timestamp(t["entry_time"])
        b = pd.Timestamp(t["exit_time"])
        if a.tzinfo is None:
            a = a.tz_localize("UTC")
        else:
            a = a.tz_convert("UTC")
        if b.tzinfo is None:
            b = b.tz_localize("UTC")
        else:
            b = b.tz_convert("UTC")
        intervals.append((a, b, t["symbol"]))
    if not intervals:
        return {"max_simultaneous": 0}
    t0 = min(a for a, _, _ in intervals)
    t1 = max(b for _, b, _ in intervals)
    points = sorted({t0, t1, *[a for a, _, _ in intervals], *[b for _, b, _ in intervals]})
    # measure duration-weighted occupancy between consecutive points
    dur = {0: 0.0, 1: 0.0, 2: 0.0}
    max_sim = 0
    overlap_dur = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        mid = a + (b - a) / 2
        open_n = sum(1 for s, e, _ in intervals if s <= mid < e)
        max_sim = max(max_sim, open_n)
        sec = (b - a).total_seconds()
        bucket = min(open_n, 2)
        dur[bucket] += sec
        if open_n >= 2:
            overlap_dur += sec
    total = sum(dur.values()) or 1.0
    return {
        "frac_time_0_open": dur[0] / total,
        "frac_time_1_open": dur[1] / total,
        "frac_time_2_open": dur[2] / total,
        "max_simultaneous": int(max_sim),
        "overlap_duration_days": overlap_dur / 86400.0,
        "coverage_start": t0.isoformat(),
        "coverage_end": t1.isoformat(),
    }
