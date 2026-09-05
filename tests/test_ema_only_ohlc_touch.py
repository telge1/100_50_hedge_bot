"""ema_only touch detection via 1m OHLC overlap (no orderbook)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    process_symbol_stream,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner import (
    candle_analysis_samples,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
    candle_touch_price_in_zone,
    classify_zone_approach_from_candle_ohlc,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
    prepare_bars_with_ema200,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    COMPUTATION_MODE_EMA_ONLY,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    make_zone,
)


def test_wick_touch_detected_when_close_outside_band():
    zone = make_zone("EMA20", 100.0, 10.0)
    ev = classify_zone_approach_from_candle_ohlc(
        low=99.5,
        high=101.0,
        close=101.0,
        zone_low=zone.low,
        zone_high=zone.high,
    )
    assert ev["exact_touch"] is True
    assert ev["in_proximity"] is False
    assert ev["touch_price_basis"] == "candle_ohlc_1m"
    assert zone.low <= float(ev["touch_price"]) <= zone.high


def test_proximity_uses_close_not_wick():
    zone = make_zone("EMA20", 100.0, 10.0)
    # close just outside above; wick does not reach band
    ev = classify_zone_approach_from_candle_ohlc(
        low=101.55,
        high=101.65,
        close=101.62,
        zone_low=zone.low,
        zone_high=zone.high,
        max_pct=0.20,
    )
    assert ev["exact_touch"] is False
    assert ev["in_proximity"] is True


def test_touch_price_from_above_wick():
    zone = make_zone("EMA20", 100.0, 10.0)
    px = candle_touch_price_in_zone(
        low=99.0,
        high=101.2,
        close=102.0,
        zone_low=zone.low,
        zone_high=zone.high,
    )
    assert px is not None
    assert zone.low <= px <= zone.high
    assert px == pytest.approx(max(zone.low, min(99.0, zone.high)))


def test_candle_samples_carry_ohlc():
    times = pd.date_range("2026-08-20", periods=3000, freq="1min", tz="UTC")
    px = 100_000.0
    rows = []
    for t in times:
        rows.append(
            {
                "open_time": t,
                "open": px,
                "high": px * 1.0002,
                "low": px * 0.9998,
                "close": px,
            }
        )
        px *= 1.00001
    candles = pd.DataFrame(rows)
    bars = prepare_bars_with_ema200(candles)
    start = times[500].to_pydatetime()
    end = times[600].to_pydatetime()
    samples = candle_analysis_samples(candles_1m=candles, bars_5m=bars, start=start, end=end)
    assert samples
    assert all(s.source_file == "candles_1m_ohlc" for s in samples)
    assert all(s.candle_low is not None and s.candle_high is not None for s in samples)


def test_stream_ema_only_emits_touch_on_wick_only_bar():
    times = pd.date_range("2026-08-20", periods=3000, freq="1min", tz="UTC")
    px = 100_000.0
    rows = []
    for i, t in enumerate(times):
        close = px
        if i == 1500:
            # wick into EMA zone, close stays above
            close = px
            high = px * 1.0001
            low = px * 0.985
        else:
            high = px * 1.0001
            low = px * 0.9999
        rows.append({"open_time": t, "open": px, "high": high, "low": low, "close": close})
        px *= 1.000005 if i != 1500 else 1.0
    candles = pd.DataFrame(rows)
    bars = prepare_bars_with_ema200(candles)
    base = int(times[1400].timestamp() * 1000)
    end_ms = int(times[1600].timestamp() * 1000)
    start_dt = times[1400].to_pydatetime()
    end_dt = times[1600].to_pydatetime()
    samples = candle_analysis_samples(
        candles_1m=candles.iloc[1400:1600],
        bars_5m=bars,
        start=start_dt,
        end=end_dt,
    )
    out = process_symbol_stream(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=lambda _a, _b: pd.DataFrame(),
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=end_ms,
        computation_mode=COMPUTATION_MODE_EMA_ONLY,
    )
    touches = [e for e in out["ema_setup_events"] if e.get("zone_event") == "exact_touch"]
    assert touches, "expected at least one OHLC-based exact touch"
    assert any(e.get("touch_price_basis") == "candle_ohlc_1m" for e in touches)
