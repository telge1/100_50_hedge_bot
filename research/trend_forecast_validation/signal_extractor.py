"""Map existing C3.4B rising-edge events to forecast signal types (no new structure logic)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.trend_forecast_validation.causal_replay import (
    PULLBACK_STATES_BEARISH_CONTEXT,
    PULLBACK_STATES_BULLISH_CONTEXT,
)
from research.trend_forecast_validation.config import ForecastValidationConfig


def _rising_bool(series: pd.Series) -> pd.Series:
    cur = series.fillna(False).astype(bool)
    prev = cur.shift(1).fillna(False).astype(bool)
    return cur & ~prev


def _ema_context(row: pd.Series) -> str:
    e9, e20, e59 = row.get("ema_9"), row.get("ema_20"), row.get("ema_59")
    try:
        if e9 > e20 > e59:
            return "bullish_stack"
        if e9 < e20 < e59:
            return "bearish_stack"
        if e9 > e20:
            return "short_bullish"
        if e9 < e20:
            return "short_bearish"
    except TypeError:
        return "unknown"
    return "mixed"


def _htf_trend_label(direction: Any) -> str:
    try:
        d = int(direction)
    except (TypeError, ValueError):
        return "unknown"
    if d > 0:
        return "bullish"
    if d < 0:
        return "bearish"
    return "flat"


def _numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def extract_forecast_signals(
    trace: pd.DataFrame,
    cfg: ForecastValidationConfig,
) -> pd.DataFrame:
    """Emit one row per causal forecast event at detected_timestamp (= candle t close)."""
    df = trace.copy().reset_index(drop=True)
    prev_state = df["previous_protected_structure_state"].astype(str)

    ext_bull = _rising_bool(df["external_bos_up"]) if "external_bos_up" in df.columns else pd.Series(False, index=df.index)
    ext_bear = _rising_bool(df["external_bos_down"]) if "external_bos_down" in df.columns else pd.Series(False, index=df.index)

    choch = df["choch_side"].astype(str) if "choch_side" in df.columns else pd.Series("", index=df.index)
    prev_choch = choch.shift(1).fillna("")
    choch_bull = (choch == "up") & (prev_choch != "up")
    choch_bear = (choch == "down") & (prev_choch != "down")

    # Protected breaks: prefer close-break rising edges from structure diag if present.
    if "close_break_protected_down" in df.columns:
        prot_low_break = _rising_bool(df["close_break_protected_down"])
    else:
        pl = _numeric_series(df, "protected_low")
        prot_low_break = (df["close"] < pl.shift(1)) & pl.shift(1).notna() & ~(
            (df["close"].shift(1) < pl.shift(1)).fillna(False)
        )
    if "close_break_protected_up" in df.columns:
        prot_high_break = _rising_bool(df["close_break_protected_up"])
    else:
        ph = _numeric_series(df, "protected_high")
        prot_high_break = (df["close"] > ph.shift(1)) & ph.shift(1).notna() & ~(
            (df["close"].shift(1) > ph.shift(1)).fillna(False)
        )

    event_masks: list[tuple[str, str, pd.Series]] = [
        (
            "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK",
            "bullish",
            ext_bull & prev_state.isin(PULLBACK_STATES_BEARISH_CONTEXT),
        ),
        (
            "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK",
            "bearish",
            ext_bear & prev_state.isin(PULLBACK_STATES_BULLISH_CONTEXT),
        ),
        ("BULLISH_CHOCH", "bullish", choch_bull),
        ("BEARISH_CHOCH", "bearish", choch_bear),
        ("PROTECTED_LOW_BREAK", "bearish", prot_low_break),
        ("PROTECTED_HIGH_BREAK", "bullish", prot_high_break),
    ]
    # Only configured types
    allowed = set(cfg.signal_types)
    event_masks = [e for e in event_masks if e[0] in allowed]

    rows: list[dict[str, Any]] = []
    last_same_dir_i: dict[str, int] = {"bullish": -10_000, "bearish": -10_000}
    signal_seq = 0

    # Snapshot protected levels from prior history (causal ffill).
    # At the BOS bar C3.4B may clear the level; pullback bars sometimes leave it NaN
    # until confirmation — use last known prior protected level.
    prev_protected_low = _numeric_series(df, "protected_low").ffill().shift(1)
    prev_protected_high = _numeric_series(df, "protected_high").ffill().shift(1)
    prev_micro_low = _numeric_series(df, "micro_swing_low").ffill().shift(1)
    prev_micro_high = _numeric_series(df, "micro_swing_high").ffill().shift(1)

    for i in range(len(df)):
        row = df.iloc[i]
        period = str(row.get("period") or "other")
        # Warm-up signals are recorded for audit but flagged excluded from stats.
        for signal_type, direction, mask in event_masks:
            if not bool(mask.iloc[i]):
                continue
            signal_seq += 1
            ts = pd.Timestamp(row["timestamp"])
            decision = pd.Timestamp(row["decision_time"]) if pd.notna(row.get("decision_time")) else ts + pd.Timedelta(minutes=5)
            forecast_active_from = decision

            bars_since = i - last_same_dir_i[direction]
            last_same_dir_i[direction] = i

            if direction == "bullish":
                inv = prev_protected_low.iloc[i]
                if pd.isna(inv):
                    inv = prev_micro_low.iloc[i]
                if pd.isna(inv):
                    inv = row.get("protected_low")
            else:
                inv = prev_protected_high.iloc[i]
                if pd.isna(inv):
                    inv = prev_micro_high.iloc[i]
                if pd.isna(inv):
                    inv = row.get("protected_high")
            invalidation = float(inv) if pd.notna(inv) else None

            m30 = row.get("m30_major_direction")
            h4 = row.get("h4_major_direction")
            m30_lab = _htf_trend_label(m30)
            h4_lab = _htf_trend_label(h4)
            aligned = (
                (direction == "bullish" and m30_lab == "bullish" and h4_lab == "bullish")
                or (direction == "bearish" and m30_lab == "bearish" and h4_lab == "bearish")
            )

            rows.append(
                {
                    "signal_id": f"{cfg.symbol}-{signal_seq:06d}",
                    "symbol": cfg.symbol,
                    "timeframe": cfg.timeframe,
                    "bar_index": int(i),
                    "source_timestamp": str(ts),  # structure pivot source not separately tracked → same as detect for BOS edge
                    "detected_timestamp": str(ts),
                    "forecast_active_from": str(forecast_active_from),
                    "signal_type": signal_type,
                    "forecast_direction": direction,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "ATR": float(row["atr_14"]) if pd.notna(row.get("atr_14")) else (
                        float(row["atr"]) if pd.notna(row.get("atr")) else None
                    ),
                    "major_trend": int(row["major_direction"]) if pd.notna(row.get("major_direction")) else None,
                    "regime": row.get("protected_structure_state"),
                    "micro_trend": row.get("protected_structure_state"),
                    "external_swing_high": row.get("micro_swing_high"),
                    "external_swing_low": row.get("micro_swing_low"),
                    "protected_high": row.get("protected_high"),
                    "protected_low": row.get("protected_low"),
                    "invalidation_price": invalidation,
                    "pullback_state": row.get("previous_protected_structure_state"),
                    "transition_reason": row.get("transition_reason"),
                    "EMA_9": row.get("ema_9"),
                    "EMA_20": row.get("ema_20"),
                    "EMA_59": row.get("ema_59"),
                    "EMA_200": row.get("ema_200"),
                    "EMA_context": _ema_context(row),
                    "ADX": row.get("adx"),
                    "DI_plus": row.get("plus_di"),
                    "DI_minus": row.get("minus_di"),
                    "trend_30m": m30_lab,
                    "trend_4h": h4_lab,
                    "HTF_alignment": "aligned" if aligned else "not_aligned",
                    "bars_since_last_same_direction_signal": int(bars_since) if bars_since < 10_000 else None,
                    "development_or_oos": (
                        "development"
                        if period == "development"
                        else "out_of_sample"
                        if period == "out_of_sample"
                        else "warmup"
                        if period == "warmup"
                        else period
                    ),
                    "include_in_stats": period in {"development", "out_of_sample"},
                    "last_visible_30m_timestamp": row.get("last_visible_30m_timestamp"),
                    "last_visible_4h_timestamp": row.get("last_visible_4h_timestamp"),
                    "structure_level_high": row.get("active_external_break_level")
                    if direction == "bullish"
                    else row.get("micro_swing_high"),
                    "structure_level_low": row.get("active_external_break_level")
                    if direction == "bearish"
                    else row.get("micro_swing_low"),
                }
            )

    return pd.DataFrame(rows)
