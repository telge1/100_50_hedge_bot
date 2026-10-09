"""Synthetic checks for the short breakout blocker. No market data."""

from datetime import datetime, timedelta, timezone

from short_breakout_regime.short_block import PoolMark, ShortBreakoutContext, should_block_short


def _uptrend_context() -> ShortBreakoutContext:
    start = datetime(2026, 9, 20, 6, tzinfo=timezone.utc)
    breakout = start + timedelta(hours=4)
    return ShortBreakoutContext(
        price=0.100,
        regime="bullish",
        h1_label="bullish",
        h4_label="bullish",
        breakout_ts=breakout,
        breakout_base=0.086,
        ema9_held_since_breakout=True,
        ema20_held_since_breakout=True,
        crossed_from_below=True,
        pools=(
            PoolMark(0.0840, 0.0850, 4.0, start),
            PoolMark(0.0780, 0.0800, 20.0, start),
            PoolMark(0.0920, 0.0930, 3.0, breakout + timedelta(hours=2)),
            PoolMark(0.1040, 0.1080, 30.0, start),
        ),
    )


def test_uptrend_breakout_blocks_short() -> None:
    decision = should_block_short(_uptrend_context())
    assert decision.block_short is True
    assert decision.reason == "ema_bands_and_thick_pools_above"
    assert all(decision.checks.values())


def test_two_pools_still_count_the_wide_one() -> None:
    ctx = _uptrend_context()
    pair = ShortBreakoutContext(
        price=0.093,
        regime=ctx.regime,
        h1_label=ctx.h1_label,
        h4_label=ctx.h4_label,
        breakout_ts=ctx.breakout_ts,
        breakout_base=ctx.breakout_base,
        ema9_held_since_breakout=True,
        ema20_held_since_breakout=True,
        crossed_from_below=True,
        pools=(
            PoolMark(0.10085, 0.11016, 0.0, ctx.breakout_ts - timedelta(days=1)),
            PoolMark(0.09539, 0.09945, 0.0, ctx.breakout_ts - timedelta(days=1)),
        ),
    )
    decision = should_block_short(pair)
    assert decision.checks["thick_pools_above"] is True
    assert decision.block_short is True


def test_bearish_regime_does_not_block() -> None:
    ctx = _uptrend_context()
    bearish = ShortBreakoutContext(
        price=ctx.price,
        regime="bearish",
        h1_label="bearish",
        h4_label="bearish",
        breakout_ts=ctx.breakout_ts,
        breakout_base=ctx.breakout_base,
        ema9_held_since_breakout=False,
        ema20_held_since_breakout=False,
        crossed_from_below=False,
        pools=ctx.pools,
    )
    decision = should_block_short(bearish)
    assert decision.block_short is False
    assert decision.checks["ema_bands"] is False


def test_launch_keeps_the_old_shelf() -> None:
    from short_breakout_regime.replay import OhlcBar, TimedPool, context_at

    start = datetime(2026, 9, 20, 0, tzinfo=timezone.utc)
    bars = []
    ema9: list[float | None] = []
    ema200: list[float | None] = []
    for hour in range(19):
        ts = start + timedelta(hours=hour)
        if hour < 11:
            close = 0.0850
            ema = 0.0860
            slow = 0.0860
        elif hour == 11:
            close = 0.0876
            ema = 0.0860
            slow = 0.0840
        else:
            close = 0.0900 + (hour - 12) * 0.001
            ema = 0.0865
            slow = 0.0840
        bars.append(OhlcBar(ts, close, close, close, close))
        ema9.append(ema)
        ema200.append(slow)
    born = start
    pools = [
        TimedPool(0.0840, 0.0846, 0.5, born),
        TimedPool(0.0800, 0.0810, 1.0, born),
        TimedPool(0.1040, 0.1080, 0.0, born),
        TimedPool(0.0950, 0.0960, 2.0, start + timedelta(hours=15)),
    ]
    decision = start + timedelta(hours=18)
    ctx = context_at(
        at=decision,
        h1=bars,
        h1_ema9=ema9,
        h1_ema20=ema9,
        h1_ema59=ema9,
        h1_ema200=ema200,
        h1_label="bullish",
        h4_label="bullish",
        pools=pools,
        bar_hours=1,
    )
    assert ctx is not None
    assert ctx.breakout_ts == start + timedelta(hours=11)
    decision_row = should_block_short(ctx)
    assert decision_row.block_short is True


def test_poke_above_ema200_is_not_a_band_cross() -> None:
    from short_breakout_regime.replay import OhlcBar, TimedPool, context_at

    start = datetime(2026, 8, 12, 0, tzinfo=timezone.utc)
    # Close under EMA200, then a close that only nicks it while EMA9 lags below.
    closes = (0.0850, 0.0862)
    ema9 = (0.0800, 0.0810)
    ema20 = (0.0790, 0.0795)
    ema59 = (0.0780, 0.0780)
    ema200 = (0.0860, 0.0860)
    bars = [OhlcBar(start + timedelta(hours=i), c, c, c, c) for i, c in enumerate(closes)]
    ctx = context_at(
        at=start + timedelta(hours=2),
        h1=bars,
        h1_ema9=list(ema9),
        h1_ema20=list(ema20),
        h1_ema59=list(ema59),
        h1_ema200=list(ema200),
        h1_label="neutral",
        h4_label="neutral",
        pools=(TimedPool(0.090, 0.095, 0.0, start),),
        bar_hours=1,
    )
    assert ctx is not None
    assert ctx.crossed_from_below is False


