"""Phase C3.3A indicator-pattern discovery for APTUSDT 30m.

Research-only scope:

* 30m-native discovery, using 30m candles and 30m C3.2A indicator features.
* Reuses C3.2A features as-is; no indicator recalculation inside this module.
* Timing is split explicitly between as-of and retrospective markers.
* No production logic or regime-gate code is changed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    INDICATOR_FEATURE_VERSION,
    load_ohlcv_frame,
    load_or_build_indicator_features,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import TIMEFRAME_MINUTES
from research.regime_scanner.trend_pine_export import build_pine_header, validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_state_forward_outcome_audit import (
    build_price_arrays,
    compute_horizon_outcome,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_3a_apt_pattern_discovery")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)


@dataclass(frozen=True)
class PatternDiscoveryConfig:
    pre_bars: int = 12
    post_bars: int = 48
    horizons: tuple[int, ...] = (3, 6, 12, 24, 48, 96)
    min_pattern_events: int = 20
    discovery_end: str | None = None
    range_lookback: int = 48
    breakout_buffer_atr: float = 0.15
    breakout_acceptance_bars: int = 2
    breakout_max_bars: int = 12
    expansion_min_change_3_atr: float = 0.08
    expansion_min_duration: int = 2
    compression_max_for_cross_context: float = 0.45
    near_ema59_atr: float = 0.5
    near_ema200_atr: float = 1.0
    clean_mfe_min: float = 0.8
    clean_mae_max: float = 0.6
    fail_mfe_max: float = 0.25
    reentry_bars: int = 6
    adverse_mae_min: float = 1.0
    delayed_horizon: int = 24
    adx_rise_min_slope_3: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _ts(value).isoformat()


def _finite(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _boolish(value: object | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(float(value))
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y", "on"}


def _direction_side(direction: str | None) -> str | None:
    if direction in {"bullish", "up", "long"}:
        return "long"
    if direction in {"bearish", "down", "short"}:
        return "short"
    return None


def _safe_bucket(delta: int | None) -> str:
    if delta is None:
        return "none"
    if delta == 0:
        return "coincident"
    if delta < 0:
        return "lead"
    if 1 <= delta <= 3:
        return "lag_1_3"
    if 4 <= delta <= 6:
        return "lag_4_6"
    return "none"


def _annotate_event(
    row: Mapping[str, Any],
    *,
    event_type: str,
    direction: str,
    symbol: str,
    timeframe: str,
    event_id: int,
    is_retrospective: bool = False,
) -> dict[str, Any]:
    out = {
        "event_id": f"{symbol}:{timeframe}:{event_type}:{event_id}",
        "event_type": event_type,
        "direction": direction,
        "event_timestamp": _iso(row.get("decision_time") or row.get("timestamp")),
        "bar_index": int(row["bar_index"]),
        "symbol": symbol,
        "timeframe": timeframe,
        "is_retrospective": bool(is_retrospective),
        "features_ready": bool(row.get("features_ready", False)),
        "regime_proxy": str(row.get("regime_proxy") or "unclear"),
        "regime_proxy_direction": str(row.get("regime_proxy_direction") or "unclear"),
        "close": _finite(row.get("close"), 0.0),
        "high": _finite(row.get("high"), 0.0),
        "low": _finite(row.get("low"), 0.0),
        "atr_14": _finite(row.get("atr_14"), 0.0),
        "ema_9": _finite(row.get("ema_9"), 0.0),
        "ema_20": _finite(row.get("ema_20"), 0.0),
        "ema_59": _finite(row.get("ema_59"), 0.0),
        "ema_200": _finite(row.get("ema_200"), 0.0),
        "ema_9_20_spread": _finite(row.get("ema_9_20_spread"), 0.0),
        "ema_9_20_spread_atr": _finite(row.get("ema_9_20_spread_atr"), 0.0),
        "ema_9_20_abs_spread_atr": _finite(row.get("ema_9_20_abs_spread_atr"), 0.0),
        "ema_9_20_spread_change_3_atr": _finite(row.get("ema_9_20_spread_change_3_atr"), 0.0),
        "close_to_ema_20_atr": _finite(row.get("close_to_ema_20_atr"), np.nan),
        "ema_9_slope_3_atr": _finite(row.get("ema_9_slope_3_atr"), 0.0),
        "ema_20_slope_3_atr": _finite(row.get("ema_20_slope_3_atr"), 0.0),
        "ema_59_slope_3_atr": _finite(row.get("ema_59_slope_3_atr"), 0.0),
        "ema_200_slope_3_atr": _finite(row.get("ema_200_slope_3_atr"), 0.0),
        "ema_fast_compression_score": _finite(row.get("ema_fast_compression_score"), 0.0),
        "ema_fast_expansion_score": _finite(row.get("ema_fast_expansion_score"), 0.0),
        "ema_bullish_ordered": bool(row.get("ema_bullish_ordered", False)),
        "ema_bearish_ordered": bool(row.get("ema_bearish_ordered", False)),
        "plus_di_14": _finite(row.get("plus_di_14"), 0.0),
        "minus_di_14": _finite(row.get("minus_di_14"), 0.0),
        "di_spread": _finite(row.get("di_spread"), 0.0),
        "adx_14": _finite(row.get("adx_14"), 0.0),
        "adx_slope_3": _finite(row.get("adx_slope_3"), 0.0),
        "adx_slope_6": _finite(row.get("adx_slope_6"), 0.0),
        "adx_rising_3": bool(row.get("adx_rising_3", False)),
        "adx_rising_6": bool(row.get("adx_rising_6", False)),
        "adx_accelerating_now": bool(row.get("adx_accelerating_now", False)),
        "range_high": _finite(row.get("range_high"), np.nan),
        "range_low": _finite(row.get("range_low"), np.nan),
        "range_mid": _finite(row.get("range_mid"), np.nan),
        "range_width_atr": _finite(row.get("range_width_atr"), np.nan),
        "range_breakout_upper": _finite(row.get("range_breakout_upper"), np.nan),
        "range_breakout_lower": _finite(row.get("range_breakout_lower"), np.nan),
        "close_to_ema_59_atr": _finite(row.get("close_to_ema_59_atr"), np.nan),
        "close_to_ema_200_atr": _finite(row.get("close_to_ema_200_atr"), np.nan),
    }
    return out


def build_discovery_frame(
    symbol: str,
    timeframe: str,
    load_start: str,
    load_end: str,
    analyze_start: str,
    analyze_end: str,
    cache_dir: Path | None,
) -> pd.DataFrame:
    tf = str(timeframe).strip().lower()
    if tf != "30m":
        raise ValueError("phase C3.3A discovery is 30m-native only")

    raw = load_ohlcv_frame(symbol, tf, start=load_start, end=load_end).copy()
    feats = load_or_build_indicator_features(
        symbol=symbol,
        timeframe=tf,
        analyze_start=load_start,
        analyze_end=load_end,
        cache_dir=cache_dir,
    ).copy()

    if raw.empty and feats.empty:
        return pd.DataFrame()

    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    feats["timestamp"] = pd.to_datetime(feats["timestamp"], utc=True)
    merged = raw.merge(feats, on="timestamp", how="inner", suffixes=("_raw", ""))

    for col in ("open", "high", "low", "close", "volume"):
        raw_col = f"{col}_raw"
        if raw_col in merged.columns:
            merged[col] = pd.to_numeric(merged[raw_col], errors="coerce").astype("float64")
            merged.drop(columns=[raw_col], inplace=True)
        elif col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce").astype("float64")

    merged = merged.sort_values("timestamp").reset_index(drop=True)
    merged["bar_index"] = np.arange(len(merged), dtype=int)
    merged["decision_time"] = merged["timestamp"] + pd.to_timedelta(
        TIMEFRAME_MINUTES[tf], unit="m"
    )

    # Causal 30m range context: prior bars only (exclude current bar high/low).
    lookback = int(PatternDiscoveryConfig().range_lookback)
    high_s = pd.to_numeric(merged["high"], errors="coerce")
    low_s = pd.to_numeric(merged["low"], errors="coerce")
    prior_high = high_s.shift(1)
    prior_low = low_s.shift(1)
    merged["range_high"] = prior_high.rolling(lookback, min_periods=1).max()
    merged["range_low"] = prior_low.rolling(lookback, min_periods=1).min()
    merged["range_mid"] = (merged["range_high"] + merged["range_low"]) / 2.0
    merged["range_width_atr"] = (
        (merged["range_high"] - merged["range_low"]) / merged["atr_14"].replace(0.0, np.nan)
    )
    merged["range_breakout_upper"] = merged["range_high"] + (
        PatternDiscoveryConfig().breakout_buffer_atr * merged["atr_14"]
    )
    merged["range_breakout_lower"] = merged["range_low"] - (
        PatternDiscoveryConfig().breakout_buffer_atr * merged["atr_14"]
    )

    compression_limit = PatternDiscoveryConfig().compression_max_for_cross_context
    bullish = merged["ema_bullish_ordered"].fillna(False).astype(bool)
    bearish = merged["ema_bearish_ordered"].fillna(False).astype(bool)
    compression = merged["ema_fast_compression_score"].fillna(0.0) >= compression_limit
    near59 = merged["close_to_ema_59_atr"].abs().fillna(np.inf) <= PatternDiscoveryConfig().near_ema59_atr
    near200 = (
        merged["close_to_ema_200_atr"].abs().fillna(np.inf)
        <= PatternDiscoveryConfig().near_ema200_atr
    )
    range_like = compression | (~bullish & ~bearish)
    merged["regime_proxy_direction"] = np.where(
        bullish, "up", np.where(bearish, "down", np.where(range_like, "range", "unclear"))
    )
    merged["regime_proxy"] = np.where(
        bullish,
        "bullish",
        np.where(bearish, "bearish", np.where(range_like, "range", "unclear")),
    )
    merged["proxy_is_near_ema59"] = near59.astype(bool)
    merged["proxy_is_near_ema200"] = near200.astype(bool)
    merged["features_ready"] = merged["features_ready"].fillna(False).astype(bool)
    merged["symbol"] = symbol
    merged["timeframe"] = tf
    merged["analysis_window_start"] = _ts(analyze_start)
    merged["analysis_window_end"] = _ts(analyze_end)
    merged["in_analyze_window"] = (
        (merged["decision_time"] >= merged["analysis_window_start"])
        & (merged["decision_time"] <= merged["analysis_window_end"])
    )

    numeric_cols = merged.select_dtypes(include=[np.number]).columns
    merged[numeric_cols] = merged[numeric_cols].replace([np.inf, -np.inf], np.nan)
    return merged


def detect_ema_crosses(frame: pd.DataFrame) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    spread = pd.to_numeric(frame["ema_9_20_spread"], errors="coerce").to_numpy(dtype=float)
    comp = pd.to_numeric(frame["ema_fast_compression_score"], errors="coerce").to_numpy(dtype=float)
    reg = frame.get("regime_proxy", pd.Series(index=frame.index, dtype=object)).astype(str).to_numpy()
    near59 = pd.to_numeric(frame.get("close_to_ema_59_atr"), errors="coerce").abs().to_numpy(dtype=float)
    near200 = pd.to_numeric(frame.get("close_to_ema_200_atr"), errors="coerce").abs().to_numpy(dtype=float)
    ts = pd.to_datetime(frame["decision_time"], utc=True)
    event_id = 0
    prev_sign = np.sign(spread[0]) if len(spread) else 0.0
    for i in range(1, len(frame)):
        cur = spread[i]
        if not math.isfinite(cur):
            prev_sign = np.sign(cur) if math.isfinite(cur) else prev_sign
            continue
        cur_sign = np.sign(cur)
        if cur_sign == 0 or prev_sign == 0 or cur_sign == prev_sign:
            prev_sign = cur_sign if cur_sign != 0 else prev_sign
            continue
        direction = "bullish" if cur_sign > 0 else "bearish"
        event_id += 1
        row = frame.iloc[i]
        events.append(
            {
                **_annotate_event(
                    row,
                    event_type="ema_cross",
                    direction=direction,
                    symbol=str(row["symbol"]),
                    timeframe=str(row["timeframe"]),
                    event_id=event_id,
                ),
                "cross_from_compression": bool(
                    _finite(comp[i - 1], 0.0) >= PatternDiscoveryConfig().compression_max_for_cross_context
                    or _finite(comp[i], 0.0) >= PatternDiscoveryConfig().compression_max_for_cross_context
                ),
                "cross_in_range": str(reg[i]) == "range",
                "cross_near_ema59": bool(near59[i] <= PatternDiscoveryConfig().near_ema59_atr),
                "cross_near_ema200": bool(near200[i] <= PatternDiscoveryConfig().near_ema200_atr),
                "spread_prev": _finite(spread[i - 1], np.nan),
                "spread_now": _finite(cur, np.nan),
                "spread_sign_change": True,
                "event_timestamp": ts.iloc[i].isoformat(),
                "source_bar_index": int(i),
            }
        )
        prev_sign = cur_sign
    return events


def detect_ema_expansions(frame: pd.DataFrame) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    cfg = PatternDiscoveryConfig()
    change = pd.to_numeric(frame["ema_9_20_spread_change_3_atr"], errors="coerce").to_numpy(dtype=float)
    spread = pd.to_numeric(frame["ema_9_20_spread_atr"], errors="coerce").to_numpy(dtype=float)
    s9 = pd.to_numeric(frame["ema_9_slope_3_atr"], errors="coerce").to_numpy(dtype=float)
    s20 = pd.to_numeric(frame["ema_20_slope_3_atr"], errors="coerce").to_numpy(dtype=float)
    ts = pd.to_datetime(frame["decision_time"], utc=True)
    event_id = 0
    active = False
    active_dir: str | None = None
    active_bars = 0
    for i in range(len(frame)):
        aligned = (
            math.isfinite(change[i])
            and abs(change[i]) >= cfg.expansion_min_change_3_atr
            and math.isfinite(s9[i])
            and math.isfinite(s20[i])
            and np.sign(s9[i]) == np.sign(s20[i])
            and np.sign(s9[i]) == np.sign(spread[i]) != 0
        )
        if aligned and not active:
            active = True
            active_bars = 1
            active_dir = "bullish" if spread[i] >= 0 else "bearish"
            event_id += 1
            row = frame.iloc[i]
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="ema_expansion_start",
                        direction=str(active_dir),
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "expansion_change_3_atr": _finite(change[i], np.nan),
                    "expansion_duration_bar": 1,
                    "aligned_slopes": True,
                    "ema_expansion": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
        elif aligned and active:
            active_bars += 1
        else:
            active = False
            active_dir = None
            active_bars = 0
    return events


def detect_di_crosses(frame: pd.DataFrame) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    spread = pd.to_numeric(frame["di_spread"], errors="coerce").to_numpy(dtype=float)
    ts = pd.to_datetime(frame["decision_time"], utc=True)
    event_id = 0
    prev_sign = np.sign(spread[0]) if len(spread) else 0.0
    for i in range(1, len(frame)):
        cur = spread[i]
        if not math.isfinite(cur):
            prev_sign = np.sign(cur) if math.isfinite(cur) else prev_sign
            continue
        cur_sign = np.sign(cur)
        if cur_sign == 0 or prev_sign == 0 or cur_sign == prev_sign:
            prev_sign = cur_sign if cur_sign != 0 else prev_sign
            continue
        direction = "bullish" if cur_sign > 0 else "bearish"
        event_id += 1
        row = frame.iloc[i]
        events.append(
            {
                **_annotate_event(
                    row,
                    event_type="di_cross",
                    direction=direction,
                    symbol=str(row["symbol"]),
                    timeframe=str(row["timeframe"]),
                    event_id=event_id,
                ),
                "di_prev": _finite(spread[i - 1], np.nan),
                "di_now": _finite(cur, np.nan),
                "event_timestamp": ts.iloc[i].isoformat(),
                "source_bar_index": int(i),
            }
        )
        prev_sign = cur_sign
    return events


def detect_adx_dynamics(frame: pd.DataFrame, cfg: PatternDiscoveryConfig) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    adx = pd.to_numeric(frame["adx_14"], errors="coerce").to_numpy(dtype=float)
    s3 = pd.to_numeric(frame["adx_slope_3"], errors="coerce").to_numpy(dtype=float)
    s6 = pd.to_numeric(frame["adx_slope_6"], errors="coerce").to_numpy(dtype=float)
    s1 = adx - np.roll(adx, 1)
    s1[0] = np.nan
    ts = pd.to_datetime(frame["decision_time"], utc=True)
    event_id = 0
    rising3_prev = False
    rising6_prev = False
    accel_prev = False
    for i in range(len(frame)):
        rising3 = math.isfinite(s3[i]) and s3[i] > cfg.adx_rise_min_slope_3
        rising6 = math.isfinite(s6[i]) and s6[i] > cfg.adx_rise_min_slope_3
        accel = bool(
            i > 0
            and math.isfinite(s3[i])
            and math.isfinite(s1[i])
            and s3[i] > 0
            and s1[i] > 0
            and math.isfinite(s3[i - 1])
            and s1[i] > s3[i - 1]
        )
        if rising3 and not rising3_prev:
            event_id += 1
            row = frame.iloc[i]
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="adx_rising_3",
                        direction="bullish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "adx_slope_3": _finite(s3[i], np.nan),
                    "adx_slope_6": _finite(s6[i], np.nan),
                    "adx_rising_3": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
        if rising6 and not rising6_prev:
            event_id += 1
            row = frame.iloc[i]
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="adx_rising_6",
                        direction="bullish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "adx_slope_3": _finite(s3[i], np.nan),
                    "adx_slope_6": _finite(s6[i], np.nan),
                    "adx_rising_6": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
        if accel and not accel_prev:
            event_id += 1
            row = frame.iloc[i]
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="adx_accelerating_now",
                        direction="bullish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "adx_slope_3": _finite(s3[i], np.nan),
                    "adx_slope_1": _finite(s1[i], np.nan),
                    "adx_accelerating_now": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
        rising3_prev = rising3
        rising6_prev = rising6
        accel_prev = accel

    # Retrospective markers use ±2 bar strict local extrema.
    for i in range(2, len(frame) - 2):
        window = adx[i - 2 : i + 3]
        if not np.isfinite(window).all():
            continue
        row = frame.iloc[i]
        if adx[i] == float(np.min(window)):
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="adx_local_low_retro",
                        direction="bullish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                        is_retrospective=True,
                    ),
                    "local_window": 5,
                    "adx_local_low_retro": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
            if s3[i] > cfg.adx_rise_min_slope_3:
                event_id += 1
                events.append(
                    {
                        **_annotate_event(
                            row,
                            event_type="adx_rise_start",
                            direction="bullish",
                            symbol=str(row["symbol"]),
                            timeframe=str(row["timeframe"]),
                            event_id=event_id,
                            is_retrospective=True,
                        ),
                        "adx_slope_3": _finite(s3[i], np.nan),
                        "adx_rise_start": True,
                        "event_timestamp": ts.iloc[i].isoformat(),
                    }
                )
        if adx[i] == float(np.max(window)):
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="adx_peak_retro",
                        direction="bearish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                        is_retrospective=True,
                    ),
                    "local_window": 5,
                    "adx_peak_retro": True,
                    "event_timestamp": ts.iloc[i].isoformat(),
                }
            )
            if i + 1 < len(frame) and adx[i + 1] < adx[i]:
                event_id += 1
                events.append(
                    {
                        **_annotate_event(
                            row,
                            event_type="adx_rollover_retro",
                            direction="bearish",
                            symbol=str(row["symbol"]),
                            timeframe=str(row["timeframe"]),
                            event_id=event_id,
                            is_retrospective=True,
                        ),
                    "adx_rollover_retro": True,
                        "event_timestamp": ts.iloc[i].isoformat(),
                    }
                )
    return events


def detect_range_breakouts(frame: pd.DataFrame, cfg: PatternDiscoveryConfig) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    event_id = 0
    active: dict[str, Any] | None = None
    for i, row in frame.iterrows():
        close = _finite(row.get("close"), np.nan)
        high = _finite(row.get("high"), np.nan)
        low = _finite(row.get("low"), np.nan)
        atr = max(_finite(row.get("atr_14"), np.nan), 1e-9)
        upper = _finite(row.get("range_breakout_upper"), np.nan)
        lower = _finite(row.get("range_breakout_lower"), np.nan)
        if not math.isfinite(upper) or not math.isfinite(lower) or not math.isfinite(close):
            if active is not None:
                active["bars_active"] += 1
            continue
        outside_up = close > upper
        outside_down = close < lower
        direction = "bullish" if outside_up else "bearish" if outside_down else None
        if active is None:
            if direction is None:
                continue
            event_id += 1
            active = {
                "direction": direction,
                "bars_active": 1,
                "bars_outside": 1,
                "start_index": int(i),
                "start_time": row["decision_time"],
                "start_close": close,
                "start_high": high,
                "start_low": low,
                "range_high": _finite(row.get("range_high"), np.nan),
                "range_low": _finite(row.get("range_low"), np.nan),
                "breakout_level": upper if direction == "bullish" else lower,
                "event_id": event_id,
            }
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="range_breakout_attempt",
                        direction=direction,
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "lifecycle_stage": "attempt",
                    "bars_outside": 1,
                    "range_high": active["range_high"],
                    "range_low": active["range_low"],
                    "breakout_level": active["breakout_level"],
                    "range_breakout_upper": upper,
                    "range_breakout_lower": lower,
                    "close_outside": True,
                    "event_timestamp": _iso(row["decision_time"]),
                }
            )
            continue

        active["bars_active"] += 1
        if direction == active["direction"]:
            if active.get("confirmed"):
                continue
            active["bars_outside"] += 1
            if active["bars_outside"] >= cfg.breakout_acceptance_bars:
                event_id += 1
                events.append(
                    {
                        **_annotate_event(
                            row,
                            event_type="range_breakout_confirmed",
                            direction=direction,
                            symbol=str(row["symbol"]),
                            timeframe=str(row["timeframe"]),
                            event_id=event_id,
                        ),
                        "lifecycle_stage": "confirmed",
                        "bars_outside": active["bars_outside"],
                        "range_high": active["range_high"],
                        "range_low": active["range_low"],
                        "breakout_level": active["breakout_level"],
                        "range_breakout_upper": upper,
                        "range_breakout_lower": lower,
                        "close_outside": True,
                        "breakout_confirmed": True,
                        "event_timestamp": _iso(row["decision_time"]),
                    }
                )
                active["confirmed"] = True
            continue

        if not outside_up and not outside_down:
            if active.get("confirmed"):
                active = None
                continue
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="range_breakout_failed",
                        direction=active["direction"],
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "lifecycle_stage": "failed",
                    "bars_outside": active["bars_outside"],
                    "range_high": active["range_high"],
                    "range_low": active["range_low"],
                    "breakout_level": active["breakout_level"],
                    "range_breakout_upper": upper,
                    "range_breakout_lower": lower,
                    "close_outside": False,
                    "breakout_failed": True,
                    "event_timestamp": _iso(row["decision_time"]),
                }
            )
            active = None
            continue

        # Opposite breakout before acceptance: close the current lifecycle, then start a new one.
        event_id += 1
        events.append(
            {
                **_annotate_event(
                    row,
                    event_type="range_breakout_failed",
                    direction=active["direction"],
                    symbol=str(row["symbol"]),
                    timeframe=str(row["timeframe"]),
                    event_id=event_id,
                ),
                "lifecycle_stage": "failed",
                "bars_outside": active["bars_outside"],
                "range_high": active["range_high"],
                "range_low": active["range_low"],
                "breakout_level": active["breakout_level"],
                "range_breakout_upper": upper,
                "range_breakout_lower": lower,
                "close_outside": True,
                "breakout_failed": True,
                "event_timestamp": _iso(row["decision_time"]),
            }
        )
        active = None
        if direction is not None:
            event_id += 1
            active = {
                "direction": direction,
                "bars_active": 1,
                "bars_outside": 1,
                "confirmed": False,
                "start_index": int(i),
                "start_time": row["decision_time"],
                "start_close": close,
                "start_high": high,
                "start_low": low,
                "range_high": _finite(row.get("range_high"), np.nan),
                "range_low": _finite(row.get("range_low"), np.nan),
                "breakout_level": upper if direction == "bullish" else lower,
                "event_id": event_id,
            }
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="range_breakout_attempt",
                        direction=direction,
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "lifecycle_stage": "attempt",
                    "bars_outside": 1,
                    "range_high": active["range_high"],
                    "range_low": active["range_low"],
                    "breakout_level": active["breakout_level"],
                    "range_breakout_upper": upper,
                    "range_breakout_lower": lower,
                    "close_outside": True,
                    "event_timestamp": _iso(row["decision_time"]),
                }
            )
    return events


def detect_trend_follow(frame: pd.DataFrame, cfg: PatternDiscoveryConfig) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if frame.empty:
        return events
    event_id = 0
    active: dict[str, Any] | None = None
    for i, row in frame.iterrows():
        direction = str(row.get("regime_proxy_direction") or "unclear")
        compression = _finite(row.get("ema_fast_compression_score"), 0.0)
        expansion = _finite(row.get("ema_fast_expansion_score"), 0.0)
        near20 = abs(_finite(row.get("close_to_ema_20_atr"), np.nan)) <= cfg.near_ema59_atr
        near59 = abs(_finite(row.get("close_to_ema_59_atr"), np.nan)) <= cfg.near_ema59_atr
        fast_align = bool(row.get("ema_bullish_ordered")) or bool(row.get("ema_bearish_ordered"))
        close_to_fast = near20 or near59
        if active is None:
            if direction not in {"up", "down"}:
                continue
            if compression <= cfg.compression_max_for_cross_context or not close_to_fast:
                continue
            event_id += 1
            active = {
                "direction": direction,
                "start_index": int(i),
                "bars_active": 1,
                "reaccel_seen": False,
            }
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="trend_follow_pullback_candidate",
                        direction="bullish" if direction == "up" else "bearish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "parent_trend": direction,
                    "pullback_to_ema59": near59,
                    "pullback_to_ema20": near20,
                    "compression_score": compression,
                    "close_to_fast_ema": close_to_fast,
                    "fast_align": fast_align,
                    "trend_follow_pullback_candidate": True,
                    "event_timestamp": _iso(row["decision_time"]),
                    "lifecycle_stage": "candidate",
                }
            )
            continue

        active["bars_active"] += 1
        if direction != active["direction"] and direction in {"up", "down"}:
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="trend_follow_failed",
                        direction="bullish" if active["direction"] == "up" else "bearish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "parent_trend": active["direction"],
                    "lifecycle_stage": "failed",
                    "trend_follow_failed": True,
                    "event_timestamp": _iso(row["decision_time"]),
                }
            )
            active = None
            continue

        if (
            not active["reaccel_seen"]
            and _finite(row.get("ema_fast_expansion_score"), 0.0) >= cfg.expansion_min_change_3_atr
            and fast_align
        ):
            active["reaccel_seen"] = True
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="trend_follow_reacceleration",
                        direction="bullish" if active["direction"] == "up" else "bearish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "parent_trend": active["direction"],
                    "reacceleration_expansion_score": _finite(
                        row.get("ema_fast_expansion_score"), 0.0
                    ),
                    "ema_ordered": bool(fast_align),
                    "trend_follow_reacceleration": True,
                    "event_timestamp": _iso(row["decision_time"]),
                    "lifecycle_stage": "reacceleration",
                }
            )
        if active["bars_active"] >= cfg.breakout_max_bars:
            event_id += 1
            events.append(
                {
                    **_annotate_event(
                        row,
                        event_type="trend_follow_failed",
                        direction="bullish" if active["direction"] == "up" else "bearish",
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                        event_id=event_id,
                    ),
                    "parent_trend": active["direction"],
                    "lifecycle_stage": "failed",
                    "trend_follow_failed": True,
                    "event_timestamp": _iso(row["decision_time"]),
                }
            )
            active = None
    return events


def compute_timing_features(
    events: list[dict[str, Any]],
    frame: pd.DataFrame,
    all_event_index: object | None,
) -> list[dict[str, Any]]:
    if not events:
        return []
    indexed = [dict(ev) for ev in events]
    indexed.sort(key=lambda ev: (int(ev["bar_index"]), str(ev["event_timestamp"]), str(ev["event_id"])))
    by_kind: dict[str, list[int]] = {}
    for idx, ev in enumerate(indexed):
        by_kind.setdefault(str(ev["event_type"]), []).append(idx)

    ema_idx = [i for i, ev in enumerate(indexed) if str(ev["event_type"]).startswith("ema_")]
    di_idx = [i for i, ev in enumerate(indexed) if str(ev["event_type"]).startswith("di_")]
    adx_idx = [i for i, ev in enumerate(indexed) if str(ev["event_type"]).startswith("adx_")]

    for i, ev in enumerate(indexed):
        bi = int(ev["bar_index"])
        for name, idxs in (
            ("ema_cross", ema_idx),
            ("di_cross", di_idx),
            ("adx_rising_3", adx_idx),
        ):
            ev.setdefault(f"bars_since_{name}", None)
            ev.setdefault(f"bars_to_next_{name}_retro", None)
            if not idxs:
                continue
            prev = [j for j in idxs if int(indexed[j]["bar_index"]) <= bi]
            nxt = [j for j in idxs if int(indexed[j]["bar_index"]) > bi]
            since = bi - int(indexed[prev[-1]]["bar_index"]) if prev else None
            to_next = int(indexed[nxt[0]]["bar_index"]) - bi if nxt else None
            ev[f"bars_since_{name}"] = since
            ev[f"bars_to_next_{name}_retro"] = to_next

        # Cross timing buckets.
        di_cross_bar = None
        ema_cross_bar = None
        if str(ev["event_type"]).startswith("di_"):
            prev_ema = [j for j in ema_idx if int(indexed[j]["bar_index"]) <= bi]
            next_ema = [j for j in ema_idx if int(indexed[j]["bar_index"]) >= bi]
            if prev_ema:
                ema_cross_bar = int(indexed[prev_ema[-1]]["bar_index"])
            elif next_ema:
                ema_cross_bar = int(indexed[next_ema[0]]["bar_index"])
            di_cross_bar = bi
            delta = None if ema_cross_bar is None else di_cross_bar - ema_cross_bar
            ev["timing_bucket_di_vs_ema"] = _safe_bucket(delta)
        elif str(ev["event_type"]).startswith("ema_"):
            prev_di = [j for j in di_idx if int(indexed[j]["bar_index"]) <= bi]
            next_di = [j for j in di_idx if int(indexed[j]["bar_index"]) >= bi]
            if prev_di:
                di_cross_bar = int(indexed[prev_di[-1]]["bar_index"])
            elif next_di:
                di_cross_bar = int(indexed[next_di[0]]["bar_index"])
            ema_cross_bar = bi
            delta = None if di_cross_bar is None else di_cross_bar - ema_cross_bar
            ev["timing_bucket_di_vs_ema"] = _safe_bucket(delta)
        else:
            ev["timing_bucket_di_vs_ema"] = "none"

        if str(ev["event_type"]).startswith("adx_"):
            prev_ema = [j for j in ema_idx if int(indexed[j]["bar_index"]) <= bi]
            next_ema = [j for j in ema_idx if int(indexed[j]["bar_index"]) >= bi]
            ema_cross_bar = int(indexed[prev_ema[-1]]["bar_index"]) if prev_ema else (
                int(indexed[next_ema[0]]["bar_index"]) if next_ema else None
            )
            delta = None if ema_cross_bar is None else bi - ema_cross_bar
            ev["timing_bucket_adx_vs_ema"] = _safe_bucket(delta)
        else:
            ev["timing_bucket_adx_vs_ema"] = "none"

        ev["has_as_of_timing"] = True
        ev["has_retrospective_timing"] = any(k.endswith("_retro") for k in ev)

    return indexed


def extract_event_windows(
    events: list[dict[str, Any]],
    frame: pd.DataFrame,
    pre_bars: int,
    post_bars: int,
) -> pd.DataFrame:
    if not events or frame.empty:
        return pd.DataFrame()
    frame = frame.reset_index(drop=True)
    numeric = frame.select_dtypes(include=[np.number]).columns
    out_rows: list[dict[str, Any]] = []
    for ev in events:
        idx = int(ev["bar_index"])
        start = max(0, idx - pre_bars)
        end = min(len(frame) - 1, idx + post_bars)
        for bar in range(start, end + 1):
            row = frame.iloc[bar].to_dict()
            row.update(
                {
                    "event_id": ev["event_id"],
                    "event_type": ev["event_type"],
                    "event_timestamp": ev["event_timestamp"],
                    "event_bar_index": idx,
                    "relative_bar": bar - idx,
                    "symbol": ev["symbol"],
                    "timeframe": ev["timeframe"],
                    "is_retrospective": bool(ev.get("is_retrospective", False)),
                }
            )
            for col in numeric:
                row[col] = _finite(row.get(col), np.nan)
            out_rows.append(row)
    windows = pd.DataFrame(out_rows)
    if not windows.empty and "relative_bar" in windows.columns:
        windows = windows.sort_values(["event_id", "relative_bar"]).reset_index(drop=True)
    return windows


def compute_event_outcomes(
    events: list[dict[str, Any]],
    frame: pd.DataFrame,
    horizons: tuple[int, ...],
    cfg: PatternDiscoveryConfig | None = None,
) -> list[dict[str, Any]]:
    cfg = cfg or PatternDiscoveryConfig()
    if not events:
        return []
    arrays = build_price_arrays(frame)
    requested = tuple(sorted(set(int(h) for h in horizons) | {int(cfg.delayed_horizon)}))
    enriched: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        side = _direction_side(str(ev.get("direction")))
        bar_i = int(ev["bar_index"])
        ref_close = _finite(ev.get("close"), np.nan)
        row["primary_outcome_horizon"] = int(cfg.delayed_horizon)
        if side is None or not math.isfinite(ref_close):
            row["outcome_class"] = "insufficient_horizon"
            enriched.append(row)
            continue
        outcome = compute_horizon_outcome(
            bar_index=bar_i,
            horizon=int(cfg.delayed_horizon),
            reference_close=ref_close,
            side=side,
            arrays=arrays,
        )
        for h in requested:
            h_out = compute_horizon_outcome(
                bar_index=bar_i,
                horizon=int(h),
                reference_close=ref_close,
                side=side,
                arrays=arrays,
            )
            prefix = f"h{h}_"
            for key, value in h_out.items():
                if key == "horizon":
                    continue
                row[f"{prefix}{key}"] = value

        if not outcome["evaluable"]:
            row["outcome_class"] = "insufficient_horizon"
        else:
            mfe = _finite(outcome.get("mfe_pct"), np.nan)
            mae = _finite(outcome.get("mae_pct"), np.nan)
            hit = bool(outcome.get("direction_hit"))
            bars_to_pos = outcome.get("bars_to_first_positive_directional")
            if mfe >= cfg.clean_mfe_min and mae <= cfg.clean_mae_max and hit:
                if bars_to_pos is not None and int(bars_to_pos) <= cfg.reentry_bars:
                    row["outcome_class"] = "clean_success"
                else:
                    row["outcome_class"] = "delayed_success"
            elif hit and mfe >= cfg.clean_mfe_min:
                row["outcome_class"] = "delayed_success"
            elif (not hit) and mfe <= cfg.fail_mfe_max:
                row["outcome_class"] = "failed_no_followthrough"
            elif mae >= cfg.adverse_mae_min:
                row["outcome_class"] = "adverse_reversal"
            else:
                row["outcome_class"] = "weak_followthrough"
        enriched.append(row)
    return enriched


def assign_pattern_families(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        et = str(row.get("event_type") or "")
        comp = bool(row.get("cross_from_compression") or row.get("lifecycle_stage") == "candidate")
        in_range = bool(row.get("cross_in_range") or row.get("regime_proxy") == "range")
        near59 = bool(row.get("cross_near_ema59") or abs(_finite(row.get("close_to_ema_59_atr"), np.inf)) <= PatternDiscoveryConfig().near_ema59_atr)
        near200 = bool(row.get("cross_near_ema200") or abs(_finite(row.get("close_to_ema_200_atr"), np.inf)) <= PatternDiscoveryConfig().near_ema200_atr)
        direction = str(row.get("direction") or "unclear")
        di_bucket = str(row.get("timing_bucket_di_vs_ema") or "none")
        adx_bucket = str(row.get("timing_bucket_adx_vs_ema") or "none")
        if et == "ema_cross":
            if comp and in_range:
                family = "ema_cross_compression_range"
            elif near59 or near200:
                family = "ema_cross_ema59_200_context"
            elif comp:
                family = "ema_cross_compression"
            else:
                family = "ema_cross_basic"
        elif et == "ema_expansion_start":
            family = "ema_expansion_aligned"
        elif et == "di_cross":
            if di_bucket in {"lead"}:
                family = "di_cross_lead_ema"
            elif di_bucket in {"lag_1_3", "lag_4_6"}:
                family = "di_cross_lag_ema"
            else:
                family = "di_cross_basic"
        elif et.startswith("adx_"):
            if et == "adx_rising_3":
                family = "adx_rise_after_ema"
            elif et == "adx_accelerating_now":
                family = "adx_accelerating_cross"
            elif et == "adx_rollover_retro":
                family = "adx_rollover_after_expansion"
            else:
                family = "adx_sequence"
        elif et == "range_breakout_attempt" or et == "range_breakout_confirmed":
            if comp:
                family = "breakout_compression_cross_expansion"
            else:
                family = "range_breakout_lifecycle"
        elif et.startswith("trend_follow_"):
            if near59 or near200 or comp:
                family = "trend_follow_pullback_reexpand"
            else:
                family = "trend_follow_lifecycle"
        else:
            family = "misc"

        row["pattern_family"] = family
        row["pattern_id"] = f"{family}:{direction}"
        row["component_flags"] = {
            "compression": comp,
            "range": in_range,
            "near_ema59": near59,
            "near_ema200": near200,
            "di_bucket": di_bucket,
            "adx_bucket": adx_bucket,
        }
        out.append(row)
    return out


def split_discovery_validation(
    events: list[dict[str, Any]],
    discovery_end: str | None,
) -> dict[str, list[dict[str, Any]]]:
    if discovery_end is None:
        return {"discovery": list(events), "validation": []}
    cutoff = _ts(discovery_end)
    discovery = [dict(ev) for ev in events if _ts(ev["event_timestamp"]) <= cutoff]
    validation = [dict(ev) for ev in events if _ts(ev["event_timestamp"]) > cutoff]
    return {"discovery": discovery, "validation": validation}


def _aggregate_one_split(
    events: list[dict[str, Any]],
    *,
    min_pattern_events: int,
    split_name: str,
) -> list[dict[str, Any]]:
    if not events:
        return []
    rows: list[dict[str, Any]] = []
    by_pattern: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        by_pattern.setdefault(str(ev.get("pattern_id") or "misc"), []).append(ev)
    for pattern_id, items in sorted(by_pattern.items()):
        if len(items) < min_pattern_events:
            rows.append(
                {
                    "split": split_name,
                    "pattern_id": pattern_id,
                    "pattern_family": str(items[0].get("pattern_family") or "misc"),
                    "status": "rejected_small_sample",
                    "n_events": len(items),
                    "clean_success_rate": None,
                    "delayed_success_rate": None,
                    "weak_followthrough_rate": None,
                    "failed_no_followthrough_rate": None,
                    "adverse_reversal_rate": None,
                    "mean_mfe": None,
                    "mean_mae": None,
                    "median_mfe": None,
                    "median_mae": None,
                    "mean_directional_return": None,
                }
            )
            continue
        evaluable = [ev for ev in items if ev.get("outcome_class") != "insufficient_horizon"]
        mfes = [float(ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_mfe_pct")) for ev in evaluable if ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_mfe_pct") is not None]
        maes = [float(ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_mae_pct")) for ev in evaluable if ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_mae_pct") is not None]
        drets = [
            float(ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_directional_close_return_pct"))
            for ev in evaluable
            if ev.get(f"h{PatternDiscoveryConfig().delayed_horizon}_directional_close_return_pct") is not None
        ]
        counts = {
            key: sum(1 for ev in items if ev.get("outcome_class") == key)
            for key in (
                "clean_success",
                "delayed_success",
                "weak_followthrough",
                "failed_no_followthrough",
                "adverse_reversal",
                "insufficient_horizon",
            )
        }
        rows.append(
            {
                "split": split_name,
                "pattern_id": pattern_id,
                "pattern_family": str(items[0].get("pattern_family") or "misc"),
                "status": "research_candidate",
                "n_events": len(items),
                "clean_success_rate": counts["clean_success"] / len(items),
                "delayed_success_rate": counts["delayed_success"] / len(items),
                "weak_followthrough_rate": counts["weak_followthrough"] / len(items),
                "failed_no_followthrough_rate": counts["failed_no_followthrough"] / len(items),
                "adverse_reversal_rate": counts["adverse_reversal"] / len(items),
                "mean_mfe": float(statistics.mean(mfes)) if mfes else None,
                "mean_mae": float(statistics.mean(maes)) if maes else None,
                "median_mfe": float(statistics.median(mfes)) if mfes else None,
                "median_mae": float(statistics.median(maes)) if maes else None,
                "mean_directional_return": float(statistics.mean(drets)) if drets else None,
            }
        )
    return rows


def aggregate_pattern_metrics(
    events: list[dict[str, Any]],
    min_pattern_events: int,
) -> dict[str, list[dict[str, Any]]]:
    discovery = [ev for ev in events if str(ev.get("split") or "") == "discovery"]
    validation = [ev for ev in events if str(ev.get("split") or "") == "validation"]
    return {
        "discovery": _aggregate_one_split(
            discovery, min_pattern_events=min_pattern_events, split_name="discovery"
        ),
        "validation": _aggregate_one_split(
            validation, min_pattern_events=min_pattern_events, split_name="validation"
        ),
    }


def build_candidate_patterns(
    discovery_metrics: list[dict[str, Any]],
    validation_metrics: list[dict[str, Any]],
    min_n: int,
) -> list[dict[str, Any]]:
    validation_by_id = {str(row["pattern_id"]): row for row in validation_metrics}
    candidates: list[dict[str, Any]] = []
    for row in sorted(
        discovery_metrics,
        key=lambda r: (
            -float(r.get("clean_success_rate") or 0.0),
            -int(r.get("n_events") or 0),
            str(r.get("pattern_id") or ""),
        ),
    ):
        n = int(row.get("n_events") or 0)
        cand = {
            "pattern_id": row["pattern_id"],
            "pattern_family": row.get("pattern_family"),
            "discovery_metrics": row,
            "validation_metrics": validation_by_id.get(str(row["pattern_id"])),
        }
        if n < min_n:
            cand["status"] = "rejected_small_sample"
        else:
            val = cand["validation_metrics"]
            if val is None:
                cand["status"] = "rejected_unstable"
            else:
                d_rate = float(row.get("clean_success_rate") or 0.0)
                v_rate = float(val.get("clean_success_rate") or 0.0)
                d_mfe = float(row.get("mean_mfe") or 0.0)
                v_mfe = float(val.get("mean_mfe") or 0.0)
                if abs(d_rate - v_rate) > 0.25 or abs(d_mfe - v_mfe) > 0.5:
                    cand["status"] = "rejected_unstable"
                else:
                    cand["status"] = "research_candidate"
        candidates.append(cand)
    return candidates


def run_ablation_on_candidates(
    events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    top_n: int = 8,
) -> list[dict[str, Any]]:
    if not events or not candidates:
        return []
    success_classes = {"clean_success", "delayed_success"}
    rows: list[dict[str, Any]] = []
    scored_candidates = candidates[:top_n]
    components = [
        "cross_from_compression",
        "cross_in_range",
        "cross_near_ema59",
        "cross_near_ema200",
        "aligned_slopes",
        "breakout_confirmed",
        "reacceleration_seen",
        "adx_rising_3",
        "adx_accelerating_now",
        "di_cross",
    ]
    for cand in scored_candidates:
        pid = str(cand["pattern_id"])
        subset = [ev for ev in events if str(ev.get("pattern_id")) == pid]
        if not subset:
            continue
        success = [ev for ev in subset if ev.get("outcome_class") in success_classes]
        failure = [ev for ev in subset if ev.get("outcome_class") not in success_classes]
        row = {
            "pattern_id": pid,
            "pattern_family": cand.get("pattern_family"),
            "status": cand.get("status"),
            "n_events": len(subset),
            "n_success": len(success),
            "n_failure": len(failure),
        }
        for comp in components:
            row[f"success_rate_{comp}"] = (
                float(sum(1 for ev in success if _boolish(ev.get(comp))) / len(success))
                if success
                else None
            )
            row[f"failure_rate_{comp}"] = (
                float(sum(1 for ev in failure if _boolish(ev.get(comp))) / len(failure))
                if failure
                else None
            )
        rows.append(row)
    return rows


def sensitivity_check(
    events: list[dict[str, Any]],
    cfg: PatternDiscoveryConfig,
    scales: tuple[float, float] = (0.9, 1.1),
) -> list[dict[str, Any]]:
    if not events:
        return []
    rows: list[dict[str, Any]] = []
    expansions = [ev for ev in events if str(ev.get("event_type")) == "ema_expansion_start"]
    evaluable = [ev for ev in events if ev.get("outcome_class") != "insufficient_horizon"]
    base_success = sum(1 for ev in evaluable if ev.get("outcome_class") in {"clean_success", "delayed_success"})
    base_rate = base_success / len(evaluable) if evaluable else None
    for scale in scales:
        scaled_exp = cfg.expansion_min_change_3_atr * scale
        scaled_clean = cfg.clean_mfe_min * scale
        exp_count = sum(
            1
            for ev in expansions
            if abs(_finite(ev.get("expansion_change_3_atr"), 0.0)) >= scaled_exp
        )
        success_count = 0
        total = 0
        for ev in evaluable:
            total += 1
            h = int(cfg.delayed_horizon)
            mfe = _finite(ev.get(f"h{h}_mfe_pct"), np.nan)
            mae = _finite(ev.get(f"h{h}_mae_pct"), np.nan)
            hit = bool(ev.get(f"h{h}_direction_hit"))
            if math.isfinite(mfe) and math.isfinite(mae) and hit and mfe >= scaled_clean and mae <= cfg.clean_mae_max:
                success_count += 1
        rows.append(
            {
                "scale": scale,
                "expansion_threshold": scaled_exp,
                "clean_mfe_threshold": scaled_clean,
                "expansion_event_count": exp_count,
                "evaluated_events": total,
                "success_rate": success_count / total if total else None,
                "base_success_rate": base_rate,
            }
        )
    return rows


def events_content_hash(events: Sequence[Mapping[str, Any]]) -> str:
    if not events:
        return hashlib.sha256(b"empty").hexdigest()
    frame = pd.DataFrame(list(events)).copy()
    for col in ("event_timestamp", "timestamp", "decision_time"):
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], utc=True).astype(str)
    cols = sorted(frame.columns)
    frame = frame.loc[:, cols].sort_values(
        [c for c in ("symbol", "timeframe", "event_timestamp", "event_id") if c in frame.columns]
    )
    return hashlib.sha256(frame.to_csv(index=False).encode("utf-8")).hexdigest()


def indicator_quantiles(frame: pd.DataFrame, columns: Sequence[str] | None = None) -> dict[str, Any]:
    cols = list(columns or (
        "ema_9_20_spread_atr",
        "ema_9_20_spread_change_3_atr",
        "ema_fast_compression_score",
        "ema_fast_expansion_score",
        "di_spread",
        "adx_14",
        "range_width_atr",
        "close_to_ema_59_atr",
        "close_to_ema_200_atr",
    ))
    out: dict[str, Any] = {}
    for col in cols:
        if col not in frame.columns:
            continue
        ser = pd.to_numeric(frame[col], errors="coerce").dropna()
        if ser.empty:
            out[col] = {"p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
        else:
            out[col] = {
                "p10": float(ser.quantile(0.10)),
                "p25": float(ser.quantile(0.25)),
                "p50": float(ser.quantile(0.50)),
                "p75": float(ser.quantile(0.75)),
                "p90": float(ser.quantile(0.90)),
            }
    return out


def _limit_labels(items: Sequence[Mapping[str, Any]], max_count: int) -> list[dict[str, Any]]:
    rows = [dict(item) for item in items]
    if len(rows) <= max_count:
        return rows
    if max_count <= 0:
        return []
    step = max(1, len(rows) // max_count)
    out = [rows[i] for i in range(0, len(rows), step)][:max_count]
    if out and out[-1] != rows[-1]:
        out[-1] = rows[-1]
    return out


def _pine_event_script(
    *,
    title: str,
    symbol: str,
    timeframe: str,
    analyze_start: str,
    analyze_end: str,
    markers: Sequence[Mapping[str, Any]],
) -> str:
    markers = _limit_labels(markers, 120)
    lines = [
        *build_pine_header(title),
        f"// Research-only marker review | {symbol} {timeframe}",
        f"// Analyze: {analyze_start} .. {analyze_end}",
        'showMarkers = input.bool(true, "Show markers")',
        "",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "var int[] mTimes = array.new_int()",
        "var string[] mLabels = array.new_string()",
        "var color[] mColors = array.new_color()",
        "",
    ]
    for idx, mk in enumerate(markers):
        ts = _ts(mk["event_timestamp"])
        label = str(mk.get("label") or mk.get("event_type") or "evt")
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        colour = "color.gray"
        if mk.get("kind") == "success":
            colour = "color.green"
        elif mk.get("kind") == "failure":
            colour = "color.red"
        elif mk.get("kind") == "candidate":
            colour = "color.orange"
        lines.extend(
            [
                f"if barstate.isfirst and {idx} == {idx}",
                f"    array.push(mTimes, f_ts({ts.year}, {ts.month}, {ts.day}, {ts.hour}, {ts.minute}))",
                f'    array.push(mLabels, "{escaped}")',
                f"    array.push(mColors, {colour})",
                "",
            ]
        )
    lines.extend(
        [
            "if showMarkers and array.size(mTimes) > 0",
            "    for i = 0 to array.size(mTimes) - 1",
            "        if time_close == array.get(mTimes, i)",
            "            label.new(bar_index, high, array.get(mLabels, i), style=label.style_label_down, color=array.get(mColors, i), textcolor=color.white, size=size.tiny)",
            "",
            "// EOF",
        ]
    )
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    return text


def _pine_dmi_script(
    *,
    title: str,
    symbol: str,
    timeframe: str,
    analyze_start: str,
    analyze_end: str,
    markers: Sequence[Mapping[str, Any]],
) -> str:
    markers = _limit_labels(markers, 80)
    lines = [
        *build_pine_header(title),
        f"// DMI/ADX review | {symbol} {timeframe}",
        f"// Analyze: {analyze_start} .. {analyze_end}",
        'showMarkers = input.bool(true, "Show markers")',
        'showDmi = input.bool(true, "Plot DMI/ADX")',
        "",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "var int[] mTimes = array.new_int()",
        "var string[] mLabels = array.new_string()",
        "var color[] mColors = array.new_color()",
        "",
    ]
    for idx, mk in enumerate(markers):
        ts = _ts(mk["event_timestamp"])
        label = str(mk.get("label") or mk.get("event_type") or "evt")
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        colour = "color.gray"
        if mk.get("kind") == "success":
            colour = "color.green"
        elif mk.get("kind") == "failure":
            colour = "color.red"
        elif mk.get("kind") == "candidate":
            colour = "color.orange"
        lines.extend(
            [
                f"if barstate.isfirst and {idx} == {idx}",
                f"    array.push(mTimes, f_ts({ts.year}, {ts.month}, {ts.day}, {ts.hour}, {ts.minute}))",
                f'    array.push(mLabels, "{escaped}")',
                f"    array.push(mColors, {colour})",
                "",
            ]
        )
    lines.extend(
        [
            "[diPlus, diMinus, adx14] = ta.dmi(14, 14)",
            'plot(showDmi ? adx14 : na, title="ADX 14", color=color.new(color.yellow, 0), linewidth=2)',
            'plot(showDmi ? diPlus : na, title="+DI 14", color=color.new(color.green, 0), linewidth=1)',
            'plot(showDmi ? diMinus : na, title="-DI 14", color=color.new(color.red, 0), linewidth=1)',
            'hline(20, "ADX 20", color=color.gray)',
            'hline(25, "ADX 25", color=color.gray)',
            "",
            "if showMarkers and array.size(mTimes) > 0",
            "    for i = 0 to array.size(mTimes) - 1",
            "        if time_close == array.get(mTimes, i)",
            "            label.new(bar_index, adx14, array.get(mLabels, i), style=label.style_label_down, color=array.get(mColors, i), textcolor=color.white, size=size.tiny)",
            "",
            "// EOF",
        ]
    )
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    return text


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def run_audit(
    *,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    load_start: str = "2026-02-01",
    load_end: str = "2026-03-15",
    analyze_start: str = "2026-03-01",
    analyze_end: str = "2026-03-12",
    discovery_end: str | None = "2026-03-07",
    pre_bars: int = 12,
    post_bars: int = 48,
    horizons: tuple[int, ...] = (3, 6, 12, 24, 48, 96),
    min_pattern_events: int = 20,
    output_dir: Path = DEFAULT_OUT,
    cache_dir: Path | None = None,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = PatternDiscoveryConfig(
        pre_bars=pre_bars,
        post_bars=post_bars,
        horizons=tuple(int(h) for h in horizons),
        min_pattern_events=min_pattern_events,
        discovery_end=discovery_end,
    )
    t0 = time.perf_counter()
    frame = build_discovery_frame(
        symbol=symbol,
        timeframe=timeframe,
        load_start=load_start,
        load_end=load_end,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        cache_dir=cache_dir or (output_dir / ".cache" / "indicator_features"),
    )
    t_load = time.perf_counter()
    if frame.empty:
        raise RuntimeError("no discovery rows available for requested window")

    # Detect once.
    ema_crosses = detect_ema_crosses(frame)
    ema_expansions = detect_ema_expansions(frame)
    di_crosses = detect_di_crosses(frame)
    adx_events = detect_adx_dynamics(frame, cfg)
    breakouts = detect_range_breakouts(frame, cfg)
    trend_follows = detect_trend_follow(frame, cfg)
    t_detect = time.perf_counter()

    all_events = assign_pattern_families(
        [
            *ema_crosses,
            *ema_expansions,
            *di_crosses,
            *adx_events,
            *breakouts,
            *trend_follows,
        ]
    )
    # Keep detection causal on full load frame, but report only analyze-window events.
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)
    all_events = [
        ev
        for ev in all_events
        if a0 <= _ts(ev.get("event_timestamp") or ev.get("decision_time")) <= a1
    ]
    all_events = compute_timing_features(all_events, frame, None)
    all_events = compute_event_outcomes(all_events, frame, cfg.horizons, cfg)
    all_events = assign_pattern_families(all_events)
    t_outcomes = time.perf_counter()

    split = split_discovery_validation(all_events, cfg.discovery_end)
    for ev in split["discovery"]:
        ev["split"] = "discovery"
    for ev in split["validation"]:
        ev["split"] = "validation"
    combined = [*split["discovery"], *split["validation"]]
    metrics = aggregate_pattern_metrics(combined, cfg.min_pattern_events)
    candidates = build_candidate_patterns(
        metrics["discovery"], metrics["validation"], min_n=cfg.min_pattern_events
    )
    ablation = run_ablation_on_candidates(combined, [c for c in candidates if c["status"] == "research_candidate"])
    sensitivity = sensitivity_check(combined, cfg)
    t_metrics = time.perf_counter()

    discovery_windows = extract_event_windows(split["discovery"], frame, cfg.pre_bars, cfg.post_bars)
    validation_windows = extract_event_windows(split["validation"], frame, cfg.pre_bars, cfg.post_bars)

    discovery_events_csv = split["discovery"]
    validation_events_csv = split["validation"]

    candidate_markers = [
        {
            "event_timestamp": ev["event_timestamp"],
            "label": f"{ev['pattern_id']}|{ev['outcome_class']}",
            "kind": "candidate"
            if ev["outcome_class"] not in {"clean_success", "delayed_success"}
            else "success",
            "event_type": ev["event_type"],
        }
        for ev in combined
        if ev["pattern_id"] in {c["pattern_id"] for c in candidates[:30]}
    ]
    failure_markers = [
        {
            "event_timestamp": ev["event_timestamp"],
            "label": f"{ev['pattern_id']}|{ev['outcome_class']}",
            "kind": "failure",
            "event_type": ev["event_type"],
        }
        for ev in combined
        if ev.get("outcome_class") in {"failed_no_followthrough", "adverse_reversal"}
    ]
    success_markers = [
        {
            "event_timestamp": ev["event_timestamp"],
            "label": f"{ev['pattern_id']}|{ev['outcome_class']}",
            "kind": "success",
            "event_type": ev["event_type"],
        }
        for ev in combined
        if ev.get("outcome_class") in {"clean_success", "delayed_success"}
    ]
    markers = _limit_labels([*candidate_markers, *success_markers, *failure_markers], 150)
    pine_overlay = _pine_event_script(
        title=f"{symbol} C3.3A Indicator Pattern Discovery",
        symbol=symbol,
        timeframe=timeframe,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        markers=markers,
    )
    pine_dmi = _pine_dmi_script(
        title=f"{symbol} C3.3A Indicator Pattern Discovery DMI",
        symbol=symbol,
        timeframe=timeframe,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        markers=markers,
    )
    overlay_path = output_dir / "indicator_pattern_discovery_events.pine"
    successes_path = output_dir / "indicator_pattern_discovery_successes.pine"
    failures_path = output_dir / "indicator_pattern_discovery_failures.pine"
    candidates_path = output_dir / "indicator_pattern_discovery_candidates.pine"
    dmi_path = output_dir / "indicator_pattern_discovery_dmi.pine"
    overlay_path.write_text(pine_overlay, encoding="utf-8")
    successes_path.write_text(
        _pine_event_script(
            title=f"{symbol} C3.3A Discovery Successes",
            symbol=symbol,
            timeframe=timeframe,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            markers=_limit_labels(success_markers, 120),
        ),
        encoding="utf-8",
    )
    failures_path.write_text(
        _pine_event_script(
            title=f"{symbol} C3.3A Discovery Failures",
            symbol=symbol,
            timeframe=timeframe,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            markers=_limit_labels(failure_markers, 120),
        ),
        encoding="utf-8",
    )
    candidates_path.write_text(
        _pine_event_script(
            title=f"{symbol} C3.3A Discovery Candidates",
            symbol=symbol,
            timeframe=timeframe,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            markers=_limit_labels(candidate_markers, 120),
        ),
        encoding="utf-8",
    )
    dmi_path.write_text(pine_dmi, encoding="utf-8")

    # Combined windows for full analyze set
    all_windows = extract_event_windows(combined, frame, cfg.pre_bars, cfg.post_bars)
    if not discovery_windows.empty:
        discovery_windows.to_csv(output_dir / "discovery_event_windows.csv", index=False)
    if not validation_windows.empty:
        validation_windows.to_csv(output_dir / "validation_event_windows.csv", index=False)
    if not all_windows.empty:
        all_windows.to_csv(output_dir / "event_windows.csv", index=False)
    else:
        (output_dir / "event_windows.csv").write_text("", encoding="utf-8")

    events_df = pd.DataFrame(combined)
    events_df.to_csv(output_dir / "events.csv", index=False)

    # Outcomes-focused export
    outcome_cols = [
        c
        for c in events_df.columns
        if c
        in {
            "event_id",
            "event_type",
            "direction",
            "event_timestamp",
            "bar_index",
            "pattern_id",
            "pattern_family",
            "outcome_class",
            "split",
            "is_retrospective",
        }
        or c.startswith(("raw_return_", "directional_return_", "mfe_", "mae_", "mfe_mae_ratio_"))
        or c.startswith(("h", "bars_since_", "bars_to_next_", "timing_bucket_"))
    ]
    if not events_df.empty:
        events_df.loc[:, [c for c in outcome_cols if c in events_df.columns]].to_csv(
            output_dir / "event_outcomes.csv", index=False
        )
        assign_cols = [
            c
            for c in (
                "event_id",
                "event_type",
                "direction",
                "event_timestamp",
                "pattern_id",
                "pattern_family",
                "ema_sequence_family",
                "ema_context_family",
                "di_sequence_family",
                "adx_sequence_family",
                "combined_family",
                "split",
                "outcome_class",
            )
            if c in events_df.columns
        ]
        events_df.loc[:, assign_cols].to_csv(output_dir / "pattern_assignments.csv", index=False)

    disc_m = pd.DataFrame(metrics["discovery"])
    val_m = pd.DataFrame(metrics["validation"])
    disc_m.to_csv(output_dir / "discovery_metrics.csv", index=False)
    val_m.to_csv(output_dir / "validation_metrics.csv", index=False)
    # pattern_metrics = discovery + validation stacked
    pd.concat(
        [disc_m.assign(split="discovery"), val_m.assign(split="validation")],
        ignore_index=True,
    ).to_csv(output_dir / "pattern_metrics.csv", index=False)

    # discovery vs validation join on pattern_id
    if not disc_m.empty or not val_m.empty:
        d = disc_m.copy()
        v = val_m.copy()
        if "pattern_id" in d.columns and "pattern_id" in v.columns:
            cmp = d.merge(v, on="pattern_id", how="outer", suffixes=("_discovery", "_validation"))
        else:
            cmp = pd.DataFrame()
        cmp.to_csv(output_dir / "discovery_vs_validation.csv", index=False)
    else:
        (output_dir / "discovery_vs_validation.csv").write_text("", encoding="utf-8")

    # timing relationships (as-of buckets only for EMA-cross base events)
    timing_rows: list[dict[str, Any]] = []
    if not events_df.empty and "event_type" in events_df.columns:
        base = events_df[events_df["event_type"].astype(str).str.contains("ema_cross", na=False)].copy()
        if base.empty:
            base = events_df[events_df["event_type"] == "ema_cross"].copy()
        group_cols = [
            c
            for c in (
                "event_type",
                "direction",
                "timing_bucket_di_vs_ema",
                "timing_bucket_adx_vs_ema",
            )
            if c in base.columns
        ]
        if group_cols and not base.empty:
            for keys, g in base.groupby(group_cols, dropna=False):
                if not isinstance(keys, tuple):
                    keys = (keys,)
                row = {col: keys[i] for i, col in enumerate(group_cols)}
                row["event_count"] = int(len(g))
                row["clean_success_rate"] = (
                    float((g["outcome_class"] == "clean_success").mean())
                    if "outcome_class" in g.columns
                    else None
                )
                for h in (12, 24):
                    col = f"directional_return_{h}"
                    if col in g.columns:
                        row[col] = float(pd.to_numeric(g[col], errors="coerce").mean())
                    mfe = f"mfe_{h}"
                    mae = f"mae_{h}"
                    if mfe in g.columns:
                        row[mfe] = float(pd.to_numeric(g[mfe], errors="coerce").mean())
                    if mae in g.columns:
                        row[mae] = float(pd.to_numeric(g[mae], errors="coerce").mean())
                timing_rows.append(row)
    pd.DataFrame(timing_rows).to_csv(output_dir / "timing_relationships.csv", index=False)

    pd.DataFrame(candidates).to_csv(output_dir / "candidate_patterns.csv", index=False)
    (output_dir / "candidate_patterns.json").write_text(
        json.dumps(json_safe(candidates), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pd.DataFrame(ablation).to_csv(output_dir / "ablation_metrics.csv", index=False)
    pd.DataFrame(ablation).to_csv(output_dir / "candidate_ablation.csv", index=False)
    pd.DataFrame(sensitivity).to_csv(output_dir / "sensitivity.csv", index=False)
    pd.DataFrame([indicator_quantiles(frame)]).to_csv(
        output_dir / "indicator_quantiles.csv", index=False
    )

    manual_review_anchors = [
        {
            "anchor_id": "mar3_mar6_range_build",
            "start_time": "2026-03-03T00:00:00+00:00",
            "end_time": "2026-03-06T23:55:00+00:00",
            "note": "Soft review window for the Mar 3-6 range build-up and transition quality.",
        },
        {
            "anchor_id": "mar6_breakout_window",
            "start_time": "2026-03-06T00:00:00+00:00",
            "end_time": "2026-03-07T00:00:00+00:00",
            "note": "Breakout trigger window to inspect the initial post-range break.",
        },
        {
            "anchor_id": "post_mar6_followthrough",
            "start_time": "2026-03-07T00:00:00+00:00",
            "end_time": "2026-03-10T23:55:00+00:00",
            "note": "Review follow-through after the first directional break.",
        },
        {
            "anchor_id": "late_march_validation",
            "start_time": "2026-03-11T00:00:00+00:00",
            "end_time": "2026-03-12T23:55:00+00:00",
            "note": "Optional downstream validation / failure check window.",
        },
    ]

    pd.DataFrame(manual_review_anchors).to_csv(
        output_dir / "manual_review_anchors.csv", index=False
    )

    timing_fields = {
        "as_of": [
            "bars_since_ema_cross",
            "bars_since_di_cross",
            "bars_since_adx_rising_3",
            "timing_bucket_di_vs_ema",
            "timing_bucket_adx_vs_ema",
        ],
        "retrospective": [
            "bars_to_next_ema_cross_retro",
            "bars_to_next_di_cross_retro",
            "bars_to_next_adx_rising_3_retro",
            "adx_local_low_retro",
            "adx_peak_retro",
            "adx_rollover_retro",
        ],
    }
    indicator_quant = indicator_quantiles(frame)

    def _count_type(prefix: str) -> int:
        return int(
            sum(1 for ev in all_events if str(ev.get("event_type", "")).startswith(prefix)
                or str(ev.get("event_type", "")) == prefix)
        )

    type_counts = {
        "ema_crosses": _count_type("ema_cross"),
        "ema_expansions": _count_type("ema_expansion"),
        "di_crosses": _count_type("di_cross"),
        "adx_events": int(
            sum(
                1
                for ev in all_events
                if str(ev.get("event_type", "")).startswith("adx_")
            )
        ),
        "range_breakouts": _count_type("range_breakout"),
        "trend_follow": int(
            sum(
                1
                for ev in all_events
                if "trend_follow" in str(ev.get("event_type", ""))
            )
        ),
        "total": len(all_events),
    }
    performance = {
        "load_s": round(t_load - t0, 4),
        "detect_s": round(t_detect - t_load, 4),
        "outcome_s": round(t_outcomes - t_detect, 4),
        "metrics_s": round(t_metrics - t_outcomes, 4),
        "export_s": None,
        "total_s": round(time.perf_counter() - t0, 4),
        "event_counts": type_counts,
        "raw_detect_counts_pre_analyze_filter": {
            "ema_crosses": len(ema_crosses),
            "ema_expansions": len(ema_expansions),
            "di_crosses": len(di_crosses),
            "adx_events": len(adx_events),
            "range_breakouts": len(breakouts),
            "trend_follow": len(trend_follows),
        },
    }

    summary_core = {
        "phase": "C3_3A_indicator_pattern_discovery",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "discovery_end": cfg.discovery_end,
        "config": cfg.to_dict(),
        "indicator_feature_version": INDICATOR_FEATURE_VERSION,
        "baseline": baseline,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "indicator_quantiles": indicator_quant,
        "event_hash": events_content_hash(combined),
        "event_counts": performance["event_counts"],
        "metrics": metrics,
        "candidates": candidates,
        "ablation": ablation,
        "sensitivity": sensitivity,
        "manual_review_anchors": manual_review_anchors,
        "timing_fields": timing_fields,
        "artifacts": {
            "summary": "summary.json",
            "metadata": "metadata.json",
            "events": "events.csv",
            "event_windows": "event_windows.csv",
            "event_outcomes": "event_outcomes.csv",
            "pattern_assignments": "pattern_assignments.csv",
            "pattern_metrics": "pattern_metrics.csv",
            "discovery_metrics": "discovery_metrics.csv",
            "validation_metrics": "validation_metrics.csv",
            "discovery_vs_validation": "discovery_vs_validation.csv",
            "timing_relationships": "timing_relationships.csv",
            "indicator_quantiles": "indicator_quantiles.csv",
            "ablation_metrics": "ablation_metrics.csv",
            "candidate_patterns_csv": "candidate_patterns.csv",
            "candidate_patterns_json": "candidate_patterns.json",
            "sensitivity": "sensitivity.csv",
            "manual_review_anchors": "manual_review_anchors.csv",
            "discovery_event_windows": "discovery_event_windows.csv",
            "validation_event_windows": "validation_event_windows.csv",
            "events_pine": overlay_path.name,
            "successes_pine": successes_path.name,
            "failures_pine": failures_path.name,
            "candidates_pine": candidates_path.name,
            "dmi_pine": dmi_path.name,
        },
        "safety": {
            "production_unchanged": True,
            "no_regime_gate_changes": True,
            "no_indicator_recalculation": True,
            "baseline_read_only": True,
        },
    }
    summary = {**summary_core, "performance": performance}
    blob = json.dumps(json_safe(summary_core), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    summary["performance"]["export_s"] = round(time.perf_counter() - t_metrics, 4)
    summary["performance"]["total_s"] = round(time.perf_counter() - t0, 4)
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "phase": summary_core["phase"],
        "symbol": symbol,
        "timeframe": timeframe,
        "timing_fields": timing_fields,
        "config_template": PatternDiscoveryConfig().to_dict(),
        "indicator_feature_version": INDICATOR_FEATURE_VERSION,
        "candidate_statuses": [
            "research_candidate",
            "rejected_small_sample",
            "rejected_unstable",
        ],
        "csv_artifacts": [
            "events.csv",
            "discovery_metrics.csv",
            "validation_metrics.csv",
            "candidate_patterns.csv",
            "candidate_ablation.csv",
            "sensitivity.csv",
            "discovery_event_windows.csv",
            "validation_event_windows.csv",
            "summary.json",
            "metadata.json",
        ],
        "pine_artifacts": [overlay_path.name, dmi_path.name],
        "as_of_fields": timing_fields["as_of"],
        "retrospective_fields": timing_fields["retrospective"],
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(metadata), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    readme = f"""# Phase C3.3A - Indicator Pattern Discovery

