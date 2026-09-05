"""Pure feature helpers from in-memory frames (no ClickHouse)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import NEAR_EDGE_BPS, TOUCH_SEP_MIN, TOUCH_TOL_BPS


def _as_naive_series(times: pd.Series) -> pd.Series:
    t = pd.to_datetime(times)
    if getattr(t.dt, "tz", None) is not None:
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
    return t


def slice_to_asof(df: pd.DataFrame, time_col: str, as_of: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out[time_col] = _as_naive_series(out[time_col])
    as_of_n = pd.Timestamp(as_of)
    if as_of_n.tzinfo is not None:
        as_of_n = as_of_n.tz_convert("UTC").tz_localize(None)
    return out.loc[out[time_col] <= as_of_n].sort_values(time_col).reset_index(drop=True)


def close_return(closes: np.ndarray, bars: int) -> float:
    if closes is None or len(closes) <= bars:
        return float("nan")
    a = float(closes[-(bars + 1)])
    b = float(closes[-1])
    if not np.isfinite(a) or not np.isfinite(b) or a == 0:
        return float("nan")
    return b / a - 1.0


def realized_vol(closes: np.ndarray, window: int) -> float:
    if closes is None or len(closes) < window + 1:
        return float("nan")
    c = np.asarray(closes[-(window + 1) :], dtype=float)
    r = np.diff(np.log(np.clip(c, 1e-12, None)))
    if len(r) < 2 or not np.isfinite(r).all():
        return float("nan")
    return float(np.std(r, ddof=1))


def rolling_rv_series(closes: np.ndarray, window: int) -> np.ndarray:
    """Causal rolling RV of `window` 1m log-returns; NaN until ready."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    out = np.full(n, np.nan)
    if n < window + 1:
        return out
    logc = np.log(np.clip(c, 1e-12, None))
    rets = np.diff(logc)
    # rets[i] = return ending at bar i+1
    for i in range(window - 1, len(rets)):
        chunk = rets[i - window + 1 : i + 1]
        if np.isfinite(chunk).all():
            out[i + 1] = float(np.std(chunk, ddof=1))
    return out


def ema(series: np.ndarray, span: int) -> np.ndarray:
    s = pd.Series(series, dtype=float)
    return s.ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


def ema_slope(closes: np.ndarray, span: int = 20, lookback: int = 15) -> float:
    if closes is None or len(closes) < span + lookback:
        return float("nan")
    e = ema(closes, span)
    a = e[-(lookback + 1)]
    b = e[-1]
    px = float(closes[-1])
    if not np.isfinite(a) or not np.isfinite(b) or not np.isfinite(px) or px == 0:
        return float("nan")
    return float((b - a) / px)


