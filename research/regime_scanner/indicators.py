"""Causal EMA / Wilder indicator suite for the regime scanner.

Notes
-----
* EMA uses ``pandas.Series.ewm(span=period, adjust=False).mean()``.
* Wilder ATR / DM / DI / DX / ADX use ``ewm(alpha=1/period, adjust=False)``.
* Seed behaviour at the start of a series can differ from TA-Lib / TradingView
  (which often seed the first ``period`` bars with an SMA). After sufficient
  warmup the series converge closely.
* All features are causal given the input frame: values at row ``t`` depend only
  on rows ``0..t``. Callers must already exclude look-ahead candles.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config


def _safe_div(numerator: pd.Series | np.ndarray, denominator: pd.Series | np.ndarray) -> pd.Series:
    num = pd.Series(numerator, dtype="float64")
    den = pd.Series(denominator, dtype="float64")
    out = num / den.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def ema(series: pd.Series, period: int) -> pd.Series:
    """EMA with ``adjust=False`` (alpha = 2 / (period + 1))."""
    if period <= 0:
        raise ValueError(f"EMA period must be positive, got {period}")
    values = pd.to_numeric(series, errors="coerce").astype("float64")
    return values.ewm(span=int(period), adjust=False).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).astype("float64"),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder moving average via ``ewm(alpha=1/period, adjust=False)``."""
    if period <= 0:
        raise ValueError(f"Wilder period must be positive, got {period}")
    values = pd.to_numeric(series, errors="coerce").astype("float64")
    return values.ewm(alpha=1.0 / float(period), adjust=False).mean()


