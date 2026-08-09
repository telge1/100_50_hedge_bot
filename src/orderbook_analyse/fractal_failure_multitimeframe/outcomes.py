"""1m-path forward returns, first-touch, and delayed-entry helpers."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_failure_multitimeframe import (
    FIRST_TOUCH_EXTRA_HTF,
    FIRST_TOUCH_LEVELS_BASE,
    MIN_SAMPLE,
    ROUNDTRIP_FEE_PCT,
    SYMBOL,
    VERY_SMALL,
)


def load_1m(*, symbol: str = SYMBOL) -> pd.DataFrame:
    """Load 1m OHLCV for path/entry mapping (symbol-selectable; default APTUSDT)."""
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m")
    df = raw.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def resolve_entries(
    events: pd.DataFrame,
    open_times: np.ndarray,
    opens: np.ndarray,
    *,
    delay_min: int = 0,
) -> pd.DataFrame:
    """Map each event to first 1m open strictly after confirmation (+ optional delay)."""
    conf = events["confirmation_available_at"].to_numpy(dtype="datetime64[ns]")
    decision = conf + np.timedelta64(int(delay_min), "m")
    entry_i = np.searchsorted(open_times, decision, side="right").astype(np.int64)
    n = len(open_times)
    valid = (entry_i >= 0) & (entry_i < n)
    entry_px = np.full(len(events), np.nan)
    entry_t = np.full(len(events), np.datetime64("NaT"), dtype="datetime64[ns]")
    entry_px[valid] = opens[entry_i[valid]]
    entry_t[valid] = open_times[entry_i[valid]]
    entry_i = np.where(valid, entry_i, -1)
    out = events.copy()
    out["delay_min"] = int(delay_min)
    out["entry_i"] = entry_i
    out["entry_price"] = entry_px
    out["entry_time"] = pd.to_datetime(entry_t, utc=True)
    out["entry_valid"] = valid & np.isfinite(entry_px) & (entry_px > 0)
    return out


def _path_metrics_batch(
    entry_i: np.ndarray,
    entry_px: np.ndarray,
    side_short: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    horizons: Iterable[int],
) -> dict[str, np.ndarray]:
    n = len(entry_i)
    n_c = len(close)
    out: dict[str, np.ndarray] = {}
    valid0 = (entry_i >= 0) & (entry_i < n_c - 1) & np.isfinite(entry_px) & (entry_px > 0)
    for h in horizons:
        raw = np.full(n, np.nan)
        fav = np.full(n, np.nan)
        adv = np.full(n, np.nan)
        # target exit timestamp = entry open_time + h minutes
        t_end = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
        t_end[valid0] = open_times[entry_i[valid0]] + np.timedelta64(int(h), "m")
        i_h = np.searchsorted(open_times, t_end, side="right") - 1
        ok = valid0 & (i_h > entry_i) & (i_h < n_c)
        idxs = np.flatnonzero(ok)
        for k in idxs:
            i0 = int(entry_i[k])
            ih = int(i_h[k])
            epx = float(entry_px[k])
            sl_h = high[i0 + 1 : ih + 1]
            sl_l = low[i0 + 1 : ih + 1]
            if sl_h.size == 0:
                continue
            raw_pct = (close[ih] / epx - 1.0) * 100.0
            max_up = (float(np.max(sl_h)) / epx - 1.0) * 100.0
            max_dn = (float(np.min(sl_l)) / epx - 1.0) * 100.0
            if side_short[k]:
                raw[k] = -raw_pct
                fav[k] = -max_dn
                adv[k] = -max_up
            else:
                raw[k] = raw_pct
                fav[k] = max_up
                adv[k] = max_dn
        out[f"dir_ret_{h}m"] = raw
        out[f"dir_fav_{h}m"] = fav
        out[f"dir_adv_{h}m"] = adv
    return out


def attach_forward_with_opens(
    events: pd.DataFrame,
    *,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    opens: np.ndarray,
    open_times: np.ndarray,
    horizons: Iterable[int],
    delay_min: int = 0,
) -> pd.DataFrame:
    mapped = resolve_entries(events, open_times, opens, delay_min=delay_min)
    side_short = (mapped["side"].astype(str) == "SHORT").to_numpy()
    metrics = _path_metrics_batch(
        mapped["entry_i"].to_numpy(),
        mapped["entry_price"].to_numpy(),
        side_short,
        high,
        low,
        close,
        open_times,
        horizons,
    )
    for k, v in metrics.items():
        mapped[k] = v
    return mapped


def first_touch_counts(
    events: pd.DataFrame,
    *,
    high: np.ndarray,
    low: np.ndarray,
    levels: tuple[float, ...],
    max_bars: int = 1440,
) -> list[dict[str, Any]]:
    """Aggregate first-touch rates by side for one TF's failure events (T0 entries)."""
    n_c = len(high)
    levels_arr = np.asarray(levels, dtype=float)
    rows_by_side: dict[str, dict[float, dict[str, int]]] = {
        side: {float(lvl): {"favorable": 0, "adverse": 0, "same_bar": 0, "neither": 0, "n": 0} for lvl in levels}
        for side in ("LONG", "SHORT", "COMBINED")
    }

    for ev in events.itertuples(index=False):
        if not bool(getattr(ev, "entry_valid", False)):
            continue
        i0 = int(ev.entry_i)
        epx = float(ev.entry_price)
        short = str(ev.side) == "SHORT"
        side = str(ev.side)
        if i0 < 0 or i0 >= n_c - 1 or epx <= 0:
            continue
        end = min(n_c - 1, i0 + max_bars)
        hh = high[i0 + 1 : end + 1]
        ll = low[i0 + 1 : end + 1]
        if hh.size == 0:
            continue
        if short:
            fav = (epx - ll) / epx * 100.0
            adv = (hh - epx) / epx * 100.0
        else:
            fav = (hh / epx - 1.0) * 100.0
            adv = (epx - ll) / epx * 100.0

        # one forward scan; resolve each level at first decisive bar
        pending = {float(lvl): True for lvl in levels}
        hits = {float(lvl): "neither" for lvl in levels}
        for f, a in zip(fav, adv):
            if not pending:
                break
            for lvl in list(pending):
                hf, ha = f >= lvl, a >= lvl
                if not (hf or ha):
                    continue
                if hf and ha:
                    hits[lvl] = "same_bar"
                elif hf:
                    hits[lvl] = "favorable"
                else:
                    hits[lvl] = "adverse"
                del pending[lvl]

        for lvl_f, hit in hits.items():
            for s in (side, "COMBINED"):
                rows_by_side[s][lvl_f]["n"] += 1
                rows_by_side[s][lvl_f][hit] += 1

    out = []
    for side in ("LONG", "SHORT", "COMBINED"):
        for lvl in levels:
            d = rows_by_side[side][float(lvl)]
            n = d["n"]
            out.append(
                {
                    "side": side,
                    "level_pct": lvl,
                    "n": n,
                    "share_favorable_first": d["favorable"] / n if n else None,
                    "share_adverse_first": d["adverse"] / n if n else None,
                    "share_same_bar": d["same_bar"] / n if n else None,
                    "share_neither": d["neither"] / n if n else None,
                }
            )
    return out


