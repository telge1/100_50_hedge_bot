"""EMA candidate detection: synchronous cross + compressed rebound."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..cluster_sweep_research.ema_features import required_warmup_bars
from .config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from .models import CandidateType, Direction, FinalVerdict


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def make_candidate_id(symbol: str, tf: str, direction: str, ts: datetime, kind: str) -> str:
    key = f"{symbol}|{tf}|{direction}|{kind}|{_utc(ts).isoformat()}"
    return "edc:" + hashlib.sha1(key.encode()).hexdigest()[:20]


def attach_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    out = df.copy()
    h, l, c = out["high"].astype(float), out["low"].astype(float), out["close"].astype(float)
    prev_c = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr"] = tr.rolling(period, min_periods=period).mean()
    return out


def _ema_vals(row: pd.Series) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k in ("ema_9", "ema_20", "ema_59", "close", "open", "high", "low", "atr"):
        v = row.get(k)
        out[k] = None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    for k in ("ema_9_slope_1", "ema_20_slope_1", "ema_59_slope_1"):
        v = row.get(k)
        out[k] = None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    return out


def _cross_side(e9: float, e59: float) -> str:
    if e9 > e59:
        return "ABOVE"
    if e9 < e59:
        return "BELOW"
    return "EQUAL"


def _both_above(e9: float, e20: float, e59: float) -> bool:
    return e9 > e59 and e20 > e59


def _both_below(e9: float, e20: float, e59: float) -> bool:
    return e9 < e59 and e20 < e59


def _band_metrics(df: pd.DataFrame, i: int, cfg: EmaDualCrossConfig) -> dict[str, Any]:
    row = df.iloc[i]
    close = float(row["close"])
    atr = float(row["atr"]) if pd.notna(row.get("atr")) and float(row["atr"]) > 0 else None
    e9, e20, e59 = float(row["ema_9"]), float(row["ema_20"]), float(row["ema_59"])
    gap_9_20 = abs(e9 - e20)
    gap_pct = gap_9_20 / close * 100.0 if close > 0 else None
    gap_atr = gap_9_20 / atr if atr else None
    band = max(e9, e20, e59) - min(e9, e20, e59)
    band_atr = band / atr if atr else None
    lb = max(0, i - cfg.max_band_lookback + 1)
    hist = df.iloc[lb : i + 1]
    max_gap_pct = None
    if len(hist):
        gaps = (hist["ema_9"] - hist["ema_20"]).abs() / hist["close"].replace(0, np.nan) * 100.0
        max_gap_pct = float(gaps.max()) if gaps.notna().any() else None
    slopes = {
        "ema_9_slope_1": row.get("ema_9_slope_1"),
        "ema_20_slope_1": row.get("ema_20_slope_1"),
        "ema_59_slope_1": row.get("ema_59_slope_1"),
    }
    flat = False
    if atr:
        for sk, sv in slopes.items():
            if pd.notna(sv) and abs(float(sv)) / atr >= cfg.flat_slope_atr:
                flat = False
                break
        else:
            flat = True
    return {
        "ema_9_20_gap_pct": gap_pct,
        "ema_9_20_gap_atr": gap_atr,
        "ema_band_width": band,
        "ema_band_width_atr": band_atr,
        "max_band_gap_pct_lookback": max_gap_pct,
        "slopes": {k: (None if pd.isna(v) else float(v)) for k, v in slopes.items()},
        "flat_slopes": flat,
        "same_candle_cross": None,
        "cross_lag_ema9": None,
        "cross_lag_ema20": None,
    }


def detect_cross_events(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    cfg: EmaDualCrossConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (valid_candidates, rejected_crosses) as raw dict rows before gate."""
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    df = attach_atr(df.sort_values("open_time").reset_index(drop=True), cfg.atr_period)
    warm = required_warmup_bars(cfg.ema_slow, 20)
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    # Track when each EMA last crossed 59 (for stagger detection)
    last_cross_9: dict[str, int | None] = {"BULLISH": None, "BEARISH": None}
    last_cross_20: dict[str, int | None] = {"BULLISH": None, "BEARISH": None}

    sync_on_bar: set[tuple[int, str]] = set()

    for i in range(max(warm, 1), len(df) - 1):
        prev, cur = df.iloc[i - 1], df.iloc[i]
        if any(pd.isna(prev[k]) or pd.isna(cur[k]) for k in ("ema_9", "ema_20", "ema_59")):
            continue
        p9, p20, p59 = float(prev["ema_9"]), float(prev["ema_20"]), float(prev["ema_59"])
        c9, c20, c59 = float(cur["ema_9"]), float(cur["ema_20"]), float(cur["ema_59"])
        ts = _utc(pd.Timestamp(cur["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
        metrics = _band_metrics(df, i, cfg)

        # Per-side cross detection on this bar
        for direction, bull in ((Direction.BULLISH, True), (Direction.BEARISH, False)):
            if not cfg.enable_sync_cross:
                continue
            d = direction.value
            if bull:
                cross9 = p9 <= p59 and c9 > c59
                cross20 = p20 <= p59 and c20 > c59
                sync_prev = p9 <= p59 and p20 <= p59
                sync_now = c9 > c59 and c20 > c59
            else:
                cross9 = p9 >= p59 and c9 < c59
                cross20 = p20 >= p59 and c20 < c59
                sync_prev = p9 >= p59 and p20 >= p59
                sync_now = c9 < c59 and c20 < c59

            if cross9:
                last_cross_9[d] = i
            if cross20:
                last_cross_20[d] = i

            if cross9 and not cross20:
                other = last_cross_20[d]
                if other is not None and other != i and abs(other - i) <= cfg.max_band_lookback:
                    lag = abs(other - i)
                    sm = dict(metrics)
                    sm["cross_lag_bars"] = lag
                    sm["cross_lag_ema9"] = i - other
                    sm["cross_lag_ema20"] = 0
                    sm["first_cross_bar"] = other
                    sm["second_cross_bar"] = i
                    rejected.append(
                        _rej_row(symbol, timeframe, direction, ts, i, "REJECTED_STAGGERED_CROSS", sm, prev, cur)
                    )
                    continue
                rejected.append(_rej_row(symbol, timeframe, direction, ts, i, "REJECTED_EMA9_ONLY", metrics, prev, cur))
                continue
            if cross20 and not cross9:
                other = last_cross_9[d]
                if other is not None and other != i and abs(other - i) <= cfg.max_band_lookback:
                    lag = abs(other - i)
                    sm = dict(metrics)
                    sm["cross_lag_bars"] = lag
                    sm["cross_lag_ema9"] = 0
                    sm["cross_lag_ema20"] = i - other
                    sm["first_cross_bar"] = other
                    sm["second_cross_bar"] = i
                    rejected.append(
                        _rej_row(symbol, timeframe, direction, ts, i, "REJECTED_STAGGERED_CROSS", sm, prev, cur)
                    )
                    continue
                rejected.append(_rej_row(symbol, timeframe, direction, ts, i, "REJECTED_EMA20_ONLY", metrics, prev, cur))
                continue
            if cross9 or cross20:
                if not (sync_prev and sync_now):
                    if cross9 and cross20:
                        rejected.append(
                            _rej_row(symbol, timeframe, direction, ts, i, "REJECTED_NO_SIDE_CHANGE", metrics, prev, cur)
                        )
                    continue

            if not (sync_prev and sync_now):
                continue

            metrics["same_candle_cross"] = True
            metrics["cross_lag_ema9"] = 0
            metrics["cross_lag_ema20"] = 0

            # Band already expanded before cross
            if metrics.get("max_band_gap_pct_lookback") is not None:
                if metrics["max_band_gap_pct_lookback"] > cfg.band_compression_pct * 2.5:
                    rejected.append(
                        _rej_row(
                            symbol, timeframe, direction, ts, i, "REJECTED_BAND_ALREADY_EXPANDED", metrics, prev, cur
                        )
                    )
                    continue

            # Compression check
            compressed = True
            if metrics.get("ema_9_20_gap_pct") is not None and metrics["ema_9_20_gap_pct"] > cfg.band_compression_pct:
                compressed = False
            if metrics.get("ema_9_20_gap_atr") is not None and metrics["ema_9_20_gap_atr"] > cfg.band_compression_atr:
                compressed = False
            if not compressed:
                rejected.append(
                    _rej_row(symbol, timeframe, direction, ts, i, "REJECTED_BAND_ALREADY_EXPANDED", metrics, prev, cur)
                )
                continue

            if metrics.get("flat_slopes"):
                rejected.append(
                    _rej_row(symbol, timeframe, direction, ts, i, "REJECTED_FLAT_NO_IMPULSE", metrics, prev, cur)
                )
                continue

            cid = make_candidate_id(symbol, timeframe, d, ts, "SYNC")
            valid.append(
                {
                    "candidate_id": cid,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": d,
                    "candidate_type": CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value,
                    "candidate_at": ts,
                    "bar_index": i,
                    "ema_before": _ema_vals(prev),
                    "ema_after": _ema_vals(cur),
                    "ema_metrics": metrics,
                    "reason_codes": ["VALID_SYNCHRONOUS_CROSS"],
                }
            )
            sync_on_bar.add((i, d))

        # Compressed rebound — skipped when sync cross already on this bar/direction
        if cfg.enable_compressed_rebound:
            for direction in (Direction.BULLISH, Direction.BEARISH):
                d = direction.value
                if (i, d) in sync_on_bar:
                    continue
                rebound = _detect_rebound(df, i, cfg, symbol, timeframe, direction=direction)
                if rebound:
                    valid.append(rebound)

    return valid, rejected


def _detect_rebound(
    df: pd.DataFrame,
    i: int,
    cfg: EmaDualCrossConfig,
    symbol: str,
    timeframe: str,
    *,
    direction: Direction | None = None,
) -> dict[str, Any] | None:
    cur = df.iloc[i]
    if any(pd.isna(cur[k]) for k in ("ema_9", "ema_20", "ema_59", "atr", "close")):
        return None
    atr = float(cur["atr"])
    if atr <= 0:
        return None
    e9, e20, e59 = float(cur["ema_9"]), float(cur["ema_20"]), float(cur["ema_59"])
    close, open_, high, low = float(cur["close"]), float(cur["open"]), float(cur["high"]), float(cur["low"])
    band = max(e9, e20, e59) - min(e9, e20, e59)
    if band / atr > cfg.max_total_band_atr:
        return None
    if band / atr > cfg.rebound_ema_dist_atr_max:
        return None
    if abs(e9 - e20) / atr > cfg.rebound_ema_dist_atr_max:
        return None
    body = abs(close - open_)
    rng = high - low
    if body / atr < cfg.rebound_body_atr_min or rng / atr < cfg.rebound_range_atr_min:
        return None

    s9 = cur.get("ema_9_slope_1")
    s20 = cur.get("ema_20_slope_1")
    if pd.isna(s9) or pd.isna(s20):
        return None
    bull_turn = float(s9) > 0 and float(s20) > 0 and close > open_
    bear_turn = float(s9) < 0 and float(s20) < 0 and close < open_
    if direction == Direction.BULLISH and not bull_turn:
        return None
    if direction == Direction.BEARISH and not bear_turn:
        return None
    if direction is None:
        if not bull_turn and not bear_turn:
            return None
    elif direction == Direction.BULLISH:
        if not bull_turn:
            return None
    elif direction == Direction.BEARISH:
        if not bear_turn:
            return None

    # Rejection: wick through EMA band and close back on trend side
    mid = (e9 + e20 + e59) / 3.0
    if (direction or (Direction.BULLISH if bull_turn else Direction.BEARISH)) == Direction.BULLISH:
        if low >= min(e9, e20, e59) * 0.999:
            return None
        if close <= mid:
            return None
        direction = Direction.BULLISH
    else:
        if high <= max(e9, e20, e59) * 1.001:
            return None
        if close >= mid:
            return None
        direction = Direction.BEARISH

    ts = _utc(pd.Timestamp(cur["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
    metrics = _band_metrics(df, i, cfg)
    metrics["rebound_body_atr"] = body / atr
    metrics["rebound_range_atr"] = rng / atr
    metrics["close_position"] = (close - low) / rng if rng > 0 else 0.5
    cid = make_candidate_id(symbol, timeframe, direction.value, ts, "REBOUND")
    return {
        "candidate_id": cid,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction.value,
        "candidate_type": CandidateType.COMPRESSED_EMA59_REBOUND.value,
        "candidate_at": ts,
        "bar_index": i,
        "ema_before": _ema_vals(df.iloc[i - 1]),
        "ema_after": _ema_vals(cur),
        "ema_metrics": metrics,
        "reason_codes": ["VALID_COMPRESSED_REBOUND"],
    }


def _rej_row(
    symbol: str,
    timeframe: str,
    direction: Direction,
    ts: datetime,
    i: int,
    code: str,
    metrics: dict[str, Any],
    prev: pd.Series,
    cur: pd.Series,
) -> dict[str, Any]:
    return {
        "candidate_id": make_candidate_id(symbol, timeframe, direction.value, ts, f"REJ:{code}"),
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction.value,
        "candidate_type": CandidateType.REJECTED_EMA_CROSS.value,
        "candidate_at": ts,
        "bar_index": i,
        "final_verdict": FinalVerdict.REJECTED.value,
        "reason_codes": [code],
        "ema_before": _ema_vals(prev),
        "ema_after": _ema_vals(cur),
        "ema_metrics": dict(metrics),
    }
