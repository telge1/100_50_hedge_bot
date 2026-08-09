"""Causal per-TF Stoch snapshots at trade entry times."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import STOCH_HIGH_K, STOCH_LOW_K
from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_cycle_wave_analysis.waves import segment_stoch_waves
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import (
    ALL_SNAP_TFS,
    ENV_FILE,
    HIGHER_SIGNAL_TFS,
)


def ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def prepare_tf_frame(symbol: str, timeframe: str) -> pd.DataFrame:
    ohlcv = load_mysql_ohlcv_tf(symbol=symbol, timeframe=timeframe, env_file=ENV_FILE)
    if ohlcv.empty:
        return ohlcv
    ind = attach_indicators(ohlcv)
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["available_at"] = pd.to_datetime(ind["available_at"], utc=True)
    ind["close_time"] = pd.to_datetime(ind["close_time"], utc=True)
    ind = ind.dropna(subset=["stoch_k", "stoch_d"]).sort_values("available_at").reset_index(drop=True)
    waves = segment_stoch_waves(ind)
    # map bar index -> last completed wave ending at/before that bar
    last_wave_dir = np.array([None] * len(ind), dtype=object)
    last_wave_end_zone = np.array([None] * len(ind), dtype=object)
    last_wave_end_avail = np.full(len(ind), np.datetime64("NaT"), dtype="datetime64[ns]")
    minutes_since_wave_end = np.full(len(ind), np.nan)
    if not waves.empty:
        waves = waves.copy()
        waves["end_available_at"] = pd.to_datetime(waves["end_available_at"], utc=True)
        waves["end_i"] = waves["end_i"].astype(int)
        # for each wave end bar, set forward until next wave end
        for _, w in waves.iterrows():
            ei = int(w["end_i"])
            if ei < 0 or ei >= len(ind):
                continue
            last_wave_dir[ei] = str(w["direction"])
            last_wave_end_zone[ei] = str(w["stoch_zone_end"])
            last_wave_end_avail[ei] = np.datetime64(
                ts_utc(w["end_available_at"]).tz_localize(None).to_datetime64()
            )
        # forward-fill wave attributes along bars (still causal: only uses past wave ends)
        for i in range(1, len(ind)):
            if last_wave_dir[i] is None:
                last_wave_dir[i] = last_wave_dir[i - 1]
                last_wave_end_zone[i] = last_wave_end_zone[i - 1]
                last_wave_end_avail[i] = last_wave_end_avail[i - 1]
        avail_ns = ind["available_at"].dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
        for i in range(len(ind)):
            if not np.isnat(last_wave_end_avail[i]):
                minutes_since_wave_end[i] = float(
                    (avail_ns[i] - last_wave_end_avail[i]) / np.timedelta64(1, "m")
                )
    ind["last_completed_wave_dir"] = last_wave_dir
    ind["last_completed_wave_end_zone"] = last_wave_end_zone
    ind["minutes_since_last_wave_end"] = minutes_since_wave_end
    return ind


def _asof_indices(entry_times: np.ndarray, available_at: np.ndarray) -> np.ndarray:
    """For each entry, index of last available_at <= entry (or -1)."""
    # entry_times and available_at as datetime64[ns] naive UTC
    idx = np.searchsorted(available_at, entry_times, side="right") - 1
    return idx.astype(np.int64)


def turn_state(bull: bool, bear: bool) -> str:
    if bull and not bear:
        return "UP_TURN"
    if bear and not bull:
        return "DOWN_TURN"
    return "NO_TURN"


def relative_state(side: str, zone: str, k: float, delta: float, turn: str) -> str:
    """Descriptive labels using only frozen zones + sign(delta) + turn (no new K cutoffs)."""
    side = str(side).upper()
    zone = str(zone)
    rising = np.isfinite(delta) and delta > 0
    falling = np.isfinite(delta) and delta < 0
    if side == "LONG":
        if zone == "LOW" and (turn == "UP_TURN" or rising):
            return "TURNING_UP_FROM_LOW"
        if zone == "LOW":
            return "LOW"
        if zone == "HIGH" and falling:
            return "FALLING_FROM_HIGH"
        if zone in ("MID", "HIGH") and falling:
            return "FALLING_TOWARD_LOW"
        if zone == "HIGH":
            return "HIGH"
        if zone == "MID":
            return "MID"
        return "OTHER"
    # SHORT mirror
    if zone == "HIGH" and (turn == "DOWN_TURN" or falling):
        return "TURNING_DOWN_FROM_HIGH"
    if zone == "HIGH":
        return "HIGH"
    if zone == "LOW" and rising:
        return "RISING_FROM_LOW"
    if zone in ("MID", "LOW") and rising:
        return "RISING_TOWARD_HIGH"
    if zone == "LOW":
        return "LOW"
    if zone == "MID":
        return "MID"
    return "OTHER"


def is_supportive(side: str, zone: str, turn: str) -> bool:
    """A priori analytical support — not outcome-tuned."""
    if str(side).upper() == "LONG":
        return zone == "LOW" or turn == "UP_TURN"
    return zone == "HIGH" or turn == "DOWN_TURN"


def support_label(count: int, n_higher: int) -> str:
    if n_higher <= 0:
        return "no_higher_tf"
    if count <= 0:
        return "no_support"
    if count >= n_higher:
        return "all_higher_support"
    return "partial_support"


def build_symbol_indicator_cache(symbol: str) -> dict[str, pd.DataFrame]:
    out = {}
    for tf in ALL_SNAP_TFS:
        print(f"[ind] {symbol} {tf} …", flush=True)
        out[tf] = prepare_tf_frame(symbol, tf)
    return out


def snapshot_trades(
    trades: pd.DataFrame,
    caches: dict[str, dict[str, pd.DataFrame]],
) -> tuple[pd.DataFrame, int]:
    """Return one row per trade with MTF stoch fields; causality_violations count."""
    rows: list[dict[str, Any]] = []
    violations = 0
    for _, tr in trades.iterrows():
        sym = str(tr["symbol"])
        side = str(tr["side"])
        entry = ts_utc(tr["entry_time"])
        first_tf = str(tr["first_signal_tf"])
        higher = HIGHER_SIGNAL_TFS.get(first_tf, ())
        cache = caches[sym]
        entry_ns = np.datetime64(entry.tz_localize(None).to_datetime64())

        rec: dict[str, Any] = {
            "trade_id": int(tr["trade_id"]),
            "symbol": sym,
            "side": side,
            "entry_time": entry.isoformat(),
            "first_signal_tf": first_tf,
            "highest_tf_reached": str(tr["highest_tf_reached"]),
            "exit_reason": str(tr["exit_reason"]),
            "net_return_pct": float(tr["net_return_pct"]),
            "gross_return_pct": float(tr.get("gross_return_pct", np.nan)),
            "upgrade_count": int(tr.get("upgrade_count", 0)),
            "holding_minutes": float(tr.get("holding_minutes", np.nan)),
        }

        support_flags = []
        for tf in ALL_SNAP_TFS:
            df = cache[tf]
            if df.empty:
                for k in (
                    "k",
                    "d",
                    "zone",
                    "delta",
                    "k_gt_d",
                    "wave_direction",
                    "turn",
                    "relative_state",
                    "supportive",
                    "candle_open_time",
                    "candle_close_time",
                    "available_at",
                    "minutes_since_wave_end",
                    "wave_end_zone",
                ):
                    rec[f"stoch_{tf}_{k}"] = None
                continue
            avail = df["available_at"].dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
            i = int(np.searchsorted(avail, entry_ns, side="right") - 1)
            if i < 0:
                for k in (
                    "k",
                    "d",
                    "zone",
                    "delta",
                    "k_gt_d",
                    "wave_direction",
                    "turn",
                    "relative_state",
                    "supportive",
                    "candle_open_time",
                    "candle_close_time",
                    "available_at",
                    "minutes_since_wave_end",
                    "wave_end_zone",
                ):
                    rec[f"stoch_{tf}_{k}"] = None
                continue
            r = df.iloc[i]
            avail_at = ts_utc(r["available_at"])
            if avail_at > entry:
                violations += 1
            k = float(r["stoch_k"])
            d = float(r["stoch_d"])
            delta = float(r["stoch_k_change"]) if pd.notna(r["stoch_k_change"]) else np.nan
            zone = str(r["stoch_zone"])
            bull = bool(r["stoch_bullish_cross"]) if pd.notna(r["stoch_bullish_cross"]) else False
            bear = bool(r["stoch_bearish_cross"]) if pd.notna(r["stoch_bearish_cross"]) else False
            turn = turn_state(bull, bear)
            wave_dir = r["last_completed_wave_dir"]
            rel = relative_state(side, zone, k, delta, turn)
            supp = bool(is_supportive(side, zone, turn))
            rec[f"stoch_{tf}_k"] = k
            rec[f"stoch_{tf}_d"] = d
            rec[f"stoch_{tf}_zone"] = zone
            rec[f"stoch_{tf}_delta"] = delta if np.isfinite(delta) else None
            rec[f"stoch_{tf}_k_gt_d"] = bool(k > d)
            rec[f"stoch_{tf}_wave_direction"] = None if wave_dir is None else str(wave_dir)
            rec[f"stoch_{tf}_wave_end_zone"] = (
                None
                if r["last_completed_wave_end_zone"] is None
                else str(r["last_completed_wave_end_zone"])
            )
            rec[f"stoch_{tf}_turn"] = turn
            rec[f"stoch_{tf}_relative_state"] = rel
            rec[f"stoch_{tf}_supportive"] = supp
            rec[f"stoch_{tf}_candle_open_time"] = ts_utc(r["timestamp"]).isoformat()
            rec[f"stoch_{tf}_candle_close_time"] = ts_utc(r["close_time"]).isoformat()
            rec[f"stoch_{tf}_available_at"] = avail_at.isoformat()
            rec[f"stoch_{tf}_minutes_since_wave_end"] = (
                float(r["minutes_since_last_wave_end"])
                if pd.notna(r["minutes_since_last_wave_end"])
                else None
            )
            if tf in higher:
                support_flags.append(supp)

        n_h = len(higher)
        sc = int(sum(1 for x in support_flags if x))
        rec["higher_tf_list"] = ",".join(higher)
        rec["higher_tf_n"] = n_h
        rec["higher_tf_support_count"] = sc
        rec["higher_tf_support_label"] = support_label(sc, n_h)
        # pattern string for higher TFs
        parts = []
        for tf in higher:
            z = rec.get(f"stoch_{tf}_zone")
            t = rec.get(f"stoch_{tf}_turn")
            parts.append(f"{tf}:{z}/{t}")
        rec["higher_tf_pattern"] = "|".join(parts) if parts else "none"
        rows.append(rec)

    return pd.DataFrame(rows), violations
