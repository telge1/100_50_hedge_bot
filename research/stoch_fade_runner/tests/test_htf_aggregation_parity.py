from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.stoch_fade_runner.config import ensure_sg_on_path
from research.stoch_fade_runner.htf import aggregate_1m_to_timeframe as grouped_agg

TFS = ("15m", "30m", "1h", "4h")


def _bar(ot: datetime, *, open_=1.0, high=1.1, low=0.9, close=1.05, volume=1.0, turnover=0.0):
    ensure_sg_on_path()
    from signal_generator.timeframes import OhlcvBar

    return OhlcvBar(
        open_time=ot,
        close_time=ot + timedelta(minutes=1),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        turnover=turnover,
    )


def _seq(start: datetime, n: int, **kwargs):
    return [_bar(start + timedelta(minutes=i), **kwargs) for i in range(n)]


def _compare(slow, fast) -> None:
    assert len(slow) == len(fast)
    for a, b in zip(slow, fast, strict=True):
        assert a.open_time == b.open_time
        assert a.close_time == b.close_time
        assert a.open == b.open
        assert a.high == b.high
        assert a.low == b.low
        assert a.close == b.close
        assert a.volume == b.volume
        assert a.turnover == b.turnover
        assert type(a.open_time) is type(b.open_time)


def _both(bars, as_of=None):
    ensure_sg_on_path()
    from signal_generator.timeframes import aggregate_1m_to_timeframe as sg_agg

    if as_of is None and bars:
        as_of = bars[-1].close_time
    out = {}
    for tf in TFS:
        slow = sg_agg(bars, tf, as_of=as_of, require_complete=True)
        fast = grouped_agg(bars, tf, as_of=as_of, require_complete=True)
        _compare(slow, fast)
        out[tf] = len(slow)
    return out


def test_normal_windows_and_4h_buckets() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    counts = _both(_seq(start, 240 * 3))
    assert counts["4h"] == 3
    assert counts["1h"] == 12
    assert counts["30m"] == 24
    assert counts["15m"] == 48


def test_utc_day_month_year_boundaries() -> None:
    for start in (
        datetime(2025, 12, 31, 22, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 31, 22, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 28, 22, 0, tzinfo=timezone.utc),
    ):
        _both(_seq(start, 240 * 2 + 15))


def test_exact_complete_and_one_short() -> None:
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    _both(_seq(start, 240))
    _both(_seq(start, 239))


def test_incomplete_last_bucket() -> None:
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    bars = _seq(start, 240 + 10)
    as_of = bars[-1].close_time
    _both(bars, as_of=as_of)


def test_start_mid_bucket_and_end_on_after_boundary() -> None:
    start = datetime(2026, 3, 1, 0, 7, tzinfo=timezone.utc)
    _both(_seq(start, 240))
    aligned = datetime(2026, 3, 1, tzinfo=timezone.utc)
    exact_end = _seq(aligned, 240)
    _both(exact_end, as_of=exact_end[-1].close_time)
    plus_one = _seq(aligned, 241)
    _both(plus_one, as_of=plus_one[-1].close_time)


def test_duplicate_timestamp_matches_original_typeerror() -> None:
    ensure_sg_on_path()
    from signal_generator.timeframes import aggregate_1m_to_timeframe as sg_agg

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    bars = _seq(start, 240)
    dup = _bar(start + timedelta(minutes=5), open_=9.0, high=9.2, low=8.8, close=9.1, volume=99.0)
    mixed = bars + [dup]
    as_of = mixed[-1].close_time
    with pytest.raises(TypeError):
        sg_agg(mixed, "15m", as_of=as_of, require_complete=True)
    with pytest.raises(TypeError):
        grouped_agg(mixed, "15m", as_of=as_of, require_complete=True)


def test_gap_and_missing_bucket_edges() -> None:
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    bars = _seq(start, 240)
    gapped = [b for b in bars if b.open_time != start + timedelta(minutes=30)]
    _both(gapped)
    _both(bars[1:])
    _both(bars[:-1])


def test_unsorted_input() -> None:
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    bars = _seq(start, 240)
    shuffled = list(reversed(bars))
    _both(shuffled)


def test_empty() -> None:
    _both([])
