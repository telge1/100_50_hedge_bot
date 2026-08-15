"""Tests for historical timestamp direction runner (no DB writes)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.trend_direction_at import (
    TrendDirectionAtError,
    decide_from_structure,
    format_text_report,
    map_major_to_direction,
    map_structure_to_direction,
    normalize_symbol,
    parse_decision_timestamp,
    query_trend_direction_at,
    run_c34b_on_ohlcv,
)


def _synth_ohlcv(n: int = 200, *, start: str = "2026-01-01", seed: int = 0) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    # trending down then up to create structure
    close = 100 - np.linspace(0, 8, n // 2)
    close = np.concatenate([close, close[-1] + np.linspace(0, 10, n - n // 2)])
    close = close + rng.normal(0, 0.05, size=n)
    high = close + 0.3
    low = close - 0.3
    open_ = close.copy()
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.ones(n),
            "close_time": ts + pd.Timedelta(minutes=5),
        }
    )


def test_parse_iso_z():
    ts, assumed = parse_decision_timestamp("2026-04-11T20:31:00Z")
    assert assumed is False
    assert ts.hour == 20 and ts.minute == 31


def test_parse_iso_offset():
    ts, assumed = parse_decision_timestamp("2026-04-11T20:31:00+00:00")
    assert assumed is False
    assert str(ts.tzinfo) is not None


def test_parse_naive_as_utc():
    ts, assumed = parse_decision_timestamp("2026-04-11 20:31:00")
    assert assumed is True
    assert ts.tzinfo is not None
    assert ts.hour == 20


def test_symbol_normalization():
    assert normalize_symbol("aptusdt") == "APTUSDT"
    assert normalize_symbol("APT/USDT") == "APTUSDT"
    assert normalize_symbol("APT_USDT") == "APTUSDT"


def test_map_major_directions():
    assert map_major_to_direction(1) == "BULLISH"
    assert map_major_to_direction(-1) == "BEARISH"
    assert map_major_to_direction(0) == "UNCLEAR"


def test_map_structure_conflict_to_unclear():
    assert map_structure_to_direction(1, "bullish_structure") == "BULLISH"
    assert map_structure_to_direction(1, "bullish_pullback") == "BULLISH"
    assert map_structure_to_direction(1, "bearish_internal_break") == "UNCLEAR"
    assert map_structure_to_direction(1, "bearish_choch") == "UNCLEAR"
    assert map_structure_to_direction(-1, "bearish_structure") == "BEARISH"
    assert map_structure_to_direction(-1, "bearish_pullback") == "BEARISH"
    assert map_structure_to_direction(-1, "bullish_internal_break") == "UNCLEAR"
    assert map_structure_to_direction(-1, "bullish_choch") == "UNCLEAR"
    assert map_structure_to_direction(1, "structure_unknown") == "UNCLEAR"


def test_bullish_normal_pullback_stays_bullish():
    assert map_structure_to_direction(1, "bullish_pullback") == "BULLISH"
    assert map_structure_to_direction(1, "bullish_structure") == "BULLISH"


def test_bearish_normal_pullback_stays_bearish():
    assert map_structure_to_direction(-1, "bearish_pullback") == "BEARISH"
    assert map_structure_to_direction(-1, "bearish_structure") == "BEARISH"


def test_confirmed_major_flip_maps_directional():
    # Aligned major + structure after confirm → directional
    assert map_structure_to_direction(-1, "bearish_structure") == "BEARISH"
    assert map_structure_to_direction(1, "bullish_structure") == "BULLISH"
    # Opposite-side CHOCH while sticky major not yet flipped → UNCLEAR
    assert map_structure_to_direction(1, "bearish_choch") == "UNCLEAR"
    assert map_structure_to_direction(-1, "bullish_choch") == "UNCLEAR"


def test_bullish_invalidation_without_bearish_confirm_unclear():
    df = _synth_ohlcv(150, seed=1)
    struct = run_c34b_on_ohlcv(df).copy()
    struct.loc[struct.index[-1], "major_direction"] = 1
    struct.loc[struct.index[-1], "protected_structure_state"] = "bearish_internal_break"
    decision = struct.iloc[-1]["timestamp"] + pd.Timedelta(minutes=5)
    r = decide_from_structure(
        struct,
        decision_time=decision,
        symbol="TEST",
        exchange="bybit",
        timestamp_assumed_utc=False,
        warmup_bars=50,
        include_htf=False,
    )
    assert r.direction == "UNCLEAR"
    assert r.reason and "MAJOR_CHALLENGED" in r.reason


def test_htf_flag_does_not_change_primary_direction():
    df = _synth_ohlcv(120, seed=2)
    decision = df.iloc[-1]["close_time"]
    a = query_trend_direction_at(
        symbol="TEST", timestamp=decision, candles=df, warmup_bars=50, include_htf=False
    )
    b = query_trend_direction_at(
        symbol="TEST", timestamp=decision, candles=df, warmup_bars=50, include_htf=True
    )
    assert a.direction == b.direction
    assert a.last_available_5m_close_utc == b.last_available_5m_close_utc


def test_running_candle_excluded():
    df = _synth_ohlcv(120)
    # decision inside last incomplete window relative to a mid close
    mid = df.iloc[80]
    decision = mid["timestamp"] + pd.Timedelta(minutes=2)  # during candle that opened at mid
    # candles with close <= decision should exclude the mid open candle (closes mid+5)
    out = query_trend_direction_at(
        symbol="TEST",
        timestamp=decision,
        candles=df,
        warmup_bars=50,
        include_htf=False,
    )
    assert out.last_available_5m_close_utc is not None
    last_close = pd.Timestamp(out.last_available_5m_close_utc)
    assert last_close <= pd.Timestamp(decision)
    assert out.causality_pass is True
    # mid candle itself must not be selected
    assert pd.Timestamp(out.last_available_5m_open_utc) < mid["timestamp"] or last_close <= decision


def test_exact_close_allows_candle():
    df = _synth_ohlcv(120)
    row = df.iloc[90]
    decision = row["close_time"]
    out = query_trend_direction_at(
        symbol="TEST",
        timestamp=decision,
        candles=df,
        warmup_bars=50,
        include_htf=False,
    )
    assert out.last_available_5m_open_utc == row["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out.last_available_5m_close_utc == decision.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_no_candle_with_close_after_t():
    df = _synth_ohlcv(100)
    decision = df.iloc[50]["close_time"]
    out = query_trend_direction_at(
        symbol="TEST",
        timestamp=decision,
        candles=df,
        warmup_bars=40,
        include_htf=False,
    )
    # rebuild filter
    used = df.loc[df["close_time"] <= decision]
    assert used["close_time"].max() <= decision
    assert out.causality_pass is True


def test_insufficient_warmup_unclear():
    df = _synth_ohlcv(40)
    decision = df.iloc[-1]["close_time"]
    out = query_trend_direction_at(
        symbol="TEST",
        timestamp=decision,
        candles=df,
        warmup_bars=72,
        include_htf=False,
    )
    assert out.direction == "UNCLEAR"
    assert out.reason == "INSUFFICIENT_WARMUP"


def test_before_data_via_empty_filter():
    df = _synth_ohlcv(50)
    decision = df["timestamp"].iloc[0] - pd.Timedelta(hours=1)
    with pytest.raises(TrendDirectionAtError) as ei:
        query_trend_direction_at(
            symbol="TEST",
            timestamp=decision,
            candles=df,
            warmup_bars=10,
            include_htf=False,
        )
    assert ei.value.reason == "NO_CLOSED_CANDLE"


def test_bullish_bearish_unclear_mapping_via_decide():
    df = _synth_ohlcv(150, seed=1)
    struct = run_c34b_on_ohlcv(df)
    # force last major for unit mapping path
    struct = struct.copy()
    struct.loc[struct.index[-1], "major_direction"] = 1
    struct.loc[struct.index[-1], "protected_structure_state"] = "bullish_structure"
    decision = struct.iloc[-1]["timestamp"] + pd.Timedelta(minutes=5)
    r = decide_from_structure(
        struct,
        decision_time=decision,
        symbol="TEST",
        exchange="bybit",
        timestamp_assumed_utc=False,
        warmup_bars=50,
        include_htf=False,
    )
    assert r.direction == "BULLISH"

    struct.loc[struct.index[-1], "major_direction"] = -1
    struct.loc[struct.index[-1], "protected_structure_state"] = "bearish_structure"
    r2 = decide_from_structure(
        struct,
        decision_time=decision,
        symbol="TEST",
        exchange="bybit",
        timestamp_assumed_utc=False,
        warmup_bars=50,
        include_htf=False,
    )
    assert r2.direction == "BEARISH"

    struct.loc[struct.index[-1], "major_direction"] = 0
    struct.loc[struct.index[-1], "protected_structure_state"] = "structure_unknown"
    r3 = decide_from_structure(
        struct,
        decision_time=decision,
        symbol="TEST",
        exchange="bybit",
        timestamp_assumed_utc=False,
        warmup_bars=50,
        include_htf=False,
    )
    assert r3.direction == "UNCLEAR"


def test_json_and_text_output_parseable():
    df = _synth_ohlcv(120)
    decision = df.iloc[-1]["close_time"]
    out = query_trend_direction_at(
        symbol="aptusdt",
        timestamp=decision,
        candles=df,
        warmup_bars=50,
        include_htf=False,
    )
    payload = out.to_dict()
    assert json.loads(json.dumps(payload, default=str))["direction"] in {
        "BULLISH",
        "BEARISH",
        "UNCLEAR",
    }
    text = format_text_report(out)
    assert "direction:" in text
    assert out.symbol == "APTUSDT"


def test_deterministic_repeat():
    df = _synth_ohlcv(130, seed=3)
    decision = df.iloc[-1]["close_time"]
    a = query_trend_direction_at(
        symbol="TEST", timestamp=decision, candles=df, warmup_bars=50, include_htf=False
    )
    b = query_trend_direction_at(
        symbol="TEST", timestamp=decision, candles=df, warmup_bars=50, include_htf=False
    )
    assert a.to_dict() == b.to_dict()


def test_cli_help_imports():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "query_trend_direction_at.py"
    spec = importlib.util.spec_from_file_location("query_trend_direction_at_cli", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ns = mod.build_parser().parse_args(
        ["--symbol", "APTUSDT", "--timestamp", "2026-04-11T20:31:00Z"]
    )
    assert ns.symbol == "APTUSDT"
