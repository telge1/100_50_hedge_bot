"""Phase C3.2A indicator feature store (research-only).

Architecture decision
---------------------
* Canonical math stays in ``research.regime_scanner.indicators`` (EMA / Wilder ATR / DMI / ADX).
* This module adds C3.2A-named columns, readiness flags, compression/expansion scores,
  persistence (parquet research cache + in-memory repository), and CLI backfill.
* No MySQL feature table in C3.2A — candles remain in ``market_candles`` / feather;
  dense float matrices follow the existing audit-local ``.cache/`` convention.
* Regime classification (C1–C3.1) is **not** modified; features are supply-only.

Feature version
---------------
``INDICATOR_FEATURE_VERSION = "c3.2a_v1"``

Warm-up
-------
``required_indicator_warmup_bars()`` = max(EMA periods) + max(slope windows for C3.2A)
+ ATR/ADX period + buffer. Bars before readiness stay NaN with ``features_ready=False``.

Slope definition (no look-ahead)
--------------------------------
``ema_20_slope_3 = ema_20[t] - ema_20[t-3]``
``ema_20_slope_3_atr = (ema_20[t] - ema_20[t-3]) / atr_14[t]``

Spread definition
-----------------
``ema_9_20_spread = ema_9 - ema_20`` (signed)
``ema_9_20_spread_atr = spread / atr_14`` (NaN if ATR unavailable)

Incremental updates
-------------------
EMA/ATR/ADX are recursive. Historical candle corrections invalidate from the
earliest changed bar through the series end and force a rebuild of the suffix.
New closed candles append via a suffix rebuild from a warm prefix snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import TIMEFRAME_MINUTES, aggregate_candles

INDICATOR_FEATURE_VERSION = "c3.2a_v1"
C32A_SLOPE_WINDOWS: tuple[int, ...] = (1, 3, 6)
C32A_CROSS_LOOKBACKS: tuple[int, ...] = (12, 24, 48)
EMA_PAIRS: tuple[tuple[int, int], ...] = ((9, 20), (20, 59), (59, 200))


def required_indicator_warmup_bars(config: RegimeScannerConfig | None = None) -> int:
    """Bars needed before EMA200 + ADX + slope_6 are considered ready.

    Uses max EMA (200) + max C3.2A slope (6) + ADX period (14) + small buffer.
    This is intentionally >= ``RegimeScannerConfig.min_warmup_candles`` for the
    C3.2A feature matrix (slope windows 1/3/6 rather than the scanner's longer set).
    """
    cfg = config or default_regime_scanner_config()
    max_ema = max(cfg.ema_periods) if cfg.ema_periods else 200
    max_slope = max(C32A_SLOPE_WINDOWS)
    return int(max_ema + max_slope + max(cfg.atr_period, cfg.adx_period) + 8)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    out = a.astype("float64") / b.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def enrich_c32a_features(
    indicator_frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    config: RegimeScannerConfig | None = None,
    feature_version: str = INDICATOR_FEATURE_VERSION,
) -> pd.DataFrame:
    """Add C3.2A columns on top of ``compute_indicator_frame`` output (additive)."""
    cfg = config or default_regime_scanner_config()
    if indicator_frame.empty:
        return pd.DataFrame()

    out = indicator_frame.copy().reset_index(drop=True)
    close = pd.to_numeric(out["close"], errors="coerce").astype("float64")

    for p in (9, 20, 59, 200):
        src = f"ema_{p}"
        if src not in out.columns:
            raise ValueError(f"missing {src} in indicator frame")
        out[f"ema_{p}"] = pd.to_numeric(out[src], errors="coerce").astype("float64")

    out["atr_14"] = pd.to_numeric(out["atr"], errors="coerce").astype("float64")
    out["adx_14"] = pd.to_numeric(out["adx"], errors="coerce").astype("float64")
    out["plus_di_14"] = pd.to_numeric(out["plus_di"], errors="coerce").astype("float64")
    out["minus_di_14"] = pd.to_numeric(out["minus_di"], errors="coerce").astype("float64")
    atr = out["atr_14"]

    for left, right in EMA_PAIRS:
        spread = out[f"ema_{left}"] - out[f"ema_{right}"]
        out[f"ema_{left}_{right}_spread"] = spread
        out[f"ema_{left}_{right}_spread_pct"] = _safe_div(spread, close) * 100.0
        out[f"ema_{left}_{right}_spread_atr"] = _safe_div(spread, atr)

    out["ema_9_20_abs_spread_atr"] = out["ema_9_20_spread_atr"].abs()

    for period in (9, 20, 59, 200):
        series = out[f"ema_{period}"]
        for w in C32A_SLOPE_WINDOWS:
            slope = series - series.shift(w)
            out[f"ema_{period}_slope_{w}"] = slope
            out[f"ema_{period}_slope_{w}_atr"] = _safe_div(slope, atr)

    base_spread_atr = out["ema_9_20_spread_atr"]
    for w in C32A_SLOPE_WINDOWS:
        out[f"ema_9_20_spread_change_{w}_atr"] = base_spread_atr - base_spread_atr.shift(w)

    out["di_spread"] = out["plus_di_14"] - out["minus_di_14"]
    out["di_spread_abs"] = out["di_spread"].abs()
    out["di_spread_normalized"] = _safe_div(
        out["di_spread"], out["plus_di_14"] + out["minus_di_14"]
    )
    for w in C32A_SLOPE_WINDOWS:
        out[f"adx_slope_{w}"] = out["adx_14"] - out["adx_14"].shift(w)
    out["adx_rising_3"] = (out["adx_slope_3"] > 0).astype("float64")
    out["adx_falling_3"] = (out["adx_slope_3"] < 0).astype("float64")

    eps = float(cfg.epsilon)
    dominant = np.where(
        out["di_spread"] > eps,
        "plus",
        np.where(out["di_spread"] < -eps, "minus", "neutral"),
    )
    out["dominant_di"] = dominant

    out["ema_bullish_ordered"] = (
        (out["ema_9"] > out["ema_20"])
        & (out["ema_20"] > out["ema_59"])
        & (out["ema_59"] > out["ema_200"])
    ).astype("float64")
    out["ema_bearish_ordered"] = (
        (out["ema_9"] < out["ema_20"])
        & (out["ema_20"] < out["ema_59"])
        & (out["ema_59"] < out["ema_200"])
    ).astype("float64")
    out["ema_fast_bullish"] = (out["ema_9"] > out["ema_20"]).astype("float64")
    out["ema_fast_bearish"] = (out["ema_9"] < out["ema_20"]).astype("float64")
    out["price_above_ema_200"] = (close > out["ema_200"]).astype("float64")
    out["price_below_ema_200"] = (close < out["ema_200"]).astype("float64")

    for p in (9, 20, 59, 200):
        out[f"close_to_ema_{p}_atr"] = _safe_div(close - out[f"ema_{p}"], atr)

    signed = out["ema_9_20_spread"]
    cross = (np.sign(signed).fillna(0) != np.sign(signed.shift(1)).fillna(0)) & signed.notna()
    cross_f = cross.astype("float64")
    for lb in C32A_CROSS_LOOKBACKS:
        out[f"ema_fast_cross_count_{lb}"] = cross_f.rolling(lb, min_periods=1).sum()

    abs_spread = out["ema_9_20_abs_spread_atr"]
    slope_mag = (
        out["ema_9_slope_3_atr"].abs().fillna(0) + out["ema_20_slope_3_atr"].abs().fillna(0)
    ) / 2.0
    cross24 = out["ema_fast_cross_count_24"].fillna(0)
    tight = (1.0 - (abs_spread / 0.35).clip(0, 1)).fillna(0)
    flat = (1.0 - (slope_mag / 0.15).clip(0, 1)).fillna(0)
    crosses = (cross24 / 6.0).clip(0, 1)
    out["ema_fast_compression_score"] = (0.45 * tight + 0.35 * flat + 0.20 * crosses).clip(0, 1)
    wide = (abs_spread / 0.80).clip(0, 1).fillna(0)
    directed = (slope_mag / 0.25).clip(0, 1).fillna(0)
    few_cross = (1.0 - (cross24 / 4.0).clip(0, 1)).fillna(0)
    out["ema_fast_expansion_score"] = (0.45 * wide + 0.40 * directed + 0.15 * few_cross).clip(0, 1)

    warmup = required_indicator_warmup_bars(cfg)
    n = len(out)
    idx = np.arange(n)
    ema_ready = (idx + 1) >= max(cfg.ema_periods)
    dmi_ready = (idx + 1) >= (max(cfg.atr_period, cfg.adx_period) * 2)
    full_ready = (idx + 1) >= warmup
    core_ok = (
        out["ema_200"].notna()
        & out["atr_14"].notna()
        & out["adx_14"].notna()
        & out["plus_di_14"].notna()
        & out["minus_di_14"].notna()
    )
    out["ema_features_ready"] = (ema_ready & out["ema_200"].notna()).astype(bool)
    out["dmi_features_ready"] = (dmi_ready & out["adx_14"].notna()).astype(bool)
    out["features_ready"] = (full_ready & core_ok).astype(bool)

    out["symbol"] = str(symbol)
    out["timeframe"] = str(timeframe)
    out["feature_version"] = str(feature_version)
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)

    numeric_cols = out.select_dtypes(include=[np.number]).columns
    out[numeric_cols] = out[numeric_cols].replace([np.inf, -np.inf], np.nan)
    return out


def compute_indicator_features(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    config: RegimeScannerConfig | None = None,
    feature_version: str = INDICATOR_FEATURE_VERSION,
) -> pd.DataFrame:
    """Full pipeline: OHLCV → base indicators → C3.2A enrichment."""
    cfg = (config or default_regime_scanner_config()).with_timeframe(timeframe)
    if candles.empty:
        return pd.DataFrame()
    base = compute_indicator_frame(candles, config=cfg)
    return enrich_c32a_features(
        base,
        symbol=symbol,
        timeframe=timeframe,
        config=cfg,
        feature_version=feature_version,
    )


def features_content_hash(features: pd.DataFrame) -> str:
    """Deterministic hash of feature values (excludes created_at/updated_at)."""
    if features.empty:
        return hashlib.sha256(b"empty").hexdigest()
    cols = [
        c
        for c in features.columns
        if c not in {"created_at", "updated_at", "source_candle_revision"}
    ]
    frame = features.loc[:, cols].copy()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).astype(str)
    blob = frame.sort_values(
        [c for c in ("symbol", "timeframe", "timestamp") if c in frame.columns]
    ).to_csv(index=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class FeatureStoreStats:
    candles_loaded: int = 0
    warmup_candles: int = 0
    features_calculated: int = 0
    features_inserted: int = 0
    features_updated: int = 0
    features_unchanged: int = 0
    features_not_ready: int = 0
    timestamp_gaps: int = 0
    strict_gaps: bool = False
    first_feature_timestamp: str | None = None
    last_feature_timestamp: str | None = None
    first_ready_timestamp: str | None = None
    duration_load_s: float = 0.0
    duration_calculate_s: float = 0.0
    duration_persist_s: float = 0.0
    duration_total_s: float = 0.0
    feature_version: str = INDICATOR_FEATURE_VERSION
    determinism_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IndicatorFeatureRepository(Protocol):
    def upsert(self, features: pd.DataFrame) -> dict[str, int]:
        ...

    def load(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> pd.DataFrame:
        ...

    def delete_from(
        self,
        *,
        symbol: str,
        timeframe: str,
        from_timestamp: pd.Timestamp,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> int:
        ...


class InMemoryIndicatorFeatureRepository:
    """Test / unit repository keyed by (symbol, timeframe, feature_version, timestamp)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def upsert(self, features: pd.DataFrame) -> dict[str, int]:
        inserted = updated = unchanged = 0
        if features.empty:
            return {"inserted": 0, "updated": 0, "unchanged": 0}
        for _, row in features.iterrows():
            key = (
                str(row["symbol"]),
                str(row["timeframe"]),
                str(row["feature_version"]),
                pd.Timestamp(row["timestamp"]).isoformat(),
            )
            payload = row.to_dict()
            if key not in self._rows:
                self._rows[key] = payload
                inserted += 1
            else:
                prev = self._rows[key]
                same = True
                for k, v in payload.items():
                    if k in {"created_at", "updated_at"}:
                        continue
                    pv = prev.get(k)
                    if isinstance(v, float) and isinstance(pv, float):
                        if math.isnan(v) and math.isnan(pv):
                            continue
                        if not math.isclose(v, pv, rel_tol=0, abs_tol=1e-12):
                            same = False
                            break
                    elif v != pv and not (pd.isna(v) and pd.isna(pv)):
                        same = False
                        break
                if same:
                    unchanged += 1
                else:
                    self._rows[key] = payload
                    updated += 1
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def load(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> pd.DataFrame:
        rows = []
        for (sym, tf, ver, _), payload in self._rows.items():
            if sym != symbol or tf != timeframe or ver != feature_version:
                continue
            ts = pd.Timestamp(payload["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            rows.append(payload)
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        return out.sort_values("timestamp").reset_index(drop=True)

    def delete_from(
        self,
        *,
        symbol: str,
        timeframe: str,
        from_timestamp: pd.Timestamp,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> int:
        from_ts = pd.Timestamp(from_timestamp)
        if from_ts.tzinfo is None:
            from_ts = from_ts.tz_localize("UTC")
        else:
            from_ts = from_ts.tz_convert("UTC")
        remove = []
        for key, payload in self._rows.items():
            sym, tf, ver, _ = key
            if sym != symbol or tf != timeframe or ver != feature_version:
                continue
            ts = pd.Timestamp(payload["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts >= from_ts:
                remove.append(key)
        for key in remove:
            del self._rows[key]
        return len(remove)


class ParquetIndicatorFeatureRepository:
    """Versioned research-cache repository (one parquet per symbol/tf/version)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: str, feature_version: str) -> Path:
        safe_ver = feature_version.replace("/", "_")
        return self.root / f"{symbol}_{timeframe}_{safe_ver}.parquet"

    def _meta_path(self, symbol: str, timeframe: str, feature_version: str) -> Path:
        return self._path(symbol, timeframe, feature_version).with_suffix(".meta.json")

    def upsert(self, features: pd.DataFrame) -> dict[str, int]:
        if features.empty:
            return {"inserted": 0, "updated": 0, "unchanged": 0}
        symbol = str(features["symbol"].iloc[0])
        timeframe = str(features["timeframe"].iloc[0])
        version = str(features["feature_version"].iloc[0])
        path = self._path(symbol, timeframe, version)
        existing = self.load(symbol=symbol, timeframe=timeframe, feature_version=version)
        if existing.empty:
            features.to_parquet(path, index=False)
            self._write_meta(symbol, timeframe, version, features)
            return {"inserted": len(features), "updated": 0, "unchanged": 0}

        existing = existing.set_index("timestamp", drop=False)
        incoming = features.set_index("timestamp", drop=False)
        inserted = updated = unchanged = 0
        for ts, row in incoming.iterrows():
            if ts not in existing.index:
                inserted += 1
            else:
                prev = existing.loc[ts]
                same = True
                for col in incoming.columns:
                    if col in {"created_at", "updated_at"}:
                        continue
                    a = row.get(col)
                    b = prev.get(col) if col in existing.columns else None
                    if isinstance(a, float) and isinstance(b, float):
                        if math.isnan(a) and math.isnan(b):
                            continue
                        if not math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12):
                            same = False
                            break
                    elif a != b and not (pd.isna(a) and pd.isna(b)):
                        same = False
                        break
                if same:
                    unchanged += 1
                else:
                    updated += 1
        merged = pd.concat([existing, incoming], axis=0)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = merged.reset_index(drop=True)
        merged.to_parquet(path, index=False)
        self._write_meta(symbol, timeframe, version, merged)
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def _write_meta(
        self, symbol: str, timeframe: str, version: str, features: pd.DataFrame
    ) -> None:
        meta = {
            "symbol": symbol,
            "timeframe": timeframe,
            "feature_version": version,
            "n_rows": len(features),
            "determinism_hash": features_content_hash(features),
            "first_timestamp": str(features["timestamp"].iloc[0]) if len(features) else None,
            "last_timestamp": str(features["timestamp"].iloc[-1]) if len(features) else None,
        }
        self._meta_path(symbol, timeframe, version).write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def load(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> pd.DataFrame:
        path = self._path(symbol, timeframe, feature_version)
        if not path.is_file():
            return pd.DataFrame()
        out = pd.read_parquet(path)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        if start is not None:
            out = out.loc[out["timestamp"] >= start]
        if end is not None:
            out = out.loc[out["timestamp"] <= end]
        return out.sort_values("timestamp").reset_index(drop=True)

    def delete_from(
        self,
        *,
        symbol: str,
        timeframe: str,
        from_timestamp: pd.Timestamp,
        feature_version: str = INDICATOR_FEATURE_VERSION,
    ) -> int:
        existing = self.load(symbol=symbol, timeframe=timeframe, feature_version=feature_version)
        if existing.empty:
            return 0
        from_ts = pd.Timestamp(from_timestamp)
        if from_ts.tzinfo is None:
            from_ts = from_ts.tz_localize("UTC")
        else:
            from_ts = from_ts.tz_convert("UTC")
        keep = existing.loc[existing["timestamp"] < from_ts].reset_index(drop=True)
        removed = len(existing) - len(keep)
        path = self._path(symbol, timeframe, feature_version)
        if keep.empty:
            if path.is_file():
                path.unlink()
            meta = self._meta_path(symbol, timeframe, feature_version)
            if meta.is_file():
                meta.unlink()
        else:
            keep.to_parquet(path, index=False)
            self._write_meta(symbol, timeframe, feature_version, keep)
        return removed


def load_ohlcv_frame(
    symbol: str,
    timeframe: str,
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Load closed OHLCV for symbol/timeframe in [start, end).

    5m comes from feather/mysql; 15m/30m are aggregated from 5m (indicators
    recomputed on the aggregate — never sampled from 5m indicator series).
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")

    raw_5m = load_symbol_candles(symbol)
    raw_5m = raw_5m.copy()
    raw_5m["timestamp"] = pd.to_datetime(raw_5m["timestamp"], utc=True)
    raw_5m = raw_5m.loc[raw_5m["timestamp"] < end_ts].reset_index(drop=True)

    tf = str(timeframe).strip().lower()
    if tf == "5m":
        out = raw_5m.loc[
            (raw_5m["timestamp"] >= start_ts) & (raw_5m["timestamp"] < end_ts)
        ].copy()
        return out.reset_index(drop=True)

    agg = aggregate_candles(raw_5m, tf, decision_time=end_ts)
    if agg.empty:
        return agg
    agg = agg.loc[(agg["timestamp"] >= start_ts) & (agg["timestamp"] < end_ts)].copy()
    return agg.reset_index(drop=True)


def load_ohlcv_with_warmup(
    symbol: str,
    timeframe: str,
    *,
    analyze_start: str | pd.Timestamp,
    analyze_end: str | pd.Timestamp,
    warmup_bars: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (full_frame_with_warmup, analyze_slice) of OHLCV only."""
    tf = str(timeframe).strip().lower()
    minutes = TIMEFRAME_MINUTES[tf]
    warm = int(warmup_bars if warmup_bars is not None else required_indicator_warmup_bars())
    a0 = pd.Timestamp(analyze_start)
    a1 = pd.Timestamp(analyze_end)
    if a0.tzinfo is None:
        a0 = a0.tz_localize("UTC")
    if a1.tzinfo is None:
        a1 = a1.tz_localize("UTC")
    load_start = a0 - pd.Timedelta(minutes=minutes * warm)
    full = load_ohlcv_frame(symbol, tf, start=load_start, end=a1)
    analyze = full.loc[(full["timestamp"] >= a0) & (full["timestamp"] < a1)].copy()
    return full.reset_index(drop=True), analyze.reset_index(drop=True)


def detect_timestamp_gaps(candles: pd.DataFrame, timeframe: str) -> list[dict[str, Any]]:
    if candles.empty or len(candles) < 2:
        return []
    minutes = TIMEFRAME_MINUTES[str(timeframe).strip().lower()]
    expected = pd.Timedelta(minutes=minutes)
    ts = pd.to_datetime(candles["timestamp"], utc=True)
    gaps = []
    for i in range(1, len(ts)):
        delta = ts.iloc[i] - ts.iloc[i - 1]
        if delta != expected:
            gaps.append(
                {
                    "prev": ts.iloc[i - 1].isoformat(),
                    "curr": ts.iloc[i].isoformat(),
                    "delta_minutes": float(delta.total_seconds() / 60.0),
                    "expected_minutes": minutes,
                }
            )
    return gaps


def backfill_indicator_features(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    repository: IndicatorFeatureRepository,
    feature_version: str = INDICATOR_FEATURE_VERSION,
    force_rebuild: bool = False,
    dry_run: bool = False,
    strict_gaps: bool | None = None,
) -> tuple[pd.DataFrame, FeatureStoreStats]:
    """Batch compute + upsert features for ``[start, end)``.

    Gap policy
    ----------
    * ``strict_gaps=True``: abort on any non-uniform bar spacing.
    * ``strict_gaps=False``: record gaps in stats and continue (needed for HTF
      frames where incomplete aggregate buckets are intentionally skipped).
    * Default: strict for ``5m``, non-strict for higher TFs.
    """
    stats = FeatureStoreStats(feature_version=feature_version)
    t0 = time.perf_counter()
    cfg = default_regime_scanner_config().with_timeframe(timeframe)
    warm = required_indicator_warmup_bars(cfg)
    tf = str(timeframe).strip().lower()
    if strict_gaps is None:
        strict_gaps = tf == "5m"

    a0 = pd.Timestamp(start)
    if a0.tzinfo is None:
        a0 = a0.tz_localize("UTC")
    load_start = a0 - pd.Timedelta(minutes=TIMEFRAME_MINUTES[tf] * warm)
    candles = load_ohlcv_frame(symbol, tf, start=load_start, end=end)
    stats.duration_load_s = time.perf_counter() - t0
    stats.candles_loaded = len(candles)
    gaps = detect_timestamp_gaps(candles, tf)
    stats.timestamp_gaps = len(gaps)
    stats.strict_gaps = bool(strict_gaps)
    if gaps and strict_gaps:
        raise ValueError(
            f"candle gaps detected for {symbol} {tf}: {gaps[:3]} "
            f"(total_gaps={len(gaps)})"
        )

    t1 = time.perf_counter()
    features = compute_indicator_features(
        candles,
        symbol=symbol,
        timeframe=tf,
        config=cfg,
        feature_version=feature_version,
    )
    end_ts = pd.Timestamp(end)
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    persist = features.loc[
        (features["timestamp"] >= a0) & (features["timestamp"] < end_ts)
    ].copy()
    stats.duration_calculate_s = time.perf_counter() - t1
    stats.features_calculated = len(persist)
    stats.warmup_candles = int((~persist["features_ready"]).sum()) if len(persist) else 0
    stats.features_not_ready = stats.warmup_candles
    if len(persist):
        stats.first_feature_timestamp = str(persist["timestamp"].iloc[0])
        stats.last_feature_timestamp = str(persist["timestamp"].iloc[-1])
        ready = persist.loc[persist["features_ready"]]
        if len(ready):
            stats.first_ready_timestamp = str(ready["timestamp"].iloc[0])
    stats.determinism_hash = features_content_hash(persist)

    t2 = time.perf_counter()
    if not dry_run:
        if force_rebuild:
            repository.delete_from(
                symbol=symbol,
                timeframe=timeframe,
                from_timestamp=a0,
                feature_version=feature_version,
            )
        result = repository.upsert(persist)
        stats.features_inserted = int(result.get("inserted", 0))
        stats.features_updated = int(result.get("updated", 0))
        stats.features_unchanged = int(result.get("unchanged", 0))
    stats.duration_persist_s = time.perf_counter() - t2
    stats.duration_total_s = time.perf_counter() - t0
    return persist, stats


def update_indicator_features_for_closed_candles(
    *,
    symbol: str,
    timeframe: str,
    closed_candles: pd.DataFrame,
    repository: IndicatorFeatureRepository,
    feature_version: str = INDICATOR_FEATURE_VERSION,
    history_candles: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Incrementally update features for newly closed candles.

    Requires contiguous history (``history_candles`` + ``closed_candles``) with
    no gaps. Rebuilds from the first new timestamp through the end so recursive
    indicators stay consistent with a full batch rebuild.
    """
    if closed_candles.empty:
        return pd.DataFrame()
    cfg = default_regime_scanner_config().with_timeframe(timeframe)
    new = closed_candles.copy()
    new["timestamp"] = pd.to_datetime(new["timestamp"], utc=True)
    first_new = pd.Timestamp(new["timestamp"].iloc[0])

    if history_candles is None or history_candles.empty:
        warm = required_indicator_warmup_bars(cfg)
        minutes = TIMEFRAME_MINUTES[timeframe]
        hist_start = first_new - pd.Timedelta(minutes=minutes * warm)
        history_candles = load_ohlcv_frame(
            symbol, timeframe, start=hist_start, end=first_new
        )

    hist = history_candles.copy()
    hist["timestamp"] = pd.to_datetime(hist["timestamp"], utc=True)
    combined = pd.concat([hist, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    gaps = detect_timestamp_gaps(combined, timeframe)
    if gaps:
        raise ValueError(f"cannot incremental-update across gaps: {gaps[:3]}")

    features = compute_indicator_features(
        combined,
        symbol=symbol,
        timeframe=timeframe,
        config=cfg,
        feature_version=feature_version,
    )
    repository.delete_from(
        symbol=symbol,
        timeframe=timeframe,
        from_timestamp=first_new,
        feature_version=feature_version,
    )
    suffix = features.loc[features["timestamp"] >= first_new].copy()
    repository.upsert(suffix)
    return suffix


def load_or_build_indicator_features(
    *,
    symbol: str,
    timeframe: str,
    analyze_start: str,
    analyze_end: str,
    cache_dir: Path | None = None,
    repository: IndicatorFeatureRepository | None = None,
    feature_version: str = INDICATOR_FEATURE_VERSION,
    force_rebuild: bool = False,
) -> pd.DataFrame:
    """Bulk-load features for Shared Context consumers (no per-bar queries)."""
    repo = repository
    if repo is None and cache_dir is not None:
        repo = ParquetIndicatorFeatureRepository(Path(cache_dir))
    if repo is None:
        repo = InMemoryIndicatorFeatureRepository()

    a0 = pd.Timestamp(analyze_start)
    a1 = pd.Timestamp(analyze_end)
    if a0.tzinfo is None:
        a0 = a0.tz_localize("UTC")
    if a1.tzinfo is None:
        a1 = a1.tz_localize("UTC")

    if not force_rebuild:
        existing = repo.load(
            symbol=symbol,
            timeframe=timeframe,
            start=a0,
            end=a1 - pd.Timedelta(microseconds=1),
            feature_version=feature_version,
        )
        if not existing.empty:
            return existing

    features, _ = backfill_indicator_features(
        symbol=symbol,
        timeframe=timeframe,
        start=analyze_start,
        end=analyze_end,
        repository=repo,
        feature_version=feature_version,
        force_rebuild=force_rebuild,
    )
    return features


def attach_indicator_features_to_context(ctx: Any, features: pd.DataFrame) -> Any:
    """Attach features onto a SharedReplayContext instance without schema migration.

    Classification code must not read these for decisions in C3.2A.
    """
    setattr(ctx, "indicator_features", features)
    setattr(ctx, "indicator_feature_version", INDICATOR_FEATURE_VERSION)
    return ctx


def assert_batch_incremental_parity(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    abs_tol: float = 1e-10,
) -> dict[str, Any]:
    """Compare full batch vs candle-by-candle prefix rebuilds."""
    cfg = default_regime_scanner_config().with_timeframe(timeframe)
    batch = compute_indicator_features(
        candles, symbol=symbol, timeframe=timeframe, config=cfg
    )
    n = len(candles)
    start_i = max(1, n - 40)
    max_abs = 0.0
    max_rel = 0.0
    compared = 0
    for i in range(start_i, n + 1):
        prefix = candles.iloc[:i].reset_index(drop=True)
        inc = compute_indicator_features(
            prefix, symbol=symbol, timeframe=timeframe, config=cfg
        )
        cols = [
            c
            for c in (
                "ema_9",
                "ema_20",
                "ema_59",
                "ema_200",
                "atr_14",
                "adx_14",
                "plus_di_14",
                "minus_di_14",
                "ema_9_20_spread_atr",
            )
            if c in batch.columns and c in inc.columns
        ]
        b_row = batch.iloc[i - 1]
        i_row = inc.iloc[-1]
        for c in cols:
            bv, iv = b_row[c], i_row[c]
            if pd.isna(bv) and pd.isna(iv):
                continue
            if pd.isna(bv) or pd.isna(iv):
                max_abs = float("inf")
                continue
            diff = abs(float(bv) - float(iv))
            max_abs = max(max_abs, diff)
            denom = max(abs(float(bv)), 1e-12)
            max_rel = max(max_rel, diff / denom)
            compared += 1
    return {
        "compared_cells": compared,
        "max_absolute_difference": max_abs if math.isfinite(max_abs) else None,
        "max_relative_difference": max_rel,
        "parity_ok": bool(max_abs <= abs_tol),
        "abs_tol": abs_tol,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3.2A indicator feature store CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="Batch backfill indicator features")
    bf.add_argument("--symbol", default="APTUSDT")
    bf.add_argument("--timeframe", default="30m")
    bf.add_argument("--start", required=True)
    bf.add_argument("--end", required=True)
    bf.add_argument("--feature-version", default=INDICATOR_FEATURE_VERSION)
    bf.add_argument("--force-rebuild", action="store_true")
    bf.add_argument("--dry-run", action="store_true")
    bf.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("research/regime_scanner/results/indicator_feature_store/.cache"),
    )
    bf.add_argument("--batch-size", type=int, default=5000, help="Reserved for future chunking")
    bf.add_argument("--report-path", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "backfill":
        repo = ParquetIndicatorFeatureRepository(args.cache_dir)
        features, stats = backfill_indicator_features(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            repository=repo,
            feature_version=args.feature_version,
            force_rebuild=args.force_rebuild,
            dry_run=args.dry_run,
        )
        report = {
            "command": "backfill",
            "symbol": args.symbol,
            "timeframe": args.timeframe,
            "start": args.start,
            "end": args.end,
            "stats": stats.to_dict(),
            "n_features": len(features),
        }
        print(json.dumps(json_safe(report), indent=2, sort_keys=True))
        if args.report_path is not None:
            args.report_path.parent.mkdir(parents=True, exist_ok=True)
            args.report_path.write_text(
                json.dumps(json_safe(report), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
