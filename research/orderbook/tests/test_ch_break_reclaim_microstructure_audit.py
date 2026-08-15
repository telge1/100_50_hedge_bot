"""Unit tests for causal CH break/reclaim microstructure audit helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research.orderbook.ch_break_reclaim_microstructure_audit.features import (
    SimpleTrade,
    assert_causal_cutoff,
    bps_distance,
    build_observation_schedule,
    depth_in_level_band,
    derive_touch_break_from_trades,
    direction_context,
    filter_trades_causal,
    imbalance,
    signed_break_flow,
    aggregate_trade_flow,
    depth_change,
)
from research.orderbook.ch_break_reclaim_microstructure_audit.outcomes import map_outcome_label


UTC = timezone.utc


class FakeBook:
    def __init__(self, bids: dict, asks: dict):
        self.bids = {Decimal(str(k)): Decimal(str(v)) for k, v in bids.items()}
        self.asks = {Decimal(str(k)): Decimal(str(v)) for k, v in asks.items()}
        self.has_snapshot = True

    def mid_price(self):
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None


def test_outcome_mapping_break_and_reclaim_fast():
    assert map_outcome_label("BREAKDOWN_CONFIRMED")["outcome_label"] == "BREAK_ACCEPTED"
    assert map_outcome_label("BREAKOUT_CONFIRMED")["outcome_label"] == "BREAK_ACCEPTED"
    m = map_outcome_label("RECLAIM_CONFIRMED", reclaim_minutes=5)
    assert m["outcome_label"] == "RECLAIM_FAST"
    m2 = map_outcome_label("RECLAIM_DOWN_CONFIRMED", reclaim_minutes=40)
    assert m2["outcome_label"] == "RECLAIM_SLOW"
    assert map_outcome_label("UNRESOLVED_WITHIN_MAX_WINDOW")["outcome_label"] == "HOLD_NO_BREAK"
    assert map_outcome_label("EVENT_DATA_INVALID")["outcome_label"] == "EXCLUDED"
    assert m["uses_future_info"] is True


def test_direction_normalization_signed_flow():
    # bearish: sells positive
    assert signed_break_flow(buy_notional=10, sell_notional=40, break_direction="bearish") == 30
    # bullish: buys positive
    assert signed_break_flow(buy_notional=40, sell_notional=10, break_direction="bullish") == 30
    ctx_l = direction_context("protected_low")
    ctx_h = direction_context("protected_high")
    assert ctx_l.break_direction == "bearish" and ctx_l.support_side == "bid"
    assert ctx_h.break_direction == "bullish" and ctx_h.support_side == "ask"


def test_bps_distance_and_depth_bands():
    level = 100.0
    assert bps_distance(99.9, level) == pytest.approx(-10.0)
    bids = {99.95: 10, 99.5: 5, 98.0: 1}  # 5bps, 50bps, 200bps
    # 0-5 bps: only 99.95
    d = depth_in_level_band(bids, side="bid", level=level, lo_bps=0, hi_bps=5)
    assert d == pytest.approx(99.95 * 10)
    d2 = depth_in_level_band(bids, side="bid", level=level, lo_bps=5, hi_bps=10)
    assert d2 == 0.0
    d3 = depth_in_level_band(bids, side="bid", level=level, lo_bps=25, hi_bps=50)
    # 99.5 is 50 bps exactly — hi exclusive → 0
    assert d3 == 0.0
    d4 = depth_in_level_band(bids, side="bid", level=level, lo_bps=40, hi_bps=60)
    assert d4 == pytest.approx(99.5 * 5)


def test_imbalance_and_wall_pull_change():
    assert imbalance(70, 30) == pytest.approx(0.7)
    assert imbalance(0, 0) is None
    assert depth_change(80, 100) == -20
    assert depth_change(None, 100) is None


def test_causal_cutoff_assertion_and_filter():
    t0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    trades = [
        SimpleTrade(t0 - timedelta(seconds=5), "Sell", 1.0, 1, 1.0),
        SimpleTrade(t0 + timedelta(seconds=1), "Buy", 1.0, 1, 1.0),
    ]
    kept = filter_trades_causal(trades, cutoff=t0)
    assert len(kept) == 1
    assert kept[0].side == "Sell"
    with pytest.raises(AssertionError):
        assert_causal_cutoff([{"ts": t0 + timedelta(seconds=1)}], cutoff=t0)


def test_trade_flow_aggregation_windows():
    t0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    trades = [
        SimpleTrade(t0 - timedelta(seconds=3), "Sell", 10.0, 2, 20.0),
        SimpleTrade(t0 - timedelta(seconds=8), "Buy", 10.0, 1, 10.0),
        SimpleTrade(t0 - timedelta(seconds=40), "Sell", 10.0, 5, 50.0),
    ]
    f5 = aggregate_trade_flow(trades, cutoff=t0, window_s=5, break_direction="bearish")
    assert f5["flow_5s_n_trades"] == 1
    assert f5["flow_5s_signed_break"] == pytest.approx(20.0)
    f30 = aggregate_trade_flow(trades, cutoff=t0, window_s=30, break_direction="bearish")
    assert f30["flow_30s_n_trades"] == 2
    # sell 20 - buy 10 → signed bearish = +10
    assert f30["flow_30s_signed_break"] == pytest.approx(10.0)


def test_first_touch_and_break_alignment_from_trades():
    t0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    level = 1.0
    trades = [
        SimpleTrade(t0 + timedelta(seconds=1), "Sell", 1.0004, 1, 1.0),  # 4 bps — touch
        SimpleTrade(t0 + timedelta(seconds=5), "Sell", 0.999, 1, 1.0),  # break
        SimpleTrade(t0 + timedelta(seconds=8), "Buy", 1.01, 1, 1.0),
    ]
    d = derive_touch_break_from_trades(
        trades,
        level=level,
        break_direction="bearish",
        window_start=t0,
        window_end=t0 + timedelta(seconds=60),
        touch_bps=5,
    )
    assert d["first_touch_ts"] == trades[0].trade_ts
    assert d["first_break_ts"] == trades[1].trade_ts


def test_observation_schedule_no_fake_post_break_without_break():
    touch = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    sched = build_observation_schedule(first_touch=touch, first_break=None)
    assert any(s["timepoint"] == "FIRST_TOUCH" for s in sched)
    assert not any(s["timepoint"].startswith("BREAK_") or s["timepoint"] == "FIRST_BREAK" for s in sched)

    sched2 = build_observation_schedule(first_touch=touch, first_break=touch + timedelta(seconds=20))
    names = {s["timepoint"] for s in sched2}
    assert "FIRST_BREAK" in names
    assert "BREAK_PLUS_10S" in names
    assert "POSTMORTEM_PLUS_5M" in names
    # postmortem not early
    pm = next(s for s in sched2 if s["timepoint"] == "POSTMORTEM_PLUS_5M")
    assert pm["is_early_signal_candidate"] is False


def test_missing_window_handling_empty_flow():
    t0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    f = aggregate_trade_flow([], cutoff=t0, window_s=10, break_direction="bullish")
    assert f["flow_10s_n_trades"] == 0
    assert f["flow_10s_signed_break"] == 0.0


def test_refill_proxy_positive_depth_change():
    assert depth_change(120, 80) == 40  # refill
    assert depth_change(50, 90) == -40  # pull
