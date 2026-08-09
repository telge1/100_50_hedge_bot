"""Period volatility from MySQL market_candles."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.periods import period_bounds
from orderbook_analyse.trend_permission_sticky_lag import atr_wilder_np
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _abs_ret_pct(close: np.ndarray) -> np.ndarray:
    if len(close) < 2:
        return np.array([], dtype=float)
    r = np.diff(close) / close[:-1] * 100.0
    return np.abs(r)


def volatility_for_symbol_period(
    symbol: str,
    period: str,
    *,
    c1h: pd.DataFrame,
    c4h: pd.DataFrame,
) -> dict[str, Any]:
    ps, pe = period_bounds(period)
    h = c1h[(c1h["timestamp"] >= ps) & (c1h["timestamp"] <= pe)].copy()
    f = c4h[(c4h["timestamp"] >= ps) & (c4h["timestamp"] <= pe)].copy()

    out: dict[str, Any] = {
        "period": period,
        "symbol": symbol,
        "n_1h_bars": int(len(h)),
        "n_4h_bars": int(len(f)),
        "median_atr14_pct": None,
        "mean_atr14_pct": None,
        "median_abs_1h_return_pct": None,
        "median_abs_4h_return_pct": None,
        "realized_vol_1h_ann_approx": None,
    }
    if len(h) >= 20:
        high = h["high"].astype(float).to_numpy()
        low = h["low"].astype(float).to_numpy()
        close = h["close"].astype(float).to_numpy()
        atr = atr_wilder_np(high, low, close, 14)
        atr_pct = atr / close * 100.0
        atr_pct = atr_pct[np.isfinite(atr_pct)]
        if len(atr_pct):
            out["median_atr14_pct"] = float(np.median(atr_pct))
            out["mean_atr14_pct"] = float(np.mean(atr_pct))
        abs1 = _abs_ret_pct(close)
        if len(abs1):
            out["median_abs_1h_return_pct"] = float(np.median(abs1))
            # simple realized vol: std of 1h returns * sqrt(24*365)
            rets = np.diff(close) / close[:-1]
            out["realized_vol_1h_ann_approx"] = float(np.std(rets, ddof=1) * np.sqrt(24 * 365) * 100.0)
    if len(f) >= 2:
        close4 = f["close"].astype(float).to_numpy()
        abs4 = _abs_ret_pct(close4)
        if len(abs4):
            out["median_abs_4h_return_pct"] = float(np.median(abs4))
    return out


def load_vol_frames(symbols: tuple[str, ...]) -> dict[str, dict[str, pd.DataFrame]]:
    load_env_file(ENV_FILE)
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in symbols:
        print(f"[vol] load {sym} 1h/4h …", flush=True)
        c1h = load_mysql_ohlcv_tf(symbol=sym, timeframe="1h", env_file=ENV_FILE)
        c4h = load_mysql_ohlcv_tf(symbol=sym, timeframe="4h", env_file=ENV_FILE)
        out[sym] = {"1h": c1h, "4h": c4h}
    return out


def volatility_table(
    periods: list[str],
    symbols: tuple[str, ...],
    frames: dict[str, dict[str, pd.DataFrame]],
) -> pd.DataFrame:
    rows = []
    for period in periods:
        for sym in symbols:
            rows.append(
                volatility_for_symbol_period(
                    sym, period, c1h=frames[sym]["1h"], c4h=frames[sym]["4h"]
                )
            )
        # BOTH = mean of available symbol medians
        sub = [r for r in rows if r["period"] == period]
        def _avg(key):
            vals = [r[key] for r in sub if r[key] is not None]
            return float(np.mean(vals)) if vals else None

        rows.append(
            {
                "period": period,
                "symbol": "BOTH",
                "n_1h_bars": sum(r["n_1h_bars"] for r in sub),
                "n_4h_bars": sum(r["n_4h_bars"] for r in sub),
                "median_atr14_pct": _avg("median_atr14_pct"),
                "mean_atr14_pct": _avg("mean_atr14_pct"),
                "median_abs_1h_return_pct": _avg("median_abs_1h_return_pct"),
                "median_abs_4h_return_pct": _avg("median_abs_4h_return_pct"),
                "realized_vol_1h_ann_approx": _avg("realized_vol_1h_ann_approx"),
            }
        )
    return pd.DataFrame(rows)