def summarize_returns(sub: pd.DataFrame, horizons: Iterable[int], **meta) -> dict[str, Any]:
    n = int(len(sub))
    row: dict[str, Any] = {
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
    for h in horizons:
        col = f"dir_ret_{h}m"
        if col not in sub.columns:
            continue
        r = sub[col].astype(float)
        fav = sub[f"dir_fav_{h}m"] if f"dir_fav_{h}m" in sub.columns else None
        adv = sub[f"dir_adv_{h}m"] if f"dir_adv_{h}m" in sub.columns else None
        valid = r.notna()
        nv = int(valid.sum())
        row[f"n_valid_{h}m"] = nv
        if nv == 0:
            continue
        rv = r[valid]
        row[f"hit_rate_{h}m"] = float((rv > 0).mean())
        row[f"median_dir_ret_{h}m"] = float(rv.median())
        row[f"mean_dir_ret_{h}m"] = float(rv.mean())
        row[f"q25_dir_ret_{h}m"] = float(rv.quantile(0.25))
        row[f"q75_dir_ret_{h}m"] = float(rv.quantile(0.75))
        row[f"median_net_after_fee_{h}m"] = float(rv.median() - ROUNDTRIP_FEE_PCT)
        row[f"mean_net_after_fee_{h}m"] = float(rv.mean() - ROUNDTRIP_FEE_PCT)
        if fav is not None:
            fv = fav.astype(float)[valid]
            row[f"median_fav_{h}m"] = float(fv.median()) if fv.notna().any() else None
        if adv is not None:
            av = adv.astype(float)[valid]
            row[f"median_adv_{h}m"] = float(av.median()) if av.notna().any() else None
    return row


def touch_levels_for_tf(tf: str) -> tuple[float, ...]:
    if tf in ("1h", "4h"):
        return FIRST_TOUCH_LEVELS_BASE + FIRST_TOUCH_EXTRA_HTF
    return FIRST_TOUCH_LEVELS_BASE