def directional_moves(
    high: pd.Series,
    low: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    plus_dm = plus_dm.fillna(0.0)
    minus_dm = minus_dm.fillna(0.0)
    return plus_dm.astype("float64"), minus_dm.astype("float64")


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return wilder_rma(true_range(high, low, close), period)


def atr_percent(atr: pd.Series, close: pd.Series) -> pd.Series:
    return _safe_div(atr, close) * 100.0


def ema_distance_pct(close: pd.Series, ema_values: pd.Series) -> pd.Series:
    return _safe_div(close - ema_values, close) * 100.0


def ema_pair_distance_pct(ema_a: pd.Series, ema_b: pd.Series, close: pd.Series) -> pd.Series:
    return _safe_div(ema_a - ema_b, close) * 100.0


def start_end_slope_pct(series: pd.Series, window: int) -> pd.Series:
    """``(value[t] - value[t-window]) / value[t-window] * 100``."""
    if window <= 0:
        raise ValueError(f"slope window must be positive, got {window}")
    lagged = series.shift(int(window))
    return _safe_div(series - lagged, lagged) * 100.0


def compute_indicator_frame(
    candles: pd.DataFrame,
    config: RegimeScannerConfig | None = None,
) -> pd.DataFrame:
    """Compute the phase-1/2/3 indicator matrix for closed candles only."""
    cfg = config or default_regime_scanner_config()
    if candles.empty:
        return pd.DataFrame()

    missing = [col for col in ("open", "high", "low", "close") if col not in candles.columns]
    if missing:
        raise ValueError(f"candles missing columns required for indicators: {missing}")

    out = candles.copy().reset_index(drop=True)
    high = pd.to_numeric(out["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(out["low"], errors="coerce").astype("float64")
    close = pd.to_numeric(out["close"], errors="coerce").astype("float64")

    for period in cfg.ema_periods:
        out[f"ema_{period}"] = ema(close, period)

    tr = true_range(high, low, close)
    plus_dm, minus_dm = directional_moves(high, low)
    atr = wilder_rma(tr, cfg.atr_period)
    plus_dm_rma = wilder_rma(plus_dm, cfg.adx_period)
    minus_dm_rma = wilder_rma(minus_dm, cfg.adx_period)

    plus_di = _safe_div(plus_dm_rma, atr) * 100.0
    minus_di = _safe_div(minus_dm_rma, atr) * 100.0
    di_sum = plus_di + minus_di
    dx = _safe_div((plus_di - minus_di).abs(), di_sum) * 100.0
    adx = wilder_rma(dx, cfg.adx_period)

    out["true_range"] = tr
    out["plus_dm"] = plus_dm
    out["minus_dm"] = minus_dm
    out["atr"] = atr
    out["atr_pct"] = atr_percent(atr, close)
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["di_spread"] = plus_di - minus_di
    out["dx"] = dx
    out["adx"] = adx

    for period in cfg.ema_periods:
        out[f"close_vs_ema_{period}_pct"] = ema_distance_pct(close, out[f"ema_{period}"])

    ema_periods = list(cfg.ema_periods)
    for i, left in enumerate(ema_periods):
        for right in ema_periods[i + 1 :]:
            out[f"ema_{left}_vs_ema_{right}_pct"] = ema_pair_distance_pct(
                out[f"ema_{left}"],
                out[f"ema_{right}"],
                close,
            )

    for period in cfg.ema_periods:
        for window in cfg.slope_windows:
            out[f"ema_{period}_slope_{window}_pct"] = start_end_slope_pct(
                out[f"ema_{period}"],
                window,
            )

    # Hard-sanitize any residual non-finite values from upstream edge cases.
    numeric_cols = out.select_dtypes(include=[np.number]).columns
    out[numeric_cols] = out[numeric_cols].replace([np.inf, -np.inf], np.nan)
    return out


def latest_indicator_snapshot(
    indicator_frame: pd.DataFrame,
    config: RegimeScannerConfig | None = None,
) -> dict[str, object]:
    """Extract the last-row audit snapshot as plain Python values."""
    cfg = config or default_regime_scanner_config()
    if indicator_frame.empty:
        raise ValueError("indicator frame is empty; cannot build snapshot")

    row = indicator_frame.iloc[-1]
    candles_used = int(len(indicator_frame))

    def _f(name: str) -> float | None:
        if name not in indicator_frame.columns:
            return None
        value = row[name]
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return float(value)

    ema_values = {period: _f(f"ema_{period}") for period in cfg.ema_periods}
    ordered = sorted(
        ((period, value) for period, value in ema_values.items() if value is not None),
        key=lambda item: item[1],
        reverse=True,
    )
    ema_order = " > ".join(f"EMA{period}" for period, _ in ordered) if ordered else None

    slopes: dict[str, float | None] = {}
    for period in cfg.ema_periods:
        for window in cfg.slope_windows:
            key = f"ema_{period}_slope_{window}_pct"
            slopes[key] = _f(key)

    pair_distances: dict[str, float | None] = {}
    periods = list(cfg.ema_periods)
    for i, left in enumerate(periods):
        for right in periods[i + 1 :]:
            key = f"ema_{left}_vs_ema_{right}_pct"
            pair_distances[key] = _f(key)

    close_distances = {
        f"close_vs_ema_{period}_pct": _f(f"close_vs_ema_{period}_pct")
        for period in cfg.ema_periods
    }

    timestamp = row["timestamp"] if "timestamp" in indicator_frame.columns else None
    if isinstance(timestamp, pd.Timestamp):
        last_ts = timestamp.isoformat()
    elif timestamp is None:
        last_ts = None
    else:
        last_ts = str(timestamp)

    return {
        "candles_used": candles_used,
        "warmup_sufficient": candles_used >= cfg.min_warmup_candles,
        "min_warmup_candles": cfg.min_warmup_candles,
        "last_closed_candle": {
            "timestamp": last_ts,
            "open": _f("open"),
            "high": _f("high"),
            "low": _f("low"),
            "close": _f("close"),
            "volume": _f("volume"),
        },
        "ema": {f"ema_{period}": ema_values[period] for period in cfg.ema_periods},
        "ema_order": ema_order,
        "close_vs_ema_pct": close_distances,
        "ema_pair_distance_pct": pair_distances,
        "ema_slopes_pct": slopes,
        "atr": _f("atr"),
        "atr_pct": _f("atr_pct"),
        "plus_di": _f("plus_di"),
        "minus_di": _f("minus_di"),
        "di_spread": _f("di_spread"),
        "dx": _f("dx"),
        "adx": _f("adx"),
        "open_interest": {
            "available": False,
            "note": "OI is optional in version 1 and is not loaded in this phase.",
        },
    }


def required_indicator_columns(config: RegimeScannerConfig | None = None) -> list[str]:
    cfg = config or default_regime_scanner_config()
    cols: list[str] = [
        "true_range",
        "plus_dm",
        "minus_dm",
        "atr",
        "atr_pct",
        "plus_di",
        "minus_di",
        "di_spread",
        "dx",
        "adx",
    ]
    for period in cfg.ema_periods:
        cols.append(f"ema_{period}")
        cols.append(f"close_vs_ema_{period}_pct")
        for window in cfg.slope_windows:
            cols.append(f"ema_{period}_slope_{window}_pct")
    periods = list(cfg.ema_periods)
    for i, left in enumerate(periods):
        for right in periods[i + 1 :]:
            cols.append(f"ema_{left}_vs_ema_{right}_pct")
    return cols
