"""Unit tests for Phase-A strong candle breakout discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ob_microstructure_breakout_bot.calibration.discover_strong_breakouts import (
    DiscoveryParams,
    discover_strong_breakouts,
)
from ob_microstructure_breakout_bot.data.bars import Bar5m


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> Bar5m:
    return Bar5m(ts, o, h, l, c, buy_notional=100.0, sell_notional=50.0, trade_count=10)


def _series_with_long_breakout() -> tuple[list[Bar5m], datetime, datetime, datetime]:
    start = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    # Quiet range first.
    for i in range(40):
        ts = start + timedelta(minutes=5 * i)
        bars.append(_bar(ts, px, px + 0.0002, px - 0.0002, px + 0.00005))
        px += 0.00001
    # Impulse long that breaks prior highs and continues.
    impulse_ts = start + timedelta(minutes=5 * 40)
    bars.append(_bar(impulse_ts, px, px + 0.0040, px - 0.0001, px + 0.0036))
    px = px + 0.0036
    for j in range(1, 10):
        ts = start + timedelta(minutes=5 * (40 + j))
        bars.append(_bar(ts, px, px + 0.0008, px - 0.0002, px + 0.0005))
        px += 0.0005
    scan_from = start + timedelta(minutes=5 * 30)
    scan_to = start + timedelta(minutes=5 * 50)
    return bars, scan_from, scan_to, impulse_ts


def test_discovers_strong_long_breakout():
    bars, scan_from, scan_to, impulse_ts = _series_with_long_breakout()
    cands = discover_strong_breakouts(
        bars,
        start=scan_from,
        end=scan_to,
        params=DiscoveryParams(min_atr_mult=1.5, min_continuation_ret=0.001),
    )
    longs = [c for c in cands if c.direction == "long"]
    assert longs, "expected at least one strong long"
    assert any(c.bar_ts == impulse_ts for c in longs)


def test_no_breakout_in_chop():
    start = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(60):
        ts = start + timedelta(minutes=5 * i)
        # Tiny oscillating bars — no structure break / expansion.
        off = 0.0001 if i % 2 == 0 else -0.0001
        bars.append(_bar(ts, px, px + 0.00015, px - 0.00015, px + off))
        px += off
    cands = discover_strong_breakouts(
        bars,
        start=start + timedelta(hours=1),
        end=start + timedelta(hours=4),
    )
    assert cands == []
