"""Forward validation of BULLISH/BEARISH direction transitions (analysis only).

Does not mutate scanner rules. Direction comes from map_structure_to_direction.
Primary entry: open of the candle after the confirming close (next_open).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.timeframes import ensure_utc_timestamp
from research.regime_scanner.trend_direction_at import (
    DEFAULT_WARMUP_BARS,
    _iso_z,
    map_structure_to_direction,
    normalize_symbol,
    reason_for_direction,
    run_c34b_on_ohlcv,
)

HORIZONS_MIN = (15, 30, 60, 120, 240)
THRESHOLDS = (0.0025, 0.005, 0.01)
PRIMARY_THRESHOLD = 0.01
BAR_MINUTES = 5


def _candles_to_ohlcv(candles: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(candles["timestamp"], utc=True),
            "open": pd.to_numeric(candles["open"], errors="coerce"),
            "high": pd.to_numeric(candles["high"], errors="coerce"),
            "low": pd.to_numeric(candles["low"], errors="coerce"),
            "close": pd.to_numeric(candles["close"], errors="coerce"),
            "volume": pd.to_numeric(candles["volume"], errors="coerce"),
        }
    )


def build_direction_series(structure: pd.DataFrame) -> pd.DataFrame:
    """One row per closed 5m bar with causal mapped direction (vectorized)."""
    open_ts = pd.to_datetime(structure["timestamp"], utc=True)
    if "candle_close_ts" in structure.columns:
        close_ts = pd.to_datetime(structure["candle_close_ts"], utc=True)
    else:
        close_ts = open_ts + pd.Timedelta(minutes=BAR_MINUTES)
    major = structure["major_direction"].fillna(0).astype(int)
    state = structure["protected_structure_state"].fillna("").astype(str)
    direction = [
        map_structure_to_direction(int(m), s) for m, s in zip(major.to_numpy(), state.to_numpy())
    ]
    reason = [
        reason_for_direction(
            direction=d, major=int(m), state=s, n=i + 1, warmup_bars=DEFAULT_WARMUP_BARS
        )
        for i, (d, m, s) in enumerate(zip(direction, major.to_numpy(), state.to_numpy()))
    ]
    cs = structure["choch_side"].fillna("").astype(str).str.lower() if "choch_side" in structure.columns else pd.Series([""] * len(structure))
    events: list[str | None] = []
    for d, st, c in zip(direction, state.to_numpy(), cs.to_numpy()):
        if d == "UNCLEAR":
            events.append(st or None)
        elif d == "BULLISH":
            events.append("bullish_choch" if c == "up" else "bullish_structure")
        else:
            events.append("bearish_choch" if c == "down" else "bearish_structure")
    return pd.DataFrame(
        {
            "i": np.arange(len(structure), dtype=int),
            "open_ts": open_ts,
            "close_ts": close_ts,
            "open": pd.to_numeric(structure["open"], errors="coerce").astype(float),
            "high": pd.to_numeric(structure["high"], errors="coerce").astype(float),
            "low": pd.to_numeric(structure["low"], errors="coerce").astype(float),
            "close": pd.to_numeric(structure["close"], errors="coerce").astype(float),
            "direction": direction,
            "major_direction": major.to_numpy(),
            "protected_structure_state": state.to_numpy(),
            "reason": reason,
            "structure_event": events,
        }
    )


def extract_direction_signals(series: pd.DataFrame) -> pd.DataFrame:
    """Only true direction transitions into BULLISH/BEARISH (one start per episode)."""
    if series.empty:
        return pd.DataFrame()
    dirs = series["direction"].tolist()
    signals: list[dict[str, Any]] = []
    prev: str | None = None
    for i, d in enumerate(dirs):
        if d in ("BULLISH", "BEARISH") and d != prev:
            row = series.iloc[i]
            signals.append(
                {
                    "signal_index": int(row["i"]),
                    "signal_direction": d,
                    "prev_direction": prev if prev is not None else "NONE",
                    "decision_time_utc": _iso_z(row["close_ts"]),
                    "signal_candle_open_utc": _iso_z(row["open_ts"]),
                    "signal_candle_close_utc": _iso_z(row["close_ts"]),
                    "signal_price_close": float(row["close"]),
                    "major_direction": int(row["major_direction"]),
                    "protected_structure_state": row["protected_structure_state"],
                    "reason": row["reason"],
                    "structure_event": row["structure_event"],
                }
            )
        prev = d
    return pd.DataFrame(signals)


def _episode_end_index(dirs: list[str], signal_i: int, signal_dir: str) -> int:
    for j in range(signal_i + 1, len(dirs)):
        if dirs[j] != signal_dir:
            return j
    return len(dirs)


def _targets(entry: float, direction: str, threshold: float) -> tuple[float, float]:
    if direction == "BULLISH":
        return entry * (1.0 + threshold), entry * (1.0 - threshold)
    return entry * (1.0 - threshold), entry * (1.0 + threshold)


def first_touch(
    *,
    direction: str,
    entry: float,
    threshold: float,
    highs: np.ndarray,
    lows: np.ndarray,
    max_bars: int | None,
) -> dict[str, Any]:
    fav_px, adv_px = _targets(entry, direction, threshold)
    n = len(highs) if max_bars is None else min(len(highs), int(max_bars))
    fav_bar = None
    adv_bar = None
    for k in range(n):
        hi = float(highs[k])
        lo = float(lows[k])
        if direction == "BULLISH":
            fav_hit = hi >= fav_px
            adv_hit = lo <= adv_px
        else:
            fav_hit = lo <= fav_px
            adv_hit = hi >= adv_px
        if fav_hit and fav_bar is None:
            fav_bar = k
        if adv_hit and adv_bar is None:
            adv_bar = k
        if fav_bar is not None and adv_bar is not None:
            break

    minutes_fav = None if fav_bar is None else (fav_bar + 1) * BAR_MINUTES
    minutes_adv = None if adv_bar is None else (adv_bar + 1) * BAR_MINUTES

    if fav_bar is None and adv_bar is None:
        first = "NONE"
    elif fav_bar is not None and adv_bar is not None and fav_bar == adv_bar:
        first = "SAME_CANDLE_AMBIGUOUS"
    elif fav_bar is not None and (adv_bar is None or fav_bar < adv_bar):
        first = "FAVORABLE"
    elif adv_bar is not None and (fav_bar is None or adv_bar < fav_bar):
        first = "ADVERSE"
    else:
        first = "NONE"

    return {
        "favorable_target": fav_px,
        "adverse_target": adv_px,
        "favorable_hit": fav_bar is not None,
        "adverse_hit": adv_bar is not None,
        "favorable_hit_bar": fav_bar,
        "adverse_hit_bar": adv_bar,
        "first_hit": first,
        "minutes_to_favorable": minutes_fav,
        "minutes_to_adverse": minutes_adv,
    }


def mfe_mae_pct(
    *,
    direction: str,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    bars: int,
) -> tuple[float | None, float | None]:
    """Return (mfe_pct, mae_pct) with MAE as positive adverse magnitude."""
    if bars <= 0 or len(highs) == 0:
        return None, None
    n = min(bars, len(highs))
    hi = float(np.max(highs[:n]))
    lo = float(np.min(lows[:n]))
    if direction == "BULLISH":
        mfe = (hi / entry) - 1.0
        mae = 1.0 - (lo / entry)
    else:
        mfe = 1.0 - (lo / entry)
        mae = (hi / entry) - 1.0
    return mfe * 100.0, mae * 100.0


def classify_outcome(
    *,
    first_hit: str,
    fav_during_episode: bool,
    fav_within_240: bool,
    fav_after_episode_only: bool,
    data_incomplete: bool,
) -> str:
    if data_incomplete and first_hit == "NONE" and not fav_within_240:
        return "DATA_INCOMPLETE"
    if first_hit == "SAME_CANDLE_AMBIGUOUS":
        return "SAME_CANDLE_AMBIGUOUS"
    if first_hit == "FAVORABLE":
        return "TARGET_FIRST"
    if first_hit == "ADVERSE":
        return "STOP_FIRST"
    if fav_after_episode_only:
        return "TARGET_ONLY_AFTER_DIRECTION_ENDED"
    return "NO_TARGET_WITHIN_240M"


def find_prior_unclear(series: pd.DataFrame, signal_i: int) -> dict[str, Any]:
    dirs = series["direction"].tolist()
    prev = dirs[signal_i - 1] if signal_i > 0 else "NONE"
    out: dict[str, Any] = {
        "prev_direction": prev if signal_i > 0 else "NONE",
        "unclear_start_utc": None,
        "unclear_duration_minutes": None,
        "move_from_unclear_start_to_confirm_pct": None,
        "favorable_move_since_unclear_start_pct": None,
        "move_consumed_before_confirm_pct": None,
    }
    if prev != "UNCLEAR":
        return out
    start = signal_i - 1
    while start - 1 >= 0 and dirs[start - 1] == "UNCLEAR":
        start -= 1
    start_row = series.iloc[start]
    confirm = series.iloc[signal_i]
    out["unclear_start_utc"] = _iso_z(start_row["close_ts"])
    out["unclear_duration_minutes"] = (
        confirm["close_ts"] - start_row["close_ts"]
    ).total_seconds() / 60.0
    entry_ref = float(confirm["close"])
    start_px = float(start_row["close"])
    direction = confirm["direction"]
    window = series.iloc[start : signal_i + 1]
    hi = float(window["high"].max())
    lo = float(window["low"].min())
    out["move_from_unclear_start_to_confirm_pct"] = ((entry_ref / start_px) - 1.0) * 100.0
    if direction == "BULLISH":
        fav = (hi / start_px) - 1.0
        consumed = max(0.0, (entry_ref / start_px) - 1.0)
    else:
        fav = 1.0 - (lo / start_px)
        consumed = max(0.0, 1.0 - (entry_ref / start_px))
    out["favorable_move_since_unclear_start_pct"] = fav * 100.0
    out["move_consumed_before_confirm_pct"] = consumed * 100.0
    return out


def signal_candle_features(series: pd.DataFrame, signal_i: int) -> dict[str, Any]:
    row = series.iloc[signal_i]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    ret = (c / o) - 1.0 if o else 0.0

    def _move(n: int) -> float | None:
        if signal_i - n < 0:
            return None
        past = float(series.iloc[signal_i - n]["close"])
        return ((c / past) - 1.0) * 100.0 if past else None

    look = series.iloc[max(0, signal_i - 11) : signal_i + 1]
    recent_hi = float(look["high"].max())
    recent_lo = float(look["low"].min())
    return {
        "signal_candle_return_pct": ret * 100.0,
        "signal_candle_range_pct": ((h / l) - 1.0) * 100.0 if l else None,
        "signal_candle_body_pct": (body / o) * 100.0 if o else None,
        "signal_candle_body_to_range": body / rng,
        "prev_3_candle_move_pct": _move(3),
        "prev_6_candle_move_pct": _move(6),
        "distance_from_recent_60m_high_pct": ((c / recent_hi) - 1.0) * 100.0,
        "distance_from_recent_60m_low_pct": ((c / recent_lo) - 1.0) * 100.0,
    }


def evaluate_signal(
    series: pd.DataFrame,
    signal: dict[str, Any],
    *,
    threshold: float = PRIMARY_THRESHOLD,
) -> dict[str, Any]:
    i = int(signal["signal_index"])
    direction = signal["signal_direction"]
    dirs = series["direction"].tolist()
    n = len(series)

    base = {
        **signal,
        "threshold": threshold,
        "entry_mode": "next_open",
        "signal_price_next_open": None,
        "evaluable": False,
        "outcome_class": "DATA_INCOMPLETE",
        "first_hit": "NONE",
        "data_incomplete": True,
    }
    if i + 1 >= n:
        return base

    entry = float(series.iloc[i + 1]["open"])
    entry_ts = series.iloc[i + 1]["open_ts"]
    ep_end = _episode_end_index(dirs, i, direction)
    fwd_start = i + 1
    fwd_end_horizon = min(n, fwd_start + (240 // BAR_MINUTES))
    episode_end_excl = min(ep_end, n)
    fwd = series.iloc[fwd_start:fwd_end_horizon]
    highs = fwd["high"].to_numpy(dtype=float)
    lows = fwd["low"].to_numpy(dtype=float)

    episode_bars = max(0, episode_end_excl - fwd_start)
    ep_highs = highs[:episode_bars] if episode_bars else np.array([], dtype=float)
    ep_lows = lows[:episode_bars] if episode_bars else np.array([], dtype=float)

    touch_240 = first_touch(
        direction=direction, entry=entry, threshold=threshold, highs=highs, lows=lows, max_bars=None
    )
    touch_ep = first_touch(
        direction=direction,
        entry=entry,
        threshold=threshold,
        highs=ep_highs,
        lows=ep_lows,
        max_bars=None,
    )

    fav_during_ep = bool(touch_ep["favorable_hit"])
    fav_within_240 = bool(touch_240["favorable_hit"])
    fav_after_only = False
    if fav_within_240 and not fav_during_ep and episode_bars < len(highs):
        post = first_touch(
            direction=direction,
            entry=entry,
            threshold=threshold,
            highs=highs[episode_bars:],
            lows=lows[episode_bars:],
            max_bars=None,
        )
        fav_after_only = bool(post["favorable_hit"])

    data_incomplete = (fwd_end_horizon - fwd_start) < (240 // BAR_MINUTES)
    rel_ep = episode_end_excl - fwd_start
    window_bars = min(240 // BAR_MINUTES, max(rel_ep, 0))
    if window_bars <= 0:
        use_touch = first_touch(
            direction=direction,
            entry=entry,
            threshold=threshold,
            highs=np.array([], dtype=float),
            lows=np.array([], dtype=float),
            max_bars=0,
        )
    else:
        use_touch = first_touch(
            direction=direction,
            entry=entry,
            threshold=threshold,
            highs=highs,
            lows=lows,
            max_bars=window_bars,
        )

    outcome = classify_outcome(
        first_hit=use_touch["first_hit"],
        fav_during_episode=fav_during_ep,
        fav_within_240=fav_within_240,
        fav_after_episode_only=fav_after_only,
        data_incomplete=data_incomplete and use_touch["first_hit"] == "NONE",
    )

    horizon_hits: dict[str, Any] = {}
    for h in HORIZONS_MIN:
        bars = h // BAR_MINUTES
        t = first_touch(
            direction=direction, entry=entry, threshold=threshold, highs=highs, lows=lows, max_bars=bars
        )
        horizon_hits[f"first_hit_{h}m"] = t["first_hit"]
        horizon_hits[f"favorable_touch_within_{h}m"] = bool(t["favorable_hit"])
        mfe, mae = mfe_mae_pct(direction=direction, entry=entry, highs=highs, lows=lows, bars=bars)
        horizon_hits[f"mfe_{h}m_pct"] = mfe
        horizon_hits[f"mae_{h}m_pct"] = mae

    ep_mfe, ep_mae = mfe_mae_pct(
        direction=direction, entry=entry, highs=ep_highs, lows=ep_lows, bars=len(ep_highs)
    )
    feat = signal_candle_features(series, i)
    unclear = find_prior_unclear(series, i)

    episode_end_ts = None
    if n > 0:
        end_i = min(episode_end_excl, n - 1)
        episode_end_ts = _iso_z(series.iloc[end_i]["close_ts"])
    episode_duration = None
    if episode_end_excl > i and n > 0:
        end_i = min(episode_end_excl, n - 1)
        episode_duration = (
            series.iloc[end_i]["close_ts"] - series.iloc[i]["close_ts"]
        ).total_seconds() / 60.0

    return {
        **signal,
        "threshold": threshold,
        "entry_mode": "next_open",
        "signal_price_next_open": entry,
        "entry_open_utc": _iso_z(entry_ts),
        "evaluable": True,
        "data_incomplete": data_incomplete,
        "episode_end_utc": episode_end_ts,
        "episode_duration_minutes": episode_duration,
        "episode_forward_bars": int(episode_bars),
        "favorable_target": use_touch["favorable_target"],
        "adverse_target": use_touch["adverse_target"],
        "favorable_hit": use_touch["favorable_hit"],
        "adverse_hit": use_touch["adverse_hit"],
        "first_hit": use_touch["first_hit"],
        "minutes_to_favorable": use_touch["minutes_to_favorable"],
        "minutes_to_adverse": use_touch["minutes_to_adverse"],
        "favorable_1pct_during_episode": fav_during_ep,
        "adverse_1pct_during_episode": bool(touch_ep["adverse_hit"]),
        "favorable_1pct_within_240m": fav_within_240,
        "outcome_class": outcome,
        "episode_mfe_pct": ep_mfe,
        "episode_mae_pct": ep_mae,
        **horizon_hits,
        **feat,
        **unclear,
    }


def label_impulse_candles(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return results
    out = results.copy()
    out["impulse_class"] = "normal_signal_candle"
    out["impulse_body_to_range_p75"] = np.nan
    for _, idx in out.groupby("symbol").groups.items():
        sub = out.loc[list(idx), "signal_candle_body_to_range"]
        thr = float(sub.quantile(0.75)) if len(sub) else 1.0
        out.loc[list(idx), "impulse_body_to_range_p75"] = thr
        large_idx = sub.index[sub >= thr]
        out.loc[large_idx, "impulse_class"] = "large_impulse_signal_candle"
    return out


def _summary_block(sub: pd.DataFrame) -> dict[str, Any]:
    ev = sub[sub["evaluable"] == True]  # noqa: E712
    n = len(sub)
    ne = len(ev)

    def cnt(cls: str) -> int:
        return int((ev["outcome_class"] == cls).sum()) if ne else 0

    target_first = cnt("TARGET_FIRST")
    stop_first = cnt("STOP_FIRST")
    amb = cnt("SAME_CANDLE_AMBIGUOUS")
    no_t = cnt("NO_TARGET_WITHIN_240M") + cnt("TARGET_ONLY_AFTER_DIRECTION_ENDED")

    def rate(col: str) -> float | None:
        if ne == 0 or col not in ev.columns:
            return None
        return float(ev[col].fillna(False).astype(bool).mean())

    def med(col: str) -> float | None:
        if ne == 0 or col not in ev.columns:
            return None
        s = pd.to_numeric(ev[col], errors="coerce").dropna()
        return float(s.median()) if len(s) else None

    def q(col: str, p: float) -> float | None:
        if ne == 0 or col not in ev.columns:
            return None
        s = pd.to_numeric(ev[col], errors="coerce").dropna()
        return float(s.quantile(p)) if len(s) else None

    large = ev[ev["impulse_class"] == "large_impulse_signal_candle"] if "impulse_class" in ev else ev.iloc[0:0]
    normal = ev[ev["impulse_class"] == "normal_signal_candle"] if "impulse_class" in ev else ev.iloc[0:0]

    def hit_rate(x: pd.DataFrame) -> float | None:
        if x.empty:
            return None
        return float((x["outcome_class"] == "TARGET_FIRST").mean())

    return {
        "signal_count": n,
        "evaluable_count": ne,
        "target_first_count": target_first,
        "stop_first_count": stop_first,
        "ambiguous_count": amb,
        "no_target_count": no_t,
        "target_after_ended_count": cnt("TARGET_ONLY_AFTER_DIRECTION_ENDED"),
        "data_incomplete_count": cnt("DATA_INCOMPLETE"),
        "target_first_rate": (target_first / ne) if ne else None,
        "stop_first_rate": (stop_first / ne) if ne else None,
        "favorable_1pct_within_15m_rate": rate("favorable_touch_within_15m"),
        "favorable_1pct_within_30m_rate": rate("favorable_touch_within_30m"),
        "favorable_1pct_within_60m_rate": rate("favorable_touch_within_60m"),
        "favorable_1pct_within_120m_rate": rate("favorable_touch_within_120m"),
        "favorable_1pct_within_240m_rate": rate("favorable_touch_within_240m"),
        "median_minutes_to_target": med("minutes_to_favorable"),
        "median_mfe_15m": med("mfe_15m_pct"),
        "median_mae_15m": med("mae_15m_pct"),
        "median_mfe_30m": med("mfe_30m_pct"),
        "median_mae_30m": med("mae_30m_pct"),
        "median_mfe_60m": med("mfe_60m_pct"),
        "median_mae_60m": med("mae_60m_pct"),
        "median_mfe_120m": med("mfe_120m_pct"),
        "median_mae_120m": med("mae_120m_pct"),
        "median_mfe_240m": med("mfe_240m_pct"),
        "median_mae_240m": med("mae_240m_pct"),
        "p75_mae_60m": q("mae_60m_pct", 0.75),
        "p90_mae_60m": q("mae_60m_pct", 0.90),
        "median_episode_duration": med("episode_duration_minutes"),
        "median_move_consumed_before_confirm": med("move_consumed_before_confirm_pct"),
        "large_impulse_share": (
            float((ev["impulse_class"] == "large_impulse_signal_candle").mean())
            if ne and "impulse_class" in ev
            else None
        ),
        "target_first_rate_large_impulse": hit_rate(large),
        "target_first_rate_normal_candle": hit_rate(normal),
    }


def summarize_results(df: pd.DataFrame, *, threshold: float = PRIMARY_THRESHOLD) -> dict[str, Any]:
    if df.empty:
        return {"signal_count": 0, "threshold": threshold, "overall": {}, "by_symbol_direction": {}}
    by: dict[str, Any] = {}
    for (sym, d), sub in df.groupby(["symbol", "signal_direction"]):
        by[f"{sym}:{d}"] = _summary_block(sub)
    return {"threshold": threshold, "overall": _summary_block(df), "by_symbol_direction": by}


def run_symbol_forward_validation(
    *,
    symbol: str,
    exchange: str = "bybit",
    env_file: str | None = None,
    candles: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Single-pass: load → C3.4B → direction series → signals → evaluate.

    Returns (results_1pct, threshold_comparison, direction_series, meta).
    """
    t0 = time.perf_counter()
    sym = normalize_symbol(symbol)
    if candles is None:
        from research.regime_scanner.candle_sources import MySQLCandleSource, load_regime_db_env_file

        if env_file:
            load_regime_db_env_file(Path(env_file))
        else:
            load_regime_db_env_file()
        src = MySQLCandleSource(exchange_default=exchange)
        try:
            candles = src.load_candles(
                exchange=exchange, symbol=sym, timeframe="5m", closed_only=True
            )
        finally:
            src.close()
        if candles is None or candles.empty:
            raise RuntimeError(f"no candles for {sym}")

    ohlcv = _candles_to_ohlcv(candles)
    structure = run_c34b_on_ohlcv(ohlcv)
    series = build_direction_series(structure)
    signals = extract_direction_signals(series)
    signals["symbol"] = sym

    evaluated_rows = [
        evaluate_signal(series, sig.to_dict(), threshold=PRIMARY_THRESHOLD)
        for _, sig in signals.iterrows()
    ]
    results = pd.DataFrame(evaluated_rows)
    if not results.empty:
        results["symbol"] = sym
        results = label_impulse_candles(results)

    thresh_rows: list[dict[str, Any]] = []
    for thr in THRESHOLDS:
        for _, sig in signals.iterrows():
            r = evaluate_signal(series, sig.to_dict(), threshold=thr)
            thresh_rows.append(
                {
                    "symbol": sym,
                    "signal_index": r["signal_index"],
                    "signal_direction": r["signal_direction"],
                    "decision_time_utc": r["decision_time_utc"],
                    "threshold": thr,
                    "evaluable": r["evaluable"],
                    "outcome_class": r["outcome_class"],
                    "first_hit": r["first_hit"],
                    "minutes_to_favorable": r.get("minutes_to_favorable"),
                    "favorable_touch_within_60m": r.get("favorable_touch_within_60m"),
                    "mfe_60m_pct": r.get("mfe_60m_pct"),
                    "mae_60m_pct": r.get("mae_60m_pct"),
                }
            )
    thresh_df = pd.DataFrame(thresh_rows)
    meta = {
        "symbol": sym,
        "bars": int(len(series)),
        "signal_count": int(len(signals)),
        "runtime_seconds": float(time.perf_counter() - t0),
    }
    return results, thresh_df, series, meta
