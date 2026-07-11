"""Descriptive slope, band, overextension and weakening analysis."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .indicators import start_end_slope_pct

SlopeStatus = Literal["strengthening", "weakening", "stable", "unavailable"]
BandStatus = Literal["expanding", "contracting", "stable", "unavailable"]
BandOrientation = Literal["bullish", "bearish", "flat"]
VolLabel = Literal[
    "above_recent_volatility",
    "near_recent_volatility",
    "below_recent_volatility",
    "unavailable",
]
Direction = Literal["up", "down", "flat", "unavailable"]
ChangeDirection = Literal["rising", "falling", "stable", "unavailable"]


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def classify_slope_change(
    current_slope: float | None,
    previous_slope: float | None,
    *,
    epsilon: float,
) -> SlopeStatus:
    """Compare current vs previous non-overlapping slope windows.

    A still-positive slope can be ``weakening`` if it shrank vs the prior window.
    """
    if current_slope is None or previous_slope is None:
        return "unavailable"
    if not np.isfinite(current_slope) or not np.isfinite(previous_slope):
        return "unavailable"
    delta = float(current_slope) - float(previous_slope)
    if abs(delta) <= float(epsilon):
        return "stable"
    if delta > 0:
        return "strengthening"
    return "weakening"


def slope_comparison_for_series(
    series: pd.Series,
    window: int,
    *,
    epsilon: float,
) -> dict[str, Any]:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(series) == 0:
        return {
            "current_slope": None,
            "previous_slope": None,
            "slope_change": None,
            "status": "unavailable",
            "direction": "unavailable",
        }

    current = start_end_slope_pct(series, window)
    lagged = series.shift(window)
    previous = start_end_slope_pct(lagged, window)
    cur = _finite(current.iloc[-1])
    prev = _finite(previous.iloc[-1])
    change = None if cur is None or prev is None else cur - prev
    status = classify_slope_change(cur, prev, epsilon=epsilon)
    if cur is None:
        direction: Direction = "unavailable"
    elif abs(cur) <= float(epsilon):
        direction = "flat"
    elif cur > 0:
        direction = "up"
    else:
        direction = "down"
    return {
        "current_slope": cur,
        "previous_slope": prev,
        "slope_change": change,
        "status": status,
        "direction": direction,
    }


def analyze_ema_slopes(
    frame: pd.DataFrame,
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()
    out: dict[str, Any] = {}
    for period in cfg.ema_periods:
        col = f"ema_{period}"
        if col not in frame.columns:
            continue
        period_map: dict[str, Any] = {}
        for window in cfg.slope_windows:
            period_map[str(window)] = slope_comparison_for_series(
                frame[col],
                window,
                epsilon=cfg.slope_change_epsilon,
            )
        out[str(period)] = period_map
    return out


def analyze_ema_bands(
    frame: pd.DataFrame,
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()
    if frame.empty or "close" not in frame.columns:
        return {}

    close = pd.to_numeric(frame["close"], errors="coerce").astype("float64")
    results: dict[str, Any] = {}
    for fast, slow in cfg.band_pairs:
        fast_col = f"ema_{fast}"
        slow_col = f"ema_{slow}"
        if fast_col not in frame.columns or slow_col not in frame.columns:
            continue
        fast_s = pd.to_numeric(frame[fast_col], errors="coerce").astype("float64")
        slow_s = pd.to_numeric(frame[slow_col], errors="coerce").astype("float64")
        signed = (fast_s - slow_s) / close.replace(0.0, np.nan) * 100.0
        signed = signed.replace([np.inf, -np.inf], np.nan)
        abs_gap = signed.abs()

        current_signed = _finite(signed.iloc[-1])
        if current_signed is None:
            orientation: BandOrientation = "flat"
        elif abs(current_signed) <= cfg.band_orientation_epsilon:
            orientation = "flat"
        elif current_signed > 0:
            orientation = "bullish"
        else:
            orientation = "bearish"

        windows: dict[str, Any] = {}
        for window in cfg.band_windows:
            cur = _finite(abs_gap.iloc[-1])
            if len(abs_gap) <= window:
                prev = None
            else:
                prev = _finite(abs_gap.iloc[-1 - window])
            if cur is None or prev is None:
                status: BandStatus = "unavailable"
                abs_change = None
                rel_change = None
            else:
                abs_change = cur - prev
                rel_change = None if abs(prev) <= cfg.epsilon else (cur - prev) / prev * 100.0
                if abs(abs_change) <= cfg.band_change_epsilon:
                    status = "stable"
                elif abs_change > 0:
                    status = "expanding"
                else:
                    status = "contracting"
            windows[str(window)] = {
                "current_abs_pct": cur,
                "previous_abs_pct": prev,
                "abs_change_pp": abs_change,
                "rel_change_pct": rel_change,
                "status": status,
            }

        pair_key = f"ema_{fast}_vs_ema_{slow}"
        results[pair_key] = {
            "fast": fast,
            "slow": slow,
            "current_signed_pct": current_signed,
            "current_abs_pct": _finite(abs_gap.iloc[-1]),
            "orientation": orientation,
            "windows": windows,
        }
    return results


def analyze_overextension(
    frame: pd.DataFrame,
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()
    if frame.empty:
        return {}
    row = frame.iloc[-1]
    close = _finite(row.get("close"))
    atr = _finite(row.get("atr"))
    atr_pct = _finite(row.get("atr_pct"))

    close_vs_ema_pct: dict[str, float | None] = {}
    close_vs_ema_atr: dict[str, float | None] = {}
    for period in cfg.ema_periods:
        ema_v = _finite(row.get(f"ema_{period}"))
        pct = None
        atr_units = None
        if close is not None and ema_v is not None and abs(close) > cfg.epsilon:
            pct = (close - ema_v) / close * 100.0
        if close is not None and ema_v is not None and atr is not None and abs(atr) > cfg.epsilon:
            atr_units = (close - ema_v) / atr
        close_vs_ema_pct[f"ema_{period}"] = pct
        close_vs_ema_atr[f"ema_{period}"] = atr_units

    ema9 = _finite(row.get("ema_9"))
    ema200 = _finite(row.get("ema_200"))
    ema9_vs_ema200_pct = None
    if close is not None and ema9 is not None and ema200 is not None and abs(close) > cfg.epsilon:
        ema9_vs_ema200_pct = (ema9 - ema200) / close * 100.0

    atr_pct_vs_means: dict[str, Any] = {}
    if "atr_pct" in frame.columns:
        series = pd.to_numeric(frame["atr_pct"], errors="coerce").astype("float64")
        for window in cfg.atr_pct_mean_windows:
            if len(series) < window:
                mean_v = None
                ratio = None
                label: VolLabel = "unavailable"
            else:
                mean_v = _finite(series.iloc[-window:].mean())
                if atr_pct is None or mean_v is None or abs(mean_v) <= cfg.epsilon:
                    ratio = None
                    label = "unavailable"
                else:
                    ratio = atr_pct / mean_v
                    if ratio > cfg.atr_pct_above_ratio:
                        label = "above_recent_volatility"
                    elif ratio < cfg.atr_pct_below_ratio:
                        label = "below_recent_volatility"
                    else:
                        label = "near_recent_volatility"
            atr_pct_vs_means[str(window)] = {
                "atr_pct_mean": mean_v,
                "ratio": ratio,
                "label": label,
            }

    return {
        "close_vs_ema_pct": close_vs_ema_pct,
        "close_vs_ema_atr_units": close_vs_ema_atr,
        "ema9_vs_ema200_pct": ema9_vs_ema200_pct,
        "atr_pct": atr_pct,
        "atr_pct_vs_means": atr_pct_vs_means,
        "ratio_thresholds": {
            "above": cfg.atr_pct_above_ratio,
            "below": cfg.atr_pct_below_ratio,
            "note": (
                "Labels are descriptive only. "
                f"ratio > {cfg.atr_pct_above_ratio} => above_recent_volatility; "
                f"ratio < {cfg.atr_pct_below_ratio} => below_recent_volatility; "
                "else near_recent_volatility."
            ),
        },
    }


def _change_direction(current: float | None, previous: float | None, epsilon: float) -> ChangeDirection:
    if current is None or previous is None:
        return "unavailable"
    delta = current - previous
    if abs(delta) <= epsilon:
        return "stable"
    return "rising" if delta > 0 else "falling"


def collect_weakening_signals(
    frame: pd.DataFrame,
    *,
    slope_analysis: dict[str, Any] | None = None,
    band_analysis: dict[str, Any] | None = None,
    last_bar_changes: dict[str, Any] | None = None,
    config: RegimeScannerConfig | None = None,
) -> list[dict[str, Any]]:
    """Descriptive momentum weakening only. Never labeled as confirmed divergence."""
    cfg = config or default_regime_scanner_config()
    signals: list[dict[str, Any]] = []
    if frame.empty:
        return signals

    for lookback in cfg.weakening_lookbacks:
        if len(frame) <= lookback:
            continue
        for col, name in (
            ("adx", "adx"),
            ("plus_di", "plus_di"),
            ("di_spread", "di_spread"),
        ):
            if col not in frame.columns:
                continue
            cur = _finite(frame.iloc[-1][col])
            prev = _finite(frame.iloc[-1 - lookback][col])
            direction = _change_direction(cur, prev, cfg.slope_change_epsilon)
            if direction == "falling":
                signals.append(
                    {
                        "type": "weakening_signal",
                        "metric": name,
                        "lookback": lookback,
                        "current": cur,
                        "previous": prev,
                        "change": None if cur is None or prev is None else cur - prev,
                        "note": f"{name} lower than {lookback} candles ago (not a confirmed divergence).",
                    }
                )

    slopes = slope_analysis if slope_analysis is not None else analyze_ema_slopes(frame, config=cfg)
    for period in ("9", "20", "59"):
        period_map = slopes.get(period) or {}
        for window in ("3", "6", "12"):
            item = period_map.get(window)
            if not item:
                continue
            if item.get("direction") == "up" and item.get("status") == "weakening":
                signals.append(
                    {
                        "type": "weakening_signal",
                        "metric": f"ema_{period}_slope_{window}",
                        "lookback": int(window),
                        "current": item.get("current_slope"),
                        "previous": item.get("previous_slope"),
                        "change": item.get("slope_change"),
                        "note": (
                            f"EMA{period} slope over {window} still up but weaker than prior window "
                            "(not a confirmed divergence)."
                        ),
                    }
                )

    bands = band_analysis if band_analysis is not None else analyze_ema_bands(frame, config=cfg)
    for pair_key, payload in bands.items():
        if payload.get("orientation") != "bullish":
            continue
        for window, window_payload in (payload.get("windows") or {}).items():
            if window_payload.get("status") == "contracting":
                signals.append(
                    {
                        "type": "weakening_signal",
                        "metric": f"{pair_key}_band",
                        "lookback": int(window),
                        "current": window_payload.get("current_abs_pct"),
                        "previous": window_payload.get("previous_abs_pct"),
                        "change": window_payload.get("abs_change_pp"),
                        "note": (
                            f"Bullish {pair_key} band contracting over {window} candles "
                            "(not a confirmed divergence)."
                        ),
                    }
                )

    signals.extend(
        detect_last_bar_rollovers(
            frame,
            last_bar_changes=last_bar_changes,
            config=cfg,
        )
    )
    return signals


def _series_or_derived(frame: pd.DataFrame, name: str) -> pd.Series | None:
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce").astype("float64")
    if name == "close_vs_ema_9_atr":
        if not {"close", "ema_9", "atr"}.issubset(frame.columns):
            return None
        close = pd.to_numeric(frame["close"], errors="coerce")
        ema9 = pd.to_numeric(frame["ema_9"], errors="coerce")
        atr = pd.to_numeric(frame["atr"], errors="coerce").replace(0.0, np.nan)
        return ((close - ema9) / atr).replace([np.inf, -np.inf], np.nan)
    if name == "close_vs_ema_20_atr":
        if not {"close", "ema_20", "atr"}.issubset(frame.columns):
            return None
        close = pd.to_numeric(frame["close"], errors="coerce")
        ema20 = pd.to_numeric(frame["ema_20"], errors="coerce")
        atr = pd.to_numeric(frame["atr"], errors="coerce").replace(0.0, np.nan)
        return ((close - ema20) / atr).replace([np.inf, -np.inf], np.nan)
    return None


LAST_BAR_METRICS: tuple[tuple[str, str], ...] = (
    ("atr", "atr"),
    ("atr_pct", "atr_pct"),
    ("adx", "adx"),
    ("plus_di", "plus_di"),
    ("minus_di", "minus_di"),
    ("di_spread", "di_spread"),
    ("close", "close"),
    ("ema_9_slope_3_pct", "ema9_slope3"),
    ("ema_9_slope_6_pct", "ema9_slope6"),
    ("close_vs_ema_9_atr", "close_vs_ema9_atr"),
    ("close_vs_ema_20_atr", "close_vs_ema20_atr"),
)


def analyze_last_bar_changes(
    frame: pd.DataFrame,
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Candle-to-candle deltas for the latest closed bar (delta_1/2/3).

    Longer-horizon labels (e.g. ADX over 12) remain separate via
    :func:`classify_series_change`; both can disagree.
    """
    cfg = config or default_regime_scanner_config()
    eps = float(cfg.last_bar_change_epsilon)
    out: dict[str, Any] = {}
    if frame.empty:
        return out

    for source, key in LAST_BAR_METRICS:
        series = _series_or_derived(frame, source)
        payload: dict[str, Any] = {
            "current": None,
            "delta_1": None,
            "delta_2": None,
            "delta_3": None,
            "direction_1": "unavailable",
            "direction_2": "unavailable",
            "direction_3": "unavailable",
            "trend_3": "unavailable",
            "trend_6": "unavailable",
            "trend_12": "unavailable",
        }
        if series is None or series.empty:
            out[key] = payload
            continue
        cur = _finite(series.iloc[-1])
        payload["current"] = cur
        for lag, name in ((1, "delta_1"), (2, "delta_2"), (3, "delta_3")):
            if len(series) <= lag:
                continue
            prev = _finite(series.iloc[-1 - lag])
            if cur is None or prev is None:
                continue
            delta = cur - prev
            payload[name] = delta
            payload[f"direction_{lag}"] = _change_direction(cur, prev, eps)
        # Also expose common short horizons for comparison with last-bar move.
        for lb in (3, 6, 12):
            if len(series) <= lb:
                continue
            prev = _finite(series.iloc[-1 - lb])
            payload[f"trend_{lb}"] = _change_direction(cur, prev, eps)
        out[key] = payload
    return out


