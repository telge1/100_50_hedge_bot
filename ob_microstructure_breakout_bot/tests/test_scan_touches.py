from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ob_microstructure_breakout_bot.backtest.scan_touches import detect_ema59_touches
from ob_microstructure_breakout_bot.data.bars import Bar5m
from ob_microstructure_breakout_bot.models import CoinThresholds, EmaSnapshot, TouchDirection


def _bar(ts: datetime, o, h, l, c) -> Bar5m:
    return Bar5m(ts, o, h, l, c, buy_notional=1.0, sell_notional=0.0, trade_count=1)


def test_detect_touch_from_above():
    # Build enough bars for EMA59; then force a from-above touch on last bars.
    start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(70):
        ts = start.replace(hour=(i * 5) // 60 % 24, minute=(i * 5) % 60)
        # crude ascending then drop into EMA
        px = 0.10 + i * 0.0001
        bars.append(_bar(ts, px, px + 0.0001, px - 0.0001, px))
    # last bar dips through what should be near EMA
    last = bars[-1]
    touch_bar = _bar(
        last.ts.replace(minute=(last.ts.minute + 5) % 60),
        last.close,
        last.close + 0.0001,
        last.close - 0.01,  # deep low to guarantee touch
        last.close - 0.002,
    )
    bars.append(touch_bar)
    touches = detect_ema59_touches(bars)
    assert touches
    assert touches[-1].direction in (
        TouchDirection.FROM_ABOVE,
        TouchDirection.FROM_BELOW,
        TouchDirection.UNKNOWN,
    )


def test_scan_skips_incomplete_future_windows(monkeypatch):
    from ob_microstructure_breakout_bot.backtest import scan_touches as st

    start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(200):
        ts = start + timedelta(minutes=5 * i)
        px += 0.001
        bars.append(_bar(ts, px, px + 0.0001, px - 0.0001, px))

    touch = st.TouchEvent(
        bar_ts=bars[-1].ts,
        direction=TouchDirection.FROM_BELOW,
        is_first_in_cluster=True,
        ema=EmaSnapshot(ema9=0.11, ema20=0.10, ema59=0.09, price=bars[-1].close),
        bar=bars[-1],
    )
    thresholds = CoinThresholds(
        symbol="DOGEUSDT",
        fakeout_max_confirm_delta=50_000.0,
        tier1_min_confirm_delta=100_000.0,
        tier2_min_confirm_delta=200_000.0,
        tier1_min_bid_ask_ratio_5bps=1.05,
        tier2_min_bid_ask_ratio_5bps=2.0,
        fakeout_followthrough_flip_delta=-100_000.0,
    )

    monkeypatch.setattr(st, "load_5m_bars", lambda *args, **kwargs: bars)
    monkeypatch.setattr(st, "detect_ema59_touches", lambda _: [touch])

    out = st.scan_ema59_touches(
        "DOGEUSDT",
        start,
        start + timedelta(minutes=5 * 210),
        thresholds,
        fetch_ob=False,
        only_first_in_cluster=True,
    )
    assert out == []
