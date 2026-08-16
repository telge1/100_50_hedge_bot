#!/usr/bin/env python3
"""AUDIT_GLOBAL_STRATEGY_REGIME_HEALTH_WALKFORWARD — research-only.

Global (portfolio-wide) weekly regime health on frozen full-history trades.
No coin×direction sizing. No strategy changes. Expanding-past-only.
Weekly check: Monday 00:00 UTC.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "results" / "full_history_baseline_symbol_direction_audit" / "full_trade_list.csv"
OUT = ROOT / "results" / "global_strategy_regime_health_walkforward"

EXPECT = {
    "closed": 6951,
    "TP": 4523,
    "SL": 2428,
    "wr": 65.07,
    "net": 1330.39,
    "pf": 1.863,
    "max_dd": -32.58,
}
TOL_NET = 0.05
TOL_WR = 0.15
TOL_PF = 0.02
TOL_DD = 0.1


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _ts(x: Any) -> datetime:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mondays_utc(start: datetime, end: datetime) -> list[datetime]:
    s = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while s.weekday() != 0:
        s += timedelta(days=1)
    out = []
    while s <= end:
        out.append(s)
        s += timedelta(days=7)
    return out


def equity_curve(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {"net": 0.0, "max_dd": 0.0, "gp": 0.0, "gl": 0.0, "pf": None, "npt": None, "wr": None}
    arr = np.asarray(nets, dtype=float)
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())
    gp = float(arr[arr > 0].sum()) if (arr > 0).any() else 0.0
    gl = float((-arr[arr < 0]).sum()) if (arr < 0).any() else 0.0
    pf = (gp / gl) if gl > 0 else None
    wins = int((arr > 0).sum())  # net_pnl based; prefer outcome when available
    return {
        "net": float(arr.sum()),
        "max_dd": max_dd,
        "gp": gp,
        "gl": -gl,
        "pf": float(pf) if pf is not None else None,
        "npt": float(arr.mean()),
        "n": len(nets),
    }


def window_metrics(trades: list[dict]) -> dict[str, Any]:
    empty = {
        "trade_count": 0, "TP": 0, "SL": 0, "WR": None, "SL_rate": None,
        "gross_profit": 0.0, "gross_loss": 0.0, "net": 0.0, "net_per_trade": None,
        "PF": None, "max_drawdown_within_window": 0.0, "max_SL_streak": 0,
        "LONG_WR": None, "LONG_PF": None, "LONG_NPT": None, "LONG_n": 0,
        "SHORT_WR": None, "SHORT_PF": None, "SHORT_NPT": None, "SHORT_n": 0,
    }
    if not trades:
        return empty
    n = len(trades)
    tp = sum(1 for t in trades if t["exit_reason"] == "TP")
    sl = sum(1 for t in trades if t["exit_reason"] == "SL")
    wins = sum(1 for t in trades if t["result"] == "WIN")
    nets = [float(t["net_pnl"]) for t in trades]
    gross = [float(t["pnl"]) for t in trades]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf = (gp / gl) if gl > 0 else None
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())

    # max SL streak in window (chronological)
    max_streak = 0
    cur = 0
    for t in trades:
        if t["exit_reason"] == "SL":
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0

    def side_stats(side: str) -> dict[str, Any]:
        st = [t for t in trades if t["direction"] == side]
        if not st:
            return {"WR": None, "PF": None, "NPT": None, "n": 0}
        nw = len(st)
        w = sum(1 for t in st if t["result"] == "WIN")
        nets_s = [float(t["net_pnl"]) for t in st]
        g = [float(t["pnl"]) for t in st]
        gp_s = sum(x for x in g if x > 0)
        gl_s = sum(-x for x in g if x < 0)
        pf_s = (gp_s / gl_s) if gl_s > 0 else None
        return {
            "WR": 100.0 * w / nw,
            "PF": float(pf_s) if pf_s is not None else None,
            "NPT": float(np.mean(nets_s)),
            "n": nw,
        }

    lng = side_stats("LONG")
    sht = side_stats("SHORT")
    return {
        "trade_count": n,
        "TP": tp,
        "SL": sl,
        "WR": 100.0 * wins / n,
        "SL_rate": 100.0 * sl / n,
        "gross_profit": float(gp),
        "gross_loss": float(-gl),
        "net": float(sum(nets)),
        "net_per_trade": float(np.mean(nets)),
        "PF": float(pf) if pf is not None else None,
        "max_drawdown_within_window": max_dd,
        "max_SL_streak": max_streak,
        "LONG_WR": lng["WR"], "LONG_PF": lng["PF"], "LONG_NPT": lng["NPT"], "LONG_n": lng["n"],
        "SHORT_WR": sht["WR"], "SHORT_PF": sht["PF"], "SHORT_NPT": sht["NPT"], "SHORT_n": sht["n"],
    }


def combo_weak_7d(trades: list[dict]) -> bool | None:
    """Descriptive weakness. None = insufficient sample (not counted as weak)."""
    if len(trades) < 3:
        return None
    m = window_metrics(trades)
    hits = 0
    if m["net_per_trade"] is not None and m["net_per_trade"] < 0:
        hits += 1
    if m["PF"] is not None and m["PF"] < 1.0:
        hits += 1
    if m["WR"] is not None and m["WR"] < 50.0:
        hits += 1
    if m["SL_rate"] is not None and m["SL_rate"] > 50.0:
        hits += 1
    return hits >= 2


def delta(cur: float | None, hist: float | None) -> float | None:
    if cur is None or hist is None:
        return None
    return float(cur - hist)


def forward_perf(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "future_WR": None, "future_PF": None, "future_NPT": None,
            "future_SL_rate": None, "future_net": 0.0, "future_maxDD": 0.0, "n": 0,
        }
    m = window_metrics(trades)
    return {
        "future_WR": m["WR"],
        "future_PF": m["PF"],
        "future_NPT": m["net_per_trade"],
        "future_SL_rate": m["SL_rate"],
        "future_net": m["net"],
        "future_maxDD": m["max_drawdown_within_window"],
        "n": m["trade_count"],
    }


def consecutive_negative_days(closed: list[dict], asof: datetime, lookback_days: int = 60) -> int:
    """Count trailing calendar days with negative net ending at asof-1day."""
    start = asof - timedelta(days=lookback_days)
    by_day: dict[str, float] = defaultdict(float)
    for t in closed:
        if t["exit_ts"] >= asof or t["exit_ts"] < start:
            continue
        day = t["exit_ts"].strftime("%Y-%m-%d")
        by_day[day] += float(t["net_pnl"])
    # walk backward from day before asof
    cur = (asof - timedelta(days=1)).date()
    streak = 0
    for _ in range(lookback_days):
        key = cur.isoformat()
        # Empty calendar days do not break the streak; only non-negative days do.
        if key not in by_day:
            cur -= timedelta(days=1)
            continue
        if by_day[key] < 0:
            streak += 1
            cur -= timedelta(days=1)
        else:
            break
    return streak


def consecutive_negative_weeks(week_nets: list[float]) -> int:
    """Trailing negative weekly nets among past completed weeks (before current)."""
    streak = 0
    for net in reversed(week_nets):
        if net < 0:
            streak += 1
        else:
            break
    return streak


def count_sl_streaks(trades: list[dict], min_len: int) -> int:
    """Number of completed SL streaks of length >= min_len in chronological sequence."""
    count = 0
    cur = 0
    for t in trades:
        if t["exit_reason"] == "SL":
            cur += 1
        else:
            if cur >= min_len:
                count += 1
            cur = 0
    if cur >= min_len:
        count += 1
    return count


def global_warning(m7: dict, hist: dict, weak_pct: float | None) -> bool:
    if m7["trade_count"] < 10:
        return False
    if hist["trade_count"] < 30:
        return False
    hits = 0
    if m7["net_per_trade"] is not None and m7["net_per_trade"] < 0:
        hits += 1
    if m7["PF"] is not None and m7["PF"] < 1.0:
        hits += 1
    if m7["WR"] is not None and hist["WR"] is not None and m7["WR"] <= hist["WR"] - 10.0:
        hits += 1
    if m7["SL_rate"] is not None and hist["SL_rate"] is not None and m7["SL_rate"] >= hist["SL_rate"] + 10.0:
        hits += 1
    if weak_pct is not None and weak_pct >= 50.0:
        hits += 1
    return hits >= 3


def global_confirmed(warn: bool, m30: dict, hist: dict, weak_pct: float | None) -> bool:
    if not warn:
        return False
    if m30["trade_count"] < 30:
        return False
    if hist["trade_count"] < 30:
        return False
    hits = 0
    if m30["net_per_trade"] is not None and m30["net_per_trade"] < 0:
        hits += 1
    if m30["PF"] is not None and m30["PF"] < 1.0:
        hits += 1
    if m30["WR"] is not None and hist["WR"] is not None and m30["WR"] <= hist["WR"] - 7.5:
        hits += 1
    if m30["SL_rate"] is not None and hist["SL_rate"] is not None and m30["SL_rate"] >= hist["SL_rate"] + 7.5:
        hits += 1
    if weak_pct is not None and weak_pct >= 50.0:
        hits += 1
    return hits >= 2


def recover_condition(warn: bool, m30: dict) -> bool:
    if warn:
        return False
    if m30["trade_count"] < 15:
        return False
    if m30["PF"] is None or m30["PF"] <= 1.2:
        return False
    if m30["net_per_trade"] is None or m30["net_per_trade"] <= 0:
        return False
    return True


def mean_safe(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not xs:
        return None
    return float(np.mean(xs))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lookahead_violations = 0
    future_reference_leakage = 0

    raw = pd.read_csv(SRC)
    all_rows = []
    for _, r in raw.iterrows():
        all_rows.append({
            "signal_id": r["signal_id"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "signal_ts": _ts(r["signal_ts"]),
            "entry_ts": _ts(r["entry_ts"]),
            "exit_ts": _ts(r["exit_ts"]) if pd.notna(r["exit_ts"]) else None,
            "outcome": r["outcome"],
            "result": r["result"],
            "exit_reason": r["exit_reason"] if pd.notna(r["exit_reason"]) else None,
            "pnl": float(r["pnl"]) if pd.notna(r["pnl"]) else None,
            "net_pnl": float(r["net_pnl"]) if pd.notna(r["net_pnl"]) else None,
        })

    closed_all = [t for t in all_rows if t["outcome"] in ("TP", "SL") and t["exit_ts"] is not None]
    # Baseline reproduction uses entry-time order (matches full_history audit MaxDD).
    closed_entry = sorted(closed_all, key=lambda t: (t["entry_ts"], t["signal_id"]))
    # Walk-forward uses exit-time order (information available when closed).
    closed = sorted(closed_all, key=lambda t: (t["exit_ts"], t["entry_ts"], t["signal_id"]))

    # Baseline guard
    tp = sum(1 for t in closed_entry if t["exit_reason"] == "TP")
    sl = sum(1 for t in closed_entry if t["exit_reason"] == "SL")
    nets_e = [float(t["net_pnl"]) for t in closed_entry]
    gross = [float(t["pnl"]) for t in closed_entry]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf_base = (gp / gl) if gl > 0 else None
    eq_e = np.cumsum(np.asarray(nets_e, dtype=float))
    peak_e = np.maximum.accumulate(eq_e)
    max_dd_e = float((eq_e - peak_e).min())
    wins = sum(1 for t in closed_entry if t["result"] == "WIN")
    wr = 100.0 * wins / len(closed_entry)
    base = {
        "closed": len(closed_entry),
        "TP": tp,
        "SL": sl,
        "WR": wr,
        "Net": float(sum(nets_e)),
        "PF": float(pf_base) if pf_base is not None else None,
        "MaxDD": max_dd_e,
    }
    ok = (
        base["closed"] == EXPECT["closed"]
        and base["TP"] == EXPECT["TP"]
        and base["SL"] == EXPECT["SL"]
        and abs(base["Net"] - EXPECT["net"]) <= TOL_NET
        and abs(base["WR"] - EXPECT["wr"]) <= TOL_WR
        and abs((base["PF"] or 0) - EXPECT["pf"]) <= TOL_PF
        and abs(base["MaxDD"] - EXPECT["max_dd"]) <= TOL_DD
    )
    if not ok:
        print("BASELINE_GUARD_FAIL", base)
        return 1
    print("BASELINE_GUARD_OK", flush=True)

    start = closed[0]["exit_ts"]
    end = closed[-1]["exit_ts"]
    # also include signal range for week list
    first_entry = min(t["entry_ts"] for t in closed)
    last_exit = end
    weeks = mondays_utc(first_entry, last_exit)
    print(f"weeks={len(weeks)} first={_iso(weeks[0])} last={_iso(weeks[-1])}", flush=True)

    symbols = sorted({t["symbol"] for t in closed})
    combos = sorted({(t["symbol"], t["direction"]) for t in closed})
    assert len(combos) == 22

    # Pre-index closed by exit for fast filters
    exit_times = np.array([t["exit_ts"].timestamp() for t in closed], dtype=float)

    def select_closed(t0: datetime, t1: datetime) -> list[dict]:
        """Trades with exit in [t0, t1)."""
        a = t0.timestamp()
        b = t1.timestamp()
        i0 = int(np.searchsorted(exit_times, a, side="left"))
        i1 = int(np.searchsorted(exit_times, b, side="left"))
        return closed[i0:i1]

    def select_after(t0: datetime, until: datetime | None = None) -> list[dict]:
        a = t0.timestamp()
        i0 = int(np.searchsorted(exit_times, a, side="left"))
        if until is None:
            return closed[i0:]
        b = until.timestamp()
        i1 = int(np.searchsorted(exit_times, b, side="left"))
        return closed[i0:i1]

    def select_next_n(t0: datetime, n: int) -> list[dict]:
        a = t0.timestamp()
        i0 = int(np.searchsorted(exit_times, a, side="left"))
        return closed[i0 : i0 + n]

    weekly_rows: list[dict[str, Any]] = []
    past_week_nets: list[float] = []
    past_weak_pcts: list[float] = []
    lookahead_check_log = []

    for wi, week in enumerate(weeks):
        t_7 = week - timedelta(days=7)
        t_30 = week - timedelta(days=30)

        trades_7 = select_closed(t_7, week)
        trades_30 = select_closed(t_30, week)
        trades_hist = select_closed(datetime(1970, 1, 1, tzinfo=timezone.utc), t_30)

        # Leakage guard: all used exits must be < week
        for bucket, label in ((trades_7, "7d"), (trades_30, "30d"), (trades_hist, "hist")):
            for t in bucket:
                if t["exit_ts"] >= week:
                    lookahead_violations += 1
                    lookahead_check_log.append((label, _iso(week), t["signal_id"]))

        m7 = window_metrics(trades_7)
        m30 = window_metrics(trades_30)
        hist = window_metrics(trades_hist)

        # Breadth: per combo 7d weakness
        weak_combos = []
        eval_combos = 0
        weak_by_symbol: dict[str, set[str]] = defaultdict(set)
        long_weak = 0
        short_weak = 0
        long_eval = 0
        short_eval = 0
        for sym, direction in combos:
            ct = [t for t in trades_7 if t["symbol"] == sym and t["direction"] == direction]
            w = combo_weak_7d(ct)
            if w is None:
                continue
            eval_combos += 1
            if direction == "LONG":
                long_eval += 1
            else:
                short_eval += 1
            if w:
                weak_combos.append((sym, direction))
                weak_by_symbol[sym].add(direction)
                if direction == "LONG":
                    long_weak += 1
                else:
                    short_weak += 1

        # Breadth over all 22 (weak / 22) as specified weak_combo_pct
        weak_combo_count = len(weak_combos)
        weak_combo_pct = 100.0 * weak_combo_count / 22.0
        symbols_with_any_weak = sum(1 for s, sides in weak_by_symbol.items() if sides)
        symbols_both_weak = sum(1 for s, sides in weak_by_symbol.items() if len(sides) == 2)
        weak_symbol_pct = 100.0 * symbols_with_any_weak / max(len(symbols), 1)

        long_weak_pct = 100.0 * long_weak / 11.0
        short_weak_pct = 100.0 * short_weak / 11.0

        # Relative breadth vs expanding past weak_pcts (previous weeks only)
        if past_weak_pcts:
            hist_median_weak = float(np.median(past_weak_pcts))
            # percentile of current among past (how extreme)
            pctile = 100.0 * (sum(1 for x in past_weak_pcts if x <= weak_combo_pct) / len(past_weak_pcts))
        else:
            hist_median_weak = None
            pctile = None

        # consecutive negative days / weeks
        cneg_days = consecutive_negative_days(closed, week)
        cneg_weeks = consecutive_negative_weeks(past_week_nets)

        # SL streaks in 7d / 30d
        n_sl4_7 = count_sl_streaks(trades_7, 4)
        n_sl5_7 = count_sl_streaks(trades_7, 5)
        n_sl4_30 = count_sl_streaks(trades_30, 4)
        n_sl5_30 = count_sl_streaks(trades_30, 5)

        # Per-symbol SL streaks ending at week (from 7d trades per symbol)
        sym_sl3 = 0
        sym_sl5 = 0
        for sym in symbols:
            st = [t for t in trades_7 if t["symbol"] == sym]
            # current streak at end of week
            streak = 0
            for t in reversed(st):
                if t["exit_reason"] == "SL":
                    streak += 1
                else:
                    break
            if streak >= 3:
                sym_sl3 += 1
            if streak >= 5:
                sym_sl5 += 1

        warn = global_warning(m7, hist, weak_combo_pct)
        conf = global_confirmed(warn, m30, hist, weak_combo_pct)

        # Forward (evaluation only) — must not feed into state
        f7 = forward_perf(select_closed(week, week + timedelta(days=7)))
        f14 = forward_perf(select_closed(week, week + timedelta(days=14)))
        f30 = forward_perf(select_closed(week, week + timedelta(days=30)))
        fn50 = forward_perf(select_next_n(week, 50))
        fn100 = forward_perf(select_next_n(week, 100))
        fn200 = forward_perf(select_next_n(week, 200))

        # Verify forward trades are after week
        for ft in select_closed(week, week + timedelta(days=7)):
            if ft["exit_ts"] < week:
                future_reference_leakage += 1

        row = {
            "week_start": _iso(week),
            "7d_trades": m7["trade_count"],
            "7d_TP": m7["TP"],
            "7d_SL": m7["SL"],
            "7d_WR": m7["WR"],
            "7d_PF": m7["PF"],
            "7d_NPT": m7["net_per_trade"],
            "7d_SL_rate": m7["SL_rate"],
            "7d_net": m7["net"],
            "7d_maxDD": m7["max_drawdown_within_window"],
            "7d_max_SL_streak": m7["max_SL_streak"],
            "7d_LONG_WR": m7["LONG_WR"],
            "7d_LONG_PF": m7["LONG_PF"],
            "7d_LONG_NPT": m7["LONG_NPT"],
            "7d_SHORT_WR": m7["SHORT_WR"],
            "7d_SHORT_PF": m7["SHORT_PF"],
            "7d_SHORT_NPT": m7["SHORT_NPT"],
            "30d_trades": m30["trade_count"],
            "30d_WR": m30["WR"],
            "30d_PF": m30["PF"],
            "30d_NPT": m30["net_per_trade"],
            "30d_SL_rate": m30["SL_rate"],
            "30d_net": m30["net"],
            "30d_maxDD": m30["max_drawdown_within_window"],
            "historical_trades": hist["trade_count"],
            "historical_WR": hist["WR"],
            "historical_PF": hist["PF"],
            "historical_NPT": hist["net_per_trade"],
            "historical_SL_rate": hist["SL_rate"],
            "7d_WR_delta_vs_history": delta(m7["WR"], hist["WR"]),
            "7d_PF_delta_vs_history": delta(m7["PF"], hist["PF"]),
            "7d_NPT_delta_vs_history": delta(m7["net_per_trade"], hist["net_per_trade"]),
            "7d_SL_delta_vs_history": delta(m7["SL_rate"], hist["SL_rate"]),
            "30d_WR_delta": delta(m30["WR"], hist["WR"]),
            "30d_PF_delta": delta(m30["PF"], hist["PF"]),
            "30d_NPT_delta": delta(m30["net_per_trade"], hist["net_per_trade"]),
            "30d_SL_delta": delta(m30["SL_rate"], hist["SL_rate"]),
            "weak_combo_count": weak_combo_count,
            "weak_combo_pct": weak_combo_pct,
            "weak_combo_eval_count": eval_combos,
            "weak_symbol_count": symbols_with_any_weak,
            "weak_symbol_pct": weak_symbol_pct,
            "symbols_with_both_sides_weak": symbols_both_weak,
            "long_weak_count": long_weak,
            "short_weak_count": short_weak,
            "long_weak_pct": long_weak_pct,
            "short_weak_pct": short_weak_pct,
            "hist_median_weak_combo_pct": hist_median_weak,
            "percentile_of_weak_breadth": pctile,
            "consecutive_negative_days": cneg_days,
            "consecutive_negative_weeks": cneg_weeks,
            "number_of_SL_streaks_4plus_7d": n_sl4_7,
            "number_of_SL_streaks_5plus_7d": n_sl5_7,
            "number_of_SL_streaks_4plus_30d": n_sl4_30,
            "number_of_SL_streaks_5plus_30d": n_sl5_30,
            "symbols_with_SL_streak_3plus": sym_sl3,
            "symbols_with_SL_streak_5plus": sym_sl5,
            "SLs_in_week_7d": m7["SL"],
            "SL_rate_7d": m7["SL_rate"],
            "global_warning": bool(warn),
            "global_confirmed": bool(conf),
            "future_7d_WR": f7["future_WR"],
            "future_7d_PF": f7["future_PF"],
            "future_7d_NPT": f7["future_NPT"],
            "future_7d_SL_rate": f7["future_SL_rate"],
            "future_7d_net": f7["future_net"],
            "future_7d_maxDD": f7["future_maxDD"],
            "future_14d_WR": f14["future_WR"],
            "future_14d_PF": f14["future_PF"],
            "future_14d_NPT": f14["future_NPT"],
            "future_14d_net": f14["future_net"],
            "future_30d_WR": f30["future_WR"],
            "future_30d_PF": f30["future_PF"],
            "future_30d_NPT": f30["future_NPT"],
            "future_30d_SL_rate": f30["future_SL_rate"],
            "future_30d_net": f30["future_net"],
            "future_30d_maxDD": f30["future_maxDD"],
            "future_50_WR": fn50["future_WR"],
            "future_50_PF": fn50["future_PF"],
            "future_50_NPT": fn50["future_NPT"],
            "future_50_n": fn50["n"],
            "future_100_WR": fn100["future_WR"],
            "future_100_PF": fn100["future_PF"],
            "future_100_NPT": fn100["future_NPT"],
            "future_100_SL_rate": fn100["future_SL_rate"],
            "future_100_n": fn100["n"],
            "future_200_WR": fn200["future_WR"],
            "future_200_PF": fn200["future_PF"],
            "future_200_NPT": fn200["future_NPT"],
            "future_200_n": fn200["n"],
        }
        weekly_rows.append(row)

        # update expanding past AFTER snapshot (for next week)
        past_weak_pcts.append(weak_combo_pct)
        past_week_nets.append(float(m7["net"] or 0.0))

    # ---- False warnings ----
    warning_detail = []
    for r in weekly_rows:
        if not r["global_warning"]:
            continue
        n100 = r["future_100_n"] or 0
        pf = r["future_100_PF"]
        npt = r["future_100_NPT"]
        if n100 < 50:
            label = "INSUFFICIENT_FORWARD"
        elif pf is not None and npt is not None and pf > 1 and npt > 0:
            label = "FALSE_WARNING"
        elif pf is not None and npt is not None and (pf < 1 or npt < 0):
            label = "GOOD_WARNING"
        else:
            label = "MIXED"
        warning_detail.append({**r, "warning_label": label})

    false_n = sum(1 for x in warning_detail if x["warning_label"] == "FALSE_WARNING")
    good_n = sum(1 for x in warning_detail if x["warning_label"] == "GOOD_WARNING")
    labeled = false_n + good_n
    false_warning_rate = (100.0 * false_n / labeled) if labeled else None

    # ---- Predictiveness tables ----
    def group_forward(rows: list[dict], label: str) -> dict[str, Any]:
        return {
            "state": label,
            "weeks": len(rows),
            "future_7d_WR": mean_safe([r["future_7d_WR"] for r in rows]),
            "future_7d_PF": mean_safe([r["future_7d_PF"] for r in rows]),
            "future_7d_NPT": mean_safe([r["future_7d_NPT"] for r in rows]),
            "future_14d_NPT": mean_safe([r["future_14d_NPT"] for r in rows]),
            "future_30d_NPT": mean_safe([r["future_30d_NPT"] for r in rows]),
            "future_30d_WR": mean_safe([r["future_30d_WR"] for r in rows]),
            "future_30d_PF": mean_safe([r["future_30d_PF"] for r in rows]),
            "future_50_NPT": mean_safe([r["future_50_NPT"] for r in rows]),
            "future_100_NPT": mean_safe([r["future_100_NPT"] for r in rows]),
            "future_100_PF": mean_safe([r["future_100_PF"] for r in rows]),
            "future_200_NPT": mean_safe([r["future_200_NPT"] for r in rows]),
        }

    normal_rows = [r for r in weekly_rows if not r["global_warning"]]
    warn_rows = [r for r in weekly_rows if r["global_warning"] and not r["global_confirmed"]]
    conf_rows = [r for r in weekly_rows if r["global_confirmed"]]
    any_warn = [r for r in weekly_rows if r["global_warning"]]

    warning_fwd = [
        group_forward(normal_rows, "NORMAL"),
        group_forward(any_warn, "GLOBAL_WARNING"),
        group_forward(conf_rows, "GLOBAL_CONFIRMED"),
        group_forward(warn_rows, "WARNING_NOT_CONFIRMED"),
    ]

    # ---- Breadth buckets ----
    buckets = [
        ("0-20", 0, 20),
        ("20-40", 20, 40),
        ("40-60", 40, 60),
        ("60-80", 60, 80),
        ("80-100", 80, 100.0001),
    ]
    breadth_fwd = []
    for name, lo, hi in buckets:
        rs = [r for r in weekly_rows if lo <= (r["weak_combo_pct"] or -1) < hi]
        breadth_fwd.append({
            "weak_combo_pct_bucket": name,
            "weeks": len(rs),
            "next_7d_WR": mean_safe([r["future_7d_WR"] for r in rs]),
            "next_7d_PF": mean_safe([r["future_7d_PF"] for r in rs]),
            "next_7d_NPT": mean_safe([r["future_7d_NPT"] for r in rs]),
            "next_14d_WR": mean_safe([r["future_14d_WR"] for r in rs]),
            "next_14d_PF": mean_safe([r["future_14d_PF"] for r in rs]),
            "next_14d_NPT": mean_safe([r["future_14d_NPT"] for r in rs]),
            "next_30d_WR": mean_safe([r["future_30d_WR"] for r in rs]),
            "next_30d_PF": mean_safe([r["future_30d_PF"] for r in rs]),
            "next_30d_NPT": mean_safe([r["future_30d_NPT"] for r in rs]),
        })

    # ---- LONG/SHORT breadth predictiveness ----
    long_short_rows = []
    for r in weekly_rows:
        long_short_rows.append({
            "week_start": r["week_start"],
            "long_weak_count": r["long_weak_count"],
            "short_weak_count": r["short_weak_count"],
            "long_weak_pct": r["long_weak_pct"],
            "short_weak_pct": r["short_weak_pct"],
            "future_7d_NPT": r["future_7d_NPT"],
            "future_30d_NPT": r["future_30d_NPT"],
            "future_7d_LONG_proxy_note": "portfolio-wide forward (not side-split)",
            "global_warning": r["global_warning"],
            "global_confirmed": r["global_confirmed"],
            "regime_hint": (
                "GLOBAL_LONG_WEAK_REGIME" if r["long_weak_pct"] >= 50 and r["short_weak_pct"] < 40
                else "GLOBAL_SHORT_WEAK_REGIME" if r["short_weak_pct"] >= 50 and r["long_weak_pct"] < 40
                else "BOTH_WEAK" if r["long_weak_pct"] >= 50 and r["short_weak_pct"] >= 50
                else "BALANCED"
            ),
        })

    # Side-regime forward summary
    side_regime_summary = []
    for label in ["GLOBAL_LONG_WEAK_REGIME", "GLOBAL_SHORT_WEAK_REGIME", "BOTH_WEAK", "BALANCED"]:
        rs = [r for r in long_short_rows if r["regime_hint"] == label]
        # map back to weekly for more metrics
        keys = {r["week_start"] for r in rs}
        wr = [w for w in weekly_rows if w["week_start"] in keys]
        side_regime_summary.append({
            "regime": label,
            "weeks": len(wr),
            "next_7d_NPT": mean_safe([x["future_7d_NPT"] for x in wr]),
            "next_30d_NPT": mean_safe([x["future_30d_NPT"] for x in wr]),
            "next_100_NPT": mean_safe([x["future_100_NPT"] for x in wr]),
            "next_100_PF": mean_safe([x["future_100_PF"] for x in wr]),
        })

    # ---- SL cluster analysis ----
    sl_cluster = []
    for r in weekly_rows:
        # high SL week flag: SL_rate >= hist + 10 or absolute >= 45
        high = False
        if r["7d_SL_rate"] is not None and r["historical_SL_rate"] is not None:
            high = r["7d_SL_rate"] >= r["historical_SL_rate"] + 10
        if r["7d_SL_rate"] is not None and r["7d_SL_rate"] >= 45:
            high = True
        sl_cluster.append({
            "week_start": r["week_start"],
            "SLs_per_week": r["SLs_in_week_7d"],
            "SL_rate": r["SL_rate_7d"],
            "symbols_with_SL_streak_3plus": r["symbols_with_SL_streak_3plus"],
            "symbols_with_SL_streak_5plus": r["symbols_with_SL_streak_5plus"],
            "high_SL_cluster_week": high,
            "future_7d_NPT": r["future_7d_NPT"],
            "future_30d_NPT": r["future_30d_NPT"],
            "future_100_NPT": r["future_100_NPT"],
            "future_100_PF": r["future_100_PF"],
        })

    high_sl = [r for r in sl_cluster if r["high_SL_cluster_week"]]
    low_sl = [r for r in sl_cluster if not r["high_SL_cluster_week"]]
    sl_pred = {
        "high_SL_cluster_weeks": len(high_sl),
        "high_next_7d_NPT": mean_safe([r["future_7d_NPT"] for r in high_sl]),
        "high_next_30d_NPT": mean_safe([r["future_30d_NPT"] for r in high_sl]),
        "high_next_100_NPT": mean_safe([r["future_100_NPT"] for r in high_sl]),
        "normal_weeks": len(low_sl),
        "normal_next_7d_NPT": mean_safe([r["future_7d_NPT"] for r in low_sl]),
        "normal_next_30d_NPT": mean_safe([r["future_30d_NPT"] for r in low_sl]),
        "normal_next_100_NPT": mean_safe([r["future_100_NPT"] for r in low_sl]),
    }

    # ---- Monthly summary ----
    monthly = []
    dfw = pd.DataFrame(weekly_rows)
    dfw["month"] = pd.to_datetime(dfw["week_start"]).dt.strftime("%Y-%m")
    # monthly from trades
    closed_df = pd.DataFrame([{
        "exit_ts": t["exit_ts"],
        "net_pnl": t["net_pnl"],
        "result": t["result"],
        "exit_reason": t["exit_reason"],
        "pnl": t["pnl"],
    } for t in closed])
    closed_df["month"] = closed_df["exit_ts"].dt.strftime("%Y-%m")
    for month, g in closed_df.groupby("month"):
        nets_m = g["net_pnl"].astype(float).tolist()
        eqm = equity_curve(nets_m)
        wins_m = (g["result"] == "WIN").sum()
        sl_m = (g["exit_reason"] == "SL").sum()
        n_m = len(g)
        wk = dfw[dfw["month"] == month]
        monthly.append({
            "month": month,
            "trades": n_m,
            "WR": 100.0 * wins_m / n_m if n_m else None,
            "PF": eqm["pf"],
            "NPT": eqm["npt"],
            "SL_rate": 100.0 * sl_m / n_m if n_m else None,
            "Max_DD": eqm["max_dd"],
            "Net": eqm["net"],
            "Avg_weak_combo_pct": float(wk["weak_combo_pct"].mean()) if len(wk) else None,
            "Max_weak_combo_pct": float(wk["weak_combo_pct"].max()) if len(wk) else None,
            "weeks_with_warning": int(wk["global_warning"].sum()) if len(wk) else 0,
            "weeks_with_confirmed": int(wk["global_confirmed"].sum()) if len(wk) else 0,
        })

    # ---- April/May special weeks (Mar–Jun) ----
    special_months = {"2026-03", "2026-04", "2026-05", "2026-06"}
    special_weeks = [r for r in weekly_rows if r["week_start"][:7] in special_months]

    # ---- Worst global weeks by 7d NPT ----
    worst_weeks = sorted(
        [r for r in weekly_rows if r["7d_trades"] and r["7d_trades"] >= 20],
        key=lambda r: (r["7d_NPT"] if r["7d_NPT"] is not None else 999),
    )[:10]

    # ---- Predictiveness gate: clear negative forward? ----
    # Compare NORMAL vs ANY_WARNING on future_7d_NPT and future_100_NPT
    n_npt7 = mean_safe([r["future_7d_NPT"] for r in normal_rows])
    w_npt7 = mean_safe([r["future_7d_NPT"] for r in any_warn])
    n_npt100 = mean_safe([r["future_100_NPT"] for r in normal_rows])
    w_npt100 = mean_safe([r["future_100_NPT"] for r in any_warn])
    n_npt30 = mean_safe([r["future_30d_NPT"] for r in normal_rows])
    w_npt30 = mean_safe([r["future_30d_NPT"] for r in any_warn])
    c_npt7 = mean_safe([r["future_7d_NPT"] for r in conf_rows])
    c_npt100 = mean_safe([r["future_100_NPT"] for r in conf_rows])

    # Breadth monotonicity rough check
    bucket_npts = [(b["weak_combo_pct_bucket"], b["next_7d_NPT"], b["weeks"]) for b in breadth_fwd if b["weeks"] > 0]
    # clear predictive if warning future NPT clearly worse than normal (e.g. delta < -0.05 on 7d or 100)
    clear_neg = False
    if w_npt7 is not None and n_npt7 is not None and (w_npt7 < n_npt7 - 0.05) and (w_npt7 < 0 or w_npt100 is not None and w_npt100 < 0):
        clear_neg = True
    if w_npt100 is not None and n_npt100 is not None and w_npt100 < n_npt100 - 0.05 and w_npt100 < 0:
        clear_neg = True
    if conf_rows and c_npt100 is not None and n_npt100 is not None and c_npt100 < 0 and c_npt100 < n_npt100 - 0.05:
        clear_neg = True

    # Also: if warning weeks still have positive future NPT similar to normal → not predictive
    warnings_still_positive = (
        (w_npt7 is not None and w_npt7 > 0)
        and (w_npt100 is not None and w_npt100 > 0)
        and (false_warning_rate is not None and false_warning_rate >= 50)
    )

    # ---- Optional counterfactual sizing ----
    sizing_rows: list[dict[str, Any]] = []
    sizing_ran = False
    if clear_neg and not warnings_still_positive:
        sizing_ran = True
        # Simulate STATIC, GLOBAL_CAUTION_50, GLOBAL_CAUTION_75
        for variant, caution_size in [
            ("STATIC_100", 1.0),
            ("GLOBAL_CAUTION_75", 0.75),
            ("GLOBAL_CAUTION_50", 0.50),
        ]:
            # state machine weekly
            size_by_week: dict[str, float] = {}
            state = "NORMAL"  # NORMAL or CAUTION
            for r in weekly_rows:
                # decide size for trades with exit in [week, next_week)
                size_by_week[r["week_start"]] = caution_size if state == "CAUTION" else 1.0
                # transition for NEXT week based on this snapshot
                if variant == "STATIC_100":
                    state = "NORMAL"
                    continue
                if state == "NORMAL":
                    if r["global_confirmed"]:
                        state = "CAUTION"
                else:
                    # recover
                    # rebuild m30-like from row
                    if (not r["global_warning"]) and (r["30d_PF"] is not None and r["30d_PF"] > 1.2) and (r["30d_NPT"] is not None and r["30d_NPT"] > 0):
                        state = "NORMAL"

            # apply size to each trade by exit week Monday
            scaled = []
            weeks_reduced = 0
            winner_reduced = 0.0
            loss_reduced = 0.0
            # map exit -> size: find latest Monday <= exit
            week_starts = [datetime.fromisoformat(r["week_start"].replace("Z", "+00:00")) for r in weekly_rows]
            size_list = [size_by_week[r["week_start"]] for r in weekly_rows]
            for t in closed:
                # find week
                et = t["exit_ts"]
                # largest Monday <= et
                idx = None
                for i, ws in enumerate(week_starts):
                    if ws <= et:
                        idx = i
                    else:
                        break
                sz = size_list[idx] if idx is not None else 1.0
                base_net = float(t["net_pnl"])
                scaled_net = base_net * sz
                scaled.append(scaled_net)
                if sz < 1.0 - 1e-9:
                    if base_net > 0:
                        winner_reduced += base_net * (1.0 - sz)
                    elif base_net < 0:
                        loss_reduced += (-base_net) * (1.0 - sz)

            weeks_reduced = sum(1 for s in size_list if s < 1.0 - 1e-9)
            eqv = equity_curve(scaled)
            # false global caution: weeks entering caution where next100 still good
            false_cautions = 0
            caution_events = 0
            # approximate: weeks where size is reduced
            for r, sz in zip(weekly_rows, size_list):
                if sz < 1.0 - 1e-9:
                    caution_events += 1
                    if r["future_100_PF"] is not None and r["future_100_NPT"] is not None:
                        if r["future_100_PF"] > 1 and r["future_100_NPT"] > 0:
                            false_cautions += 1
            sizing_rows.append({
                "variant": variant,
                "net": eqv["net"],
                "PF": eqv["pf"],
                "MaxDD": eqv["max_dd"],
                "winner_profit_reduced": winner_reduced,
                "loss_pnl_reduced": loss_reduced,
                "net_benefit": loss_reduced - winner_reduced,
                "weeks_at_reduced_size": weeks_reduced,
                "false_global_caution_rate": (100.0 * false_cautions / caution_events) if caution_events else None,
            })
    else:
        sizing_rows.append({
            "variant": "NOT_RUN",
            "reason": "GLOBAL_WARNING_FORWARD_NOT_CLEARLY_NEGATIVE",
            "note": "Counterfactual sizing skipped per §18 (requires clear negative forward predictiveness).",
            "normal_future_7d_NPT": n_npt7,
            "warning_future_7d_NPT": w_npt7,
            "normal_future_100_NPT": n_npt100,
            "warning_future_100_NPT": w_npt100,
            "confirmed_future_100_NPT": c_npt100,
            "false_warning_rate": false_warning_rate,
        })

    # ---- Primary decision ----
    n_warn = len(any_warn)
    n_conf = len(conf_rows)
    breadth_predictive = False
    # check if higher weak buckets have lower next NPT
    usable = [b for b in breadth_fwd if b["weeks"] >= 2 and b["next_7d_NPT"] is not None]
    if len(usable) >= 3:
        # compare low vs high
        low = [b for b in usable if b["weak_combo_pct_bucket"] in ("0-20", "20-40")]
        high = [b for b in usable if b["weak_combo_pct_bucket"] in ("60-80", "80-100")]
        if low and high:
            low_m = mean_safe([b["next_7d_NPT"] for b in low])
            high_m = mean_safe([b["next_7d_NPT"] for b in high])
            if low_m is not None and high_m is not None and high_m < low_m - 0.05 and high_m < 0:
                breadth_predictive = True

    long_short_differ = False
    ls_map = {s["regime"]: s for s in side_regime_summary}
    if (
        ls_map.get("GLOBAL_LONG_WEAK_REGIME", {}).get("weeks", 0) >= 2
        and ls_map.get("GLOBAL_SHORT_WEAK_REGIME", {}).get("weeks", 0) >= 2
    ):
        a = ls_map["GLOBAL_LONG_WEAK_REGIME"].get("next_7d_NPT")
        b = ls_map["GLOBAL_SHORT_WEAK_REGIME"].get("next_7d_NPT")
        if a is not None and b is not None and abs(a - b) > 0.08:
            long_short_differ = True

    if len(weeks) < 8:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "KEEP_STATIC"
    elif breadth_predictive and clear_neg:
        primary = "MARKET_BREADTH_PREDICTS_STRATEGY_WEAKNESS"
        recommendation = "GLOBAL_WARNING_LAYER_WORTH_NEXT_STAGE" if not sizing_ran else "GLOBAL_CAUTION_SIZING_WORTH_NEXT_STAGE"
    elif clear_neg and not warnings_still_positive:
        primary = "GLOBAL_REGIME_HEALTH_HAS_PREDICTIVE_VALUE"
        recommendation = "GLOBAL_CAUTION_SIZING_WORTH_NEXT_STAGE" if sizing_ran else "GLOBAL_WARNING_LAYER_WORTH_NEXT_STAGE"
    elif long_short_differ and not clear_neg:
        primary = "GLOBAL_LONG_SHORT_REGIMES_DIFFER"
        recommendation = "KEEP_STATIC"
    elif n_warn >= 3 and warnings_still_positive:
        primary = "GLOBAL_WARNINGS_TOO_NOISY"
        recommendation = "KEEP_STATIC"
    elif n_warn >= 3 and w_npt7 is not None and n_npt7 is not None and abs((w_npt7 or 0) - (n_npt7 or 0)) < 0.03:
        primary = "GLOBAL_WARNINGS_TOO_NOISY"
        recommendation = "KEEP_STATIC"
    else:
        # default: baseline already robust / not predictive
        primary = "STATIC_BASELINE_ALREADY_ROBUST"
        recommendation = "KEEP_STATIC"
        if n_warn == 0 and n_conf == 0:
            # check if any weak periods existed but didn't trip gates - still robust
            primary = "STATIC_BASELINE_ALREADY_ROBUST"

    # refine: if warnings exist but forward not worse → noisy or not predictive
    if n_warn > 0 and not clear_neg:
        if false_warning_rate is not None and false_warning_rate >= 50:
            primary = "GLOBAL_WARNINGS_TOO_NOISY"
        else:
            # could still be robust
            if w_npt7 is not None and w_npt7 > 0 and (n_npt7 is None or w_npt7 >= (n_npt7 or 0) - 0.03):
                primary = "STATIC_BASELINE_ALREADY_ROBUST"
            else:
                primary = "GLOBAL_WARNINGS_TOO_NOISY"
        recommendation = "KEEP_STATIC"

    # Write artifacts
    write_csv(OUT / "weekly_global_timeline.csv", weekly_rows)
    write_csv(OUT / "monthly_global_summary.csv", monthly)
    write_csv(OUT / "warning_forward_performance.csv", warning_fwd)
    write_csv(OUT / "breadth_forward_performance.csv", breadth_fwd)
    write_csv(OUT / "long_short_breadth.csv", long_short_rows)
    write_csv(OUT / "long_short_regime_summary.csv", side_regime_summary)
    write_csv(OUT / "sl_cluster_analysis.csv", sl_cluster)
    write_csv(OUT / "global_warning_detail.csv", warning_detail)
    write_csv(OUT / "optional_global_sizing_counterfactual.csv", sizing_rows)
    write_csv(OUT / "worst_global_weeks.csv", [{
        "week_start": r["week_start"],
        "7d_trades": r["7d_trades"],
        "7d_WR": r["7d_WR"],
        "7d_PF": r["7d_PF"],
        "7d_NPT": r["7d_NPT"],
        "weak_combo_pct": r["weak_combo_pct"],
        "global_warning": r["global_warning"],
        "future_7d_NPT": r["future_7d_NPT"],
        "future_30d_NPT": r["future_30d_NPT"],
    } for r in worst_weeks])
    write_csv(OUT / "april_may_special.csv", special_weeks)

    meta = {
        "task": "AUDIT_GLOBAL_STRATEGY_REGIME_HEALTH_WALKFORWARD",
        "weekly_check": "Monday 00:00 UTC",
        "baseline": base,
        "dataset": {
            "start": _iso(closed[0]["exit_ts"]),
            "end": _iso(closed[-1]["exit_ts"]),
            "symbols": len(symbols),
            "combos": len(combos),
            "closed": len(closed),
            "weekly_snapshots": len(weeks),
        },
        "counts": {
            "GLOBAL_WARNING": n_warn,
            "GLOBAL_CONFIRMED": n_conf,
            "FALSE_WARNING": false_n,
            "GOOD_WARNING": good_n,
            "false_warning_rate": false_warning_rate,
        },
        "predictiveness": {
            "normal": warning_fwd[0],
            "warning": warning_fwd[1],
            "confirmed": warning_fwd[2],
            "deltas_warning_minus_normal_7d_NPT": (None if w_npt7 is None or n_npt7 is None else w_npt7 - n_npt7),
            "deltas_warning_minus_normal_100_NPT": (None if w_npt100 is None or n_npt100 is None else w_npt100 - n_npt100),
            "clear_negative_forward": clear_neg,
            "warnings_still_positive": warnings_still_positive,
        },
        "breadth_predictive": breadth_predictive,
        "sl_cluster_predictiveness": sl_pred,
        "sizing_counterfactual_ran": sizing_ran,
        "lookahead_violations": lookahead_violations,
        "future_reference_leakage": future_reference_leakage,
        "primary_decision": primary,
        "recommendation": recommendation,
        "strategy_logic_changed": "NO",
    }
    (OUT / "summary.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    # summary.md
    def fmt(x: Any, nd: int = 3) -> str:
        if x is None:
            return "—"
        if isinstance(x, float):
            return f"{x:.{nd}f}"
        return str(x)

    lines = [
        "# AUDIT_GLOBAL_STRATEGY_REGIME_HEALTH_WALKFORWARD",
        "",
        f"Primary: `{primary}`  ",
        f"Recommendation: `{recommendation}`  ",
        "Weekly check: **Monday 00:00 UTC** · expanding past only  ",
        f"Baseline guard: **OK** · lookahead={lookahead_violations} · leakage={future_reference_leakage}",
        "",
        "## Baseline",
        f"closed {base['closed']} · TP {base['TP']} · SL {base['SL']} · WR {base['WR']:.2f}% · "
        f"Net {base['Net']:.2f} · PF {base['PF']:.3f} · MaxDD {base['MaxDD']:.2f}",
        "",
        f"## Snapshots: {len(weeks)} · WARNINGS {n_warn} · CONFIRMED {n_conf}",
        "",
        "## Forward: Normal vs Warning vs Confirmed",
        "",
        "| State | Weeks | Fut7d WR | Fut7d PF | Fut7d NPT | Fut30d NPT | Fut100 NPT |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for g in warning_fwd[:3]:
        lines.append(
            f"| {g['state']} | {g['weeks']} | {fmt(g['future_7d_WR'],1)} | {fmt(g['future_7d_PF'],2)} | "
            f"{fmt(g['future_7d_NPT'],3)} | {fmt(g['future_30d_NPT'],3)} | {fmt(g['future_100_NPT'],3)} |"
        )
    lines += [
        "",
        f"False warning rate: **{fmt(false_warning_rate,1)}%** ({false_n}/{labeled})",
        "",
        "## Breadth buckets",
        "",
        "| Weak% | Weeks | Next7d NPT | Next30d NPT |",
        "|---|---:|---:|---:|",
    ]
    for b in breadth_fwd:
        lines.append(
            f"| {b['weak_combo_pct_bucket']} | {b['weeks']} | {fmt(b['next_7d_NPT'],3)} | {fmt(b['next_30d_NPT'],3)} |"
        )
    lines += [
        "",
        f"Sizing counterfactual ran: **{sizing_ran}**",
        "",
        "## Strategy Logic Changed",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"PRIMARY {primary}")
    print(f"REC {recommendation}")
    print(f"warn={n_warn} conf={n_conf} false_rate={false_warning_rate}")
    print(f"sizing_ran={sizing_ran}")
    print(f"lookahead={lookahead_violations} leakage={future_reference_leakage}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