def detect_last_bar_rollovers(
    frame: pd.DataFrame,
    *,
    last_bar_changes: dict[str, Any] | None = None,
    config: RegimeScannerConfig | None = None,
) -> list[dict[str, Any]]:
    """Detect last-bar rollovers after a prior short-term rise.

    Always labeled ``weakening_signal`` / ``*_LAST_BAR_ROLLOVER`` — never divergence.
    """
    cfg = config or default_regime_scanner_config()
    eps = float(cfg.last_bar_change_epsilon)
    changes = last_bar_changes if last_bar_changes is not None else analyze_last_bar_changes(frame, config=cfg)
    signals: list[dict[str, Any]] = []
    if frame.empty or len(frame) < 3:
        return signals

    rollover_specs = (
        ("adx", "ADX_LAST_BAR_ROLLOVER"),
        ("plus_di", "PLUS_DI_LAST_BAR_ROLLOVER"),
        ("di_spread", "DI_SPREAD_LAST_BAR_ROLLOVER"),
        ("atr_pct", "ATR_PERCENT_LAST_BAR_ROLLOVER"),
    )
    falling_now: list[str] = []

    for metric, code in rollover_specs:
        series = _series_or_derived(frame, metric if metric != "atr_pct" else "atr_pct")
        if series is None or len(series) < 3:
            continue
        v0 = _finite(series.iloc[-1])
        v1 = _finite(series.iloc[-2])
        v2 = _finite(series.iloc[-3])
        if v0 is None or v1 is None or v2 is None:
            continue
        prior_rise = (v1 - v2) > eps
        last_fall = (v0 - v1) < -eps
        if last_fall:
            falling_now.append(metric)
        if prior_rise and last_fall:
            signals.append(
                {
                    "type": "weakening_signal",
                    "metric": code,
                    "lookback": 1,
                    "current": v0,
                    "previous": v1,
                    "change": v0 - v1,
                    "prior_change": v1 - v2,
                    "trend_12": (changes.get(metric if metric != "plus_di" else "plus_di") or {}).get("trend_12"),
                    "note": (
                        f"{code}: prior short-term rise then last-bar decline "
                        "(not a confirmed divergence)."
                    ),
                }
            )

    if len(falling_now) >= 2:
        signals.append(
            {
                "type": "weakening_signal",
                "metric": "MULTI_METRIC_LAST_BAR_ROLLOVER",
                "lookback": 1,
                "current": None,
                "previous": None,
                "change": None,
                "falling_metrics": falling_now,
                "note": (
                    "MULTI_METRIC_LAST_BAR_ROLLOVER: at least two of ADX/+DI/DI-spread/ATR% "
                    "fell on the last closed candle (not a confirmed divergence)."
                ),
            }
        )
    return signals


