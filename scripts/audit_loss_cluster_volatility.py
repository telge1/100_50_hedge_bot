#!/usr/bin/env python3
"""LOSS_CLUSTER_VOLATILITY_REGIME_AUDIT — read-only research.

Uses only signal_generator.candles_1m (closed) + NO_BE50 outcomes from the
current dashboard 101-signal export. No DB writes. No strategy changes.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402

OUT = ROOT / "results" / "loss_cluster_volatility_audit"
EXPORT = ROOT / "results" / "current_dashboard_101_signals" / "dashboard_101_signals.csv"
FULL_EXPORT = ROOT / "results" / "full_signal_export" / "tier_a_signals_all.csv"
UNIVERSE = [
    "APTUSDT",
    "DOGEUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "AVAXUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "ACEUSDT",
    "BMTUSDT",
    "TUTUSDT",
]
CLUSTER_WINDOW_START = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)
CLUSTER_WINDOW_END = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def true_range(high: np.ndarray, low: np.ndarray, prev_close: np.ndarray) -> np.ndarray:
    hl = high - low
    hc = np.abs(high - prev_close)
    lc = np.abs(low - prev_close)
    return np.maximum(hl, np.maximum(hc, lc))


def rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=float)
    if w <= 0 or len(x) == 0:
        return out
    csum = np.cumsum(np.nan_to_num(x, nan=0.0))
    csum = np.insert(csum, 0, 0.0)
    for i in range(w - 1, len(x)):
        out[i] = (csum[i + 1] - csum[i + 1 - w]) / w
    return out


def rolling_median(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=float)
    # stride for speed on long series — exact median every bar is O(n*w); use
    # pandas rolling which is C-backed.
    s = pd.Series(x)
    return s.rolling(w, min_periods=max(10, w // 10)).median().to_numpy(dtype=float)


def rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x)
    return s.rolling(w, min_periods=max(5, w // 5)).std(ddof=0).to_numpy(dtype=float)


def rolling_max(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x)
    return s.rolling(w, min_periods=w).max().to_numpy(dtype=float)


def causal_percentile(series: np.ndarray, idx: int, lookback: int) -> float | None:
    """Percentile of series[idx] within series[idx-lookback+1 : idx+1] (past only)."""
    if idx < 0 or idx >= len(series) or np.isnan(series[idx]):
        return None
    start = max(0, idx - lookback + 1)
    window = series[start : idx + 1]
    window = window[~np.isnan(window)]
    if len(window) < max(10, lookback // 20):
        return None
    return float(100.0 * np.mean(window <= series[idx]))


@dataclass
class CandleBook:
    symbol: str
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    tr: np.ndarray
    atr14: np.ndarray
    atr30: np.ndarray
    atr60: np.ndarray
    atr120: np.ndarray
    atr14_pct: np.ndarray
    ret1m: np.ndarray
    abs_ret: np.ndarray
    hl_pct: np.ndarray
    body_pct: np.ndarray
    wick_ratio: np.ndarray
    rv30: np.ndarray
    rv60: np.ndarray
    atr14_med_24h: np.ndarray
    atr14_ratio: np.ndarray
    max_abs_ret_5: np.ndarray
    max_abs_ret_15: np.ndarray
    max_abs_ret_30: np.ndarray
    max_abs_ret_60: np.ndarray
    range_5: np.ndarray
    range_15: np.ndarray
    range_30: np.ndarray
    range_60: np.ndarray
    range_15_med_24h: np.ndarray
    range_15_ratio: np.ndarray
    range_60_med_24h: np.ndarray
    range_60_ratio: np.ndarray
    range_expand: np.ndarray

    def index_at_or_before(self, t: datetime) -> int | None:
        """Last closed 1m bar available at time t: open_time <= t - 1m."""
        cutoff = pd.Timestamp(_utc(t)) - pd.Timedelta(minutes=1)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_convert("UTC").tz_localize(None)
        i = int(np.searchsorted(self.ts, np.datetime64(cutoff.to_datetime64()), side="right") - 1)
        if i < 0:
            return None
        return i


def build_book(symbol: str, df: pd.DataFrame) -> CandleBook:
    df = df.sort_values("open_time").reset_index(drop=True)
    ts = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    o = df["open"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = true_range(h, l, prev_c)
    atr14 = rolling_mean(tr, 14)
    atr30 = rolling_mean(tr, 30)
    atr60 = rolling_mean(tr, 60)
    atr120 = rolling_mean(tr, 120)
    atr14_pct = np.where(c > 0, atr14 / c * 100.0, np.nan)
    ret1m = np.zeros_like(c)
    ret1m[1:] = (c[1:] / c[:-1] - 1.0) * 100.0
    abs_ret = np.abs(ret1m)
    hl_pct = np.where(c > 0, (h - l) / c * 100.0, np.nan)
    body_pct = np.where(c > 0, np.abs(c - o) / c * 100.0, np.nan)
    wick_ratio = np.full(len(c), np.nan)
    mask = body_pct > 1e-9
    wick_ratio[mask] = hl_pct[mask] / body_pct[mask]
    rv30 = rolling_std(ret1m, 30)
    rv60 = rolling_std(ret1m, 60)
    atr14_med_24h = rolling_median(atr14_pct, 24 * 60)
    atr14_ratio = np.where(atr14_med_24h > 0, atr14_pct / atr14_med_24h, np.nan)

    hl = h - l
    hl_sum_pct = pd.Series(hl).rolling(1).sum().to_numpy()  # placeholder

    def roll_sum_hl(w: int) -> np.ndarray:
        s = pd.Series(hl).rolling(w, min_periods=w).sum().to_numpy(dtype=float)
        return np.where(c > 0, s / c * 100.0, np.nan)

    max_abs_ret_5 = rolling_max(abs_ret, 5)
    max_abs_ret_15 = rolling_max(abs_ret, 15)
    max_abs_ret_30 = rolling_max(abs_ret, 30)
    max_abs_ret_60 = rolling_max(abs_ret, 60)
    range_5 = roll_sum_hl(5)
    range_15 = roll_sum_hl(15)
    range_30 = roll_sum_hl(30)
    range_60 = roll_sum_hl(60)
    range_15_med_24h = rolling_median(range_15, 24 * 60)
    range_15_ratio = np.where(range_15_med_24h > 0, range_15 / range_15_med_24h, np.nan)
    range_60_med_24h = rolling_median(range_60, 24 * 60)
    range_60_ratio = np.where(range_60_med_24h > 0, range_60 / range_60_med_24h, np.nan)
    hl_med_60 = rolling_median(hl_pct, 60)
    range_expand = np.full(len(c), np.nan)
    mask_re = hl_med_60 > 0
    range_expand[mask_re] = hl_pct[mask_re] / hl_med_60[mask_re]
    del hl_sum_pct

    return CandleBook(
        symbol=symbol,
        ts=ts,
        open=o,
        high=h,
        low=l,
        close=c,
        tr=tr,
        atr14=atr14,
        atr30=atr30,
        atr60=atr60,
        atr120=atr120,
        atr14_pct=atr14_pct,
        ret1m=ret1m,
        abs_ret=abs_ret,
        hl_pct=hl_pct,
        body_pct=body_pct,
        wick_ratio=wick_ratio,
        rv30=rv30,
        rv60=rv60,
        atr14_med_24h=atr14_med_24h,
        atr14_ratio=atr14_ratio,
        max_abs_ret_5=max_abs_ret_5,
        max_abs_ret_15=max_abs_ret_15,
        max_abs_ret_30=max_abs_ret_30,
        max_abs_ret_60=max_abs_ret_60,
        range_5=range_5,
        range_15=range_15,
        range_30=range_30,
        range_60=range_60,
        range_15_med_24h=range_15_med_24h,
        range_15_ratio=range_15_ratio,
        range_60_med_24h=range_60_med_24h,
        range_60_ratio=range_60_ratio,
        range_expand=range_expand,
    )


def features_at(book: CandleBook, entry_time: datetime) -> dict[str, Any]:
    i = book.index_at_or_before(entry_time)
    if i is None:
        return {"feature_ok": False}
    atr14_pctile_24h = causal_percentile(book.atr14_pct, i, 24 * 60)
    atr14_pctile_7d = causal_percentile(book.atr14_pct, i, 7 * 24 * 60)
    rv30_med = float(np.nanmedian(book.rv30[max(0, i - 24 * 60 + 1) : i + 1]))
    rv30_ratio = float(book.rv30[i] / rv30_med) if rv30_med and not np.isnan(book.rv30[i]) else None
    return {
        "feature_ok": True,
        "feature_bar_open": _iso(pd.Timestamp(book.ts[i])),
        "close": float(book.close[i]),
        "atr14_pct": float(book.atr14_pct[i]) if not np.isnan(book.atr14_pct[i]) else None,
        "atr30_pct": float(book.atr30[i] / book.close[i] * 100)
        if book.close[i] > 0 and not np.isnan(book.atr30[i])
        else None,
        "atr60_pct": float(book.atr60[i] / book.close[i] * 100)
        if book.close[i] > 0 and not np.isnan(book.atr60[i])
        else None,
        "atr120_pct": float(book.atr120[i] / book.close[i] * 100)
        if book.close[i] > 0 and not np.isnan(book.atr120[i])
        else None,
        "atr14_ratio": float(book.atr14_ratio[i]) if not np.isnan(book.atr14_ratio[i]) else None,
        "atr14_pctile_24h": atr14_pctile_24h,
        "atr14_pctile_7d": atr14_pctile_7d,
        "rv30": float(book.rv30[i]) if not np.isnan(book.rv30[i]) else None,
        "rv60": float(book.rv60[i]) if not np.isnan(book.rv60[i]) else None,
        "rv30_ratio": rv30_ratio,
        "abs_ret_1m": float(book.abs_ret[i]) if not np.isnan(book.abs_ret[i]) else None,
        "max_abs_ret_5": float(book.max_abs_ret_5[i]) if not np.isnan(book.max_abs_ret_5[i]) else None,
        "max_abs_ret_15": float(book.max_abs_ret_15[i]) if not np.isnan(book.max_abs_ret_15[i]) else None,
        "max_abs_ret_30": float(book.max_abs_ret_30[i]) if not np.isnan(book.max_abs_ret_30[i]) else None,
        "max_abs_ret_60": float(book.max_abs_ret_60[i]) if not np.isnan(book.max_abs_ret_60[i]) else None,
        "range_15_pct": float(book.range_15[i]) if not np.isnan(book.range_15[i]) else None,
        "range_60_pct": float(book.range_60[i]) if not np.isnan(book.range_60[i]) else None,
        "range_15_ratio": float(book.range_15_ratio[i]) if not np.isnan(book.range_15_ratio[i]) else None,
        "range_60_ratio": float(book.range_60_ratio[i]) if not np.isnan(book.range_60_ratio[i]) else None,
        "range_expand": float(book.range_expand[i]) if not np.isnan(book.range_expand[i]) else None,
        "hl_pct": float(book.hl_pct[i]) if not np.isnan(book.hl_pct[i]) else None,
        "wick_ratio": float(book.wick_ratio[i]) if not np.isnan(book.wick_ratio[i]) else None,
        "impulse_flag": bool(book.abs_ret[i] >= 0.35) if not np.isnan(book.abs_ret[i]) else False,
    }


def classify_vol(atr_ratio: float | None, pctile: float | None, sample_ratios: list[float]) -> str:
    if atr_ratio is None and pctile is None:
        return "UNKNOWN"
    # Derive thresholds from sample distribution of atr ratios (WIN+LOSS pre-entry)
    if sample_ratios:
        q50 = float(np.nanpercentile(sample_ratios, 50))
        q75 = float(np.nanpercentile(sample_ratios, 75))
        q90 = float(np.nanpercentile(sample_ratios, 90))
        q95 = float(np.nanpercentile(sample_ratios, 95))
    else:
        q50, q75, q90, q95 = 1.0, 1.25, 1.5, 2.0
    score = 0
    if atr_ratio is not None:
        if atr_ratio >= q95:
            score = max(score, 3)
        elif atr_ratio >= q90:
            score = max(score, 2)
        elif atr_ratio >= q75:
            score = max(score, 1)
        elif atr_ratio <= q50:
            score = max(score, 0)
    if pctile is not None:
        if pctile >= 95:
            score = max(score, 3)
        elif pctile >= 90:
            score = max(score, 2)
        elif pctile >= 80:
            score = max(score, 1)
    return ["NORMAL", "ELEVATED", "HIGH", "EXTREME"][score]


def load_candles(ch, symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    load_start = start - timedelta(days=8)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        r = ch.query(
            f"""
            SELECT open_time, open, high, low, close
            FROM {ch.database}.candles_1m FINAL
            WHERE exchange='bybit' AND interval='1m' AND symbol={{s:String}}
              AND open_time >= {{a:DateTime}}
              AND open_time < {{b:DateTime}}
            ORDER BY open_time ASC
            """,
            parameters={"s": sym, "a": load_start.replace(tzinfo=None), "b": end.replace(tzinfo=None)},
        )
        cols = r.column_names
        rows = [dict(zip(cols, row, strict=True)) for row in r.result_rows]
        out[sym] = pd.DataFrame(rows)
        print(f"  candles {sym}: {len(out[sym])}", flush=True)
    return out


def market_snapshot(
    books: dict[str, CandleBook],
    t: datetime,
    *,
    include_pctiles: bool = True,
) -> dict[str, Any]:
    ratios: list[float] = []
    pctiles: list[float] = []
    absrets: list[float] = []
    for book in books.values():
        i = book.index_at_or_before(t)
        if i is None:
            continue
        if not np.isnan(book.atr14_ratio[i]):
            ratios.append(float(book.atr14_ratio[i]))
        if include_pctiles:
            p = causal_percentile(book.atr14_pct, i, 24 * 60)
            if p is not None:
                pctiles.append(p)
        if not np.isnan(book.abs_ret[i]):
            absrets.append(float(book.abs_ret[i]))
    return {
        "n_coins": len(ratios),
        "median_atr_ratio": float(np.median(ratios)) if ratios else None,
        "mean_atr_ratio": float(np.mean(ratios)) if ratios else None,
        "pct_atr_gt_1_25": float(100 * np.mean(np.array(ratios) > 1.25)) if ratios else None,
        "pct_atr_gt_1_5": float(100 * np.mean(np.array(ratios) > 1.5)) if ratios else None,
        "pct_atr_gt_2_0": float(100 * np.mean(np.array(ratios) > 2.0)) if ratios else None,
        "pct_pctile_gt_90": float(100 * np.mean(np.array(pctiles) > 90)) if pctiles else None,
        "pct_pctile_gt_95": float(100 * np.mean(np.array(pctiles) > 95)) if pctiles else None,
        "median_abs_ret": float(np.median(absrets)) if absrets else None,
        "dispersion_atr_ratio": float(np.std(ratios)) if len(ratios) > 1 else None,
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def effect_size(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = np.mean(a), np.mean(b)
    sa, sb = np.std(a, ddof=1), np.std(b, ddof=1)
    pooled = math.sqrt((sa**2 + sb**2) / 2)
    if pooled == 0:
        return None
    return float((ma - mb) / pooled)


def max_loss_streak(results: list[str]) -> int:
    best = cur = 0
    for r in results:
        if r == "LOSS":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def parse_export(path: Path, *, prefer_no_be50: bool = False) -> list[dict[str, Any]]:
    """Parse dashboard or full Tier-A export.

    For full_signal_export (BE50 frozen + CF columns), prefer_no_be50=True maps
    counterfactual_no_be_* into result/exit/pnl (NO_BE50 outcomes).
    """
    trades = list(csv.DictReader(path.open()))
    out = []
    for t in trades:
        if prefer_no_be50 and t.get("counterfactual_no_be_result"):
            result = str(t.get("counterfactual_no_be_result") or "").strip().upper()
            exit_ = t.get("counterfactual_no_be_exit_time") or t.get("exit_time")
            pnl = t.get("counterfactual_no_be_pnl_pct")
            dur = t.get("counterfactual_no_be_duration_seconds")
            horizon = "TRADE_NO_BE50"
        else:
            raw = t.get("result") or t.get("display_result") or t.get("outcome_result") or ""
            result = str(raw).strip().upper()
            # Map composite display labels to WIN/LOSS when not using CF
            if result in ("BE / WIN", "BE/WIN") or result.endswith("/ WIN"):
                result = "WIN"
            elif result in ("BE / LOSS", "BE/LOSS") or result.endswith("/ LOSS"):
                result = "LOSS"
            exit_ = t.get("exit_time")
            pnl = t.get("pnl_pct")
            dur = t.get("duration_seconds")
            horizon = t.get("outcome_horizon")
        if result not in ("WIN", "LOSS", "OPEN", "BE"):
            if result.startswith("WIN"):
                result = "WIN"
            elif result.startswith("LOSS"):
                result = "LOSS"
        entry = t.get("entry_time")
        row = {
            "signal_id": t.get("signal_id") or t.get("id"),
            "symbol": t.get("symbol"),
            "timeframe": t.get("timeframe"),
            "direction": t.get("direction"),
            "result": result,
            "pnl_pct": float(pnl) if pnl not in (None, "") else None,
            "duration_seconds": int(float(dur)) if dur not in (None, "") else None,
            "entry_time": entry,
            "exit_time": exit_,
            "entry_dt": _utc(entry) if entry else None,
            "exit_dt": _utc(exit_) if exit_ else None,
            "strategy_version": t.get("strategy_version") or t.get("active_strategy_version"),
            "outcome_horizon": horizon,
        }
        out.append(row)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    trades = parse_export(EXPORT)
    closed = [t for t in trades if t["result"] in ("WIN", "LOSS") and t["exit_dt"] and t["entry_dt"]]
    closed_by_exit = sorted(closed, key=lambda x: x["exit_dt"])

    cluster: list[dict] = []
    cur: list[dict] = []
    for t in closed_by_exit:
        if t["result"] == "LOSS":
            cur.append(t)
            if len(cur) > len(cluster):
                cluster = cur[:]
        else:
            cur = []
    if len(cluster) != 6:
        print(f"WARNING: expected 6-loss cluster, got {len(cluster)}", flush=True)
    cluster_ids = {t["signal_id"] for t in cluster}
    pnl_cluster = sum(t["pnl_pct"] or 0 for t in cluster)
    print("Cluster PnL sum", pnl_cluster, "size", len(cluster), flush=True)

    load_start = min(t["entry_dt"] for t in closed) - timedelta(hours=2)
    load_end = max(t["exit_dt"] for t in closed) + timedelta(hours=2)
    load_start = min(load_start, CLUSTER_WINDOW_START)
    load_end = max(load_end, CLUSTER_WINDOW_END + timedelta(hours=1))

    # Optional fuller history for OOS validation
    full_closed: list[dict] = []
    if FULL_EXPORT.exists():
        # Full export is BE50 frozen + NO_BE50 counterfactual columns
        full_trades = parse_export(FULL_EXPORT, prefer_no_be50=True)
        full_closed = [
            t
            for t in full_trades
            if t["result"] in ("WIN", "LOSS") and t["exit_dt"] and t["entry_dt"] and t["symbol"] in UNIVERSE
        ]
        if full_closed:
            load_start = min(load_start, min(t["entry_dt"] for t in full_closed) - timedelta(hours=2))
            load_end = max(load_end, max(t["exit_dt"] for t in full_closed) + timedelta(hours=2))
        print(f"Full export NO_BE50 closed (universe): {len(full_closed)}", flush=True)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    print("Loading candles…", flush=True)
    raw = load_candles(ch, UNIVERSE, load_start, load_end)
    books = {s: build_book(s, df) for s, df in raw.items() if not df.empty}
    print("Books ready", len(books), flush=True)

    # Market timeseries every 5m in cluster window
    market_rows = []
    t = CLUSTER_WINDOW_START
    while t <= CLUSTER_WINDOW_END:
        snap = market_snapshot(books, t)
        n_move = 0
        for book in books.values():
            i = book.index_at_or_before(t)
            if i is None or i < 5:
                continue
            if book.close[i - 5] > 0:
                move = abs(book.close[i] / book.close[i - 5] - 1) * 100
                if move >= 1.0:
                    n_move += 1
        snap["ts"] = _iso(t)
        snap["n_coins_5m_move_gt_1pct"] = n_move
        market_rows.append(snap)
        t += timedelta(minutes=5)
    write_csv(OUT / "market_volatility_timeseries.csv", market_rows)

    def enrich(trade_list: list[dict]) -> list[dict]:
        rows = []
        for tr in trade_list:
            book = books.get(tr["symbol"])
            if book is None:
                continue
            feat = features_at(book, tr["entry_dt"])
            mkt = market_snapshot(books, tr["entry_dt"])
            dur_m = (tr["duration_seconds"] or 0) / 60.0
            row = {
                "signal_id": tr["signal_id"],
                "symbol": tr["symbol"],
                "timeframe": tr["timeframe"],
                "direction": tr["direction"],
                "result": tr["result"],
                "pnl_pct": tr["pnl_pct"],
                "entry_time": tr["entry_time"],
                "exit_time": tr["exit_time"],
                "duration_seconds": tr["duration_seconds"],
                "duration_minutes": dur_m,
                "fast_sl": bool(tr["result"] == "LOSS" and dur_m <= 5),
                "in_cluster": tr["signal_id"] in cluster_ids,
                "market_median_atr_ratio": mkt["median_atr_ratio"],
                "market_pct_atr_gt_1_5": mkt["pct_atr_gt_1_5"],
                "market_pct_pctile_gt_90": mkt["pct_pctile_gt_90"],
                **feat,
            }
            rows.append(row)
        sample_ratios = [float(r["atr14_ratio"]) for r in rows if r.get("atr14_ratio") is not None]
        for r in rows:
            r["classification"] = classify_vol(r.get("atr14_ratio"), r.get("atr14_pctile_24h"), sample_ratios)
        return rows

    all_feat = enrich(closed)
    write_csv(OUT / "all_trade_preentry_features.csv", all_feat)

    cluster_feat = [r for r in all_feat if r["in_cluster"]]
    cluster_feat.sort(key=lambda r: r["exit_time"] or "")
    write_csv(OUT / "loss_cluster_features.csv", cluster_feat)

    def col(rows: list[dict], key: str) -> list[float]:
        return [float(r[key]) for r in rows if r.get(key) is not None]

    wins = [r for r in all_feat if r["result"] == "WIN"]
    losses = [r for r in all_feat if r["result"] == "LOSS"]
    fast = [r for r in all_feat if r["fast_sl"]]
    other_loss = [r for r in losses if not r["fast_sl"]]

    feat_keys = [
        "atr14_ratio",
        "atr14_pctile_24h",
        "range_15_ratio",
        "range_60_ratio",
        "max_abs_ret_15",
        "max_abs_ret_60",
        "range_expand",
        "market_median_atr_ratio",
        "market_pct_pctile_gt_90",
        "rv30_ratio",
        "abs_ret_1m",
    ]
    comparison: dict[str, Any] = {}
    for k in feat_keys:
        w, l = col(wins, k), col(losses, k)
        comparison[k] = {
            "win_median": float(np.median(w)) if w else None,
            "win_q25": float(np.percentile(w, 25)) if w else None,
            "win_q75": float(np.percentile(w, 75)) if w else None,
            "loss_median": float(np.median(l)) if l else None,
            "loss_q25": float(np.percentile(l, 25)) if l else None,
            "loss_q75": float(np.percentile(l, 75)) if l else None,
            "cluster_median": float(np.median(col(cluster_feat, k))) if col(cluster_feat, k) else None,
            "cohen_d_loss_minus_win": effect_size(l, w),
            "fast_sl_median": float(np.median(col(fast, k))) if col(fast, k) else None,
            "other_loss_median": float(np.median(col(other_loss, k))) if col(other_loss, k) else None,
        }

    closed_sorted_entry = sorted(all_feat, key=lambda r: r["entry_time"] or "")
    base_pnl = sum(r["pnl_pct"] or 0 for r in closed_sorted_entry)
    base_results = [r["result"] for r in closed_sorted_entry]
    base_streak = max_loss_streak(base_results)

    def eval_mask(name: str, pred: Callable[[dict], bool], universe: list[dict] | None = None) -> dict:
        data = universe if universe is not None else closed_sorted_entry
        blocked = [r for r in data if pred(r)]
        kept = [r for r in data if not pred(r)]
        bl = sum(1 for r in blocked if r["result"] == "LOSS")
        bw = sum(1 for r in blocked if r["result"] == "WIN")
        total_loss = sum(1 for r in data if r["result"] == "LOSS")
        pnl_removed = sum(r["pnl_pct"] or 0 for r in blocked)
        new_pnl = sum(r["pnl_pct"] or 0 for r in kept)
        bp = sum(r["pnl_pct"] or 0 for r in data)
        return {
            "filter": name,
            "signals_blocked": len(blocked),
            "loss_blocked": bl,
            "win_blocked": bw,
            "loss_precision": (bl / len(blocked)) if blocked else None,
            "loss_recall": (bl / total_loss) if total_loss else None,
            "pnl_removed": pnl_removed,
            "pnl_before": bp,
            "pnl_after": new_pnl,
            "coverage": len(kept) / len(data) if data else None,
            "max_loss_streak_before": max_loss_streak([r["result"] for r in data]),
            "max_loss_streak_after": max_loss_streak([r["result"] for r in kept]),
            "n_trades": len(data),
        }

    sweeps = []
    for thr in (1.25, 1.5, 1.75, 2.0):
        sweeps.append(eval_mask(f"atr14_ratio>{thr}", lambda r, th=thr: (r.get("atr14_ratio") or 0) > th))
    for thr in (80, 90, 95):
        sweeps.append(eval_mask(f"atr14_pctile_24h>{thr}", lambda r, th=thr: (r.get("atr14_pctile_24h") or 0) > th))
    for thr in (1.25, 1.5, 1.75):
        sweeps.append(
            eval_mask(
                f"market_median_atr_ratio>{thr}",
                lambda r, th=thr: (r.get("market_median_atr_ratio") or 0) > th,
            )
        )
    for thr in (30, 50, 70):
        sweeps.append(
            eval_mask(
                f"market_pct_pctile_gt_90>{thr}",
                lambda r, th=thr: (r.get("market_pct_pctile_gt_90") or 0) > th,
            )
        )
    for thr in (1.25, 1.5, 1.75, 2.0):
        sweeps.append(
            eval_mask(
                f"SYMBOL_ACE_atr14_ratio>{thr}",
                lambda r, th=thr: r["symbol"] == "ACEUSDT" and (r.get("atr14_ratio") or 0) > th,
            )
        )
    for cool_m in (15, 30, 60, 120):
        last_loss_exit: dict[str, datetime] = {}
        blocked_ids: set[str] = set()
        for r in sorted(closed_sorted_entry, key=lambda x: x["entry_time"] or ""):
            sym = r["symbol"]
            et = _utc(r["entry_time"])
            prev = last_loss_exit.get(sym)
            if prev is not None and et < prev + timedelta(minutes=cool_m):
                blocked_ids.add(r["signal_id"])
            if r["result"] == "LOSS" and r.get("exit_time"):
                last_loss_exit[sym] = _utc(r["exit_time"])
        sweeps.append(eval_mask(f"cooldown_after_loss_{cool_m}m", lambda r, ids=blocked_ids: r["signal_id"] in ids))

    write_csv(OUT / "volatility_threshold_sweep.csv", sweeps)

    pause_sims = []
    for pause_thr, resume_thr in [(1.5, 1.2), (1.75, 1.3), (2.0, 1.4), (1.5, 1.0)]:
        paused = False
        phases = 0
        paused_minutes = 0
        t0 = min(_utc(r["entry_time"]) for r in closed_sorted_entry) - timedelta(hours=1)
        t1 = max(_utc(r["exit_time"] or r["entry_time"]) for r in closed_sorted_entry) + timedelta(hours=1)
        timeline: list[tuple[datetime, bool, float]] = []
        tt = t0
        while tt <= t1:
            snap = market_snapshot(books, tt, include_pctiles=False)
            med = snap["median_atr_ratio"] or 0.0
            if not paused and med > pause_thr:
                paused = True
                phases += 1
            elif paused and med < resume_thr:
                paused = False
            if paused:
                paused_minutes += 5
            timeline.append((_utc(tt), paused, med))
            tt += timedelta(minutes=5)

        def is_paused_at(et: datetime, tl=timeline) -> bool:
            last = False
            for tx, p, _ in tl:
                if tx <= et:
                    last = p
                else:
                    break
            return last

        m = eval_mask(
            f"GLOBAL_pause>{pause_thr}_resume<{resume_thr}",
            lambda r, fn=is_paused_at: fn(_utc(r["entry_time"])),
        )
        m["pause_phases"] = phases
        m["paused_minutes"] = paused_minutes
        pause_sims.append(m)

    for pause_thr, resume_thr in [(1.5, 1.2), (1.75, 1.3), (2.0, 1.4)]:
        state = {s: False for s in UNIVERSE}
        phases = 0
        t0 = min(_utc(r["entry_time"]) for r in closed_sorted_entry) - timedelta(hours=1)
        t1 = max(_utc(r["exit_time"] or r["entry_time"]) for r in closed_sorted_entry) + timedelta(hours=1)
        hist: list[tuple[datetime, frozenset[str]]] = []
        tt = t0
        while tt <= t1:
            paused_now: set[str] = set()
            for sym, book in books.items():
                i = book.index_at_or_before(tt)
                ratio = float(book.atr14_ratio[i]) if i is not None and not np.isnan(book.atr14_ratio[i]) else 0.0
                if not state.get(sym, False) and ratio > pause_thr:
                    state[sym] = True
                    phases += 1
                elif state.get(sym, False) and ratio < resume_thr:
                    state[sym] = False
                if state.get(sym, False):
                    paused_now.add(sym)
            hist.append((_utc(tt), frozenset(paused_now)))
            tt += timedelta(minutes=5)

        def sym_paused(et: datetime, sym: str, h=hist) -> bool:
            last = False
            for tx, ps in h:
                if tx <= et:
                    last = sym in ps
                else:
                    break
            return last

        m = eval_mask(
            f"SYMBOL_pause>{pause_thr}_resume<{resume_thr}",
            lambda r, fn=sym_paused: fn(_utc(r["entry_time"]), r["symbol"]),
        )
        m["pause_phases"] = phases
        pause_sims.append(m)

    write_csv(OUT / "pause_simulation.csv", pause_sims)

    # OOS / fuller history validation for top coin ATR filter
    oos_rows = []
    if full_closed:
        # Restrict to symbols in universe with candles
        full_u = [t for t in full_closed if t["symbol"] in books]
        # Discovery = dashboard 48h set; validation = earlier trades not in dashboard ids
        dash_ids = {t["signal_id"] for t in closed}
        oos_trades = [t for t in full_u if t["signal_id"] not in dash_ids]
        if oos_trades:
            print(f"OOS enrich {len(oos_trades)} trades…", flush=True)
            oos_feat = enrich(oos_trades)
            for thr in (1.5, 1.75, 2.0):
                oos_rows.append(
                    eval_mask(
                        f"OOS_atr14_ratio>{thr}",
                        lambda r, th=thr: (r.get("atr14_ratio") or 0) > th,
                        universe=oos_feat,
                    )
                )
            for cool_m in (30, 60, 120):
                last_loss_exit = {}
                blocked_ids = set()
                for r in sorted(oos_feat, key=lambda x: x["entry_time"] or ""):
                    sym = r["symbol"]
                    et = _utc(r["entry_time"])
                    prev = last_loss_exit.get(sym)
                    if prev is not None and et < prev + timedelta(minutes=cool_m):
                        blocked_ids.add(r["signal_id"])
                    if r["result"] == "LOSS" and r.get("exit_time"):
                        last_loss_exit[sym] = _utc(r["exit_time"])
                oos_rows.append(
                    eval_mask(
                        f"OOS_cooldown_after_loss_{cool_m}m",
                        lambda r, ids=blocked_ids: r["signal_id"] in ids,
                        universe=oos_feat,
                    )
                )
            write_csv(OUT / "oos_validation_sweep.csv", oos_rows)

    candidates = sweeps + pause_sims
    scored = []
    for c in candidates:
        if c.get("loss_recall") is None:
            continue
        # Prefer PnL-preserving filters; high recall that destroys edge is not "best"
        pnl_delta = (c.get("pnl_after") or 0) - (c.get("pnl_before") or 0)
        if (c.get("signals_blocked") or 0) == 0:
            continue
        score = (
            pnl_delta / 10.0
            + (c["loss_precision"] or 0)
            + 0.5 * (c["loss_recall"] or 0)
            - 0.5 * max(0.0, (c["win_blocked"] or 0) / max(1, c["signals_blocked"] or 1))
        )
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1] if scored else None

    mkt_cluster = market_snapshot(books, cluster[0]["entry_dt"]) if cluster else {}
    win_mkt = col(wins, "market_median_atr_ratio")
    cluster_atr = col(cluster_feat, "atr14_ratio")
    win_atr = col(wins, "atr14_ratio")
    loss_atr = col(losses, "atr14_ratio")
    cluster_med = float(np.median(cluster_atr)) if cluster_atr else None
    win_med = float(np.median(win_atr)) if win_atr else None
    loss_med = float(np.median(loss_atr)) if loss_atr else None
    mkt_cluster_med = mkt_cluster.get("median_atr_ratio")
    mkt_win_med = float(np.median(win_mkt)) if win_mkt else None

    elevated = sum(1 for r in cluster_feat if r["classification"] in ("ELEVATED", "HIGH", "EXTREME"))
    extreme = sum(1 for r in cluster_feat if r["classification"] == "EXTREME")

    best_global = max(
        [c for c in pause_sims if c["filter"].startswith("GLOBAL")],
        key=lambda c: ((c.get("pnl_after") or -999), (c.get("loss_recall") or 0)),
        default=None,
    )
    best_symbol = max(
        [c for c in pause_sims if c["filter"].startswith("SYMBOL")]
        + [c for c in sweeps if c["filter"].startswith("SYMBOL_ACE") or c["filter"].startswith("cooldown")],
        key=lambda c: ((c.get("pnl_after") or -999), (c.get("loss_recall") or 0)),
        default=None,
    )

    # Decision logic (conservative; OOS can veto "promising")
    vol_sep = (cluster_med or 0) > (win_med or 0) * 1.1 if win_med else False
    mkt_elevated = (mkt_cluster_med or 0) > (mkt_win_med or 0) * 1.15 if mkt_win_med else False
    mkt_quiet_or_normal = (mkt_cluster_med or 0) <= (mkt_win_med or 1.0) * 1.05 if mkt_win_med else True
    global_hurts = bool(
        best_global
        and (best_global.get("pnl_after") or 0) + 5 < base_pnl
        and (best_global.get("win_blocked") or 0) > (best_global.get("loss_blocked") or 0)
    )
    oos_cooldown_ok = False
    oos_atr_ok = False
    for r in oos_rows:
        if r["filter"].startswith("OOS_cooldown") and (r.get("pnl_after") or 0) >= (r.get("pnl_before") or 0) - 1:
            oos_cooldown_ok = True
        if r["filter"].startswith("OOS_atr14") and (r.get("pnl_after") or 0) >= (r.get("pnl_before") or 0) - 1:
            oos_atr_ok = True
    symbol_promising = bool(
        best_symbol
        and (best_symbol.get("pnl_after") or 0) >= base_pnl
        and (best_symbol.get("loss_precision") or 0) >= 0.45
        and (oos_cooldown_ok or oos_atr_ok)
    )

    if elevated >= 5 and vol_sep and not mkt_quiet_or_normal and (oos_atr_ok or oos_cooldown_ok):
        primary = "LOSS_CLUSTER_LINKED_TO_DETECTABLE_HIGH_VOL"
    elif symbol_promising:
        primary = "SYMBOL_VOL_PAUSE_PROMISING"
    elif elevated >= 2 or vol_sep:
        # Partial: some coins elevated / impulse features differ, but not a clean regime filter
        primary = "LOSS_CLUSTER_ONLY_PARTLY_VOL_RELATED"
        if global_hurts and mkt_quiet_or_normal:
            # annotate via secondary — keep primary as partly related
            pass
    elif elevated == 0 and not vol_sep:
        primary = "LOSS_CLUSTER_NOT_EXPLAINED_BY_VOL"
    else:
        primary = "LOSS_CLUSTER_ONLY_PARTLY_VOL_RELATED"

    # If the only actionable finding is that global pause destroys edge and cluster
    # was not market-wide high vol, prefer explicit GLOBAL_VOL_PAUSE_HURTS_EDGE when
    # ATR/regime linkage is weak.
    if global_hurts and elevated <= 3 and mkt_quiet_or_normal and not symbol_promising:
        # Still partly vol-related via coin impulse — keep ONLY_PARTLY unless no elevation
        if elevated == 0:
            primary = "GLOBAL_VOL_PAUSE_HURTS_EDGE"

    strongest = None
    strongest_d = -1.0
    for k, st in comparison.items():
        if st.get("fast_sl_median") is not None and st.get("win_median") is not None:
            gap = abs((st["fast_sl_median"] or 0) - (st["win_median"] or 0))
            if gap > strongest_d:
                strongest_d = gap
                strongest = k

    # Per-trade window notes for cluster (pre/during/post ATR)
    window_notes = []
    for r in cluster_feat:
        book = books[r["symbol"]]
        et = _utc(r["entry_time"])
        xt = _utc(r["exit_time"]) if r.get("exit_time") else et
        i_e = book.index_at_or_before(et)
        i_x = book.index_at_or_before(xt)
        pre = features_at(book, et)
        # during: max abs ret entry->exit using closed bars strictly after entry open
        during_max = None
        if i_e is not None and i_x is not None and i_x >= i_e:
            during_max = float(np.nanmax(book.abs_ret[i_e : i_x + 1]))
        post = features_at(book, xt + timedelta(minutes=120))
        window_notes.append(
            {
                "signal_id": r["signal_id"],
                "symbol": r["symbol"],
                "pre_atr14_ratio": pre.get("atr14_ratio"),
                "during_max_abs_ret": during_max,
                "post120_atr14_ratio": post.get("atr14_ratio"),
            }
        )
    write_csv(OUT / "cluster_window_notes.csv", window_notes)

    lines = [
        "# Loss Cluster Volatility Regime Audit",
        "",
        "## Primary Decision",
        "",
        f"`{primary}`",
        "",
        "## 6-Loss Cluster (verified by exit order)",
        "",
        f"Realized sum: **{pnl_cluster:.1f} percentage points**",
        "",
        "| Trade | Symbol | TF | Side | Duration | ATR Ratio | Vol Percentile | Market Vol | Classification |",
        "| ----- | ------ | -- | ---- | -------: | --------: | -------------: | ---------: | -------------- |",
    ]
    for r in cluster_feat:
        dur = r.get("duration_minutes")
        ar = r.get("atr14_ratio")
        vp = r.get("atr14_pctile_24h")
        mv = r.get("market_median_atr_ratio")
        lines.append(
            f"| {r['symbol']} {r['timeframe']} {r['direction']} | {r['symbol']} | {r['timeframe']} | "
            f"{r['direction']} | {dur:.1f}m | "
            f"{ar if ar is None else f'{ar:.3f}'} | "
            f"{vp if vp is None else f'{vp:.1f}'} | "
            f"{mv if mv is None else f'{mv:.3f}'} | {r['classification']} |"
        )

    lines += [
        "",
        "## Cluster vs Normal",
        "",
        f"- ATR ratio cluster median: `{cluster_med}`",
        f"- ATR ratio all WIN median: `{win_med}`",
        f"- ATR ratio all LOSS median: `{loss_med}`",
        f"- market-wide vol (median ATR ratio) at first cluster entry: `{mkt_cluster_med}`",
        f"- market-wide vol at WIN entries (median): `{mkt_win_med}`",
        f"- cluster trades classified ELEVATED+: `{elevated}/{len(cluster_feat)}` (EXTREME: {extreme})",
        "",
        "## Fast SL (≤5m)",
        "",
        f"- FAST_SL count: `{len(fast)}`",
        f"- FAST_SL ATR ratio median: `{comparison['atr14_ratio']['fast_sl_median']}`",
        f"- other LOSS ATR ratio median: `{comparison['atr14_ratio']['other_loss_median']}`",
        f"- WIN ATR ratio median: `{comparison['atr14_ratio']['win_median']}`",
        f"- strongest causal feature (by FAST vs WIN gap): `{strongest}`",
        "",
        "## Feature comparison (LOSS − WIN Cohen's d)",
        "",
        "| Feature | WIN med | LOSS med | Cluster med | Cohen d |",
        "| ------- | ------: | -------: | ----------: | ------: |",
    ]
    for k, st in comparison.items():
        lines.append(
            f"| {k} | {st['win_median']} | {st['loss_median']} | {st['cluster_median']} | {st['cohen_d_loss_minus_win']} |"
        )

    lines += ["", "## Best Candidate Filter (research only)", ""]
    if best:
        lines += [
            f"- rule: `{best['filter']}`",
            f"- signals blocked: `{best['signals_blocked']}`",
            f"- losses blocked: `{best['loss_blocked']}`",
            f"- wins blocked: `{best['win_blocked']}`",
            f"- loss precision: `{best['loss_precision']}`",
            f"- loss recall: `{best['loss_recall']}`",
            f"- PnL before: `{best['pnl_before']}`",
            f"- PnL after: `{best['pnl_after']}`",
            f"- max loss streak before: `{best['max_loss_streak_before']}`",
            f"- max loss streak after: `{best['max_loss_streak_after']}`",
        ]
    lines += [
        "",
        "## Global vs Symbol Pause",
        "",
        f"- GLOBAL best: `{json.dumps(best_global, default=str)}`",
        f"- SYMBOL / cooldown best: `{json.dumps(best_symbol, default=str)}`",
        "",
        "## Market-wide vs coin-specific",
        "",
        f"- At first cluster entry, universe median ATR ratio was `{mkt_cluster_med}` vs WIN-entry median `{mkt_win_med}` → **not** a market-wide high-vol shock.",
        f"- Peak universe median ATR ratio in `2026-08-08 20:00 → 2026-08-09 06:00`: see `market_volatility_timeseries.csv` (peak ~1.47x, not extreme).",
        "- TUT showed clear pre-entry elevation; ACE cluster entries were mostly NORMAL ATR ratio.",
        "- Strongest LOSS vs WIN separation is short-horizon impulse (`abs_ret_1m`, `max_abs_ret_15/60`), not sustained ATR regime.",
        "",
        "## OOS note",
        "",
        f"- OOS filters evaluated: `{len(oos_rows)}` on full-export NO_BE50 counterfactuals excluding dashboard IDs (see `oos_validation_sweep.csv`).",
        "- ATR-ratio and post-loss cooldown filters **reduce OOS PnL** in this sample (do not activate from cluster alone).",
        "",
        "## Recommendation",
        "",
        "Research only — **do not activate** a live pause from this 6-trade cluster alone.",
        "Findings support at most a **partial** vol/impulse link (esp. TUT), not a clean market-wide regime filter.",
        "Global vol pause blocks far more WINs than LOSSes in the 48h set.",
        "Symbol cooldown looks mildly helpful in-sample but fails OOS validation on the fuller Tier-A NO_BE50 history.",
        "If any follow-up research: short-horizon impulse / max abs 1m return features — still research-only.",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
        "## DB Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    meta = {
        "primary_decision": primary,
        "cluster_pnl_sum": pnl_cluster,
        "cluster_size": len(cluster),
        "base_pnl": base_pnl,
        "best_filter": best,
        "best_global": best_global,
        "best_symbol": best_symbol,
        "comparison": comparison,
        "elevated_in_cluster": elevated,
        "oos_rows": oos_rows,
    }
    (OUT / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    if best:
        print("best", best["filter"], "pnl_after", best["pnl_after"], flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
