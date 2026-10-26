"""Phase 4E — pending limit order parity tests."""

from __future__ import annotations

from pool_zone_tracker import LifecycleIndex, PoolAnchor, anchor_from_signal, depth2_matches_at_bar
from pending_limit_simulator import simulate_user_pending_limit


def test_pending_persists_without_time_limit():
    lifecycle = LifecycleIndex()
    anchor = PoolAnchor("upper", 0.1, 0.11, frozenset({"o1", "o2"}))
    prices = {}
    for i in range(20):
        t = f"2026-09-11T12:{i:02d}:00Z".replace("T12:24", "T12:24").replace("T12:25", "T12:25")
        if i < 10:
            t = f"2026-09-11T12:{5 + i:02d}:00Z"
        else:
            t = f"2026-09-11T13:{i - 10:02d}:00Z"
        prices[t] = {"o": 0.12, "h": 0.12, "l": 0.12, "c": 0.12}
    # No lifecycle events → zone stays None; last bar touches limit
    prices["2026-09-11T13:09:00Z"] = {"o": 0.105, "h": 0.11, "l": 0.09, "c": 0.1}
    r = simulate_user_pending_limit(
        "SHORT",
        0.1,
        0.1,
        0.11,
        "o1|o2",
        "2026-09-11T12:05:00Z",
        prices,
        "2026-09-11T13:09:00Z",
        lifecycle,
    )
    assert r.bars_waited > 5


def test_cancel_on_disappeared_without_continuation():
    lifecycle = LifecycleIndex()
    bar = "2026-09-11T12:05:00Z"
    lifecycle.by_bar[bar] = [
        {
            "side": "upper",
            "event_type": "POOL_DISAPPEARED",
            "bottom_price": "0.083678",
            "top_price": "0.08369",
            "depth": "2",
            "origins_json": '["lldv2:DOGEUSDT:5m:upper:1789127100"]',
            "identity_confidence": "HIGH",
            "causal_as_of_utc": bar,
        }
    ]
    prices = {
        "2026-09-11T12:05:00Z": {"o": 0.084, "h": 0.084, "l": 0.0841, "c": 0.084},
    }
    r = simulate_user_pending_limit(
        "SHORT",
        0.083678,
        0.083678,
        0.08369,
        "lldv2:DOGEUSDT:5m:upper:1789127100",
        "2026-09-11T12:05:00Z",
        prices,
        "2026-09-11T12:05:00Z",
        lifecycle,
    )
    assert r.status == "ORDER_CANCELLED_POOL_DEACTIVATED"


def test_continued_depth2_not_cancelled_on_density_event_only():
    lifecycle = LifecycleIndex()
    bar = "2026-09-11T12:10:00Z"
    lifecycle.by_bar[bar] = [
        {
            "side": "upper",
            "event_type": "POOL_CONTINUED",
            "bottom_price": "0.083678",
            "top_price": "0.08369",
            "depth": "2",
            "origins_json": '["o1","o2"]',
            "identity_confidence": "HIGH",
            "causal_as_of_utc": bar,
        }
    ]
    anchor = anchor_from_signal("SHORT", 0.083678, 0.08369, "o1|o2")
    assert len(depth2_matches_at_bar(anchor, bar, lifecycle)) == 1


def test_ambiguous_same_bar_touch_and_disappear():
    lifecycle = LifecycleIndex()
    bar = "2026-09-11T12:15:00Z"
    lifecycle.by_bar[bar] = [
        {
            "side": "upper",
            "event_type": "POOL_DISAPPEARED",
            "bottom_price": "0.084",
            "top_price": "0.085",
            "depth": "2",
            "origins_json": '["o1"]',
            "identity_confidence": "HIGH",
            "causal_as_of_utc": bar,
        }
    ]
    prices = {bar: {"o": 0.084, "h": 0.086, "l": 0.083, "c": 0.084}}
    r = simulate_user_pending_limit(
        "SHORT",
        0.084,
        0.084,
        0.085,
        "o1",
        bar,
        prices,
        bar,
        lifecycle,
    )
    assert r.status == "AMBIGUOUS_POOL_FILL_SEQUENCE"