def build_last_closed_table(
    frame: pd.DataFrame,
    *,
    candles: int = 12,
    config: RegimeScannerConfig | None = None,
) -> list[dict[str, Any]]:
    """Compact table of the latest closed candles including delta_1 fields."""
    cfg = config or default_regime_scanner_config()
    n = min(int(candles), len(frame))
    if n <= 0:
        return []
    rows: list[dict[str, Any]] = []
    start = len(frame) - n
    for idx in range(start, len(frame)):
        row = frame.iloc[idx]
        ts = row["timestamp"]
        ts_out = ts.isoformat() if isinstance(ts, pd.Timestamp) else str(ts)

        def _delta(col: str) -> float | None:
            if col not in frame.columns or idx == 0:
                return None
            cur = _finite(frame.iloc[idx][col])
            prev = _finite(frame.iloc[idx - 1][col])
            if cur is None or prev is None:
                return None
            return cur - prev

        rows.append(
            {
                "timestamp": ts_out,
                "close": _finite(row.get("close")),
                "high": _finite(row.get("high")),
                "low": _finite(row.get("low")),
                "atr_pct": _finite(row.get("atr_pct")),
                "adx": _finite(row.get("adx")),
                "plus_di": _finite(row.get("plus_di")),
                "minus_di": _finite(row.get("minus_di")),
                "di_spread": _finite(row.get("di_spread")),
                "adx_delta1": _delta("adx"),
                "plus_di_delta1": _delta("plus_di"),
                "di_spread_delta1": _delta("di_spread"),
                "atr_pct_delta1": _delta("atr_pct"),
            }
        )
    _ = cfg
    return rows


