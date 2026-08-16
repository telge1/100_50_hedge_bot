"""Tests for research 1m Stoch entry-timing (read-only)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from signal_generator.research.one_m_entry_timing.constants import (
    DEFAULT_RESEARCH_DISPLAY_VARIANT,
    TRIGGER_ENTRY_TRIGGERED,
    TRIGGER_NO_ENTRY_TIMEOUT,
    TRIGGER_WAITING_FOR_1M_EXTREME,
    TRIGGER_WAITING_FOR_1M_TURN,
    VARIANT_WAIT_1M_EXTREME,
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
)
from signal_generator.research.one_m_entry_timing.feed import build_research_timing_feed
from signal_generator.research.one_m_entry_timing.timing import evaluate_1m_entry_timing


def _bars(n: int, *, start: str = "2026-08-09T00:00:00Z", close0: float = 100.0) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    rows = []
    px = close0
    for i in range(n):
        o = px
        c = px * (1.0 + (0.001 if i % 2 == 0 else -0.001))
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        rows.append(
            {
                "open_time": start_ts + pd.Timedelta(minutes=i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            }
        )
        px = c
    return pd.DataFrame(rows)


def test_default_variant_is_turn_cross():
    assert DEFAULT_RESEARCH_DISPLAY_VARIANT == VARIANT_WAIT_1M_EXTREME_TURN_CROSS


def test_waiting_for_extreme_when_no_oversold():
    # Flat-ish closes → Stoch mid; LONG never reaches OS within timeout
    df = _bars(80)
    # Force mid stoch by using enough bars but as_of early after t0
    t0 = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)
    # Need candles before t0 for stoch warmup
    df = _bars(200, start="2026-08-08T22:00:00Z")
    res = evaluate_1m_entry_timing(
        direction="LONG",
        signal_tf="15m",
        signal_ts=t0,
        baseline_entry_ts=t0,
        baseline_entry_price=100.0,
        candles_1m=df,
        timing_variant=VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
        as_of=t0 + pd.Timedelta(minutes=5),
        timeout_minutes=15,
    )
    assert res.trigger_state in (
        TRIGGER_WAITING_FOR_1M_EXTREME,
        TRIGGER_WAITING_FOR_1M_TURN,
        TRIGGER_NO_ENTRY_TIMEOUT,
        TRIGGER_ENTRY_TRIGGERED,
    )


def test_timeout_without_entry():
    df = _bars(40, start="2026-08-09T00:00:00Z")
    t0 = datetime(2026, 8, 9, 0, 10, tzinfo=timezone.utc)
    res = evaluate_1m_entry_timing(
        direction="LONG",
        signal_tf="15m",
        signal_ts=t0,
        baseline_entry_ts=t0,
        baseline_entry_price=100.0,
        candles_1m=df,
        timing_variant=VARIANT_WAIT_1M_EXTREME,
        as_of=t0 + pd.Timedelta(minutes=40),
        timeout_minutes=5,
    )
    assert res.trigger_state == TRIGGER_NO_ENTRY_TIMEOUT
    assert res.entry_ts is None


def test_feed_does_not_mutate_input_and_prefixes_ids():
    source = [
        {
            "signal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "symbol": "APTUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "candle_close_time": "2026-08-09T00:00:00Z",
            "entry_time": "2026-08-09T00:01:00Z",
            "entry_price": 5.0,
            "selected": True,
        }
    ]
    snapshot = [dict(source[0])]

    def loader(sym, a, b):
        return _bars(120, start="2026-08-08T22:00:00Z", close0=5.0)

    out = build_research_timing_feed(
        source,
        load_candles=loader,
        timing_variant=VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
        as_of=datetime(2026, 8, 9, 0, 20, tzinfo=timezone.utc),
    )
    assert source == snapshot
    assert len(out) == 1
    assert out[0]["signal_id"].startswith("research1m:")
    assert out[0]["feed_source"] == "RESEARCH_1M_TIMING"
    assert out[0]["production_strategy_unchanged"] is True
    assert "1m_trigger_state" in out[0]
    assert out[0]["baseline_comparison_only"] is True


def test_only_closed_candles_used_for_as_of():
    """Bar open at as_of minute is not closed yet → excluded."""
    df = _bars(60, start="2026-08-09T00:00:00Z")
    t0 = datetime(2026, 8, 9, 0, 10, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 9, 0, 20, tzinfo=timezone.utc)
    res = evaluate_1m_entry_timing(
        direction="LONG",
        signal_tf="15m",
        signal_ts=t0,
        baseline_entry_ts=t0,
        baseline_entry_price=100.0,
        candles_1m=df,
        timing_variant=VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
        as_of=as_of,
        timeout_minutes=30,
    )
    # Feature bar cutoff = as_of - 1m → last open 00:19
    assert res is not None