def range_60_metrics(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> dict[str, Any]:
    w = 60
    if highs is None or len(highs) < w:
        return {
            "ok": False,
            "width": float("nan"),
            "rh": float("nan"),
            "rl": float("nan"),
            "mid": float("nan"),
            "ret_w": float("nan"),
            "touches_up": 0,
            "touches_dn": 0,
            "near_high": False,
            "near_low": False,
            "outside_high": False,
            "outside_low": False,
            "trend_share": float("nan"),
        }
    hh = np.asarray(highs[-w:], dtype=float)
    ll = np.asarray(lows[-w:], dtype=float)
    cc = np.asarray(closes[-w:], dtype=float)
    rh, rl = float(np.nanmax(hh)), float(np.nanmin(ll))
    mid = 0.5 * (rh + rl)
    if mid <= 0 or rh <= rl:
        return {
            "ok": False,
            "width": float("nan"),
            "rh": rh,
            "rl": rl,
            "mid": mid,
            "ret_w": float("nan"),
            "touches_up": 0,
            "touches_dn": 0,
            "near_high": False,
            "near_low": False,
            "outside_high": False,
            "outside_low": False,
            "trend_share": float("nan"),
        }
    width = (rh - rl) / mid
    ret_w = (float(cc[-1]) - float(cc[0])) / mid
    rng = rh - rl
    tol = max(mid * TOUCH_TOL_BPS / 1e4, 0.10 * rng)
    touches_up = _count_touches(hh, ll, rh, tol, "upper")
    touches_dn = _count_touches(hh, ll, rl, tol, "lower")
    px = float(cc[-1])
    near_bps = NEAR_EDGE_BPS
    near_high = ((rh - px) / mid * 1e4) <= near_bps and px <= rh
    near_low = ((px - rl) / mid * 1e4) <= near_bps and px >= rl
    outside_high = px > rh
    outside_low = px < rl
    # fraction of net move vs range width (trend share inside window)
    trend_share = abs(ret_w) / width if width > 0 else float("nan")
    return {
        "ok": True,
        "width": float(width),
        "rh": rh,
        "rl": rl,
        "mid": mid,
        "ret_w": float(ret_w),
        "touches_up": int(touches_up),
        "touches_dn": int(touches_dn),
        "near_high": bool(near_high),
        "near_low": bool(near_low),
        "outside_high": bool(outside_high),
        "outside_low": bool(outside_low),
        "trend_share": float(trend_share) if np.isfinite(trend_share) else float("nan"),
    }


def _count_touches(highs, lows, edge, tol, side) -> int:
    idxs: list[int] = []
    for i in range(len(highs)):
        hit = highs[i] >= edge - tol if side == "upper" else lows[i] <= edge + tol
        if hit and (not idxs or (i - idxs[-1]) >= TOUCH_SEP_MIN):
            idxs.append(i)
    return len(idxs)


def width_percentile_rank(current_width: float, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> float:
    """Rank of current 60m width vs trailing ~24h of 60m widths (causal)."""
    n = len(closes)
    if n < 120 or not np.isfinite(current_width):
        return float("nan")
    widths = []
    look = min(n, 1440 + 60)
    start = n - look
    for i in range(start + 60, n + 1):
        sl = slice(i - 60, i)
        rh = float(np.nanmax(highs[sl]))
        rl = float(np.nanmin(lows[sl]))
        mid = 0.5 * (rh + rl)
        if mid > 0 and rh > rl:
            widths.append((rh - rl) / mid)
    if len(widths) < 30:
        return float("nan")
    arr = np.asarray(widths, dtype=float)
    return float(np.mean(arr <= current_width))


def merge_frame(
    candles: pd.DataFrame,
    trades: pd.DataFrame | None,
    ob: pd.DataFrame | None,
) -> pd.DataFrame:
    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = _as_naive_series(df["open_time"])
    if trades is not None and not trades.empty:
        tr = trades.copy()
        tr["minute"] = _as_naive_series(tr["minute"])
        df = df.merge(tr, left_on="open_time", right_on="minute", how="left")
        if "minute" in df.columns:
            df.drop(columns=["minute"], inplace=True)
    else:
        for c in (
            "trade_count",
            "total_volume",
            "aggressive_buy_volume",
            "aggressive_sell_volume",
            "trade_delta",
            "tps",
            "delta_ratio",
        ):
            if c not in df.columns:
                df[c] = np.nan
    if ob is not None and not ob.empty:
        o = ob.copy()
        o["minute"] = _as_naive_series(o["minute"])
        df = df.merge(o, left_on="open_time", right_on="minute", how="left", suffixes=("", "_ob"))
        for c in [x for x in df.columns if str(x).startswith("minute")]:
            df.drop(columns=[c], inplace=True)
    else:
        for c in (
            "seconds",
            "valid_seconds",
            "spread_bps",
            "imbalance_l50",
            "ofi",
            "ofi_5m",
            "bid_depth_l50",
            "ask_depth_l50",
        ):
            if c not in df.columns:
                df[c] = np.nan

    for c in ["trade_count", "trade_delta", "tps", "total_volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if "trade_delta" in df.columns:
        df["delta_3m"] = df["trade_delta"].rolling(3, min_periods=2).sum()
        df["delta_5m"] = df["trade_delta"].rolling(5, min_periods=3).sum()
    else:
        df["delta_3m"] = np.nan
        df["delta_5m"] = np.nan
    if "ofi" in df.columns and "ofi_5m" not in df.columns:
        df["ofi_5m"] = pd.to_numeric(df["ofi"], errors="coerce").rolling(5, min_periods=1).sum()
    return df


def last_row_features(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {}
    r = df.iloc[-1]
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    rv60_series = rolling_rv_series(closes, 60)
    rv60_now = float(rv60_series[-1]) if len(rv60_series) else float("nan")
    hist = rv60_series[np.isfinite(rv60_series)]
    # use last 24h of ready points for tertiles
    hist = hist[-1440:] if len(hist) > 1440 else hist
    p33 = float(np.nanpercentile(hist, 33)) if len(hist) >= 30 else float("nan")
    p66 = float(np.nanpercentile(hist, 66)) if len(hist) >= 30 else float("nan")

    rng = range_60_metrics(highs, lows, closes)
    wrank = width_percentile_rank(float(rng["width"]), closes, highs, lows)

    def _f(name: str) -> float:
        if name not in df.columns:
            return float("nan")
        v = r[name]
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    valid_seconds = _f("valid_seconds")
    seconds = _f("seconds")
    ob_ok = (
        np.isfinite(valid_seconds)
        and np.isfinite(seconds)
        and seconds > 0
        and valid_seconds >= 30
        and (valid_seconds / seconds) >= 0.90
    )
    trades_ok = bool(np.isfinite(_f("trade_count")) and _f("trade_count") >= 0)

    return {
        "close": float(r["close"]),
        "ret_15m": close_return(closes, 15),
        "ret_1h": close_return(closes, 60),
        "ret_4h": close_return(closes, 240),
        "rv_15m": realized_vol(closes, 15),
        "rv_60m": realized_vol(closes, 60),
        "rv_24h": realized_vol(closes, 1440),
        "rv_60m_now": rv60_now,
        "rv_60m_p33": p33,
        "rv_60m_p66": p66,
        "ema20_slope_15": ema_slope(closes, 20, 15),
        "range": rng,
        "width_rank": wrank,
        "tps": _f("tps") if "tps" in df.columns else (_f("trade_count") / 60.0 if "trade_count" in df.columns else float("nan")),
        "trade_count": _f("trade_count"),
        "delta_3m": _f("delta_3m"),
        "delta_5m": _f("delta_5m"),
        "spread_bps": _f("spread_bps"),
        "imbalance_l50": _f("imbalance_l50"),
        "ofi_5m": _f("ofi_5m"),
        "ob_ok": bool(ob_ok),
        "trades_ok": bool(trades_ok),
        "n_bars": int(len(df)),
    }
