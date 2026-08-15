"""Tests for historical range trend-direction runner."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.trend_direction_at import (
    TrendDirectionAtError,
    decide_from_structure,
    normalize_symbol,
    parse_decision_timestamp,
    query_trend_direction_at,
    run_c34b_on_ohlcv,
)
from research.regime_scanner.trend_direction_range import (
    TIMELINE_COLUMNS,
    build_decision_times,
    build_summary,
    filter_transitions,
    parse_step,
    query_trend_direction_range,
)


def _synth_ohlcv(n: int = 400, *, start: str = "2026-01-01", seed: int = 0) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    close = 100 - np.linspace(0, 8, n // 2)
    close = np.concatenate([close, close[-1] + np.linspace(0, 10, n - n // 2)])
    close = close + rng.normal(0, 0.05, size=n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close.copy(),
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.ones(n),
            "close_time": ts + pd.Timedelta(minutes=5),
        }
    )


def test_cli_file_exists():
    path = Path(__file__).resolve().parents[3] / "scripts" / "query_trend_direction_range.py"
    assert path.is_file()


def test_cli_parser_imports():
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "scripts" / "query_trend_direction_range.py"
    spec = importlib.util.spec_from_file_location("query_trend_direction_range_cli", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ns = mod.build_parser().parse_args(
        [
            "--symbol",
            "APTUSDT",
            "--start",
            "2026-04-11T17:00:00Z",
            "--end",
            "2026-04-12T04:00:00Z",
            "--step",
            "5m",
        ]
    )
    assert ns.symbol == "APTUSDT"
    assert ns.transitions_only is False


def test_start_ge_end_rejected():
    with pytest.raises(TrendDirectionAtError) as ei:
        build_decision_times(
            pd.Timestamp("2026-04-12T04:00:00Z"),
            pd.Timestamp("2026-04-11T17:00:00Z"),
            step_minutes=5,
        )
    assert ei.value.reason == "INVALID_RANGE"


def test_iso_z_and_offset_parse():
    a, assumed_a = parse_decision_timestamp("2026-04-11T17:00:00Z")
    b, assumed_b = parse_decision_timestamp("2026-04-11T17:00:00+00:00")
    assert assumed_a is False and assumed_b is False
    assert a == b


def test_symbol_normalization():
    assert normalize_symbol("apt/usdt") == "APTUSDT"


def test_step_5m_only():
    assert parse_step("5m") == 5
    with pytest.raises(TrendDirectionAtError):
        parse_step("15m")


def test_decision_times_inclusive_and_aligned():
    times = build_decision_times(
        pd.Timestamp("2026-04-11T17:00:00Z"),
        pd.Timestamp("2026-04-11T17:20:00Z"),
        step_minutes=5,
    )
    assert str(times[0]) == "2026-04-11 17:00:00+00:00"
    assert str(times[-1]) == "2026-04-11 17:20:00+00:00"
    assert len(times) == 5


def test_no_lookahead_and_first_last_decision():
    df = _synth_ohlcv(200, seed=4)
    start = df.iloc[100]["close_time"]
    end = df.iloc[120]["close_time"]
    out = query_trend_direction_range(
        symbol="TEST",
        start=start,
        end=end,
        step="5m",
        candles=df,
        warmup_bars=50,
    )
    assert out.rows[0]["decision_time_utc"] == start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out.rows[-1]["decision_time_utc"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in out.rows:
        assert pd.Timestamp(row["last_5m_close_utc"]) <= pd.Timestamp(row["decision_time_utc"])
        assert row["causality_pass"] is True


def test_single_and_range_agree_on_shared_candle():
    df = _synth_ohlcv(250, seed=5)
    decision = df.iloc[180]["close_time"]
    single = query_trend_direction_at(
        symbol="TEST", timestamp=decision, candles=df, warmup_bars=50, include_htf=False
    )
    start = decision - pd.Timedelta(minutes=30)
    end = decision + pd.Timedelta(minutes=30)
    rng = query_trend_direction_range(
        symbol="TEST",
        start=start,
        end=end,
        step="5m",
        candles=df,
        warmup_bars=50,
    )
    match = [r for r in rng.rows if r["decision_time_utc"] == single.requested_at_utc]
    assert len(match) == 1
    row = match[0]
    assert row["direction"] == single.direction
    assert row["reason"] == single.reason
    assert row["structure_event"] == single.structure_event
    assert row["direction_since_utc"] == single.direction_since_utc


def test_forced_unclear_since_starts_at_challenge():
    df = _synth_ohlcv(200, seed=6)
    struct = run_c34b_on_ohlcv(df).copy()
    for i in range(-4, 0):
        struct.loc[struct.index[i], "major_direction"] = 1
        struct.loc[struct.index[i], "protected_structure_state"] = "bearish_internal_break"
    struct.loc[struct.index[-5], "major_direction"] = 1
    struct.loc[struct.index[-5], "protected_structure_state"] = "bullish_structure"
    decision = struct.iloc[-1]["candle_close_ts"]
    r = decide_from_structure(
        struct,
        decision_time=decision,
        symbol="TEST",
        exchange="bybit",
        timestamp_assumed_utc=False,
        warmup_bars=50,
    )
    assert r.direction == "UNCLEAR"
    assert r.reason == "MAJOR_CHALLENGED:bearish_internal_break"
    assert r.structure_event == "bearish_internal_break"
    expected_since = (
        pd.Timestamp(struct.iloc[-4]["timestamp"]).tz_convert("UTC") + pd.Timedelta(minutes=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert r.direction_since_utc == expected_since


def test_transitions_only_has_no_unchanged_rows():
    df = _synth_ohlcv(220, seed=7)
    start = df.iloc[80]["close_time"]
    end = df.iloc[160]["close_time"]
    full = query_trend_direction_range(
        symbol="TEST", start=start, end=end, step="5m", candles=df, warmup_bars=50
    )
    trans = query_trend_direction_range(
        symbol="TEST",
        start=start,
        end=end,
        step="5m",
        candles=df,
        warmup_bars=50,
        transitions_only=True,
    )
    assert trans.output_rows() == filter_transitions(full.rows)
    prev = None
    for row in trans.output_rows():
        if prev is not None:
            assert any(
                prev[k] != row[k]
                for k in ("direction", "structure_event", "reason", "protected_structure_state")
            )
        prev = row


def test_summary_and_json_csv_stable():
    df = _synth_ohlcv(180, seed=8)
    start = df.iloc[60]["close_time"]
    end = df.iloc[100]["close_time"]
    out = query_trend_direction_range(
        symbol="TEST", start=start, end=end, step="5m", candles=df, warmup_bars=40
    )
    summary = build_summary(out.rows, runtime_seconds=1.23)
    assert summary["total_rows"] == len(out.rows)
    assert summary["bullish_rows"] + summary["bearish_rows"] + summary["unclear_rows"] == len(
        out.rows
    )
    assert "transition_matrix" in summary
    payload = json.loads(json.dumps(out.to_dict(), default=str))
    assert "rows" in payload and "summary" in payload
    assert list(pd.DataFrame(out.rows).columns)[:3] == TIMELINE_COLUMNS[:3]


def test_deterministic_repeat():
    df = _synth_ohlcv(160, seed=9)
    start = df.iloc[70]["close_time"]
    end = df.iloc[110]["close_time"]
    a = query_trend_direction_range(
        symbol="TEST", start=start, end=end, step="5m", candles=df, warmup_bars=40
    )
    b = query_trend_direction_range(
        symbol="TEST", start=start, end=end, step="5m", candles=df, warmup_bars=40
    )
    assert a.rows == b.rows
    assert a.transitions == b.transitions


def test_no_htf_in_default_range_path():
    df = _synth_ohlcv(140, seed=10)
    start = df.iloc[50]["close_time"]
    end = df.iloc[80]["close_time"]
    out = query_trend_direction_range(
        symbol="TEST", start=start, end=end, step="5m", candles=df, warmup_bars=40
    )
    assert "htf_context" not in out.rows[0]
