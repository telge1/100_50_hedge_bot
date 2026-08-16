"""Unit tests for UTC HTF aggregation core (fast, no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from signal_generator.timeframes import (
    BucketStatus,
    OhlcvBar,
    aggregate_1m_to_timeframe,
    aggregate_bucket,
    aggregate_strategy_timeframes,
    bar_from_mapping,
    bucket_start,
    inspect_bucket,
    is_bucket_closed,
    is_htf_available_for_signals,
    strategy_timeframes,
    supported_timeframes,
)

BASE = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_strategy_timeframes_match_validated_wave_fade():
    assert strategy_timeframes() == ("15m", "30m", "1h", "4h")
    assert "5m" in supported_timeframes()
    assert "5m" not in strategy_timeframes()


def test_5m_ohlcv_from_exact_five_1m():
    bars = [
        OhlcvBar(BASE + timedelta(minutes=i), BASE + timedelta(minutes=i + 1), *vals)
        for i, vals in enumerate(
            [
                (10.0, 12.0, 9.0, 11.0, 1.0, 10.0),
                (11.0, 13.0, 10.0, 12.0, 2.0, 20.0),
                (12.0, 14.0, 8.0, 13.0, 3.0, 30.0),
                (13.0, 15.0, 11.0, 14.0, 4.0, 40.0),
                (14.0, 16.0, 12.0, 15.0, 5.0, 50.0),
            ]
        )
    ]
    as_of = BASE + timedelta(minutes=5)
    out = aggregate_1m_to_timeframe(bars, "5m", as_of=as_of)
    assert len(out) == 1
    b = out[0]
    assert b.open_time == BASE
    assert b.close_time == BASE + timedelta(minutes=5)
    assert b.open == 10.0
    assert b.high == 16.0
    assert b.low == 8.0
    assert b.close == 15.0
    assert b.volume == 15.0
    assert b.turnover == 150.0
    assert b.available_at == b.close_time


def test_incomplete_5m_not_closed_with_four_minutes():
    bars = [
        OhlcvBar(BASE + timedelta(minutes=i), BASE + timedelta(minutes=i + 1), 1, 1, 1, 1, 1, 1)
        for i in range(4)
    ]
    as_of = BASE + timedelta(minutes=5)
    assert aggregate_1m_to_timeframe(bars, "5m", as_of=as_of) == []
    insp = inspect_bucket(bars, timeframe="5m", bucket_open=BASE, as_of=as_of)
    assert insp.status == BucketStatus.INCOMPLETE
    assert insp.missing_open_times == (BASE + timedelta(minutes=4),)


def test_missing_middle_minute_incomplete_then_complete_after_repair():
    bars = [
        OhlcvBar(
            BASE + timedelta(minutes=i),
            BASE + timedelta(minutes=i + 1),
            1,
            2,
            0.5,
            1.5,
            1,
            1,
        )
        for i in (0, 1, 2, 4)
    ]
    as_of = BASE + timedelta(minutes=5)
    assert aggregate_bucket(bars, timeframe="5m", bucket_open=BASE, as_of=as_of) is None
    repaired = bars + [
        OhlcvBar(
            BASE + timedelta(minutes=3),
            BASE + timedelta(minutes=4),
            1,
            2,
            0.5,
            1.5,
            1,
            1,
        )
    ]
    bar = aggregate_bucket(repaired, timeframe="5m", bucket_open=BASE, as_of=as_of)
    assert bar is not None
    assert bar.open_time == BASE


def test_utc_bucket_boundaries():
    assert bucket_start(datetime(2024, 6, 1, 12, 7, tzinfo=timezone.utc), "5m") == datetime(
        2024, 6, 1, 12, 5, tzinfo=timezone.utc
    )
    assert bucket_start(datetime(2024, 6, 1, 12, 14, tzinfo=timezone.utc), "15m") == datetime(
        2024, 6, 1, 12, 0, tzinfo=timezone.utc
    )
    assert bucket_start(datetime(2024, 6, 1, 12, 29, tzinfo=timezone.utc), "30m") == datetime(
        2024, 6, 1, 12, 0, tzinfo=timezone.utc
    )
    assert bucket_start(datetime(2024, 6, 1, 12, 59, tzinfo=timezone.utc), "1h") == datetime(
        2024, 6, 1, 12, 0, tzinfo=timezone.utc
    )
    assert bucket_start(datetime(2024, 6, 1, 13, 0, tzinfo=timezone.utc), "4h") == datetime(
        2024, 6, 1, 12, 0, tzinfo=timezone.utc
    )


def test_multiple_strategy_timeframes():
    bars = [
        OhlcvBar(BASE + timedelta(minutes=i), BASE + timedelta(minutes=i + 1), 1, 1, 1, 1, 1, 1)
        for i in range(60)
    ]
    as_of = BASE + timedelta(hours=1)
    multi = aggregate_strategy_timeframes(bars, as_of=as_of)
    assert len(multi["15m"]) == 4
    assert len(multi["30m"]) == 2
    assert len(multi["1h"]) == 1
    assert len(multi["4h"]) == 0


def test_unfinished_htf_not_available_for_signals():
    assert not is_htf_available_for_signals(
        bucket_open=BASE,
        timeframe="5m",
        as_of=BASE + timedelta(minutes=4, seconds=59),
    )
    assert is_htf_available_for_signals(
        bucket_open=BASE,
        timeframe="5m",
        as_of=BASE + timedelta(minutes=5),
    )
    bars = [
        OhlcvBar(BASE + timedelta(minutes=i), BASE + timedelta(minutes=i + 1), 1, 1, 1, 1, 1, 1)
        for i in range(5)
    ]
    insp = inspect_bucket(
        bars,
        timeframe="5m",
        bucket_open=BASE,
        as_of=BASE + timedelta(minutes=4, seconds=30),
    )
    assert insp.status == BucketStatus.NOT_CLOSED
    assert (
        aggregate_1m_to_timeframe(bars, "5m", as_of=BASE + timedelta(minutes=4, seconds=30))
        == []
    )


def test_deterministic_same_input_same_output():
    bars = [
        OhlcvBar(
            BASE + timedelta(minutes=i),
            BASE + timedelta(minutes=i + 1),
            i,
            i + 1,
            i - 1,
            i + 0.5,
            1,
            1,
        )
        for i in range(15)
    ]
    as_of = BASE + timedelta(minutes=15)
    a = aggregate_1m_to_timeframe(bars, "15m", as_of=as_of)
    b = aggregate_1m_to_timeframe(list(reversed(bars)), "15m", as_of=as_of)
    assert a == b
    assert len(a) == 1


def test_bar_from_mapping_adapter():
    row = {
        "open_time": BASE,
        "close_time": BASE + timedelta(minutes=1),
        "open": "1.5",
        "high": "2",
        "low": "1",
        "close": "1.8",
        "volume": "9",
        "turnover": "12",
    }
    bar = bar_from_mapping(row)
    assert bar.open == 1.5
    assert bar.turnover == 12.0


def test_15m_closed_semantics_unchanged():
    open_ = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    assert not is_bucket_closed(
        bucket_open=open_,
        as_of=datetime(2024, 6, 1, 10, 14, 59, tzinfo=timezone.utc),
        timeframe="15m",
    )
    assert is_bucket_closed(
        bucket_open=open_,
        as_of=datetime(2024, 6, 1, 10, 15, tzinfo=timezone.utc),
        timeframe="15m",
    )


def test_naive_datetime_treated_as_utc():
    ts = datetime(2024, 6, 1, 10, 7)
    assert bucket_start(ts, "5m") == datetime(2024, 6, 1, 10, 5, tzinfo=timezone.utc)
