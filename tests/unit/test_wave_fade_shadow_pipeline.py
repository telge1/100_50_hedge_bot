"""Unit tests for Wave-Fade shadow signal pipeline (no long history)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from signal_generator.pipeline.mapper import wave_event_to_signal
from signal_generator.pipeline.signal_id import deterministic_signal_id
from signal_generator.pipeline.versions import (
    EDGES_VERSION,
    GENERATOR_VERSION,
    GLOBAL_FROZEN_TIER_A,
    STRATEGY_VERSION,
)
from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges
from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS
from signal_generator.timeframes import (
    OhlcvBar,
    aggregate_bucket,
    bucket_close,
    bucket_start,
    is_htf_available_for_signals,
)


def test_global_frozen_tier_a_policy():
    assert GLOBAL_FROZEN_TIER_A is True
    edges = load_frozen_eff_edges()
    # Same edges object for all symbols — no per-symbol keys
    assert ("15m", "UP", "directional_efficiency") in edges
    assert EDGES_VERSION.startswith("apt_is_q4_frozen_")


def test_deterministic_signal_id_stable():
    ot = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    a = deterministic_signal_id(
        strategy_version=STRATEGY_VERSION,
        symbol="APTUSDT",
        timeframe="15m",
        candle_open_time=ot,
        direction="LONG",
        signal_type="wave_fade",
    )
    b = deterministic_signal_id(
        strategy_version=STRATEGY_VERSION,
        symbol="APTUSDT",
        timeframe="15m",
        candle_open_time=ot,
        direction="LONG",
        signal_type="wave_fade",
    )
    c = deterministic_signal_id(
        strategy_version=STRATEGY_VERSION,
        symbol="DOGEUSDT",
        timeframe="15m",
        candle_open_time=ot,
        direction="LONG",
        signal_type="wave_fade",
    )
    assert a == b
    assert a != c
    assert isinstance(a, UUID)


def test_closed_only_available_at_all_tfs():
    for tf, mins in [("15m", 15), ("30m", 30), ("1h", 60), ("4h", 240)]:
        open_t = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        if tf == "4h":
            open_t = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
        close_t = bucket_close(open_t, tf)
        assert close_t == open_t + timedelta(minutes=mins)
        assert is_htf_available_for_signals(
            bucket_open=open_t, timeframe=tf, as_of=close_t
        )
        assert not is_htf_available_for_signals(
            bucket_open=open_t, timeframe=tf, as_of=close_t - timedelta(seconds=1)
        )


def _bars_for_bucket(tf: str, open_t: datetime) -> list[OhlcvBar]:
    n = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[tf]
    out = []
    for i in range(n):
        ot = open_t + timedelta(minutes=i)
        ct = ot + timedelta(minutes=1)
        px = 100.0 + i * 0.01
        out.append(
            OhlcvBar(ot, ct, px, px + 0.1, px - 0.1, px, 1.0, 1.0)
        )
    return out


def test_htf_aggregation_parity_manual():
    from signal_generator.pipeline.audit import manual_aggregate_1m

    for tf in SIGNAL_TFS:
        open_t = bucket_start(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc), tf)
        bars = _bars_for_bucket(tf, open_t)
        as_of = bucket_close(open_t, tf)
        bar = aggregate_bucket(bars, timeframe=tf, bucket_open=open_t, as_of=as_of)
        manual = manual_aggregate_1m(bars, timeframe=tf, bucket_open=open_t)
        assert bar is not None and manual is not None
        assert bar.available_at == as_of == manual["available_at"]
        assert abs(bar.open - manual["open"]) < 1e-12
        assert abs(bar.high - manual["high"]) < 1e-12
        assert abs(bar.low - manual["low"]) < 1e-12
        assert abs(bar.close - manual["close"]) < 1e-12
        assert abs(bar.volume - manual["volume"]) < 1e-12


def test_future_candle_cannot_close_bucket():
    open_t = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    bars = _bars_for_bucket("15m", open_t)
    # as_of before close → no bar
    early = open_t + timedelta(minutes=10)
    assert aggregate_bucket(bars, timeframe="15m", bucket_open=open_t, as_of=early) is None


def test_mapper_saves_tier_a_false_and_traded_false():
    conf = datetime(2026, 8, 1, 10, 15, tzinfo=timezone.utc)
    row = {
        "side": "SHORT",
        "confirmation_available_at": conf,
        "end_ts": datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        "end_price": 1.23,
        "entry_price": 1.25,
        "entry_time": datetime(2026, 8, 1, 10, 16, tzinfo=timezone.utc),
        "entry_valid": True,
        "is_tier_a": False,
        "is_q4": False,
        "trend_bucket": "MIXED",
        "eff_quantile": "Q2",
        "direction": "UP",
        "stoch_path": "MID->HIGH",
        "stoch_k_end": 75.0,
        "n_bars": 5,
    }
    sig = wave_event_to_signal(row, symbol="APTUSDT", timeframe="15m")
    assert sig.tier_a is False
    assert sig.traded is False
    assert sig.trade_id is None
    assert sig.direction == "SHORT"
    assert sig.signal_type == "wave_fade"
    assert float(sig.signal_price) == 1.25
    assert sig.generator_version == GENERATOR_VERSION
    assert sig.strategy_version == STRATEGY_VERSION
    assert "apt_is_q4_frozen" in sig.metadata
    assert '"entry_valid":true' in sig.metadata.replace(" ", "")
    assert sig.generated_at == conf


def test_mapper_tier_a_true():
    conf = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)
    row = {
        "side": "LONG",
        "confirmation_available_at": conf,
        "end_ts": datetime(2026, 8, 1, 10, 30, tzinfo=timezone.utc),
        "end_price": 0.5,
        "entry_price": 0.51,
        "entry_time": datetime(2026, 8, 1, 11, 1, tzinfo=timezone.utc),
        "entry_valid": True,
        "is_tier_a": True,
        "is_q4": True,
        "trend_bucket": "TREND_ALIGNED",
        "eff_quantile": "Q4",
        "direction": "DOWN",
        "stoch_k_end": 20.0,
        "n_bars": 8,
    }
    sig = wave_event_to_signal(row, symbol="DOGEUSDT", timeframe="30m")
    assert sig.tier_a is True
    assert sig.traded is False
    assert float(sig.signal_price) == 0.51
    meta = sig.metadata
    assert "tp_price" in meta
    assert "sl_price" in meta


def test_mapper_without_entry_does_not_use_end_price():
    conf = datetime(2026, 8, 1, 10, 15, tzinfo=timezone.utc)
    row = {
        "side": "LONG",
        "confirmation_available_at": conf,
        "end_ts": conf,
        "end_price": 9.99,
        "is_tier_a": True,
        "is_q4": True,
        "trend_bucket": "TREND_ALIGNED",
        "eff_quantile": "Q4",
        "direction": "DOWN",
        "stoch_k_end": 10.0,
        "n_bars": 3,
    }
    sig = wave_event_to_signal(row, symbol="SOLUSDT", timeframe="15m")
    assert float(sig.signal_price) == 0.0
    assert '"entry_valid":false' in sig.metadata.replace(" ", "")


def test_same_edges_across_symbols():
    e = load_frozen_eff_edges()
    # No code path that takes symbol into load_frozen_eff_edges
    assert load_frozen_eff_edges()[("4h", "DOWN", "directional_efficiency")] == e[
        ("4h", "DOWN", "directional_efficiency")
    ]


class _MemState:
    def __init__(self):
        self._d = {}

    def get(self, symbol, timeframe, strategy_version, *, exchange="bybit"):
        return self._d.get((exchange, symbol, timeframe, strategy_version))

    def upsert(self, state):
        key = (state.exchange, state.symbol, state.timeframe, state.strategy_version)
        self._d[key] = state


class _MemSignals:
    def __init__(self):
        self.rows = []

    def insert_signals(self, signals):
        self.rows.extend(list(signals))
        return len(signals)


def test_no_signal_bucket_still_advances_state():
    from signal_generator.db.processing_state import ProcessingState
    from signal_generator.pipeline.processor import ShadowPipelineConfig, WaveFadeShadowPipeline

    # Minimal: monkeypatch _run_symbol path via _process_timeframe directly
    state = _MemState()
    signals = _MemSignals()

    class FakeCandles:
        pass

    cfg = ShadowPipelineConfig(
        symbols=["APTUSDT"],
        start=datetime(2026, 8, 1, 0, 15, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
    )
    pipe = WaveFadeShadowPipeline(
        candles=FakeCandles(),  # type: ignore[arg-type]
        signals=signals,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        config=cfg,
        edges={},
    )
    open_t = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    bars = _bars_for_bucket("15m", open_t)
    # empty signals df
    pipe._process_timeframe(
        symbol="APTUSDT",
        timeframe="15m",
        bars_1m=bars,
        all_sig=pd.DataFrame(),
        as_of=datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
        window_start=cfg.start,
        window_end=cfg.end,
        open_times=__import__('numpy').array([], dtype='datetime64[ns]'),
        opens=__import__('numpy').array([], dtype=float),
    )
    st = state.get("APTUSDT", "15m", STRATEGY_VERSION)
    assert st is not None
    assert st.last_processed_candle_open_time == open_t
    assert len(signals.rows) == 0


def test_crash_before_watermark_allows_replay():
    """Signal insert then missing watermark → reprocess same id (at-least-once)."""
    from signal_generator.pipeline.processor import ShadowPipelineConfig, WaveFadeShadowPipeline

    state = _MemState()
    signals = _MemSignals()
    cfg = ShadowPipelineConfig(
        symbols=["APTUSDT"],
        start=datetime(2026, 8, 1, 0, 15, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc),
    )
    pipe = WaveFadeShadowPipeline(
        candles=object(),  # type: ignore[arg-type]
        signals=signals,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        config=cfg,
        edges={},
    )
    conf = datetime(2026, 8, 1, 0, 15, tzinfo=timezone.utc)
    all_sig = pd.DataFrame(
        [
            {
                "side": "LONG",
                "confirmation_available_at": conf,
                "end_ts": datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
                "end_price": 1.0,
                "is_tier_a": True,
                "is_q4": True,
                "trend_bucket": "TREND_ALIGNED",
                "eff_quantile": "Q4",
                "direction": "DOWN",
                "stoch_k_end": 10.0,
                "n_bars": 4,
                "signal_tf": "15m",
                "signal_id": 0,
            }
        ]
    )
    bars = _bars_for_bucket("15m", datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
    pipe._process_timeframe(
        symbol="APTUSDT",
        timeframe="15m",
        bars_1m=bars,
        all_sig=all_sig,
        as_of=conf,
        window_start=cfg.start,
        window_end=cfg.end,
        open_times=__import__('numpy').array([], dtype='datetime64[ns]'),
        opens=__import__('numpy').array([], dtype=float),
    )
    assert len(signals.rows) == 1
    sid = signals.rows[0].signal_id
    # Simulate crash: clear watermark only
    state._d.clear()
    pipe._process_timeframe(
        symbol="APTUSDT",
        timeframe="15m",
        bars_1m=bars,
        all_sig=all_sig,
        as_of=conf,
        window_start=cfg.start,
        window_end=cfg.end,
        open_times=__import__('numpy').array([], dtype='datetime64[ns]'),
        opens=__import__('numpy').array([], dtype=float),
    )
    assert len(signals.rows) == 2
    assert signals.rows[0].signal_id == signals.rows[1].signal_id == sid


def test_multi_tf_state_independence():
    from signal_generator.pipeline.processor import ShadowPipelineConfig, WaveFadeShadowPipeline

    state = _MemState()
    signals = _MemSignals()
    cfg = ShadowPipelineConfig(
        symbols=["APTUSDT"],
        start=datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc),
    )
    pipe = WaveFadeShadowPipeline(
        candles=object(),  # type: ignore[arg-type]
        signals=signals,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        config=cfg,
        edges={},
    )
    as_of = datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
    for tf in ("15m", "1h"):
        # Include prior bucket so resume_from (start - tf) is COMPLETE
        prior = cfg.start - timedelta(minutes={"15m": 15, "1h": 60}[tf])
        bars = _bars_for_bucket(tf, prior) + _bars_for_bucket(tf, cfg.start)
        pipe._process_timeframe(
            symbol="APTUSDT",
            timeframe=tf,
            bars_1m=bars,
            all_sig=pd.DataFrame(),
            as_of=as_of,
            window_start=cfg.start,
            window_end=cfg.start + timedelta(minutes={"15m": 15, "1h": 60}[tf] + 1),
            open_times=__import__('numpy').array([], dtype='datetime64[ns]'),
            opens=__import__('numpy').array([], dtype=float),
        )
    s15 = state.get("APTUSDT", "15m", STRATEGY_VERSION)
    s1h = state.get("APTUSDT", "1h", STRATEGY_VERSION)
    assert s15 is not None and s1h is not None
    assert s15.timeframe == "15m" and s1h.timeframe == "1h"
