"""Tests for episode-based level events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research.btc_ob_fight.level_events import compute_level_events


def _trade(ts, tid, price, side="Buy"):
    return {
        "ts": ts,
        "trade_id": tid,
        "side": side,
        "price": price,
        "size": 1.0,
        "notional": price,
    }


def test_initial_below_cross_up_down():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    level = 100.0
    trades = [
        _trade(start, "0", 99.0),
        _trade(start + timedelta(seconds=1), "1", 100.5),
        _trade(start + timedelta(seconds=4), "2", 99.5),
    ]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    ep = ev["first_complete_above_episode"]
    assert ep is not None
    assert ep["duration_seconds"] == 3.0
    assert ev["first_return_below_after_cross_up_ts"] == ep["end_ts"]


def test_up_down_up_two_above_episodes():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    level = 100.0
    trades = [
        _trade(start, "0", 99.0),
        _trade(start + timedelta(seconds=1), "1", 101.0),
        _trade(start + timedelta(seconds=3), "2", 99.0),
        _trade(start + timedelta(seconds=5), "3", 101.0),
        _trade(start + timedelta(seconds=8), "4", 99.0),
    ]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    above = [e for e in ev["episodes"] if e["direction"] == "ABOVE"]
    assert len(above) == 2
    assert above[0]["duration_seconds"] == 2.0
    assert above[1]["duration_seconds"] == 3.0
    assert above[1]["duration_seconds"] != above[0]["duration_seconds"]


def test_at_touch_without_cross():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    level = 100.0
    trades = [_trade(start, "0", 100.0), _trade(start + timedelta(seconds=1), "1", 99.0)]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    assert ev["first_touch_ts"] is not None
    assert ev["first_cross_up_ts"] is None


def test_same_timestamp_stable_trade_id_order():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    ts = start + timedelta(seconds=1)
    level = 100.0
    trades = [
        _trade(start, "0", 99.0),
        _trade(ts, "b", 99.5),
        _trade(ts, "a", 100.5),
    ]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    assert ev["first_cross_up_ts"] is not None


def test_incomplete_above_episode():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    level = 100.0
    trades = [_trade(start, "0", 99.0), _trade(start + timedelta(seconds=1), "1", 101.0)]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    assert ev["first_complete_above_episode"] is None
    assert ev["episodes"][-1]["complete"] is False


def test_no_contradictory_first_return_and_cross_down():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    level = 100.0
    trades = [
        _trade(start, "0", 99.0),
        _trade(start + timedelta(seconds=1), "1", 101.0),
        _trade(start + timedelta(seconds=3), "2", 99.0),
        _trade(start + timedelta(seconds=5), "3", 101.0),
    ]
    ev = compute_level_events(
        trades,
        [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}],
        start,
        start + timedelta(minutes=10),
        anchor=start,
    )[0]
    ret = ev["first_return_below_after_cross_up_ts"]
    assert ret == ev["first_complete_above_episode"]["end_ts"]
