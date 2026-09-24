"""Unit tests for Phase-C threshold calibration helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from ob_microstructure_breakout_bot.calibration.calibrate_thresholds import (
    LabeledEvent,
    evaluate,
    walk_forward_split,
)
from ob_microstructure_breakout_bot.models import ObBandSnapshot, TradeWindowStats
from ob_microstructure_breakout_bot.thresholds import make_thresholds


def _ev(label: str, direction: str, confirm: float, ft: float, ratio: float) -> LabeledEvent:
    if direction == "long":
        bid, ask = ratio, 1.0
    else:
        # ask/bid = ratio  => ask = ratio * bid
        bid, ask = 1.0, ratio
    return LabeledEvent(
        symbol="DOGEUSDT",
        label=label,
        direction=direction,
        bar_ts=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
        confirm=TradeWindowStats(
            buy_notional=max(confirm, 0) + 100_000,
            sell_notional=100_000 - min(confirm, 0),
        ),
        followthrough=TradeWindowStats(
            buy_notional=max(ft, 0) + 50_000,
            sell_notional=50_000 - min(ft, 0),
        ),
        ob=ObBandSnapshot(bid_5bps=bid * 10_000, ask_5bps=ask * 10_000, bid_10bps=0, ask_10bps=0),
        ema=None,
    )


def test_evaluate_separates_long_and_short():
    th = make_thresholds(
        symbol="DOGEUSDT",
        fakeout_max_confirm_delta=50_000,
        tier1_min_confirm_delta=100_000,
        tier2_min_confirm_delta=200_000,
        tier1_min_bid_ask_ratio_5bps=1.05,
        tier2_min_bid_ask_ratio_5bps=2.0,
        fakeout_followthrough_flip_delta=-100_000,
    )
    events = [
        _ev("strong_breakout", "long", 250_000, 50_000, 2.5),
        _ev("fakeout", "long", 20_000, -150_000, 0.8),
        _ev("strong_breakout", "short", -250_000, -50_000, 2.5),
        _ev("fakeout", "short", -20_000, 150_000, 0.8),
    ]
    # Fix deltas precisely via buy/sell
    events[0] = LabeledEvent(
        **{
            **events[0].__dict__,
            "confirm": TradeWindowStats(300_000, 50_000),
            "followthrough": TradeWindowStats(80_000, 30_000),
        }
    )
    events[1] = LabeledEvent(
        **{
            **events[1].__dict__,
            "confirm": TradeWindowStats(60_000, 40_000),
            "followthrough": TradeWindowStats(20_000, 200_000),
        }
    )
    events[2] = LabeledEvent(
        **{
            **events[2].__dict__,
            "confirm": TradeWindowStats(50_000, 300_000),
            "followthrough": TradeWindowStats(30_000, 80_000),
        }
    )
    events[3] = LabeledEvent(
        **{
            **events[3].__dict__,
            "confirm": TradeWindowStats(40_000, 60_000),
            "followthrough": TradeWindowStats(200_000, 20_000),
        }
    )
    res = evaluate(th, events)
    assert res.long.n_strong == 1
    assert res.short.n_strong == 1
    assert res.long.strong_recall == 1.0
    assert res.short.strong_recall == 1.0
    assert res.long.fake_reject == 1.0
    assert res.short.fake_reject == 1.0


def test_walk_forward_split_is_chronological():
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    events = []
    for i in range(10):
        ev = _ev("fakeout", "long", 10_000, -10_000, 1.0)
        events.append(
            LabeledEvent(**{**ev.__dict__, "bar_ts": base.replace(day=1 + i)})
        )
    train, test = walk_forward_split(events, train_frac=0.7)
    assert len(train) == 7
    assert len(test) == 3
    assert train[-1].bar_ts < test[0].bar_ts
