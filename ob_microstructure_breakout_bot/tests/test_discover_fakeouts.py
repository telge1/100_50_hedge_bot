"""Unit tests for Phase-B EMA59 fakeout discovery (long + short)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ob_microstructure_breakout_bot.calibration.discover_fakeouts import (
    FakeoutParams,
    discover_fakeouts,
)
from ob_microstructure_breakout_bot.data.bars import Bar5m


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> Bar5m:
    return Bar5m(ts, o, h, l, c, buy_notional=80.0, sell_notional=70.0, trade_count=8)


def _flat(start: datetime, n: int, px: float) -> list[Bar5m]:
    return [
        _bar(
            start + timedelta(minutes=5 * i),
            px,
            px + 0.00002,
            px - 0.00002,
            px,
        )
        for i in range(n)
    ]


def test_discovers_long_fakeout():
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    px = 0.10
    bars = _flat(start, 220, px)
    t0 = start + timedelta(minutes=5 * 220)
    # Dip below EMA, then touch from below, then dump.
    bars.append(_bar(t0, px, px + 0.00002, px - 0.00050, px - 0.00040))
    touch = t0 + timedelta(minutes=5)
    bars.append(_bar(touch, px - 0.00040, px + 0.00005, px - 0.00045, px - 0.00005))
    px_l = px - 0.00005
    for j in range(1, 8):
        ts = touch + timedelta(minutes=5 * j)
        px_l -= 0.00050
        bars.append(_bar(ts, px_l + 0.00050, px_l + 0.00055, px_l, px_l))

    cands = discover_fakeouts(
        bars,
        start=touch - timedelta(minutes=5),
        end=touch + timedelta(hours=1),
        params=FakeoutParams(min_fail_ret=0.001, only_first_in_cluster=False),
    )
    longs = [c for c in cands if c.direction == "long"]
    assert longs
    assert all(c.fail_reasons for c in longs)


def test_discovers_short_fakeout():
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    px = 0.10
    bars = _flat(start, 220, px)
    t0 = start + timedelta(minutes=5 * 220)
    # Spike above EMA, then touch from above, then squeeze up.
    bars.append(_bar(t0, px, px + 0.00050, px - 0.00002, px + 0.00040))
    touch = t0 + timedelta(minutes=5)
    bars.append(_bar(touch, px + 0.00040, px + 0.00045, px - 0.00005, px + 0.00005))
    px_s = px + 0.00005
    for j in range(1, 8):
        ts = touch + timedelta(minutes=5 * j)
        px_s += 0.00050
        bars.append(_bar(ts, px_s - 0.00050, px_s, px_s - 0.00055, px_s))

    cands = discover_fakeouts(
        bars,
        start=touch - timedelta(minutes=5),
        end=touch + timedelta(hours=1),
        params=FakeoutParams(min_fail_ret=0.001, only_first_in_cluster=False),
    )
    shorts = [c for c in cands if c.direction == "short"]
    assert shorts
    assert all(c.fail_reasons for c in shorts)


def test_excludes_phase_a_strong_timestamps():
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    px = 0.10
    bars = _flat(start, 230, px)
    exclude = {bars[200].ts}
    cands = discover_fakeouts(
        bars,
        start=bars[180].ts,
        end=bars[220].ts,
        exclude_bar_ts=exclude,
    )
    assert all(c.bar_ts not in exclude for c in cands)
