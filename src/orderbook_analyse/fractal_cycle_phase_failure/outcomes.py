"""Forward outcomes after 15m failure decision_time (5m path)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure import HORIZONS_MIN, MIN_SAMPLE, VERY_SMALL
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf


def load_5m_path(*, symbol: str = "APTUSDT") -> pd.DataFrame:
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe="5m")
    df = raw.copy()
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df.sort_values("available_at").reset_index(drop=True)


def bars_for(minutes: int) -> int:
    return max(1, int(minutes // 5))


def attach_outcomes(events: pd.DataFrame, candles: pd.DataFrame) -> pd.DataFrame:
    """
    Entry = 5m close at first bar with available_at >= decision_time.
    Directional sign: expected UP -> +raw; expected DOWN -> -raw.
    """
    out = events.copy()
    c_avail = candles["available_at"].to_numpy(dtype="datetime64[ns]")
    close = candles["close"].astype(float).to_numpy()
    high = candles["high"].astype(float).to_numpy()
    low = candles["low"].astype(float).to_numpy()
    n_c = len(candles)
    n = len(out)

    times = out["decision_time"].to_numpy(dtype="datetime64[ns]")
    entry_i = np.searchsorted(c_avail, times, side="left").astype(np.int64)
    valid = (entry_i >= 0) & (entry_i < n_c)

    for h in HORIZONS_MIN:
        hb = bars_for(h)
        raw = np.full(n, np.nan)
        fav = np.full(n, np.nan)
        adv = np.full(n, np.nan)
        i_h = entry_i + hb
        ok = valid & (i_h < n_c)
        ii = entry_i[ok]
        ih = i_h[ok]
        c0 = close[ii]
        good = np.isfinite(c0) & (c0 != 0)
        ii, ih, c0 = ii[good], ih[good], c0[good]
        idx = np.flatnonzero(ok)[good]
        raw[idx] = (close[ih] / c0 - 1.0) * 100.0
        # path excursions: loop only over events (8k) — acceptable
        for j, i0 in zip(idx, ii):
            sl_h = high[i0 + 1 : i0 + 1 + hb]
            sl_l = low[i0 + 1 : i0 + 1 + hb]
            if sl_h.size:
                fav[j] = (float(np.max(sl_h)) / close[i0] - 1.0) * 100.0
                adv[j] = (float(np.min(sl_l)) / close[i0] - 1.0) * 100.0
        out[f"raw_ret_{h}m"] = raw
        out[f"raw_mfe_{h}m"] = fav
        out[f"raw_mae_{h}m"] = adv

    sign = np.where(out["expected_reversal"].astype(str) == "UP", 1.0, -1.0)
    for h in HORIZONS_MIN:
        raw = out[f"raw_ret_{h}m"].astype(float).to_numpy()
        mfe = out[f"raw_mfe_{h}m"].astype(float).to_numpy()
        mae = out[f"raw_mae_{h}m"].astype(float).to_numpy()
        out[f"dir_ret_{h}m"] = raw * sign
        out[f"dir_fav_{h}m"] = np.where(sign > 0, mfe, -mae)
        out[f"dir_adv_{h}m"] = np.where(sign > 0, mae, -mfe)
    out["entry_bar_i"] = entry_i
    return out


def metrics(sub: pd.DataFrame, **meta) -> dict:
    n = int(len(sub))
    row = {
        **meta,
        "n": n,
        "sample_flag": (
            "VERY_SMALL_SAMPLE"
            if n < VERY_SMALL
            else ("SMALL_SAMPLE" if n < MIN_SAMPLE else "OK")
        ),
    }
    if n == 0:
        return row
    for h in HORIZONS_MIN:
        r = sub[f"dir_ret_{h}m"].astype(float)
        fav = sub[f"dir_fav_{h}m"].astype(float)
        adv = sub[f"dir_adv_{h}m"].astype(float)
        row[f"hit_rate_{h}m"] = float((r > 0).mean()) if r.notna().any() else None
        row[f"median_dir_ret_{h}m"] = float(r.median()) if r.notna().any() else None
        row[f"mean_dir_ret_{h}m"] = float(r.mean()) if r.notna().any() else None
        row[f"median_fav_{h}m"] = float(fav.median()) if fav.notna().any() else None
        row[f"median_adv_{h}m"] = float(adv.median()) if adv.notna().any() else None
    return row
