"""Causal lower-TF attach, classification, path outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure.phase import cycle_phase_from_wave
from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.waves import segment_stoch_waves
from orderbook_analyse.fractal_directional_control.load_join import asof_last_completed
from orderbook_analyse.fractal_parent_signal_lower_tf_context import (
    COUNT_TFS,
    EVENTS_PATH,
    FEE_PCT,
    HORIZONS,
    LOWER_TFS,
    MIN_SAMPLE,
    REACH_LEVELS,
    STAGING_1M_DIR,
    VERY_SMALL,
    WAVE_CACHE_ROOT,
)
from orderbook_analyse.fractal_wave_fade_tier_tpsl.simulate import assign_tier
from orderbook_analyse.mtf_rsi_stoch_audit.data import load_base_1m


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def load_tier_a_parents() -> pd.DataFrame:
    usecols = [
        "symbol",
        "timeframe",
        "wave_i",
        "direction",
        "side",
        "ema_context",
        "trend_bucket",
        "eff_quantile",
        "confirmation_available_at",
        "entry_time",
        "entry_price",
    ]
    df = pd.read_csv(EVENTS_PATH, usecols=usecols)
    df = df[df["timeframe"].isin(("1h", "4h"))].copy()
    df["confirmation_available_at"] = pd.to_datetime(df["confirmation_available_at"], utc=True)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["tier"] = [
        assign_tier(t, q) for t, q in zip(df["trend_bucket"], df["eff_quantile"])
    ]
    df = df[df["tier"] == "A"].reset_index(drop=True)
    return df


def _parse_wave_times(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("start_available_at", "end_available_at", "start_ts", "end_ts"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], utc=True)
    out = out.sort_values("end_available_at").reset_index(drop=True)
    return out


def ensure_1m_waves(symbol: str, cache_dir: Path) -> pd.DataFrame:
    """Build 1m waves from local staging feather (no download); cache CSV."""
    path = cache_dir / "waves_1m.csv"
    if path.exists():
        print(f"[waves] load cache {symbol} 1m", flush=True)
        return _parse_wave_times(pd.read_csv(path))
    print(f"[waves] build {symbol} 1m from local feather …", flush=True)
    raw = load_base_1m(symbol, candle_dir=STAGING_1M_DIR)
    # align columns for attach_indicators
    if "available_at" not in raw.columns:
        raw = raw.copy()
        raw["available_at"] = pd.to_datetime(raw["timestamp"], utc=True) + pd.Timedelta(minutes=1)
    ind = attach_indicators(raw)
    waves = segment_stoch_waves(ind)
    waves["symbol"] = symbol
    waves["timeframe"] = "1m"
    cache_dir.mkdir(parents=True, exist_ok=True)
    waves.to_csv(path, index=False)
    print(f"[waves] {symbol} 1m: n={len(waves)}", flush=True)
    return _parse_wave_times(waves)


def load_symbol_waves(symbol: str, tfs: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    cache_dir = WAVE_CACHE_ROOT / symbol
    out: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        if tf == "1m":
            out[tf] = ensure_1m_waves(symbol, cache_dir)
            continue
        path = cache_dir / f"waves_{tf}.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        print(f"[waves] load cache {symbol} {tf}", flush=True)
        out[tf] = _parse_wave_times(pd.read_csv(path))
    return out


WAVE_COLS = [
    "direction",
    "stoch_zone_end",
    "stoch_k_end",
    "stoch_k_start",
    "end_available_at",
    "signed_price_move_pct",
]


def phase_from_dir_zone(direction: Any, zone: Any) -> str | None:
    d, z = str(direction), str(zone)
    if d == "UP" and z == "LOW":
        return "LOW_UP"
    if d == "UP" and z == "MID":
        return "MID_UP"
    if d == "UP" and z == "HIGH":
        return "HIGH_UP"
    if d == "DOWN" and z == "HIGH":
        return "HIGH_DOWN"
    if d == "DOWN" and z == "MID":
        return "MID_DOWN"
    if d == "DOWN" and z == "LOW":
        return "LOW_DOWN"
    return None


def relative_context(side: str, direction: Any, zone: Any, phase: Any) -> str:
    """Priority classification; raw phase kept separately."""
    z = str(zone) if zone is not None and str(zone) != "None" else None
    d = str(direction) if direction is not None and str(direction) != "None" else None
    ph = str(phase) if phase is not None and str(phase) != "None" else None
    if side == "SHORT":
        if z == "HIGH":
            return "FAVORABLE_EARLY"
        if ph == "MID_DOWN":
            return "FAVORABLE_MID"
        if z == "LOW":
            return "LATE"
        if d == "UP":
            return "COUNTER"
        return "OTHER"
    # LONG
    if z == "LOW":
        return "FAVORABLE_EARLY"
    if ph == "MID_UP":
        return "FAVORABLE_MID"
    if z == "HIGH":
        return "LATE"
    if d == "DOWN":
        return "COUNTER"
    return "OTHER"


def attach_lower_tf_context(
    events: pd.DataFrame,
    waves_by_tf: dict[str, pd.DataFrame],
    lower_tfs: tuple[str, ...],
) -> pd.DataFrame:
    """As-of last completed lower-TF wave at confirmation_available_at."""
    out = events.reset_index(drop=True).copy()
    times = out["confirmation_available_at"].to_numpy(dtype="datetime64[ns]")
    for tf in lower_tfs:
        pref = f"ltf_{tf}"
        joined = asof_last_completed(waves_by_tf[tf], times, WAVE_COLS, pref)
        for c in joined.columns:
            out[c] = joined[c].to_numpy()
        # rename stoch_k_end -> stoch_k for clarity
        out[f"{pref}_stoch_k"] = out[f"{pref}_stoch_k_end"]
        # D not in wave CSV end — approximate unused; leave NaN unless present
        out[f"{pref}_stoch_d"] = np.nan
        out[f"{pref}_zone"] = out[f"{pref}_stoch_zone_end"]
        phases = [
            phase_from_dir_zone(d, z)
            for d, z in zip(out[f"{pref}_direction"], out[f"{pref}_zone"])
        ]
        out[f"{pref}_phase"] = phases
        out[f"{pref}_rel"] = [
            relative_context(side, d, z, ph)
            for side, d, z, ph in zip(
                out["side"], out[f"{pref}_direction"], out[f"{pref}_zone"], phases
            )
        ]
    return out


def add_counts(df: pd.DataFrame, parent_tf: str) -> pd.DataFrame:
    out = df.copy()
    count_tfs = COUNT_TFS[parent_tf]

    exhausted = []
    ready = []
    for _, row in out.iterrows():
        side = str(row["side"])
        ex = 0
        rd = 0
        for tf in count_tfs:
            z = row.get(f"ltf_{tf}_zone")
            ph = row.get(f"ltf_{tf}_phase")
            if side == "SHORT":
                if str(z) == "LOW":
                    ex += 1
                if str(z) == "HIGH" or str(ph) == "HIGH_DOWN":
                    rd += 1
            else:
                if str(z) == "HIGH":
                    ex += 1
                if str(z) == "LOW" or str(ph) == "LOW_UP":
                    rd += 1
        exhausted.append(ex)
        ready.append(rd)
    out["exhausted_count"] = exhausted
    out["ready_count"] = ready
    return out


def phase_sequence_label(row: pd.Series, parent_tf: str) -> str:
    """Coarse sequence buckets A/B/C/D (+OTHER) for 30m/15m/5m (and 1h for 4h parent)."""
    side = str(row["side"])
    # use 30m,15m,5m always when present
    z30 = str(row.get("ltf_30m_zone"))
    z15 = str(row.get("ltf_15m_zone"))
    z5 = str(row.get("ltf_5m_zone"))
    p30 = str(row.get("ltf_30m_phase"))
    p15 = str(row.get("ltf_15m_phase"))
    p5 = str(row.get("ltf_5m_phase"))

    if side == "SHORT":
        if z30 == "HIGH" and z15 == "HIGH" and z5 in ("HIGH", "MID"):
            return "A_STACKED_HIGH"
        if p30 == "HIGH_DOWN" and p15 == "MID_DOWN" and "DOWN" in p5:
            return "B_TURNING_DOWN"
        if p30 == "MID_DOWN" and p15 == "LOW_DOWN" and z5 == "LOW":
            return "C_MID_TO_LOW"
        if z30 == "LOW" and z15 == "LOW" and z5 == "LOW":
            return "D_ALL_LOW"
        return "OTHER"
    # LONG mirror
    if z30 == "LOW" and z15 == "LOW" and z5 in ("LOW", "MID"):
        return "A_STACKED_LOW"
    if p30 == "LOW_UP" and p15 == "MID_UP" and "UP" in p5:
        return "B_TURNING_UP"
    if p30 == "MID_UP" and p15 == "HIGH_UP" and z5 == "HIGH":
        return "C_MID_TO_HIGH"
    if z30 == "HIGH" and z15 == "HIGH" and z5 == "HIGH":
        return "D_ALL_HIGH"
    return "OTHER"


def propagation_times(
    decision_t: pd.Timestamp,
    side: str,
    waves_by_tf: dict[str, pd.DataFrame],
    lower_tfs: tuple[str, ...],
    max_look_min: int = 7 * 24 * 60,
) -> dict[str, Any]:
    """First post-signal completed wave aligned with expected fade direction."""
    expected = "DOWN" if side == "SHORT" else "UP"
    t0 = np.datetime64(pd.Timestamp(decision_t).tz_convert("UTC").to_datetime64())
    t_max = t0 + np.timedelta64(int(max_look_min), "m")
    out: dict[str, Any] = {"expected_dir": expected, "first_aligned_tf": None}
    first_tf = None
    first_dt = None
    for tf in lower_tfs:
        w = waves_by_tf[tf]
        ends = w["end_available_at"].to_numpy(dtype="datetime64[ns]")
        dirs = w["direction"].astype(str).to_numpy()
        # first wave ending strictly after decision with aligned direction
        i0 = int(np.searchsorted(ends, t0, side="right"))
        mins = None
        for j in range(i0, len(ends)):
            if ends[j] > t_max:
                break
            if dirs[j] == expected:
                mins = float((ends[j] - t0) / np.timedelta64(1, "m"))
                break
        out[f"time_to_{tf}_align_min"] = mins
        if mins is not None and (first_dt is None or mins < first_dt):
            first_dt = mins
            first_tf = tf
    out["first_aligned_tf"] = first_tf
    out["first_aligned_min"] = first_dt
    return out


def path_outcomes(
    *,
    entry_i: int,
    entry_px: float,
    side: str,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if entry_i < 0 or not np.isfinite(entry_px) or entry_px <= 0:
        return out
    n = len(close)
    sign = -1.0 if side == "SHORT" else 1.0
    for h in horizons:
        t_h = open_times[entry_i] + np.timedelta64(int(h), "m")
        i_h = int(np.searchsorted(open_times, t_h, side="right") - 1)
        if i_h <= entry_i or i_h >= n:
            out[f"dir_ret_{h}m"] = np.nan
            out[f"mfe_{h}m"] = np.nan
            out[f"mae_{h}m"] = np.nan
            continue
        raw = (float(close[i_h]) / entry_px - 1.0) * 100.0
        sl_h = high[entry_i + 1 : i_h + 1]
        sl_l = low[entry_i + 1 : i_h + 1]
        if sl_h.size == 0:
            fav = adv = np.nan
        else:
            up = (float(np.max(sl_h)) / entry_px - 1.0) * 100.0
            dn = (float(np.min(sl_l)) / entry_px - 1.0) * 100.0
            if side == "LONG":
                fav, adv = up, dn
            else:
                fav, adv = -dn, -up
        out[f"dir_ret_{h}m"] = raw * sign
        out[f"mfe_{h}m"] = fav
        out[f"mae_{h}m"] = adv
    # reach + time to fav levels on max horizon path
    h_max = max(horizons)
    t_h = open_times[entry_i] + np.timedelta64(int(h_max), "m")
    i_h = int(np.searchsorted(open_times, t_h, side="right") - 1)
    i_h = min(n - 1, max(entry_i + 1, i_h))
    hh = high[entry_i + 1 : i_h + 1]
    ll = low[entry_i + 1 : i_h + 1]
    hold = (
        (open_times[entry_i + 1 : i_h + 1] - open_times[entry_i]) / np.timedelta64(1, "m")
    ).astype(float)
    if hh.size and entry_px > 0:
        if side == "LONG":
            fav = (hh / entry_px - 1.0) * 100.0
            adv = (ll / entry_px - 1.0) * 100.0
        else:
            fav = (entry_px - ll) / entry_px * 100.0
            adv = -((hh - entry_px) / entry_px * 100.0)
        for lvl in REACH_LEVELS:
            m = fav >= lvl
            out[f"reach_{lvl:g}pct"] = bool(np.any(m))
            out[f"time_to_{lvl:g}pct_min"] = float(hold[int(np.argmax(m))]) if np.any(m) else None
        out["path_mfe"] = float(np.max(fav))
        out["path_mae"] = float(np.min(adv))
    return out


def simulate_tpsl(
    *,
    entry_i: int,
    entry_px: float,
    side: str,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    tp_pct: float,
    sl_pct: float,
    max_hold_min: int,
) -> dict[str, Any]:
    if entry_i < 0 or entry_px <= 0:
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan}
    n = len(close)
    t_end = open_times[entry_i] + np.timedelta64(int(max_hold_min), "m")
    end_i = int(np.searchsorted(open_times, t_end, side="right") - 1)
    end_i = min(n - 1, max(entry_i + 1, end_i))
    hh = high[entry_i + 1 : end_i + 1]
    ll = low[entry_i + 1 : end_i + 1]
    cc = close[entry_i + 1 : end_i + 1]
    if hh.size == 0:
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan}
    if side == "LONG":
        fav = (hh / entry_px - 1.0) * 100.0
        adv = (ll / entry_px - 1.0) * 100.0
        raw = (cc / entry_px - 1.0) * 100.0
    else:
        fav = (entry_px - ll) / entry_px * 100.0
        adv = -((hh - entry_px) / entry_px * 100.0)
        raw = -((cc / entry_px - 1.0) * 100.0)
    i_tp = int(np.argmax(fav >= tp_pct)) if np.any(fav >= tp_pct) else -1
    if not np.any(fav >= tp_pct):
        i_tp = -1
    i_sl = int(np.argmax(adv <= -sl_pct)) if np.any(adv <= -sl_pct) else -1
    if not np.any(adv <= -sl_pct):
        i_sl = -1
    if i_tp < 0 and i_sl < 0:
        g = float(raw[-1])
        return {"exit_type": "TIMEOUT", "gross": g, "net": g - FEE_PCT}
    if i_tp < 0:
        return {"exit_type": "SL", "gross": float(-sl_pct), "net": float(-(sl_pct + FEE_PCT))}
    if i_sl < 0:
        return {"exit_type": "TP", "gross": float(tp_pct), "net": float(tp_pct - FEE_PCT)}
    if i_tp == i_sl or i_sl < i_tp:
        # SL_FIRST including same bar
        return {"exit_type": "SL", "gross": float(-sl_pct), "net": float(-(sl_pct + FEE_PCT))}
    return {"exit_type": "TP", "gross": float(tp_pct), "net": float(tp_pct - FEE_PCT)}


def summarize_group(sub: pd.DataFrame, h: int, **meta) -> dict[str, Any]:
    col = f"dir_ret_{h}m"
    mfe_c = f"mfe_{h}m"
    mae_c = f"mae_{h}m"
    n = int(len(sub))
    row: dict[str, Any] = {**meta, "horizon_min": h, "n": n, "sample_flag": sample_flag(n)}
    if n == 0 or col not in sub.columns:
        return row
    x = sub[col].astype(float).to_numpy()
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return row
    row.update(
        {
            "hit_rate": float(np.mean(x > 0)),
            "median_dir_ret": float(np.median(x)),
            "mean_dir_ret": float(np.mean(x)),
            "mean_net_proxy": float(np.mean(x) - FEE_PCT),
        }
    )
    if mfe_c in sub.columns:
        mf = sub[mfe_c].astype(float).to_numpy()
        mf = mf[np.isfinite(mf)]
        if len(mf):
            row["median_mfe"] = float(np.median(mf))
            row["mean_mfe"] = float(np.mean(mf))
    if mae_c in sub.columns:
        ma = sub[mae_c].astype(float).to_numpy()
        ma = ma[np.isfinite(ma)]
        if len(ma):
            row["median_mae"] = float(np.median(ma))
            row["mean_mae"] = float(np.mean(ma))
    for lvl in REACH_LEVELS:
        rc = f"reach_{lvl:g}pct"
        if rc in sub.columns:
            row[f"reach_rate_{lvl:g}pct"] = float(sub[rc].astype(bool).mean())
        tc = f"time_to_{lvl:g}pct_min"
        if tc in sub.columns:
            tt = sub[tc].astype(float).to_numpy()
            tt = tt[np.isfinite(tt)]
            if len(tt):
                row[f"median_time_to_{lvl:g}pct"] = float(np.median(tt))
    return row


def summarize_tpsl_nets(nets: np.ndarray, exits: np.ndarray, **meta) -> dict[str, Any]:
    n = int(len(nets))
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    tp_n = int((exits == "TP").sum())
    sl_n = int((exits == "SL").sum())
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    eq = np.cumsum(nets)
    dd = eq - np.maximum.accumulate(eq)
    row.update(
        {
            "expectancy": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "tp_rate": tp_n / n,
            "sl_rate": sl_n / n,
            "win_rate": float(np.mean(nets > 0)),
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "max_drawdown": float(dd.min()) if len(dd) else None,
            "cumulative_net": float(np.sum(nets)),
        }
    )
    return row
