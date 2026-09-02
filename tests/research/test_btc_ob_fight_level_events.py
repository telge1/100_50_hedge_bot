"""Tests for geometric level events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research.btc_ob_fight.level_events import compute_level_events


def _mk_trades(prices, start):
    out = []
    for i, p in enumerate(prices):
        out.append(
            {
                "ts": start + timedelta(seconds=i),
                "trade_id": str(i),
                "side": "Buy" if p >= prices[max(0, i - 1)] else "Sell",
                "price": p,
                "size": 1.0,
                "notional": p,
            }
        )
    return out


def test_cross_up_and_return_below():
    level = 100.0
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    trades = _mk_trades([99.0, 99.5, 100.5, 101.0, 99.5], start)
    levels = [{"level_id": "tpo_vah", "label": "TPO-VAH", "price": level}]
    end = start + timedelta(minutes=10)
    ev = compute_level_events(trades, levels, start, end, anchor=start)[0]
    assert ev["first_cross_up_ts"] is not None
    ep = ev["first_complete_above_episode"]
    assert ep is not None
    assert ev["first_return_below_after_cross_up_ts"] == ep["end_ts"]
    assert ep["duration_seconds"] == 2.0


def test_cross_down_and_return_above():
    level = 100.0
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    trades = _mk_trades([101.0, 100.5, 99.5, 98.0, 100.5], start)
    levels = [{"level_id": "tpo_val", "label": "TPO-VAL", "price": level}]
    ev = compute_level_events(trades, levels, start, start + timedelta(minutes=10), anchor=start)[0]
    assert ev["first_cross_down_ts"] is not None
    ep = ev["first_complete_below_episode"]
    assert ep is not None
    assert ev["first_return_above_after_cross_down_ts"] == ep["end_ts"]


def test_multiple_levels_all_tracked():
    start = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    trades = _mk_trades([90, 95, 100, 105], start)
    levels = [
        {"level_id": "tpo_val", "label": "VAL", "price": 95.0},
        {"level_id": "tpo_vah", "label": "VAH", "price": 100.0},
    ]
    evs = compute_level_events(trades, levels, start, start + timedelta(minutes=10), anchor=start)
    assert len(evs) == 2
    assert any(e["first_cross_up_ts"] for e in evs)