def test_close_that_clears_the_band_gap_blocks() -> None:
    from short_breakout_regime.replay import OhlcBar, TimedPool, context_at

    start = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    closes = (0.0700, 0.0920)
    ema9 = (0.0800, 0.0820)
    ema20 = (0.0790, 0.0800)
    ema59 = (0.0780, 0.0790)
    ema200 = (0.0860, 0.0860)
    bars = [OhlcBar(start + timedelta(hours=i), c, c, c, c) for i, c in enumerate(closes)]
    ctx = context_at(
        at=start + timedelta(hours=2),
        h1=bars,
        h1_ema9=list(ema9),
        h1_ema20=list(ema20),
        h1_ema59=list(ema59),
        h1_ema200=list(ema200),
        h1_label="neutral",
        h4_label="neutral",
        pools=(
            TimedPool(0.100, 0.110, 0.0, start),
            TimedPool(0.093, 0.094, 0.0, start),
            TimedPool(0.095, 0.096, 0.0, start),
        ),
        bar_hours=1,
    )
    assert ctx is not None
    assert ctx.breakout_ts == start + timedelta(hours=1)
    assert should_block_short(ctx).block_short is True


def test_dip_under_ema9_keeps_the_cross_while_above_ema200() -> None:
    from short_breakout_regime.replay import OhlcBar, context_at

    start = datetime(2026, 9, 18, 0, tzinfo=timezone.utc)
    # Armed under EMA200, clear the band, then a close under EMA9 that stays above EMA200.
    rows = (
        (0.0800, 0.0810, 0.0805, 0.0790, 0.0840),
        (0.0900, 0.0820, 0.0810, 0.0800, 0.0840),
        (0.0850, 0.0860, 0.0820, 0.0805, 0.0840),
    )
    bars = []
    ema9, ema20, ema59, ema200 = [], [], [], []
    for hour, (close, e9, e20, e59, e200) in enumerate(rows):
        bars.append(OhlcBar(start + timedelta(hours=hour), close, close, close, close))
        ema9.append(e9)
        ema20.append(e20)
        ema59.append(e59)
        ema200.append(e200)
    ctx = context_at(
        at=start + timedelta(hours=3),
        h1=bars,
        h1_ema9=ema9,
        h1_ema20=ema20,
        h1_ema59=ema59,
        h1_ema200=ema200,
        h1_label="bullish",
        h4_label="bullish",
        pools=(),
        bar_hours=1,
    )
    assert ctx is not None
    assert ctx.crossed_from_below is True
    assert ctx.breakout_ts == start + timedelta(hours=1)


def test_ema9_reclaim_without_ema200_does_not_block() -> None:
    from short_breakout_regime.replay import OhlcBar, TimedPool, context_at

    start = datetime(2026, 8, 24, 0, tzinfo=timezone.utc)
    bars = []
    ema9: list[float | None] = []
    ema20: list[float | None] = []
    slow: list[float | None] = []
    for hour, close in enumerate((0.09117, 0.09133, 0.09220, 0.09117)):
        bars.append(OhlcBar(start + timedelta(hours=hour), close, close, close, close))
        ema9.append(0.0915)
        ema20.append(0.0890)
        slow.append(0.0750)
    # Price is already above EMA200. The dip is only through EMA9.
    bars[0] = OhlcBar(start, 0.0910, 0.0910, 0.0910, 0.0910)
    ctx = context_at(
        at=start + timedelta(hours=4),
        h1=bars,
        h1_ema9=ema9,
        h1_ema20=ema20,
        h1_ema59=slow,
        h1_ema200=slow,
        h1_label="bullish",
        h4_label="bullish",
        pools=(TimedPool(0.080, 0.081, 1.0, start),),
        bar_hours=1,
    )
    assert ctx is not None
    assert ctx.crossed_from_below is False
    assert should_block_short(ctx).block_short is False


def test_block_ranges_collapse_a_stretch() -> None:
    from datetime import timedelta

    from short_breakout_regime.replay import BlockSample, block_ranges

    t0 = datetime(2026, 9, 21, 8, tzinfo=timezone.utc)
    samples = [
        BlockSample(t0, False, "missing", 0.09),
        BlockSample(t0 + timedelta(hours=1), True, "bullish_breakout_structure", 0.10),
        BlockSample(t0 + timedelta(hours=2), True, "bullish_breakout_structure", 0.101),
        BlockSample(t0 + timedelta(hours=3), False, "bearish_regime", 0.099),
    ]
    ranges = block_ranges(samples)
    assert len(ranges) == 1
    assert ranges[0].start == t0 + timedelta(hours=1)
    assert ranges[0].end == t0 + timedelta(hours=2)


if __name__ == "__main__":
    test_uptrend_breakout_blocks_short()
    test_two_pools_still_count_the_wide_one()
    test_bearish_regime_does_not_block()
    test_launch_keeps_the_old_shelf()
    test_poke_above_ema200_is_not_a_band_cross()
    test_close_that_clears_the_band_gap_blocks()
    test_dip_under_ema9_keeps_the_cross_while_above_ema200()
    test_ema9_reclaim_without_ema200_does_not_block()
    test_block_ranges_collapse_a_stretch()
    print("ok")
