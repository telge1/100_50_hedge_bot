"""Path outcomes, delay entries, pullback, micro-wait, first-touch."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_confirmation_entry import (
    ENTRY_DELAYS_MIN,
    FIRST_TOUCH_LEVELS,
    FORWARD_HORIZONS_MIN,
    MIN_SAMPLE,
    PULLBACK_BUCKETS,
    ROUNDTRIP_FEE_PCT,
    VERY_SMALL,
)
from orderbook_analyse.fractal_15m_failure_confirmation_entry.events import first_open_after


def _sign_for_side(side: str) -> float:
    return -1.0 if side == "SHORT" else 1.0


def path_metrics_from_entry(
    *,
    entry_i: int,
    entry_px: float,
    side: str,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    open_times: np.ndarray,
    horizons: tuple[int, ...] = FORWARD_HORIZONS_MIN,
) -> dict[str, Any]:
    """Directional metrics using 1m path after entry bar (bars entry_i+1 ...)."""
    out: dict[str, Any] = {}
    if entry_i < 0 or not np.isfinite(entry_px) or entry_px == 0:
        for h in horizons:
            out[f"dir_ret_{h}m"] = np.nan
            out[f"dir_fav_{h}m"] = np.nan
            out[f"dir_adv_{h}m"] = np.nan
        return out
    sign = _sign_for_side(side)
    n = len(close)
    for h in horizons:
        # horizon end = entry open_time + h minutes
        t_h = open_times[entry_i] + np.timedelta64(int(h), "m")
        i_h = int(np.searchsorted(open_times, t_h, side="right") - 1)
        if i_h <= entry_i or i_h >= n:
            out[f"dir_ret_{h}m"] = np.nan
            out[f"dir_fav_{h}m"] = np.nan
            out[f"dir_adv_{h}m"] = np.nan
            continue
        raw = (float(close[i_h]) / entry_px - 1.0) * 100.0
        sl_h = high[entry_i + 1 : i_h + 1]
        sl_l = low[entry_i + 1 : i_h + 1]
        if sl_h.size == 0:
            fav = adv = np.nan
        else:
            mfe = (float(np.max(sl_h)) / entry_px - 1.0) * 100.0
            mae = (float(np.min(sl_l)) / entry_px - 1.0) * 100.0
            if side == "LONG":
                fav, adv = mfe, mae
            else:
                fav, adv = -mae, -mfe
        out[f"dir_ret_{h}m"] = raw * sign
        out[f"dir_fav_{h}m"] = fav
        out[f"dir_adv_{h}m"] = adv
    return out


def metrics(sub: pd.DataFrame, **meta) -> dict[str, Any]:
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
    for h in FORWARD_HORIZONS_MIN:
        col = f"dir_ret_{h}m"
        if col not in sub.columns:
            continue
        r = sub[col].astype(float)
        fav = sub[f"dir_fav_{h}m"].astype(float)
        adv = sub[f"dir_adv_{h}m"].astype(float)
        row[f"hit_rate_{h}m"] = float((r > 0).mean()) if r.notna().any() else None
        row[f"median_dir_ret_{h}m"] = float(r.median()) if r.notna().any() else None
        row[f"mean_dir_ret_{h}m"] = float(r.mean()) if r.notna().any() else None
        row[f"q25_dir_ret_{h}m"] = float(r.quantile(0.25)) if r.notna().any() else None
        row[f"q75_dir_ret_{h}m"] = float(r.quantile(0.75)) if r.notna().any() else None
        row[f"median_fav_{h}m"] = float(fav.median()) if fav.notna().any() else None
        row[f"median_adv_{h}m"] = float(adv.median()) if adv.notna().any() else None
        row[f"median_dir_ret_{h}m_net_fee"] = (
            float((r - ROUNDTRIP_FEE_PCT).median()) if r.notna().any() else None
        )
    return row


def build_delay_entries(events: pd.DataFrame, c1: pd.DataFrame) -> pd.DataFrame:
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
    opens = c1["open"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()

    rows: list[dict] = []
    for ev in events.itertuples(index=False):
        conf = np.datetime64(pd.Timestamp(ev.confirmation_available_at).to_datetime64())
        # confirmation reference price: last 1m close with available_at <= confirmation
        avail = c1["available_at"].to_numpy(dtype="datetime64[ns]")
        ic = int(np.searchsorted(avail, conf, side="right") - 1)
        conf_px = float(close[ic]) if ic >= 0 else np.nan

        for delay in ENTRY_DELAYS_MIN:
            decision = conf + np.timedelta64(int(delay), "m")
            ei, epx, et = first_open_after(open_times, opens, decision)
            if ei < 0:
                continue
            m = path_metrics_from_entry(
                entry_i=ei,
                entry_px=epx,
                side=str(ev.side),
                close=close,
                high=high,
                low=low,
                open_times=open_times,
            )
            rows.append(
                {
                    "wave_i": int(ev.wave_i),
                    "failure_type": ev.failure_type,
                    "side": ev.side,
                    "expected_reversal": ev.expected_reversal,
                    "confirmation_available_at": pd.Timestamp(conf, tz="UTC"),
                    "delay_min": int(delay),
                    "decision_time": pd.Timestamp(decision, tz="UTC"),
                    "entry_time": pd.Timestamp(et, tz="UTC"),
                    "entry_price": epx,
                    "confirmation_ref_price": conf_px,
                    "entry_i": ei,
                    **m,
                }
            )
    return pd.DataFrame(rows)


def micro_state_at(
    waves: pd.DataFrame,
    t: np.datetime64,
    *,
    expected_reversal: str,
) -> dict[str, Any]:
    ends = waves["end_available_at"].to_numpy(dtype="datetime64[ns]")
    j = int(np.searchsorted(ends, t, side="right") - 1)
    if j < 0:
        return {
            "direction": None,
            "signed_price_move_pct": np.nan,
            "directional_efficiency": np.nan,
            "rsi_end": np.nan,
            "stoch_zone_end": None,
            "stoch_state_end": None,
            "align": "MIXED",
        }
    d = str(waves.iloc[j]["direction"])
    if d == expected_reversal:
        align = "ALIGNED"
    elif d in ("UP", "DOWN"):
        align = "COUNTER"
    else:
        align = "MIXED"
    return {
        "direction": d,
        "signed_price_move_pct": float(waves.iloc[j]["signed_price_move_pct"]),
        "directional_efficiency": float(waves.iloc[j]["directional_efficiency"]),
        "rsi_end": float(waves.iloc[j]["rsi_end"]),
        "stoch_zone_end": waves.iloc[j]["stoch_zone_end"],
        "stoch_state_end": waves.iloc[j]["stoch_state_end"],
        "align": align,
        "wave_end_available_at": waves.iloc[j]["end_available_at"],
    }


def attach_micro_at_entry(
    delay_df: pd.DataFrame,
    waves_1m: pd.DataFrame,
    waves_5m: pd.DataFrame,
) -> pd.DataFrame:
    """Attach 1m/5m state at entry_time (as-of completed waves)."""
    out = delay_df.reset_index(drop=True).copy()
    times = out["entry_time"].to_numpy(dtype="datetime64[ns]")
    exp = out["expected_reversal"].astype(str).to_numpy()

    def _join(waves: pd.DataFrame, prefix: str) -> None:
        ends = waves["end_available_at"].to_numpy(dtype="datetime64[ns]")
        idx = np.searchsorted(ends, times, side="right") - 1
        dirs = waves["direction"].astype(str).to_numpy()
        signed = waves["signed_price_move_pct"].astype(float).to_numpy()
        eff = waves["directional_efficiency"].astype(float).to_numpy()
        rsi = waves["rsi_end"].astype(float).to_numpy()
        zone = waves["stoch_zone_end"].astype(str).to_numpy()
        state = waves["stoch_state_end"].astype(str).to_numpy()
        n = len(times)
        d_out = np.array([None] * n, dtype=object)
        s_out = np.full(n, np.nan)
        e_out = np.full(n, np.nan)
        r_out = np.full(n, np.nan)
        z_out = np.array([None] * n, dtype=object)
        st_out = np.array([None] * n, dtype=object)
        align = np.array(["MIXED"] * n, dtype=object)
        ok = idx >= 0
        ii = idx[ok]
        d_out[ok] = dirs[ii]
        s_out[ok] = signed[ii]
        e_out[ok] = eff[ii]
        r_out[ok] = rsi[ii]
        z_out[ok] = zone[ii]
        st_out[ok] = state[ii]
        align[ok & (d_out == exp)] = "ALIGNED"
        # counter when direction opposite
        align[ok & (d_out != exp) & np.isin(d_out, ["UP", "DOWN"])] = "COUNTER"
        # fix: d_out is object; compare carefully
        for i in np.flatnonzero(ok):
            if d_out[i] == exp[i]:
                align[i] = "ALIGNED"
            elif d_out[i] in ("UP", "DOWN"):
                align[i] = "COUNTER"
            else:
                align[i] = "MIXED"
        out[f"{prefix}_direction"] = d_out
        out[f"{prefix}_signed"] = s_out
        out[f"{prefix}_eff"] = e_out
        out[f"{prefix}_rsi"] = r_out
        out[f"{prefix}_stoch_zone"] = z_out
        out[f"{prefix}_stoch_state"] = st_out
        out[f"{prefix}_align"] = align

    _join(waves_1m, "m1")
    _join(waves_5m, "m5")
    return out


def wait_for_micro_realign(
    events: pd.DataFrame,
    c1: pd.DataFrame,
    waves_1m: pd.DataFrame,
    waves_5m: pd.DataFrame,
) -> pd.DataFrame:
    """Strategies A/B/C/D entry times after confirmation."""
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
    opens = c1["open"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    ends1 = waves_1m["end_available_at"].to_numpy(dtype="datetime64[ns]")
    dir1 = waves_1m["direction"].astype(str).to_numpy()
    ends5 = waves_5m["end_available_at"].to_numpy(dtype="datetime64[ns]")
    dir5 = waves_5m["direction"].astype(str).to_numpy()

    rows = []
    for ev in events.itertuples(index=False):
        conf = np.datetime64(pd.Timestamp(ev.confirmation_available_at).to_datetime64())
        exp = str(ev.expected_reversal)
        side = str(ev.side)

        # A immediate
        variants: list[tuple[str, np.datetime64 | None]] = [("A_immediate", conf)]

        # B first 1m wave in reversal direction with end >= conf
        j0 = int(np.searchsorted(ends1, conf, side="left"))
        t_b = None
        for j in range(j0, len(ends1)):
            if ends1[j] < conf:
                continue
            if dir1[j] == exp:
                t_b = ends1[j]
                break
        variants.append(("B_wait_1m_realign", t_b))

        # C first 5m
        j0 = int(np.searchsorted(ends5, conf, side="left"))
        t_c = None
        for j in range(j0, len(ends5)):
            if ends5[j] < conf:
                continue
            if dir5[j] == exp:
                t_c = ends5[j]
                break
        variants.append(("C_wait_5m_realign", t_c))

        # D both: max of the two times if both exist
        t_d = None
        if t_b is not None and t_c is not None:
            t_d = max(t_b, t_c)
        variants.append(("D_wait_1m_and_5m", t_d))

        for name, t_dec in variants:
            if t_dec is None or (isinstance(t_dec, float) and np.isnan(t_dec)):
                rows.append(
                    {
                        "wave_i": int(ev.wave_i),
                        "side": side,
                        "strategy": name,
                        "filled": False,
                        "wait_min": np.nan,
                    }
                )
                continue
            ei, epx, et = first_open_after(open_times, opens, t_dec)
            if ei < 0:
                rows.append(
                    {
                        "wave_i": int(ev.wave_i),
                        "side": side,
                        "strategy": name,
                        "filled": False,
                        "wait_min": np.nan,
                    }
                )
                continue
            wait = (pd.Timestamp(et, tz="UTC") - pd.Timestamp(conf, tz="UTC")).total_seconds() / 60.0
            m = path_metrics_from_entry(
                entry_i=ei,
                entry_px=epx,
                side=side,
                close=close,
                high=high,
                low=low,
                open_times=open_times,
            )
            rows.append(
                {
                    "wave_i": int(ev.wave_i),
                    "side": side,
                    "failure_type": ev.failure_type,
                    "strategy": name,
                    "filled": True,
                    "wait_min": wait,
                    "decision_time": pd.Timestamp(t_dec, tz="UTC"),
                    "entry_time": pd.Timestamp(et, tz="UTC"),
                    "entry_price": epx,
                    **m,
                }
            )
    return pd.DataFrame(rows)


def pullback_entries(events: pd.DataFrame, c1: pd.DataFrame) -> pd.DataFrame:
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
    opens = c1["open"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    avail = c1["available_at"].to_numpy(dtype="datetime64[ns]")

    rows = []
    # immediate baseline per event for opportunity compare
    for ev in events.itertuples(index=False):
        conf = np.datetime64(pd.Timestamp(ev.confirmation_available_at).to_datetime64())
        side = str(ev.side)
        ic = int(np.searchsorted(avail, conf, side="right") - 1)
        if ic < 0:
            continue
        ref = float(close[ic])
        # T0 immediate
        ei0, epx0, et0 = first_open_after(open_times, opens, conf)
        imm = None
        if ei0 >= 0:
            imm = path_metrics_from_entry(
                entry_i=ei0,
                entry_px=epx0,
                side=side,
                close=close,
                high=high,
                low=low,
                open_times=open_times,
            )
            imm_ret60 = imm.get("dir_ret_60m")
        else:
            imm_ret60 = np.nan

        # scan path after confirmation for pullback touch (causal at bar available_at)
        j0 = int(np.searchsorted(avail, conf, side="right"))
        max_look = min(len(close), j0 + 240)  # up to ~4h

        for bname, lo_pct, hi_pct in PULLBACK_BUCKETS:
            touch_j = -1
            for j in range(j0, max_look):
                if side == "SHORT":
                    move = (float(high[j]) / ref - 1.0) * 100.0
                else:
                    move = ((ref - float(low[j])) / ref) * 100.0 if ref else np.nan
                if not np.isfinite(move):
                    continue
                ok = move >= lo_pct and (True if hi_pct == float("inf") else move < hi_pct)
                if ok:
                    touch_j = j
                    break
            if touch_j < 0:
                rows.append(
                    {
                        "wave_i": int(ev.wave_i),
                        "side": side,
                        "bucket": bname,
                        "filled": False,
                        "missed": True,
                        "wait_min": np.nan,
                        "entry_improvement_pct": np.nan,
                        "imm_dir_ret_60m": imm_ret60,
                    }
                )
                continue
            touch_t = avail[touch_j]
            ei, epx, et = first_open_after(open_times, opens, touch_t)
            if ei < 0:
                rows.append(
                    {
                        "wave_i": int(ev.wave_i),
                        "side": side,
                        "bucket": bname,
                        "filled": False,
                        "missed": True,
                        "wait_min": np.nan,
                        "entry_improvement_pct": np.nan,
                        "imm_dir_ret_60m": imm_ret60,
                    }
                )
                continue
            wait = (pd.Timestamp(et, tz="UTC") - pd.Timestamp(conf, tz="UTC")).total_seconds() / 60.0
            # improvement vs T0 entry: for SHORT higher entry better; LONG lower better
            if ei0 >= 0 and np.isfinite(epx0) and epx0:
                if side == "SHORT":
                    entry_imp = (epx / epx0 - 1.0) * 100.0
                else:
                    entry_imp = (epx0 / epx - 1.0) * 100.0
            else:
                entry_imp = np.nan
            m = path_metrics_from_entry(
                entry_i=ei,
                entry_px=epx,
                side=side,
                close=close,
                high=high,
                low=low,
                open_times=open_times,
            )
            rows.append(
                {
                    "wave_i": int(ev.wave_i),
                    "side": side,
                    "failure_type": ev.failure_type,
                    "bucket": bname,
                    "filled": True,
                    "missed": False,
                    "wait_min": wait,
                    "entry_improvement_pct": entry_imp,
                    "entry_price": epx,
                    "imm_entry_price": epx0 if ei0 >= 0 else np.nan,
                    "imm_dir_ret_60m": imm_ret60,
                    **m,
                }
            )
    return pd.DataFrame(rows)


def first_touch_analysis(delay_t0: pd.DataFrame, c1: pd.DataFrame) -> pd.DataFrame:
    """For T0 entries: which side hits +/- level first."""
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
    close = c1["close"].astype(float).to_numpy()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    rows = []
    sub = delay_t0[delay_t0["delay_min"] == 0]
    for r in sub.itertuples(index=False):
        ei = int(r.entry_i)
        epx = float(r.entry_price)
        side = str(r.side)
        if ei < 0 or not np.isfinite(epx):
            continue
        max_j = min(len(close), ei + 240)
        for lvl in FIRST_TOUCH_LEVELS:
            first = "none"
            t_fav = t_adv = np.nan
            for j in range(ei + 1, max_j):
                up = (float(high[j]) / epx - 1.0) * 100.0
                dn = (float(low[j]) / epx - 1.0) * 100.0
                if side == "LONG":
                    hit_fav = up >= lvl
                    hit_adv = dn <= -lvl
                else:
                    hit_fav = dn <= -lvl
                    hit_adv = up >= lvl
                if hit_fav and hit_adv:
                    # same bar both — count as simultaneous
                    first = "both_same_bar"
                    t_fav = t_adv = (open_times[j] - open_times[ei]) / np.timedelta64(1, "m")
                    break
                if hit_fav:
                    first = "favorable_first"
                    t_fav = (open_times[j] - open_times[ei]) / np.timedelta64(1, "m")
                    break
                if hit_adv:
                    first = "adverse_first"
                    t_adv = (open_times[j] - open_times[ei]) / np.timedelta64(1, "m")
                    break
            rows.append(
                {
                    "wave_i": int(r.wave_i),
                    "side": side,
                    "level_pct": lvl,
                    "first_touch": first,
                    "min_to_fav": t_fav,
                    "min_to_adv": t_adv,
                    "dir_ret_60m": getattr(r, "dir_ret_60m", np.nan),
                }
            )
    return pd.DataFrame(rows)
