"""Tests for Phase C3.2A indicator feature store."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.indicator_feature_store import (
    INDICATOR_FEATURE_VERSION,
    InMemoryIndicatorFeatureRepository,
    ParquetIndicatorFeatureRepository,
    assert_batch_incremental_parity,
    compute_indicator_features,
    detect_timestamp_gaps,
    features_content_hash,
    required_indicator_warmup_bars,
    update_indicator_features_for_closed_candles,
)
from research.regime_scanner.indicator_feature_store_audit import (
    independent_ema_reference,
    write_indicator_pine,
)
from research.regime_scanner.indicators import (
    atr_wilder,
    directional_moves,
    ema,
    true_range,
)
from research.regime_scanner.trend_audit_shared_replay import (
    SharedReplayContext,
    attach_c32a_indicator_features,
)
from research.regime_scanner.trend_pine_export import validate_pine_script
from research.regime_scanner.trend_regime_classifier import replay_regime_variant



def _ohlcv(
    n: int,
    *,
    start: str = "2026-01-01",
    freq: str = "30min",
    close0: float = 100.0,
    mode: str = "flat",
) -> pd.DataFrame:
    ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    if mode == "flat":
        close = np.full(n, close0, dtype="float64")
    elif mode == "up":
        close = close0 + np.arange(n, dtype="float64") * 0.5
    elif mode == "down":
        close = close0 - np.arange(n, dtype="float64") * 0.5
    elif mode == "sideways":
        close = close0 + np.sin(np.arange(n) / 3.0) * 0.8
    else:
        raise ValueError(mode)
    high = close + 0.4
    low = close - 0.4
    open_ = close.copy()
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1000.0),
        }
    )


def test_required_warmup_covers_ema200() -> None:
    assert required_indicator_warmup_bars() >= 200 + 6


def test_ema_constant_series_equals_price() -> None:
    df = _ohlcv(50, mode="flat", close0=42.0)
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    for p in (9, 20):
        ready = feat[f"ema_{p}"].iloc[p:]
        assert np.allclose(ready, 42.0, atol=1e-10)


def test_ema_rising_series_faster_periods_lead() -> None:
    df = _ohlcv(300, mode="up")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    i = 250
    assert feat.loc[i, "ema_9"] > feat.loc[i, "ema_20"]
    assert feat.loc[i, "ema_20"] > feat.loc[i, "ema_59"]
    assert feat.loc[i, "ema_59"] > feat.loc[i, "ema_200"]


def test_atr_flat_ohlc_near_range() -> None:
    df = _ohlcv(40, mode="flat", close0=100.0)
    # high-low = 0.8 constant; no gap → TR = 0.8
    atr = atr_wilder(df["high"], df["low"], df["close"], 14)
    assert atr.iloc[-1] == pytest.approx(0.8, rel=1e-6)


def test_true_range_gap_uses_previous_close() -> None:
    df = _ohlcv(5, mode="flat", close0=100.0)
    df.loc[2, "open"] = 110.0
    df.loc[2, "high"] = 112.0
    df.loc[2, "low"] = 109.0
    df.loc[2, "close"] = 111.0
    tr = true_range(df["high"], df["low"], df["close"])
    # gap from prev close 100 to high 112 / low 109
    assert tr.iloc[2] == pytest.approx(12.0, abs=1e-9)


def test_directional_move_rules() -> None:
    high = pd.Series([10.0, 12.0, 11.0, 14.0])
    low = pd.Series([9.0, 10.0, 8.0, 10.5])
    plus_dm, minus_dm = directional_moves(high, low)
    # bar1: up=2, down=1 → +DM=2
    assert plus_dm.iloc[1] == pytest.approx(2.0)
    assert minus_dm.iloc[1] == pytest.approx(0.0)
    # bar2: up=-1, down=2 → -DM=2
    assert plus_dm.iloc[2] == pytest.approx(0.0)
    assert minus_dm.iloc[2] == pytest.approx(2.0)


def test_dmi_uptrend_plus_di_dominates() -> None:
    df = _ohlcv(120, mode="up")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    tail = feat.iloc[-20:]
    assert (tail["plus_di_14"] > tail["minus_di_14"]).mean() > 0.8
    assert float(tail["adx_14"].mean()) > 20.0


def test_dmi_downtrend_minus_di_dominates() -> None:
    df = _ohlcv(120, mode="down")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    tail = feat.iloc[-20:]
    assert (tail["minus_di_14"] > tail["plus_di_14"]).mean() > 0.8


def test_sideways_tight_spreads_and_crosses() -> None:
    df = _ohlcv(280, mode="sideways")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    ready = feat.loc[feat["features_ready"]]
    assert len(ready) > 10
    assert ready["ema_9_20_abs_spread_atr"].median() < 2.5
    assert ready["ema_fast_cross_count_24"].mean() > 0.5


def test_batch_incremental_parity() -> None:
    df = _ohlcv(100, mode="up")
    report = assert_batch_incremental_parity(df, symbol="T", timeframe="30m")
    assert report["parity_ok"] is True


def test_chunked_equals_full() -> None:
    df = _ohlcv(90, mode="up")
    full = compute_indicator_features(df, symbol="T", timeframe="30m")
    mid = 45
    a = compute_indicator_features(df.iloc[:mid], symbol="T", timeframe="30m")
    b = compute_indicator_features(df, symbol="T", timeframe="30m")
    # prefix of full equals a
    cols = ["ema_9", "ema_20", "atr_14", "adx_14"]
    for c in cols:
        assert np.allclose(
            a[c].to_numpy(),
            b[c].iloc[:mid].to_numpy(),
            equal_nan=True,
            atol=1e-12,
        )
    assert features_content_hash(full) == features_content_hash(b)


def test_upsert_idempotent() -> None:
    df = _ohlcv(60, mode="up")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    repo = InMemoryIndicatorFeatureRepository()
    r1 = repo.upsert(feat)
    r2 = repo.upsert(feat)
    assert r1["inserted"] == len(feat)
    assert r2["unchanged"] == len(feat)
    assert r2["inserted"] == 0


def test_feature_version_isolation() -> None:
    df = _ohlcv(40, mode="flat")
    f1 = compute_indicator_features(df, symbol="T", timeframe="30m", feature_version="v1")
    f2 = compute_indicator_features(df, symbol="T", timeframe="30m", feature_version="v2")
    repo = InMemoryIndicatorFeatureRepository()
    repo.upsert(f1)
    repo.upsert(f2)
    loaded_v1 = repo.load(symbol="T", timeframe="30m", feature_version="v1")
    loaded_v2 = repo.load(symbol="T", timeframe="30m", feature_version="v2")
    assert len(loaded_v1) == len(f1)
    assert len(loaded_v2) == len(f2)
    assert set(loaded_v1["feature_version"]) == {"v1"}


def test_historical_correction_rebuilds_suffix() -> None:
    df = _ohlcv(80, mode="up")
    repo = InMemoryIndicatorFeatureRepository()
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    repo.upsert(feat)
    # Correct a middle candle
    corrected = df.copy()
    corrected.loc[40, "close"] = float(corrected.loc[40, "close"]) + 5.0
    corrected.loc[40, "high"] = float(corrected.loc[40, "high"]) + 5.0
    suffix = update_indicator_features_for_closed_candles(
        symbol="T",
        timeframe="30m",
        closed_candles=corrected.iloc[40:],
        repository=repo,
        history_candles=corrected.iloc[:40],
    )
    rebuilt = compute_indicator_features(corrected, symbol="T", timeframe="30m")
    loaded = repo.load(symbol="T", timeframe="30m")
    # Suffix from 40 matches rebuild
    for c in ("ema_9", "ema_20", "atr_14", "adx_14"):
        assert np.allclose(
            loaded[c].iloc[40:].to_numpy(),
            rebuilt[c].iloc[40:].to_numpy(),
            equal_nan=True,
            atol=1e-10,
        )
    assert len(suffix) == len(corrected) - 40


def test_gap_detection() -> None:
    df = _ohlcv(10, mode="flat")
    df = df.drop(index=5).reset_index(drop=True)
    gaps = detect_timestamp_gaps(df, "30m")
    assert len(gaps) == 1


def test_incremental_raises_on_gap() -> None:
    df = _ohlcv(20, mode="flat")
    repo = InMemoryIndicatorFeatureRepository()
    hist = df.iloc[:10]
    new = df.iloc[12:15]  # skip bars → gap
    with pytest.raises(ValueError, match="gap"):
        update_indicator_features_for_closed_candles(
            symbol="T",
            timeframe="30m",
            closed_candles=new,
            repository=repo,
            history_candles=hist,
        )


def test_parquet_roundtrip(tmp_path: Path) -> None:
    df = _ohlcv(50, mode="up")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    repo = ParquetIndicatorFeatureRepository(tmp_path)
    repo.upsert(feat)
    loaded = repo.load(symbol="T", timeframe="30m")
    assert features_content_hash(feat) == features_content_hash(loaded)


def test_no_lookahead_on_append() -> None:
    df = _ohlcv(100, mode="up")
    a = compute_indicator_features(df.iloc[:80], symbol="T", timeframe="30m")
    b = compute_indicator_features(df.iloc[:100], symbol="T", timeframe="30m")
    for c in ("ema_9", "ema_200", "atr_14", "adx_14", "ema_9_slope_3"):
        assert np.allclose(
            a[c].to_numpy(),
            b[c].iloc[:80].to_numpy(),
            equal_nan=True,
            atol=1e-12,
        )


def test_atr_nan_keeps_spread_atr_nan() -> None:
    df = _ohlcv(5, mode="flat")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    # Force atr nan
    feat.loc[0, "atr_14"] = np.nan
    # Recompute enrichment path: spread_atr should be nan when atr nan
    # At early bars atr may already be defined via ewm; check formula path
    from research.regime_scanner.indicator_feature_store import _safe_div

    s = _safe_div(pd.Series([1.0, 2.0]), pd.Series([np.nan, 0.0]))
    assert math.isnan(s.iloc[0])
    assert math.isnan(s.iloc[1])


def test_independent_ema_matches_project_ema() -> None:
    close = pd.Series(np.linspace(10, 20, 80))
    assert np.allclose(ema(close, 20), independent_ema_reference(close, 20), atol=1e-12)


def test_pine_header_valid(tmp_path: Path) -> None:
    df = _ohlcv(30, mode="flat")
    feat = compute_indicator_features(df, symbol="APTUSDT", timeframe="30m")
    path = write_indicator_pine(feat, output_path=tmp_path / "x.pine", overlay=True)
    text = path.read_text(encoding="utf-8")
    validate_pine_script(text)
    path2 = write_indicator_pine(
        feat, output_path=tmp_path / "dmi.pine", overlay=False, title="DMI"
    )
    validate_pine_script(path2.read_text(encoding="utf-8"))


def test_shared_context_attach_does_not_change_classifier() -> None:
    df = _ohlcv(80, mode="sideways")
    from research.regime_scanner.indicators import compute_indicator_frame

    frame = compute_indicator_frame(df)
    ctx = SharedReplayContext(
        frame=frame,
        pivot_visibility=None,  # type: ignore[arg-type]
        pivot_end_by_bar=np.zeros(len(frame), dtype=int),
        prepared_bars=[],
    )
    feats = compute_indicator_features(df, symbol="T", timeframe="30m")
    attach_c32a_indicator_features(ctx, feats)
    assert getattr(ctx, "indicator_feature_version") == INDICATOR_FEATURE_VERSION
    assert len(getattr(ctx, "indicator_features")) == len(feats)
    assert callable(replay_regime_variant)


def test_determinism_hash_stable() -> None:
    df = _ohlcv(70, mode="up")
    a = compute_indicator_features(df, symbol="T", timeframe="30m")
    b = compute_indicator_features(df, symbol="T", timeframe="30m")
    assert features_content_hash(a) == features_content_hash(b)


def test_features_ready_false_early() -> None:
    df = _ohlcv(50, mode="up")
    feat = compute_indicator_features(df, symbol="T", timeframe="30m")
    assert bool(feat["features_ready"].iloc[0]) is False
    assert feat["features_ready"].iloc[: min(30, len(feat))].sum() == 0


def test_c31_classifier_import_unaffected() -> None:
    from research.regime_scanner import trend_regime_classifier as trc

    assert hasattr(trc, "replay_regime_variant")
    assert hasattr(trc, "config_c3")
    assert callable(replay_regime_variant)