def classify_series_change(
    frame: pd.DataFrame,
    column: str,
    lookbacks: tuple[int, ...] = (3, 6, 12),
    *,
    epsilon: float = 0.05,
) -> dict[str, ChangeDirection]:
    out: dict[str, ChangeDirection] = {}
    if frame.empty or column not in frame.columns:
        return {str(lb): "unavailable" for lb in lookbacks}
    current = _finite(frame.iloc[-1][column])
    for lb in lookbacks:
        if len(frame) <= lb:
            out[str(lb)] = "unavailable"
            continue
        previous = _finite(frame.iloc[-1 - lb][column])
        out[str(lb)] = _change_direction(current, previous, epsilon)
    return out


def build_descriptive_summary(
    *,
    ema_order: str | None,
    slope_analysis: dict[str, Any],
    band_analysis: dict[str, Any],
    frame: pd.DataFrame,
    pivots_high_count: int,
    pivots_low_count: int,
    last_two_highs: list[dict[str, Any]],
    last_two_lows: list[dict[str, Any]],
    confirmed_divergences: list[dict[str, Any]],
    weakening_signals: list[dict[str, Any]],
    overextension: dict[str, Any],
    last_bar_changes: dict[str, Any] | None = None,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()

    def _slope(period: str, window: str) -> dict[str, Any]:
        return (slope_analysis.get(period) or {}).get(window) or {
            "direction": "unavailable",
            "status": "unavailable",
            "current_slope": None,
            "previous_slope": None,
        }

    short = _slope("9", "3")
    medium = _slope("20", "12")
    long = _slope("200", "144")

    band_statuses = []
    for key in ("ema_9_vs_ema_20", "ema_20_vs_ema_59", "ema_59_vs_ema_200", "ema_9_vs_ema_200"):
        payload = band_analysis.get(key) or {}
        win12 = ((payload.get("windows") or {}).get("12") or {})
        band_statuses.append(
            {
                "pair": key,
                "orientation": payload.get("orientation"),
                "status_window_12": win12.get("status"),
                "current_abs_pct": payload.get("current_abs_pct"),
            }
        )

    confirmed_only = [
        d
        for d in confirmed_divergences
        if str(d.get("status") or "").startswith("confirmed_")
    ]
    last_bar_weakening = [
        s
        for s in weakening_signals
        if "LAST_BAR_ROLLOVER" in str(s.get("metric") or "")
    ]
    changes = last_bar_changes or {}
    medium_term_lines: list[str] = []
    for metric, label in (
        ("adx", "ADX"),
        ("plus_di", "+DI"),
        ("di_spread", "DI-spread"),
        ("atr_pct", "ATR%"),
    ):
        trend12 = (changes.get(metric) or {}).get("trend_12")
        if trend12 and trend12 != "unavailable":
            medium_term_lines.append(f"{label} remains {trend12} over 12 candles")

    return {
        "ema_orientation": ema_order,
        "short_term_slope_direction": short.get("direction"),
        "short_term_slope_change": short.get("status"),
        "medium_term_slope_direction": medium.get("direction"),
        "long_term_slope_direction": long.get("direction"),
        "ema_bands": band_statuses,
        "adx_change": classify_series_change(
            frame, "adx", cfg.weakening_lookbacks, epsilon=cfg.slope_change_epsilon
        ),
        "di_spread_change": classify_series_change(
            frame, "di_spread", cfg.weakening_lookbacks, epsilon=cfg.slope_change_epsilon
        ),
        "confirmed_pivot_high_count": pivots_high_count,
        "confirmed_pivot_low_count": pivots_low_count,
        "last_two_confirmed_pivot_highs": last_two_highs,
        "last_two_confirmed_pivot_lows": last_two_lows,
        "confirmed_divergences": confirmed_only,
        "weakening_signals": weakening_signals,
        "current_last_bar_weakening": last_bar_weakening,
        "medium_term_trend_notes": medium_term_lines,
        "overextension_atr_units": overextension.get("close_vs_ema_atr_units"),
        "disclaimer": (
            "Descriptive summary only. No trade permission, blocker, or entry recommendation."
        ),
    }
