"""I/O, Gold HTF aggregation, causal snapshots, overlap, and outcome paths."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    ATR_LENGTH,
    CANDLE_LOAD_END_EXCLUSIVE,
    CANDLE_LOAD_START,
    EMA_EXTRA_SPANS,
    FEE_PP,
    GOLD_ROOT,
    LIVE_BOT_ENV,
    MANUAL_CASES,
    OUTCOMES_JSONL,
    PIN_CANDLE_DATA_TO,
    SIGNAL_TFS,
    SIGNALS_JSONL,
    SNAPSHOT_TFS,
    STOCH_HIGH,
    STOCH_LOW,
    SYMBOL,
    TF_MINUTES,
    TF_RANK,
    TPSL_BY_TF,
)


def _ensure_gold_on_path() -> None:
    src = str((GOLD_ROOT / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)


def iso_z(value: object) -> str | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def to_utc_ns(values: object) -> np.ndarray:
    return pd.to_datetime(values, utc=True).to_numpy(dtype="datetime64[ns]")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_trades() -> tuple[pd.DataFrame, dict[str, Any]]:
    outcomes = load_jsonl(OUTCOMES_JSONL)
    signals = load_jsonl(SIGNALS_JSONL)
    sig_by_id = {str(r["signal_id"]): r for r in signals}
    rows: list[dict[str, Any]] = []
    missing_signal = 0
    for rec in outcomes:
        sid = str(rec["signal_id"])
        sig = sig_by_id.get(sid)
        if sig is None:
            missing_signal += 1
            meta: dict[str, Any] = {}
        else:
            try:
                meta = json.loads(sig.get("metadata") or "{}")
            except json.JSONDecodeError:
                meta = {}
        entry = to_utc(rec["entry_time"])
        exit_time = to_utc(rec["exit_time"]) if rec.get("exit_time") else pd.NaT
        direction = str(rec["direction"]).upper()
        tf = str(rec["timeframe"])
        outcome = str(rec.get("outcome") or rec.get("display_result") or "").upper()
        tp_pct, sl_pct = TPSL_BY_TF[tf]
        rows.append(
            {
                "signal_id": sid,
                "setup_id": rec.get("setup_id"),
                "generation_key": rec.get("generation_key"),
                "symbol": rec.get("symbol") or SYMBOL,
                "timeframe": tf,
                "direction": direction,
                "outcome": outcome,
                "is_open": bool(rec.get("is_open") or outcome == "OPEN"),
                "entry_time": entry,
                "exit_time": exit_time,
                "entry_price": float(rec["entry_price"]),
                "exit_price": float(rec["exit_price"]) if rec.get("exit_price") is not None else np.nan,
                "tp_price": float(rec["tp_price"]),
                "sl_price": float(rec.get("initial_sl_price") or rec.get("final_sl_price")),
                "tp_pct": float(meta.get("tp_pct") or tp_pct),
                "sl_pct": float(meta.get("sl_pct") or sl_pct),
                "pnl_pct_gross": float(rec["pnl_pct_gross"]) if rec.get("pnl_pct_gross") is not None else np.nan,
                "hold_seconds": rec.get("duration_seconds"),
                "exit_reason": rec.get("exit_reason"),
                "end_ts": rec.get("end_ts"),
                "end_available_at": rec.get("end_available_at"),
                "recognition_ts": rec.get("recognition_ts"),
                "recognition_available_at": rec.get("recognition_available_at"),
                "confirmation_available_at": rec.get("confirmation_available_at"),
                "start_ts": sig.get("start_ts") if sig else meta.get("start_ts"),
                "start_available_at": sig.get("start_available_at") if sig else meta.get("start_available_at"),
                "wave_end_price": meta.get("wave_end_price"),
                "wave_direction": meta.get("wave_direction"),
                "trend_bucket": sig.get("trend_bucket") if sig else meta.get("trend_bucket"),
                "eff_quantile": sig.get("eff_quantile") if sig else meta.get("eff_quantile"),
                "n_bars": meta.get("n_bars"),
                "tier_a": bool(sig.get("tier_a")) if sig is not None else None,
                "source_signal_present": sig is not None,
            }
        )
    frame = pd.DataFrame(rows)
    frame = frame.sort_values(["entry_time", "signal_id"]).reset_index(drop=True)
    inventory = inventory_summary(frame, n_raw_signals=len(signals), missing_signal=missing_signal)
    return frame, inventory


def inventory_summary(frame: pd.DataFrame, *, n_raw_signals: int, missing_signal: int) -> dict[str, Any]:
    closed = frame.loc[~frame["is_open"]]
    wins = int((frame["outcome"] == "WIN").sum())
    losses = int((frame["outcome"] == "LOSS").sum())
    opens = int(frame["is_open"].sum())
    by_tf = frame.groupby("timeframe").size().to_dict()
    by_side = frame.groupby("direction").size().to_dict()
    by_tf_side = (
        frame.groupby(["timeframe", "direction"]).size().rename("n").reset_index().to_dict("records")
    )
    dup_entry = int((frame.duplicated(["entry_time"], keep=False)).sum())
    entry_groups = frame.groupby("entry_time").size()
    multi_entry = int((entry_groups > 1).sum())
    overlapping = 0
    same_dir = 0
    opp_dir = 0
    entries = frame["entry_time"].to_numpy()
    exits = frame["exit_time"].to_numpy()
    dirs = frame["direction"].to_numpy()
    open_flags = frame["is_open"].to_numpy()
    for i in range(len(frame)):
        e_i = entries[i]
        for j in range(i):
            e_j = entries[j]
            if e_j >= e_i:
                continue
            still_open = bool(open_flags[j]) or (pd.notna(exits[j]) and exits[j] > e_i)
            if still_open:
                overlapping += 1
                if dirs[j] == dirs[i]:
                    same_dir += 1
                else:
                    opp_dir += 1
                break
    return {
        "symbol": SYMBOL,
        "n_trades": int(len(frame)),
        "wins": wins,
        "losses": losses,
        "open": opens,
        "win_rate_closed_pct": (wins / (wins + losses) * 100.0) if (wins + losses) else None,
        "period_start": iso_z(frame["entry_time"].min()),
        "period_end_entry": iso_z(frame["entry_time"].max()),
        "last_exit": iso_z(pd.to_datetime(frame["exit_time"]).max()),
        "timeframes": sorted(frame["timeframe"].unique().tolist()),
        "counts_by_timeframe": {str(k): int(v) for k, v in by_tf.items()},
        "counts_by_direction": {str(k): int(v) for k, v in by_side.items()},
        "counts_by_tf_direction": by_tf_side,
        "n_raw_source_signals": n_raw_signals,
        "n_outcomes_missing_source_signal": missing_signal,
        "duplicate_signal_ids": int(frame["signal_id"].duplicated().sum()),
        "duplicate_setup_ids": int(frame["setup_id"].duplicated().sum()),
        "duplicate_generation_keys": int(frame["generation_key"].duplicated().sum()),
        "trades_sharing_an_entry_time": dup_entry,
        "distinct_entry_times_with_multiple_tfs": multi_entry,
        "trades_with_at_least_one_open_overlap": overlapping,
        "overlap_same_direction_trades": same_dir,
        "overlap_opposite_direction_trades": opp_dir,
        "no_trade_removed": True,
        "signal_view": "all ZEC evaluation outcomes kept unchanged",
    }


def load_candles_1m() -> tuple[pd.DataFrame, dict[str, Any]]:
    _ensure_gold_on_path()
    from dotenv import load_dotenv
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import get_client

    if not LIVE_BOT_ENV.is_file():
        raise RuntimeError(f"CLICKHOUSE_ENV_MISSING:{LIVE_BOT_ENV}")
    load_dotenv(LIVE_BOT_ENV, override=False)
    inner = get_client()
    ping = inner.query("SELECT 1")
    repo = CandleRepository(inner)
    start = to_utc(CANDLE_LOAD_START).to_pydatetime()
    end = to_utc(CANDLE_LOAD_END_EXCLUSIVE).to_pydatetime()
    rows = repo.get_candles(SYMBOL, start, end)
    if not rows:
        raise RuntimeError("ZEC_CANDLES_EMPTY")
    df = pd.DataFrame(
        [
            {
                "open_time": to_utc(r["open_time"]),
                "close_time": to_utc(r["close_time"]) if r.get("close_time") is not None else to_utc(r["open_time"]) + pd.Timedelta(minutes=1),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0.0),
            }
            for r in rows
        ]
    )
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    pin = to_utc(PIN_CANDLE_DATA_TO)
    df = df.loc[df["open_time"] <= pin].reset_index(drop=True)
    df["available_at"] = pd.to_datetime(df["close_time"], utc=True)
    df["timestamp"] = pd.to_datetime(df["open_time"], utc=True)
    gaps = int((df["open_time"].diff() > pd.Timedelta(minutes=1)).sum())
    meta = {
        "config_source_path": str(LIVE_BOT_ENV),
        "loader_name": "dotenv(live_bot/.env)+signal_generator.db.candles.CandleRepository.get_candles",
        "connect_ok": True,
        "select_1_ok": bool(ping.result_rows and ping.result_rows[0][0] == 1),
        "query_types": ["SELECT"],
        "tables": ["signal_generator.candles_1m"],
        "writes": 0,
        "read_only": True,
        "symbol": SYMBOL,
        "n_1m": int(len(df)),
        "first_open": iso_z(df["open_time"].iloc[0]),
        "last_open": iso_z(df["open_time"].iloc[-1]),
        "last_close": iso_z(df["close_time"].iloc[-1]),
        "gap_count": gaps,
        "n_raw_rows": int(len(rows)),
    }
    return df, meta


def aggregate_complete(
    candles_1m: pd.DataFrame,
    timeframe: str,
    *,
    as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Gold inspect_bucket completeness: UTC, expected 1m count, first/max/min/last/sum."""
    minutes = TF_MINUTES[timeframe]
    ot = pd.to_datetime(candles_1m["open_time"], utc=True)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    total_min = ((ot - epoch) // pd.Timedelta(minutes=1)).astype(np.int64)
    bucket = total_min - (total_min % minutes)
    offset = total_min - bucket
    frame = pd.DataFrame(
        {
            "bucket": bucket,
            "offset": offset,
            "open": candles_1m["open"].to_numpy(dtype=float),
            "high": candles_1m["high"].to_numpy(dtype=float),
            "low": candles_1m["low"].to_numpy(dtype=float),
            "close": candles_1m["close"].to_numpy(dtype=float),
            "volume": candles_1m["volume"].to_numpy(dtype=float),
        }
    )
    stats = frame.groupby("bucket", sort=True).agg(
        n=("open", "size"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        off_min=("offset", "min"),
        off_max=("offset", "max"),
        off_nunique=("offset", "nunique"),
    )
    complete_mask = (
        (stats["n"] == minutes)
        & (stats["off_min"] == 0)
        & (stats["off_max"] == minutes - 1)
        & (stats["off_nunique"] == minutes)
    )
    incomplete = int((~complete_mask).sum())
    complete = stats.loc[complete_mask]
    starts = epoch + pd.to_timedelta(complete.index.astype(np.int64), unit="m")
    close_times = starts + pd.Timedelta(minutes=minutes)
    keep = np.asarray(close_times <= as_of)
    dropped_open = int((~keep).sum())
    complete = complete.iloc[keep]
    starts = pd.DatetimeIndex(starts[keep]).tz_convert("UTC")
    close_times = pd.DatetimeIndex(close_times[keep]).tz_convert("UTC")
    out = pd.DataFrame(
        {
            "timestamp": starts,
            "available_at": close_times,
            "close_time": close_times,
            "open": complete["open"].to_numpy(),
            "high": complete["high"].to_numpy(),
            "low": complete["low"].to_numpy(),
            "close": complete["close"].to_numpy(),
            "volume": complete["volume"].to_numpy(),
        }
    )
    audit = {
        "timeframe": timeframe,
        "expected_1m": minutes,
        "complete_buckets": int(len(out)),
        "incomplete_or_gapped_discarded": incomplete,
        "not_closed_as_of_discarded": dropped_open,
        "first_open": iso_z(out["timestamp"].iloc[0]) if len(out) else None,
        "last_open": iso_z(out["timestamp"].iloc[-1]) if len(out) else None,
        "last_close": iso_z(out["available_at"].iloc[-1]) if len(out) else None,
        "ohlc_rule": "first_open, max_high, min_low, last_close, volume_sum",
        "utc_boundaries": True,
    }
    return out.reset_index(drop=True), audit


def _bars_since(mask: np.ndarray) -> np.ndarray:
    out = np.full(mask.shape, np.nan)
    last = -1
    for i, flag in enumerate(mask):
        if bool(flag):
            last = i
        if last >= 0:
            out[i] = i - last
    return out


def _stoch_raw(rsi: pd.Series, length: int = 14) -> pd.Series:
    lowest = rsi.rolling(length, min_periods=length).min()
    highest = rsi.rolling(length, min_periods=length).max()
    span = highest - lowest
    raw = pd.Series(np.nan, index=rsi.index, dtype=float)
    valid = lowest.notna() & highest.notna() & rsi.notna()
    flat = valid & (span == 0)
    movable = valid & (span > 0)
    raw = raw.where(~movable, 100.0 * (rsi - lowest) / span)
    return raw.where(~flat, 50.0)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = ATR_LENGTH) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def enrich_frame(ohlcv: pd.DataFrame) -> pd.DataFrame:
    _ensure_gold_on_path()
    from signal_generator.strategy.wave_fade.indicators import attach_indicators

    df = attach_indicators(ohlcv.copy())
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    for span in EMA_EXTRA_SPANS:
        col = f"ema{span}"
        if col not in df.columns:
            df[col] = close.ewm(span=span, adjust=False, min_periods=span).mean()
    df["stoch_rsi_raw"] = _stoch_raw(df["rsi"])
    df["stoch_k_minus_d"] = df["stoch_k"] - df["stoch_d"]
    df["stoch_k_prev"] = df["stoch_k"].shift(1)
    df["stoch_d_prev"] = df["stoch_d"].shift(1)
    for n in (1, 2, 3):
        df[f"stoch_k_slope_{n}"] = df["stoch_k"] - df["stoch_k"].shift(n)
        df[f"stoch_d_slope_{n}"] = df["stoch_d"] - df["stoch_d"].shift(n)
    bull = df["stoch_bullish_cross"].fillna(False).to_numpy(dtype=bool)
    bear = df["stoch_bearish_cross"].fillna(False).to_numpy(dtype=bool)
    df["bars_since_cross_up"] = _bars_since(bull)
    df["bars_since_cross_down"] = _bars_since(bear)
    df["k_lt_20"] = df["stoch_k"] < STOCH_LOW
    df["k_d_lt_20"] = (df["stoch_k"] < STOCH_LOW) & (df["stoch_d"] < STOCH_LOW)
    df["k_gt_80"] = df["stoch_k"] > STOCH_HIGH
    df["k_d_gt_80"] = (df["stoch_k"] > STOCH_HIGH) & (df["stoch_d"] > STOCH_HIGH)
    df["stoch_bias"] = np.where(
        df["stoch_k"] > df["stoch_d"],
        "bullish",
        np.where(df["stoch_k"] < df["stoch_d"], "bearish", "neutral"),
    )
    k = df["stoch_k"]
    d = df["stoch_d"]
    k_prev = df["stoch_k_prev"]
    d_prev = df["stoch_d_prev"]
    turning_up = df["stoch_bullish_cross"].fillna(False) | ((k > k_prev) & (d > d_prev))
    turning_down = df["stoch_bearish_cross"].fillna(False) | ((k < k_prev) & (d < d_prev))
    phase = np.full(len(df), "NEUTRAL", dtype=object)
    oversold = k < STOCH_LOW
    overbought = k > STOCH_HIGH
    phase = np.where(oversold & turning_up, "OVERSOLD_TURNING_UP", phase)
    phase = np.where(oversold & ~turning_up, "OVERSOLD", phase)
    phase = np.where(overbought & turning_down, "OVERBOUGHT_TURNING_DOWN", phase)
    phase = np.where(overbought & ~turning_down, "OVERBOUGHT", phase)
    mid = (~oversold) & (~overbought)
    phase = np.where(mid & (k > d), "BULL_MOMENTUM", phase)
    phase = np.where(mid & (k < d), "BEAR_MOMENTUM", phase)
    df["stoch_phase"] = phase

    rng = (high - low).replace(0.0, np.nan)
    body = (close - open_).abs()
    df["body_range"] = body / rng
    df["upper_wick"] = (high - np.maximum(open_, close)) / rng
    df["lower_wick"] = (np.minimum(open_, close) - low) / rng
    df["close_location"] = (close - low) / rng
    df["atr14"] = _wilder_atr(high, low, close)
    df["atr_pct"] = df["atr14"] / close * 100.0
    for n in (1, 3, 5):
        df[f"ret_{n}bar_pct"] = (close / close.shift(n) - 1.0) * 100.0
    for n in (10, 20, 50):
        df[f"roll_high_{n}"] = high.rolling(n, min_periods=n).max()
        df[f"roll_low_{n}"] = low.rolling(n, min_periods=n).min()
    span20 = df["roll_high_20"] - df["roll_low_20"]
    df["range20_pos_close"] = (close - df["roll_low_20"]) / span20.replace(0.0, np.nan)
    df["dist_roll_high_20_pct"] = (close / df["roll_high_20"] - 1.0) * 100.0
    df["dist_roll_low_20_pct"] = (close / df["roll_low_20"] - 1.0) * 100.0
    df["dist_roll_high_20_atr"] = (df["roll_high_20"] - close) / df["atr14"]
    df["dist_roll_low_20_atr"] = (close - df["roll_low_20"]) / df["atr14"]
    prior_high20 = high.shift(1).rolling(20, min_periods=20).max()
    prior_low20 = low.shift(1).rolling(20, min_periods=20).min()
    df["breakout_closed"] = close > prior_high20
    df["breakdown_closed"] = close < prior_low20
    df["higher_high"] = high > high.shift(1)
    df["lower_low"] = low < low.shift(1)
    df["higher_low"] = low > low.shift(1)
    df["lower_high"] = high < high.shift(1)
    df["two_bar_hh_hl"] = df["higher_high"] & df["higher_low"]
    df["two_bar_lh_ll"] = df["lower_high"] & df["lower_low"]
    for span in (9, 20, 50, 100, 200):
        col = f"ema{span}"
        df[f"close_minus_ema{span}_pct"] = (close / df[col] - 1.0) * 100.0
        for n in (1, 3, 5):
            prev = df[col].shift(n)
            df[f"ema{span}_slope_{n}"] = (df[col] / prev - 1.0) * 100.0
        df[f"close_vs_ema{span}"] = np.where(
            df[col].notna(),
            np.where(close > df[col], "ABOVE", np.where(close < df[col], "BELOW", "AT")),
            "MISSING",
        )
    df["ema20_minus_ema50_pct"] = (df["ema20"] / df["ema50"] - 1.0) * 100.0
    df["ema50_minus_ema200_pct"] = (df["ema50"] / df["ema200"] - 1.0) * 100.0
    e9, e20, e50, e100, e200 = df["ema9"], df["ema20"], df["ema50"], df["ema100"], df["ema200"]
    complete = e9.notna() & e20.notna() & e50.notna() & e100.notna() & e200.notna()
    bear_stack = complete & (e9 < e20) & (e20 < e50) & (e50 < e100) & (e100 < e200)
    bull_stack = complete & (e9 > e20) & (e20 > e50) & (e50 > e100) & (e100 > e200)
    stack = np.full(len(df), "MIXED", dtype=object)
    stack = np.where(~complete, "INCOMPLETE", stack)
    stack = np.where(bear_stack, "BEAR_STACK_9_20_50_100_200", stack)
    stack = np.where(bull_stack, "BULL_STACK_9_20_50_100_200", stack)
    df["ema_stack"] = stack
    e20 = df["ema20"]
    e50 = df["ema50"]
    e200 = df["ema200"]
    s20 = df["ema20_slope_3"]
    s50 = df["ema50_slope_3"]
    s200 = df["ema200_slope_3"]
    cls = np.full(len(df), "NEUTRAL", dtype=object)
    have_core = e20.notna() & e50.notna()
    have_200 = have_core & e200.notna()
    strong_bull = have_200 & (close > e20) & (e20 > e50) & (e50 > e200) & (s20 > 0) & (s50 > 0) & (s200 > 0)
    strong_bear = have_200 & (close < e20) & (e20 < e50) & (e50 < e200) & (s20 < 0) & (s50 < 0) & (s200 < 0)
    bull = have_core & (close > e20) & (e20 > e50) & (s20 > 0)
    bear = have_core & (close < e20) & (e20 < e50) & (s20 < 0)
    cls = np.where(bull, "BULL", cls)
    cls = np.where(bear, "BEAR", cls)
    cls = np.where(strong_bull, "STRONG_BULL", cls)
    cls = np.where(strong_bear, "STRONG_BEAR", cls)
    cls = np.where(~have_core, "MISSING", cls)
    df["ema_trend"] = cls
    df["ema200_missing"] = e200.isna()
    compression = (span20 / df["atr14"]) < 4.0
    trend_up = df["two_bar_hh_hl"] & (df["ret_5bar_pct"] > 0)
    trend_down = df["two_bar_lh_ll"] & (df["ret_5bar_pct"] < 0)
    regime = np.full(len(df), "RANGE", dtype=object)
    regime = np.where(compression, "COMPRESSION", regime)
    regime = np.where(trend_up, "TREND_UP", regime)
    regime = np.where(trend_down, "TREND_DOWN", regime)
    df["structure_regime"] = regime
    df["price_trend"] = np.where(
        trend_up,
        "UP",
        np.where(trend_down, "DOWN", np.where(compression, "COMPRESSION", "RANGE")),
    )
    return df


def last_closed_index(available_at: np.ndarray, entry: np.datetime64) -> int | None:
    i = int(np.searchsorted(available_at, entry, side="right")) - 1
    if i < 0:
        return None
    return i


def supports_opposes(direction: str, ema_trend: str, stoch_phase: str, exhausted: bool) -> str:
    side = str(direction).upper()
    ema_sup = ema_trend in ({"BULL", "STRONG_BULL"} if side == "LONG" else {"BEAR", "STRONG_BEAR"})
    ema_opp = ema_trend in ({"BEAR", "STRONG_BEAR"} if side == "LONG" else {"BULL", "STRONG_BULL"})
    stoch_sup = stoch_phase in (
        ("OVERSOLD_TURNING_UP", "BULL_MOMENTUM") if side == "LONG" else ("OVERBOUGHT_TURNING_DOWN", "BEAR_MOMENTUM")
    )
    stoch_opp = stoch_phase in (
        ("OVERBOUGHT", "OVERBOUGHT_TURNING_DOWN", "BEAR_MOMENTUM")
        if side == "LONG"
        else ("OVERSOLD", "OVERSOLD_TURNING_UP", "BULL_MOMENTUM")
    )
    if ema_opp or stoch_opp or exhausted:
        if ema_sup and not ema_opp and not exhausted:
            return "MIXED"
        return "OPPOSES"
    if ema_sup or stoch_sup:
        return "SUPPORTS"
    return "NEUTRAL"


def snapshot_row(
    *,
    tf: str,
    frame: pd.DataFrame,
    avail: np.ndarray,
    entry: pd.Timestamp,
    entry_price: float,
    direction: str,
) -> dict[str, Any]:
    entry_ns = np.datetime64(entry.to_datetime64())
    idx = last_closed_index(avail, entry_ns)
    base = {
        "timeframe": tf,
        "source_bar_open": None,
        "source_bar_close": None,
        "available_at": None,
        "available_at_le_entry": None,
        "snapshot_missing": True,
    }
    if idx is None:
        return base
    row = frame.iloc[idx]
    avail_ts = to_utc(row["available_at"])
    open_ts = to_utc(row["timestamp"])
    if avail_ts > entry:
        raise RuntimeError(f"LOOKAHEAD:{tf}:{iso_z(avail_ts)}>{iso_z(entry)}")
    close_px = float(row["close"])
    atr = float(row["atr14"]) if pd.notna(row["atr14"]) else np.nan
    span20 = (
        float(row["roll_high_20"]) - float(row["roll_low_20"])
        if pd.notna(row["roll_high_20"]) and pd.notna(row["roll_low_20"])
        else np.nan
    )
    entry_range_pos = (
        (entry_price - float(row["roll_low_20"])) / span20 if np.isfinite(span20) and span20 != 0 else np.nan
    )
    k = row["stoch_k"]
    phase = str(row["stoch_phase"])
    exhausted = bool(k < STOCH_LOW) if str(direction).upper() == "SHORT" else bool(k > STOCH_HIGH) if pd.notna(k) else False
    if pd.isna(k):
        exhausted = False
    ema_trend = str(row["ema_trend"])
    stoch_sup = phase in (
        ("OVERSOLD_TURNING_UP", "BULL_MOMENTUM") if direction == "LONG" else ("OVERBOUGHT_TURNING_DOWN", "BEAR_MOMENTUM")
    )
    stoch_opp = phase in (
        ("OVERBOUGHT", "OVERBOUGHT_TURNING_DOWN", "BEAR_MOMENTUM")
        if direction == "LONG"
        else ("OVERSOLD", "OVERSOLD_TURNING_UP", "BULL_MOMENTUM")
    )
    ema_sup = ema_trend in ({"BULL", "STRONG_BULL"} if direction == "LONG" else {"BEAR", "STRONG_BEAR"})
    ema_opp = ema_trend in ({"BEAR", "STRONG_BEAR"} if direction == "LONG" else {"BULL", "STRONG_BULL"})
    ema_strong_opp = ema_trend == ("STRONG_BEAR" if direction == "LONG" else "STRONG_BULL")
    out = {
        "timeframe": tf,
        "source_bar_open": iso_z(open_ts),
        "source_bar_close": iso_z(avail_ts),
        "available_at": iso_z(avail_ts),
        "available_at_le_entry": bool(avail_ts <= entry),
        "snapshot_missing": False,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": close_px,
        "volume": float(row["volume"]),
        "rsi": _f(row["rsi"]),
        "stoch_rsi_raw": _f(row["stoch_rsi_raw"]),
        "stoch_k": _f(row["stoch_k"]),
        "stoch_d": _f(row["stoch_d"]),
        "stoch_k_minus_d": _f(row["stoch_k_minus_d"]),
        "stoch_k_prev": _f(row["stoch_k_prev"]),
        "stoch_d_prev": _f(row["stoch_d_prev"]),
        "stoch_k_slope_1": _f(row["stoch_k_slope_1"]),
        "stoch_k_slope_2": _f(row["stoch_k_slope_2"]),
        "stoch_k_slope_3": _f(row["stoch_k_slope_3"]),
        "stoch_d_slope_1": _f(row["stoch_d_slope_1"]),
        "stoch_d_slope_2": _f(row["stoch_d_slope_2"]),
        "stoch_d_slope_3": _f(row["stoch_d_slope_3"]),
        "cross_up": _b(row["stoch_bullish_cross"]),
        "cross_down": _b(row["stoch_bearish_cross"]),
        "bars_since_cross_up": _f(row["bars_since_cross_up"]),
        "bars_since_cross_down": _f(row["bars_since_cross_down"]),
        "k_lt_20": _b(row["k_lt_20"]),
        "k_d_lt_20": _b(row["k_d_lt_20"]),
        "k_gt_80": _b(row["k_gt_80"]),
        "k_d_gt_80": _b(row["k_d_gt_80"]),
        "stoch_bias": row["stoch_bias"],
        "stoch_phase": phase,
        "stoch_supports_trade": bool(stoch_sup),
        "stoch_opposes_trade": bool(stoch_opp),
        "stoch_exhausted_in_trade_direction": bool(exhausted),
        "ema9": _f(row["ema9"]),
        "ema20": _f(row["ema20"]),
        "ema50": _f(row["ema50"]),
        "ema100": _f(row["ema100"]),
        "ema200": _f(row["ema200"]),
        "ema200_missing": bool(row["ema200_missing"]),
        "close_minus_ema9_pct": _f(row["close_minus_ema9_pct"]),
        "close_minus_ema20_pct": _f(row["close_minus_ema20_pct"]),
        "close_minus_ema50_pct": _f(row["close_minus_ema50_pct"]),
        "close_minus_ema100_pct": _f(row["close_minus_ema100_pct"]),
        "close_minus_ema200_pct": _f(row["close_minus_ema200_pct"]),
        "entry_minus_ema9_pct": _pct_vs(entry_price, row["ema9"]),
        "entry_minus_ema20_pct": _pct_vs(entry_price, row["ema20"]),
        "entry_minus_ema50_pct": _pct_vs(entry_price, row["ema50"]),
        "entry_minus_ema100_pct": _pct_vs(entry_price, row["ema100"]),
        "entry_minus_ema200_pct": _pct_vs(entry_price, row["ema200"]),
        "ema20_minus_ema50_pct": _f(row["ema20_minus_ema50_pct"]),
        "ema50_minus_ema200_pct": _f(row["ema50_minus_ema200_pct"]),
        "ema_stack": row["ema_stack"],
        "ema_trend": ema_trend,
        "ema_trend_supports_trade": bool(ema_sup),
        "ema_trend_opposes_trade": bool(ema_opp),
        "ema_strongly_opposes_trade": bool(ema_strong_opp),
        "close_vs_ema9": row["close_vs_ema9"],
        "close_vs_ema20": row["close_vs_ema20"],
        "close_vs_ema50": row["close_vs_ema50"],
        "close_vs_ema100": row["close_vs_ema100"],
        "close_vs_ema200": row["close_vs_ema200"],
        "ret_1bar_pct": _f(row["ret_1bar_pct"]),
        "ret_3bar_pct": _f(row["ret_3bar_pct"]),
        "ret_5bar_pct": _f(row["ret_5bar_pct"]),
        "atr14": _f(row["atr14"]),
        "atr_pct": _f(row["atr_pct"]),
        "body_range": _f(row["body_range"]),
        "upper_wick": _f(row["upper_wick"]),
        "lower_wick": _f(row["lower_wick"]),
        "close_location": _f(row["close_location"]),
        "range20_pos_close": _f(row["range20_pos_close"]),
        "range20_pos_entry": _f(entry_range_pos),
        "dist_roll_high_20_pct": _f(row["dist_roll_high_20_pct"]),
        "dist_roll_low_20_pct": _f(row["dist_roll_low_20_pct"]),
        "dist_roll_high_20_atr": _f(row["dist_roll_high_20_atr"]),
        "dist_roll_low_20_atr": _f(row["dist_roll_low_20_atr"]),
        "higher_high": _b(row["higher_high"]),
        "lower_low": _b(row["lower_low"]),
        "higher_low": _b(row["higher_low"]),
        "lower_high": _b(row["lower_high"]),
        "two_bar_hh_hl": _b(row["two_bar_hh_hl"]),
        "two_bar_lh_ll": _b(row["two_bar_lh_ll"]),
        "breakout_closed": _b(row["breakout_closed"]),
        "breakdown_closed": _b(row["breakdown_closed"]),
        "structure_regime": row["structure_regime"],
        "price_trend": row["price_trend"],
        "near_range_high": bool(_f(entry_range_pos) is not None and entry_range_pos >= 0.8),
        "near_range_low": bool(_f(entry_range_pos) is not None and entry_range_pos <= 0.2),
        "supports_opposes": supports_opposes(direction, ema_trend, phase, exhausted),
    }
    for span in (9, 20, 50, 100, 200):
        for n in (1, 3, 5):
            out[f"ema{span}_slope_{n}"] = _f(row[f"ema{span}_slope_{n}"])
    if np.isfinite(atr) and atr > 0 and np.isfinite(span20):
        if direction == "SHORT":
            room = float(row["roll_low_20"])
            room_pct = (entry_price - room) / entry_price * 100.0
            out["room_to_structure_pct"] = room_pct
            out["room_to_structure_atr"] = (entry_price - room) / atr
        else:
            room = float(row["roll_high_20"])
            room_pct = (room - entry_price) / entry_price * 100.0
            out["room_to_structure_pct"] = room_pct
            out["room_to_structure_atr"] = (room - entry_price) / atr
    else:
        out["room_to_structure_pct"] = None
        out["room_to_structure_atr"] = None
    return out


def _f(value: object) -> float | None:
    if value is None or (isinstance(value, (float, np.floating)) and not np.isfinite(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _b(value: object) -> bool | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return bool(value)


def _pct_vs(price: float, ema: object) -> float | None:
    e = _f(ema)
    if e is None or e == 0:
        return None
    return (price / e - 1.0) * 100.0


def calendar_returns(c1m: pd.DataFrame, avail: np.ndarray, entry: pd.Timestamp) -> dict[str, Any]:
    idx = last_closed_index(avail, np.datetime64(entry.to_datetime64()))
    out = {k: None for k in ("ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct")}
    if idx is None:
        return out
    close = c1m["close"].to_numpy(dtype=float)
    now = float(close[idx])
    for key, bars in (("ret_15m_pct", 15), ("ret_30m_pct", 30), ("ret_1h_pct", 60), ("ret_4h_pct", 240), ("ret_24h_pct", 1440)):
        j = idx - bars
        if j >= 0 and close[j] != 0:
            out[key] = (now / float(close[j]) - 1.0) * 100.0
    return out


def pre_entry_path(
    *,
    close: np.ndarray,
    close_times: np.ndarray,
    open_times: np.ndarray,
    trade: pd.Series,
) -> dict[str, Any]:
    entry = to_utc(trade["entry_time"])
    direction = str(trade["direction"]).upper()
    ep = float(trade["entry_price"])
    tp_pct = float(trade["tp_pct"])
    a_close = to_utc(trade["end_available_at"]) if pd.notna(trade["end_available_at"]) else None
    b_close = to_utc(trade["recognition_available_at"]) if pd.notna(trade["recognition_available_at"]) else None
    entry_ns = np.datetime64(entry.to_datetime64())
    end_i = last_closed_index(close_times, entry_ns)
    out: dict[str, Any] = {
        "wave_end_available_at": iso_z(a_close),
        "recognition_available_at": iso_z(b_close),
        "seconds_a_to_entry": int((entry - a_close).total_seconds()) if a_close is not None else None,
        "seconds_b_to_entry": int((entry - b_close).total_seconds()) if b_close is not None else None,
    }

    def price_at_close(ts: pd.Timestamp | None) -> float | None:
        if ts is None:
            return None
        i = last_closed_index(close_times, np.datetime64(ts.to_datetime64()))
        if i is None:
            return None
        got = pd.Timestamp(close_times[i], tz="UTC")
        if got != ts:
            return None
        return float(close[i])

    a_price = _f(trade["wave_end_price"])
    if a_price is None:
        a_price = price_at_close(a_close)
    if a_price and a_price != 0:
        move = (ep / a_price - 1.0) * 100.0
        aligned = -move if direction == "SHORT" else move
        out["a_to_entry_move_pct"] = move
        out["a_to_entry_aligned_pct"] = aligned
        out["a_to_entry_favorable_pct"] = max(aligned, 0.0)
        out["a_to_entry_adverse_pct"] = max(-aligned, 0.0)
        out["tp_consumed_frac"] = aligned / tp_pct if tp_pct else None
    else:
        out["a_to_entry_move_pct"] = None
        out["a_to_entry_aligned_pct"] = None
        out["a_to_entry_favorable_pct"] = None
        out["a_to_entry_adverse_pct"] = None
        out["tp_consumed_frac"] = None
    b_price = price_at_close(b_close)
    if b_price:
        move = (ep / b_price - 1.0) * 100.0
        aligned = -move if direction == "SHORT" else move
        out["b_to_entry_aligned_pct"] = aligned
    else:
        out["b_to_entry_aligned_pct"] = None
    if end_i is None:
        out["pre_entry_5m_return_pct"] = None
        out["pre_entry_15m_return_pct"] = None
        out["pre_entry_5m_aligned_pct"] = None
        out["pre_entry_15m_aligned_pct"] = None
    else:
        now = float(close[end_i])
        out["pre_entry_5m_return_pct"] = (now / float(close[end_i - 5]) - 1.0) * 100.0 if end_i >= 5 else None
        out["pre_entry_15m_return_pct"] = (now / float(close[end_i - 15]) - 1.0) * 100.0 if end_i >= 15 else None
        if direction == "SHORT":
            out["pre_entry_5m_aligned_pct"] = None if out["pre_entry_5m_return_pct"] is None else -out["pre_entry_5m_return_pct"]
            out["pre_entry_15m_aligned_pct"] = None if out["pre_entry_15m_return_pct"] is None else -out["pre_entry_15m_return_pct"]
        else:
            out["pre_entry_5m_aligned_pct"] = out["pre_entry_5m_return_pct"]
            out["pre_entry_15m_aligned_pct"] = out["pre_entry_15m_return_pct"]
    frac = out.get("tp_consumed_frac")
    for thr, name in ((0.25, "25"), (0.50, "50"), (0.75, "75"), (1.00, "100")):
        out[f"already_ran_{name}pct_tp"] = bool(frac is not None and frac >= thr)
    return out


def outcome_path(c1m: pd.DataFrame, trade: pd.Series) -> dict[str, Any]:
    entry = to_utc(trade["entry_time"])
    times = pd.to_datetime(c1m["open_time"], utc=True)
    start_i = int(np.searchsorted(times.to_numpy(dtype="datetime64[ns]"), np.datetime64(entry.to_datetime64()), side="left"))
    if start_i >= len(c1m) or times.iloc[start_i] != entry:
        return {
            "outcome_path_ok": False,
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_frac_tp": None,
            "reached_tp_25pct": None,
            "reached_tp_50pct": None,
            "reached_tp_75pct": None,
            "reached_tp_90pct": None,
        }
    if pd.notna(trade["exit_time"]):
        exit_t = to_utc(trade["exit_time"])
        end_i = int(np.searchsorted(times.to_numpy(dtype="datetime64[ns]"), np.datetime64(exit_t.to_datetime64()), side="left"))
        if end_i >= len(c1m):
            end_i = len(c1m) - 1
    else:
        end_i = len(c1m) - 1
    hh = c1m["high"].to_numpy(dtype=float)[start_i : end_i + 1]
    ll = c1m["low"].to_numpy(dtype=float)[start_i : end_i + 1]
    ep = float(trade["entry_price"])
    tp = float(trade["tp_price"])
    side = str(trade["direction"]).upper()
    if hh.size == 0:
        return {"outcome_path_ok": False}
    if side == "LONG":
        mfe = float(np.max(hh) - ep)
        mae = float(ep - np.min(ll))
        tp_dist = tp - ep
        reached = lambda frac: bool(np.max(hh) >= ep + frac * tp_dist) if tp_dist > 0 else False
    else:
        mfe = float(ep - np.min(ll))
        mae = float(np.max(hh) - ep)
        tp_dist = ep - tp
        reached = lambda frac: bool(np.min(ll) <= ep - frac * tp_dist) if tp_dist > 0 else False
    gross = _f(trade["pnl_pct_gross"])
    return {
        "outcome_path_ok": True,
        "mfe_pct": (mfe / ep) * 100.0 if ep else None,
        "mae_pct": (mae / ep) * 100.0 if ep else None,
        "mfe_frac_tp": (mfe / tp_dist) if tp_dist > 0 else None,
        "reached_tp_25pct": reached(0.25),
        "reached_tp_50pct": reached(0.50),
        "reached_tp_75pct": reached(0.75),
        "reached_tp_90pct": reached(0.90),
        "pnl_pct_gross": gross,
        "pnl_pct_net_0_11pp": None if gross is None else gross - FEE_PP,
        "hold_seconds": trade["hold_seconds"],
        "exit_time": iso_z(trade["exit_time"]) if pd.notna(trade["exit_time"]) else None,
        "outcome": trade["outcome"],
    }


def overlap_flags(frame: pd.DataFrame) -> pd.DataFrame:
    n = len(frame)
    exact = np.zeros(n, dtype=bool)
    higher_tf = np.zeros(n, dtype=bool)
    overlaps = np.zeros(n, dtype=bool)
    same = np.zeros(n, dtype=bool)
    opp = np.zeros(n, dtype=bool)
    n_open = np.zeros(n, dtype=int)
    entries = frame["entry_time"].to_numpy()
    exits = frame["exit_time"].to_numpy()
    tfs = frame["timeframe"].to_numpy()
    dirs = frame["direction"].to_numpy()
    opens = frame["is_open"].to_numpy()
    groups: dict[Any, list[int]] = {}
    for i, e in enumerate(entries):
        groups.setdefault(pd.Timestamp(e), []).append(i)
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        ranks = [TF_RANK.get(str(tfs[i]), -1) for i in idxs]
        top = max(ranks)
        for i, r in zip(idxs, ranks):
            exact[i] = True
            higher_tf[i] = r < top
    for i in range(n):
        e_i = entries[i]
        count = 0
        same_hit = False
        opp_hit = False
        for j in range(i):
            e_j = entries[j]
            if e_j >= e_i:
                continue
            still = bool(opens[j]) or (pd.notna(exits[j]) and exits[j] > e_i)
            if not still:
                continue
            count += 1
            if dirs[j] == dirs[i]:
                same_hit = True
            else:
                opp_hit = True
        n_open[i] = count
        overlaps[i] = count > 0
        same[i] = same_hit
        opp[i] = opp_hit
    out = frame.copy()
    out["exact_entry_duplicate"] = exact
    out["higher_tf_would_win"] = higher_tf
    out["overlaps_previous_trade"] = overlaps
    out["overlap_same_direction"] = same
    out["overlap_opposite_direction"] = opp
    out["number_of_open_zec_trades_at_entry"] = n_open
    return out


def assign_split(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["entry_time", "signal_id"]).reset_index(drop=True)
    n = len(out)
    n_dev = int(n * 0.60)
    n_val = int(n * 0.20)
    labels = np.array(["test"] * n, dtype=object)
    labels[:n_dev] = "development"
    labels[n_dev : n_dev + n_val] = "validation"
    labels[n_dev + n_val :] = "test"
    out["split"] = labels
    return out


def expected_bar_times(entry: pd.Timestamp) -> dict[str, tuple[str, str]]:
    """Last fully closed bar open/close at entry T, matching the task example."""
    t = to_utc(entry).floor("min")
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    total = int((t - epoch) // pd.Timedelta(minutes=1))
    out: dict[str, tuple[str, str]] = {}
    for tf, minutes in TF_MINUTES.items():
        current_open = total - (total % minutes)
        current_close = current_open + minutes
        if current_close <= total:
            last_close = current_close
        else:
            last_close = current_open
        last_open = last_close - minutes
        out[tf] = (
            iso_z(epoch + pd.Timedelta(minutes=last_open)),
            iso_z(epoch + pd.Timedelta(minutes=last_close)),
        )
    return out


def build_tf_frames(c1m: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]]]:
    as_of = to_utc(c1m["close_time"].iloc[-1])
    frames: dict[str, pd.DataFrame] = {}
    audits: list[dict[str, Any]] = []
    one = c1m.copy()
    one["timestamp"] = pd.to_datetime(one["open_time"], utc=True)
    one["available_at"] = pd.to_datetime(one["close_time"], utc=True)
    frames["1m"] = enrich_frame(one)
    audits.append(
        {
            "timeframe": "1m",
            "expected_1m": 1,
            "complete_buckets": int(len(frames["1m"])),
            "incomplete_or_gapped_discarded": 0,
            "not_closed_as_of_discarded": 0,
            "first_open": iso_z(frames["1m"]["timestamp"].iloc[0]),
            "last_open": iso_z(frames["1m"]["timestamp"].iloc[-1]),
            "last_close": iso_z(frames["1m"]["available_at"].iloc[-1]),
            "ohlc_rule": "native 1m",
            "utc_boundaries": True,
        }
    )
    for tf in SNAPSHOT_TFS:
        if tf == "1m":
            continue
        agg, audit = aggregate_complete(c1m, tf, as_of=as_of)
        audits.append(audit)
        frames[tf] = enrich_frame(agg)
    return frames, audits


def flatten_snapshot(prefix: str, snap: dict[str, Any]) -> dict[str, Any]:
    skip = {"timeframe"}
    return {f"{prefix}_{k}": v for k, v in snap.items() if k not in skip}


def alignment_fields(snaps: dict[str, dict[str, Any]], signal_tf: str, direction: str) -> dict[str, Any]:
    def so(tf: str) -> str | None:
        s = snaps.get(tf) or {}
        if s.get("snapshot_missing"):
            return None
        return s.get("supports_opposes")

    out: dict[str, Any] = {
        "signal_tf": signal_tf,
        "htf_30m_supports_opposes": so("30m"),
        "htf_1h_supports_opposes": so("1h"),
        "htf_4h_supports_opposes": so("4h"),
        "htf_1d_supports_opposes": so("1d"),
        "ltf_15m_supports_opposes": so("15m"),
        "ltf_5m_exhausted": bool((snaps.get("5m") or {}).get("stoch_exhausted_in_trade_direction")),
        "ltf_1m_opposite_recross": False,
    }
    one = snaps.get("1m") or {}
    if direction == "SHORT":
        out["ltf_1m_opposite_recross"] = bool(one.get("cross_up"))
    else:
        out["ltf_1m_opposite_recross"] = bool(one.get("cross_down"))
    four = snaps.get("4h") or {}
    out["htf_support_before_short_tp"] = None
    out["htf_resistance_before_long_tp"] = None
    return out


def htf_structure_vs_tp(snap_4h: dict[str, Any], trade: pd.Series) -> dict[str, Any]:
    direction = str(trade["direction"]).upper()
    ep = float(trade["entry_price"])
    tp = float(trade["tp_price"])
    low20 = snap_4h.get("dist_roll_low_20_pct")
    high20 = snap_4h.get("dist_roll_high_20_pct")
    # Reconstruct 4h 20-bar low/high from close and dist
    close = snap_4h.get("close")
    out = {
        "htf_support_before_short_tp": None,
        "htf_resistance_before_long_tp": None,
        "room_to_target_vs_tp": None,
        "entry_near_4h_range_high": bool(snap_4h.get("near_range_high")),
        "entry_near_4h_range_low": bool(snap_4h.get("near_range_low")),
        "range20_pos_4h_entry": snap_4h.get("range20_pos_entry"),
    }
    if close is None:
        return out
    if snap_4h.get("dist_roll_low_20_pct") is not None:
        low20_px = close / (1.0 + snap_4h["dist_roll_low_20_pct"] / 100.0)
    else:
        low20_px = None
    if snap_4h.get("dist_roll_high_20_pct") is not None:
        high20_px = close / (1.0 + snap_4h["dist_roll_high_20_pct"] / 100.0)
    else:
        high20_px = None
    if direction == "SHORT" and low20_px is not None:
        out["htf_support_before_short_tp"] = bool(low20_px > tp)
        room = ep - low20_px
        tp_dist = ep - tp
        out["room_to_target_vs_tp"] = room / tp_dist if tp_dist else None
        out["room_to_target"] = room / ep * 100.0 if ep else None
    if direction == "LONG" and high20_px is not None:
        out["htf_resistance_before_long_tp"] = bool(high20_px < tp)
        room = high20_px - ep
        tp_dist = tp - ep
        out["room_to_target_vs_tp"] = room / tp_dist if tp_dist else None
        out["room_to_target"] = room / ep * 100.0 if ep else None
    return out


def compact_matrix(snaps: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for tf in SNAPSHOT_TFS:
        s = snaps.get(tf) or {}
        rows.append(
            {
                "tf": tf,
                "price_trend": s.get("price_trend"),
                "ema_trend": s.get("ema_trend"),
                "stoch_phase": s.get("stoch_phase"),
                "supports_opposes": s.get("supports_opposes"),
            }
        )
    return rows


def is_manual_case(trade: pd.Series) -> bool:
    entry = iso_z(trade["entry_time"])
    direction = str(trade["direction"]).upper()
    return (entry, direction) in MANUAL_CASES