## Scope
30m-native discovery for `{symbol}` using the C3.2A indicator feature cache. This audit is research-only and does not change production regime gates.

## Timing
- As-of fields are causal and available on the current bar.
- Retrospective markers are suffix-tagged with `_retro` and are only for audit review.

## Artifacts
- `events.csv`
- `discovery_metrics.csv`
- `validation_metrics.csv`
- `candidate_patterns.csv`
- `candidate_ablation.csv`
- `sensitivity.csv`
- `indicator_pattern_discovery_overlay.pine`
- `indicator_pattern_discovery_dmi.pine`
"""
    (output_dir / "README_results.md").write_text(readme, encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C3.3A indicator pattern discovery")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-02-01")
    parser.add_argument("--load-end", default="2026-03-15")
    parser.add_argument("--analyze-start", default="2026-03-01")
    parser.add_argument("--analyze-end", default="2026-03-12")
    parser.add_argument("--discovery-end", default="2026-03-07")
    parser.add_argument("--pre-bars", type=int, default=12)
    parser.add_argument("--post-bars", type=int, default=48)
    parser.add_argument("--horizons", nargs="+", type=int, default=[3, 6, 12, 24, 48, 96])
    parser.add_argument("--min-pattern-events", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR, help="C2 baseline dir"
    )
    args = parser.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        discovery_end=args.discovery_end,
        pre_bars=args.pre_bars,
        post_bars=args.post_bars,
        horizons=tuple(int(h) for h in args.horizons),
        min_pattern_events=args.min_pattern_events,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "n_events": summary["event_counts"]["total"],
                "n_candidates": len(summary["candidates"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
