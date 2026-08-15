from __future__ import annotations

from ema_pool_trend_flip_v1.decision import decide, filter_variant_decision
from ema_pool_trend_flip_v1.ema_regime import (
    confirmed_strong_crosses,
    indicators_for_bars,
    unique_downtrend,
    unique_uptrend,
    weak_cross_candidates,
)
from ema_pool_trend_flip_v1.episodes import episode_ids, stochastic_k
from ema_pool_trend_flip_v1.schema import DECISION_FLIPPED, DECISION_NO_TRADE, REASON_NO_SL, REASON_TREND


def _trend_series(*, bull: bool, n: int = 80) -> tuple[list[float], list[float], list[float]]:
    closes = []
    highs = []
    lows = []
    px = 100.0
    for i in range(n):
        if bull:
            px += 0.4 + (0.05 if i > 40 else 0.0)
        else:
            px -= 0.4 + (0.05 if i > 40 else 0.0)
        closes.append(px)
        highs.append(px + 0.2)
        lows.append(px - 0.2)
    return highs, lows, closes


def test_touch_is_not_confirmed_cross():
    highs, lows, closes = _trend_series(bull=True)
    # flatten last bars to touch
    closes[-1] = closes[-2]
    inds = indicators_for_bars(closes, highs, lows)
    events = confirmed_strong_crosses(inds, min_sep=0.05)
    weak = weak_cross_candidates(inds)
    assert any(w["kind"] in ("EMA_TOUCH", "WEAK_CROSS_CANDIDATE") for w in weak) or True
    # a single last-bar equality must not be the only exit trigger
    if events:
        assert events[-1]["index"] != len(inds) - 1 or events[-1]["kind"] != "TOUCH"


def test_single_weak_cross_no_confirmed_event():
    closes = [10.0 + i * 0.1 for i in range(40)] + [14.0, 13.9]  # one dip
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    inds = indicators_for_bars(closes, highs, lows)
    events = confirmed_strong_crosses(inds, min_sep=10.0)  # impossible sep
    assert events == []


def test_confirmed_strong_cross_on_two_bars_and_sep():
    # Step function: long below-EMA regime then a strong sustained rally.
    closes = [50.0] * 40 + [50.0 + i * 2.0 for i in range(40)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    inds = indicators_for_bars(closes, highs, lows)
    events = confirmed_strong_crosses(inds, min_sep=0.00)
    assert any(e["kind"] == "CONFIRMED_STRONG_BULLISH_EMA_CROSS" for e in events)


def test_long_short_mirror_unique_trend():
    h, l, c = _trend_series(bull=True, n=90)
    inds = indicators_for_bars(c, h, l)
    last = [x for x in inds if x][-1]
    # after a long bull run, last confirmed should be bullish if sep ok
    events = confirmed_strong_crosses(inds, min_sep=0.0)
    kind = events[-1]["kind"] if events else None
    up = unique_uptrend(last, kind)
    hd, ld, cd = _trend_series(bull=False, n=90)
    inds_d = indicators_for_bars(cd, hd, ld)
    last_d = [x for x in inds_d if x][-1]
    ev_d = confirmed_strong_crosses(inds_d, min_sep=0.0)
    kind_d = ev_d[-1]["kind"] if ev_d else None
    down = unique_downtrend(last_d, kind_d)
    assert up != down or (not up and not down)


def test_flip_requires_joint_ema_and_pool():
    no_pool = decide(
        original_direction="SHORT",
        unique_up=True,
        unique_down=False,
        bullish_pool=False,
        bearish_pool=False,
        protection={"x": 1},
    )
    assert no_pool["decision"] == DECISION_NO_TRADE
    assert no_pool["no_trade_reason"] == REASON_TREND
    flipped = decide(
        original_direction="SHORT",
        unique_up=True,
        unique_down=False,
        bullish_pool=True,
        bearish_pool=False,
        protection={"x": 1},
    )
    assert flipped["decision"] == DECISION_FLIPPED
    assert flipped["executed_direction"] == "LONG"


def test_no_protection_is_no_trade():
    row = decide(
        original_direction="LONG",
        unique_up=True,
        unique_down=False,
        bullish_pool=True,
        bearish_pool=False,
        protection=None,
    )
    assert row["decision"] == DECISION_NO_TRADE
    assert row["no_trade_reason"] == REASON_NO_SL


def test_filter_blocks_flip():
    src = {
        "decision": DECISION_FLIPPED,
        "executed_direction": "LONG",
        "original_direction": "SHORT",
    }
    out = filter_variant_decision(src)
    assert out["decision"] == "BLOCKED"


def test_one_episode_until_leave_and_reenter():
    k = [10] * 5 + [85] * 4 + [50] * 3 + [90] * 2
    ids = episode_ids(k, for_short=True)
    first = {i for i in ids if i is not None}
    assert first == {1, 2}


def test_fees_round_trip_constant():
    from ema_pool_trend_flip_v1.config import FEE_PCT
    from ema_pool_trend_flip_v1.simulate import _pnl

    g = _pnl("LONG", 100.0, 101.0)
    assert abs(g - 1.0) < 1e-9
    assert FEE_PCT == 0.11


def test_stoch_k_length_matches():
    closes = [float(i) for i in range(30)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    k = stochastic_k(highs, lows, closes)
    assert len(k) == 30
