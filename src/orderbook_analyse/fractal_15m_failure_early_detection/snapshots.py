"""Load waves/candles and build causal intra-wave snapshots."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_early_detection import (
    FAILURE_EVENTS,
    MIN_ABS_STOCH,
    SNAPSHOT_OFFSETS_MIN,
    SYMBOL,
    WAVE_DIR,
    WEAK_PRICE_ABS,
)
from orderbook_analyse.fractal_15m_failure_early_detection.indicators_np import (
    ewm_last,
    stochastic_rsi_last,
    zone_of,
)
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_directional_control.load_join import asof_last_completed


def _bool(s: pd.Series) -> pd.Series:
    return s.map(
        lambda x: True
        if str(x).lower() in ("1", "true", "yes")
        else (False if str(x).lower() in ("0", "false", "no") else False)
    )


def load_failure_labels(path: Path | str = FAILURE_EVENTS) -> pd.DataFrame:
    fe = pd.read_csv(
        path, usecols=["wave_i", "failure_type", "decision_time", "expected_reversal"]
    )
    fe["decision_time"] = pd.to_datetime(fe["decision_time"], utc=True)
    return fe


def load_waves_15m(wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    path = Path(wave_dir) / "waves_15m.csv"
    cols = [
        "direction",
        "start_i",
        "end_i",
        "n_bars",
        "start_ts",
        "end_ts",
        "start_available_at",
        "end_available_at",
        "start_price",
        "end_price",
        "signed_price_move_pct",
        "directional_efficiency",
        "stoch_k_start",
        "stoch_k_end",
        "stoch_delta",
        "rsi_start",
        "inefficient_flag",
    ]
    df = pd.read_csv(path, usecols=cols)
    for c in ("start_ts", "end_ts", "start_available_at", "end_available_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    df["inefficient_flag"] = _bool(df["inefficient_flag"])
    df = df.sort_values("end_available_at").reset_index(drop=True)
    df["wave_i"] = np.arange(len(df), dtype=np.int64)
    return df


def load_micro_waves(tf: str, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    path = Path(wave_dir) / f"waves_{tf}.csv"
    df = pd.read_csv(
        path,
        usecols=["direction", "signed_price_move_pct", "directional_efficiency", "end_available_at"],
    )
    df["end_available_at"] = pd.to_datetime(df["end_available_at"], utc=True)
    return df.sort_values("end_available_at").reset_index(drop=True)


def prepare_ohlcv(tf: str, *, symbol: str = SYMBOL) -> pd.DataFrame:
    raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe=tf)
    df = raw.sort_values("available_at").drop_duplicates("available_at").reset_index(drop=True)
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def estimate_15m_state_np(
    *,
    close15: np.ndarray,
    avail15: np.ndarray,
    ts15: np.ndarray,
    close1: np.ndarray,
    high1: np.ndarray,
    low1: np.ndarray,
    avail1: np.ndarray,
    snap_t: np.datetime64,
    hist: int = 120,
) -> dict:
    n15 = int(np.searchsorted(avail15, snap_t, side="right"))
    if n15 <= 0:
        return {
            "stoch_k": np.nan,
            "stoch_d": np.nan,
            "stoch_kd": np.nan,
            "rsi": np.nan,
            "ema9": np.nan,
            "ema20": np.nan,
            "price_vs_ema20": "NA",
            "ema9_vs_ema20": "NA",
            "stoch_zone": "NA",
            "stoch_dir": "NA",
        }

    if n15 < len(ts15):
        period_start = ts15[n15]
    else:
        period_start = ts15[n15 - 1] + np.timedelta64(15, "m")

    i1 = int(np.searchsorted(avail1, snap_t, side="right"))
    form_close = np.nan
    if i1 > 0:
        left = int(np.searchsorted(avail1, np.datetime64(period_start, "ns"), side="right"))
        left = min(max(left, 0), i1)
        if left < i1:
            form_close = float(close1[i1 - 1])

    lo = max(0, n15 - hist)
    c = close15[lo:n15].astype(float).copy()
    if np.isfinite(form_close):
        c = np.append(c, form_close)
    if len(c) < 40:
        return {
            "stoch_k": np.nan,
            "stoch_d": np.nan,
            "stoch_kd": np.nan,
            "rsi": np.nan,
            "ema9": np.nan,
            "ema20": np.nan,
            "price_vs_ema20": "NA",
            "ema9_vs_ema20": "NA",
            "stoch_zone": "NA",
            "stoch_dir": "NA",
        }

    kv, dv, rv = stochastic_rsi_last(c)
    e9 = ewm_last(c, 9)
    e20 = ewm_last(c, 20)
    px = float(c[-1])
    pve = "ABOVE" if px > e20 else ("BELOW" if px < e20 else "AT")
    e9v = "BULL" if e9 > e20 else ("BEAR" if e9 < e20 else "FLAT")
    sdir = "UP" if kv > dv else ("DOWN" if kv < dv else "FLAT")
    return {
        "stoch_k": kv,
        "stoch_d": dv,
        "stoch_kd": kv - dv if np.isfinite(kv) and np.isfinite(dv) else np.nan,
        "rsi": rv,
        "ema9": e9,
        "ema20": e20,
        "price_vs_ema20": pve,
        "ema9_vs_ema20": e9v,
        "stoch_zone": zone_of(kv),
        "stoch_dir": sdir,
    }


def _eff_decay_label(eff_first: float, eff_second: float) -> str:
    if not np.isfinite(eff_first) or not np.isfinite(eff_second):
        return "NA"
    # simple fixed bands — not optimized
    if eff_second < eff_first - 0.001:
        return "EFFICIENCY_DECAYING"
    if abs(eff_second - eff_first) <= 0.001:
        return "EFFICIENCY_STABLE"
    return "EFFICIENCY_IMPROVING"


def build_snapshots(
    *,
    waves: pd.DataFrame,
    labels: pd.DataFrame,
    c1: pd.DataFrame,
    c15: pd.DataFrame,
    waves_1m: pd.DataFrame,
    waves_5m: pd.DataFrame,
) -> pd.DataFrame:
    fail_map = labels.set_index("wave_i")["failure_type"].to_dict()
    exp_map = labels.set_index("wave_i")["expected_reversal"].to_dict()

    avail1 = c1["available_at"].to_numpy(dtype="datetime64[ns]")
    close1 = c1["close"].astype(float).to_numpy()
    high1 = c1["high"].astype(float).to_numpy()
    low1 = c1["low"].astype(float).to_numpy()

    avail15 = c15["available_at"].to_numpy(dtype="datetime64[ns]")
    close15 = c15["close"].astype(float).to_numpy()
    ts15 = c15["timestamp"].to_numpy(dtype="datetime64[ns]")

    rows: list[dict] = []
    persist_rows: list[dict] = []
    n_w = len(waves)
    est_cache: dict[tuple[int, float], dict] = {}

    def _cached_est(snap_t: np.datetime64) -> dict:
        n15 = int(np.searchsorted(avail15, snap_t, side="right"))
        i1 = int(np.searchsorted(avail1, snap_t, side="right"))
        form_close = np.nan
        if n15 > 0 and i1 > 0:
            if n15 < len(ts15):
                period_start = ts15[n15]
            else:
                period_start = ts15[n15 - 1] + np.timedelta64(15, "m")
            left = int(np.searchsorted(avail1, np.datetime64(period_start, "ns"), side="right"))
            left = min(max(left, 0), i1)
            if left < i1:
                form_close = float(close1[i1 - 1])
        key = (n15, round(form_close, 5) if np.isfinite(form_close) else -1.0)
        if key in est_cache:
            return est_cache[key]
        st = estimate_15m_state_np(
            close15=close15,
            avail15=avail15,
            ts15=ts15,
            close1=close1,
            high1=high1,
            low1=low1,
            avail1=avail1,
            snap_t=snap_t,
        )
        est_cache[key] = st
        return st

    for wi in range(n_w):
        if wi % 2000 == 0:
            print(f"[snap] wave {wi}/{n_w}", flush=True)
        w = waves.iloc[wi]
        t0 = np.datetime64(pd.Timestamp(w["start_available_at"]).to_datetime64())
        t_end = np.datetime64(pd.Timestamp(w["end_available_at"]).to_datetime64())
        start_px = float(w["start_price"])
        direction = str(w["direction"])
        k0 = float(w["stoch_k_start"])
        rsi0 = float(w["rsi_start"])
        wave_i = int(w["wave_i"])
        is_fail = wave_i in fail_map
        ftype = fail_map.get(wave_i)
        expected = exp_map.get(wave_i) or ("DOWN" if direction == "UP" else "UP")

        # --- persistence on 1m grid: consecutive bars with signed price fail ---
        left_all = int(np.searchsorted(avail1, t0, side="right"))
        right_all = int(np.searchsorted(avail1, t_end, side="right"))
        streak = 0
        max_streak = 0
        for j in range(left_all, right_all):
            px = float(close1[j])
            price_move = (px / start_px - 1.0) * 100.0 if start_px else np.nan
            signed = price_move if direction == "UP" else -price_move
            bad = np.isfinite(signed) and signed <= 0.0
            if bad:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        persist_rows.append(
            {
                "wave_i": wave_i,
                "direction": direction,
                "is_later_failure": is_fail,
                "failure_type": ftype,
                "max_partial_fail_streak_1m": int(max_streak),
            }
        )

        # --- fixed offset snapshots ---
        for off in SNAPSHOT_OFFSETS_MIN:
            snap_t = t0 + np.timedelta64(int(off), "m")
            if off < 15 and snap_t > t_end:
                continue
            if off == 15:
                snap_t = min(snap_t, t_end)

            j = int(np.searchsorted(avail1, snap_t, side="right") - 1)
            if j < 0:
                continue
            px = float(close1[j])
            left = int(np.searchsorted(avail1, t0, side="right"))
            if left > j:
                hh = ll = start_px
            else:
                hh = float(np.max(high1[left : j + 1]))
                ll = float(np.min(low1[left : j + 1]))

            price_move = (px / start_px - 1.0) * 100.0 if start_px else np.nan
            if direction == "UP":
                signed = price_move
                fav = (hh / start_px - 1.0) * 100.0 if start_px else np.nan
                adv = (ll / start_px - 1.0) * 100.0 if start_px else np.nan
            else:
                signed = -price_move
                fav = (ll / start_px - 1.0) * 100.0 if start_px else np.nan
                adv = (hh / start_px - 1.0) * 100.0 if start_px else np.nan

            st = _cached_est(snap_t)
            k_now = float(st["stoch_k"]) if st["stoch_k"] == st["stoch_k"] else np.nan
            partial_stoch = (k_now - k0) if np.isfinite(k_now) and np.isfinite(k0) else np.nan
            if direction == "UP":
                stoch_with = bool(np.isfinite(partial_stoch) and partial_stoch > 0)
            else:
                stoch_with = bool(np.isfinite(partial_stoch) and partial_stoch < 0)

            abs_stoch = abs(partial_stoch) if np.isfinite(partial_stoch) else np.nan
            if np.isfinite(signed) and np.isfinite(abs_stoch) and abs_stoch > MIN_ABS_STOCH:
                partial_eff = float(signed) / float(abs_stoch)
            else:
                partial_eff = np.nan

            price_fail = (not np.isfinite(signed)) or (signed <= 0.0) or (
                np.isfinite(price_move) and abs(price_move) <= WEAK_PRICE_ABS
            )
            early_cand = bool(stoch_with and (price_fail or (np.isfinite(partial_eff) and partial_eff <= 0.0)))

            # efficiency decay: first vs second half signed price response (fixed, no opt)
            mid_j = left + max(1, (j - left + 1) // 2) - 1
            mid_j = min(max(mid_j, left), j)
            if left <= mid_j <= j and start_px:
                px_mid = float(close1[mid_j])
                move1 = (px_mid / start_px - 1.0) * 100.0
                move2 = (px / px_mid - 1.0) * 100.0 if px_mid else np.nan
                signed1 = move1 if direction == "UP" else -move1
                signed2 = move2 if direction == "UP" else -move2
                # normalize by minutes in each half (fixed)
                n1 = max(1, mid_j - left + 1)
                n2 = max(1, j - mid_j)
                eff1 = float(signed1) / n1
                eff2 = float(signed2) / n2 if np.isfinite(signed2) else np.nan
                decay = _eff_decay_label(eff1, eff2)
            else:
                decay = "NA"
                eff1 = eff2 = np.nan

            rsi_now = st["rsi"]
            rows.append(
                {
                    "wave_i": wave_i,
                    "direction": direction,
                    "offset_min": int(off),
                    "snapshot_time": pd.Timestamp(snap_t, tz="UTC"),
                    "wave_start_available_at": pd.Timestamp(t0, tz="UTC"),
                    "wave_end_available_at": pd.Timestamp(t_end, tz="UTC"),
                    "is_later_failure": bool(is_fail),
                    "failure_type": ftype,
                    "expected_reversal": expected,
                    "start_price": start_px,
                    "snapshot_price": px,
                    "partial_price_move_pct": price_move,
                    "partial_signed_price_move_pct": signed,
                    "partial_fav_pct": fav,
                    "partial_adv_pct": adv,
                    "wave_high_so_far": hh,
                    "wave_low_so_far": ll,
                    "stoch_k_start": k0,
                    "stoch_k": k_now,
                    "stoch_d": st["stoch_d"],
                    "stoch_kd": st["stoch_kd"],
                    "partial_stoch_move": partial_stoch,
                    "stoch_with_wave": stoch_with,
                    "stoch_dir_est": st["stoch_dir"],
                    "stoch_zone_est": st["stoch_zone"],
                    "partial_directional_efficiency": partial_eff,
                    "early_failure_candidate": early_cand,
                    "rsi": rsi_now,
                    "rsi_start": rsi0,
                    "rsi_delta": (rsi_now - rsi0) if np.isfinite(rsi_now) and np.isfinite(rsi0) else np.nan,
                    "rsi_gt50": bool(np.isfinite(rsi_now) and rsi_now > 50),
                    "rsi_lt50": bool(np.isfinite(rsi_now) and rsi_now < 50),
                    "rsi_falling": bool(
                        np.isfinite(rsi_now) and np.isfinite(rsi0) and (rsi_now - rsi0) < 0
                    ),
                    "rsi_rising": bool(
                        np.isfinite(rsi_now) and np.isfinite(rsi0) and (rsi_now - rsi0) > 0
                    ),
                    "price_vs_ema20": st["price_vs_ema20"],
                    "ema9_vs_ema20": st["ema9_vs_ema20"],
                    "eff_first_half": eff1,
                    "eff_second_half": eff2,
                    "efficiency_path": decay,
                    "minutes_to_wave_end": float(
                        (pd.Timestamp(t_end, tz="UTC") - pd.Timestamp(snap_t, tz="UTC")).total_seconds()
                        / 60.0
                    ),
                    "max_partial_fail_streak_1m": int(max_streak),
                }
            )

    snap = pd.DataFrame(rows)
    persist = pd.DataFrame(persist_rows)
    if snap.empty:
        return snap, persist

    print("[snap] join 1m/5m wave context", flush=True)
    times = snap["snapshot_time"].to_numpy(dtype="datetime64[ns]")
    for waves_m, pref in ((waves_1m, "m1"), (waves_5m, "m5")):
        joined = asof_last_completed(
            waves_m,
            times,
            ["direction", "signed_price_move_pct", "directional_efficiency", "end_available_at"],
            pref,
        )
        snap = pd.concat([snap.reset_index(drop=True), joined], axis=1)
        snap[f"{pref}_counter"] = snap[f"{pref}_direction"].astype(str) != snap["direction"].astype(
            str
        )
        snap[f"{pref}_aligned"] = snap[f"{pref}_direction"].astype(str) == snap["direction"].astype(
            str
        )

    up = snap["direction"].astype(str) == "UP"
    snap["overlay_rsi_against"] = (up & snap["rsi_falling"]) | ((~up) & snap["rsi_rising"])
    snap["overlay_rsi_side"] = (up & snap["rsi_lt50"]) | ((~up) & snap["rsi_gt50"])
    snap["overlay_m5_counter"] = snap["m5_counter"].fillna(False).astype(bool)
    snap["overlay_m1_counter"] = snap["m1_counter"].fillna(False).astype(bool)
    return snap, persist
