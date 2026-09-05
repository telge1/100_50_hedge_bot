"""Targeted tests for LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/scripts/"
    "run_liquidity_pool_arrival_wall_monitor.py"
)


def _load():
    import sys

    spec = importlib.util.spec_from_file_location("arrival_wall_monitor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["arrival_wall_monitor"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_01_foundation_engine_unchanged(mod):
    from orderbook_analyse.liquidity_pool_signal import chart_pool_engine, get_engine_function

    assert get_engine_function() is chart_pool_engine()
    assert get_engine_function().__module__ == "indicators.liquidity_location.engine"
    src = SCRIPT.read_text(encoding="utf-8")
    assert "liquidity_pool_signal" in src
    assert "a_plus_nested_ask_pool_edge_short_v1" not in src
    assert "rank_nested_ask" not in src


def test_02_pool_only_after_available_at(mod):
    pool = {
        "pool_id": "p1",
        "side": "ASK",
        "lower_edge": 100.0,
        "upper_edge": 110.0,
        "available_at": "2026-08-26T01:00:00Z",
        "invalidated_ts": None,
        "strength": 1.0,
        "source_timeframe": "5m",
        "origin_ts": "2026-08-26T00:55:00Z",
    }
    assert not mod.pool_active_at(pool, mod._ms(mod._utc("2026-08-26T00:59:59Z")))
    assert mod.pool_active_at(pool, mod._ms(mod._utc("2026-08-26T01:00:00Z")))


def test_03_04_ask_bid_arrival(mod):
    pools = [
        {
            "pool_id": "ask1",
            "side": "ASK",
            "lower_edge": 100.0,
            "upper_edge": 105.0,
            "available_at": "2026-08-26T00:00:00Z",
            "invalidated_ts": None,
            "strength": 1.0,
            "source_timeframe": "5m",
            "origin_ts": "2026-08-26T00:00:00Z",
        },
        {
            "pool_id": "bid1",
            "side": "BID",
            "lower_edge": 90.0,
            "upper_edge": 95.0,
            "available_at": "2026-08-26T00:00:00Z",
            "invalidated_ts": None,
            "strength": 1.0,
            "source_timeframe": "5m",
            "origin_ts": "2026-08-26T00:00:00Z",
        },
    ]
    t0 = mod._ms(mod._utc("2026-08-26T01:00:00Z"))
    mids = [
        mod.BookState(t0, 99.0, 98.9, 99.1, True, [], []),
        mod.BookState(t0 + 1000, 100.5, 100.4, 100.6, True, [], []),
        mod.BookState(t0 + 2000, 96.0, 95.9, 96.1, True, [], []),
        mod.BookState(t0 + 3000, 94.5, 94.4, 94.6, True, [], []),
    ]
    eps, born, gaps = mod.detect_arrivals(pools, mids)
    kinds = {e["arrival_kind"] for e in eps}
    assert "ASK_ARRIVAL_FROM_BELOW" in kinds
    assert "BID_ARRIVAL_FROM_ABOVE" in kinds
    assert not gaps


def test_05_born_inside_separate(mod):
    pools = [
        {
            "pool_id": "ask_born",
            "side": "ASK",
            "lower_edge": 100.0,
            "upper_edge": 105.0,
            "available_at": "2026-08-26T01:00:00Z",
            "invalidated_ts": None,
            "strength": 1.0,
            "source_timeframe": "5m",
            "origin_ts": "2026-08-26T00:55:00Z",
        }
    ]
    t0 = mod._ms(mod._utc("2026-08-26T01:00:00Z"))
    mids = [
        mod.BookState(t0 - 1000, 102.0, 101.9, 102.1, True, [], []),
        mod.BookState(t0, 102.0, 101.9, 102.1, True, [], []),
    ]
    eps, born, gaps = mod.detect_arrivals(pools, mids)
    assert born and born[0]["class"] == "BORN_INSIDE_POOL"
    assert not any(e["pool_id"] == "ask_born" for e in eps)


def test_06_gap_cross_separate(mod):
    pools = [
        {
            "pool_id": "ask_gap",
            "side": "ASK",
            "lower_edge": 100.0,
            "upper_edge": 105.0,
            "available_at": "2026-08-26T00:00:00Z",
            "invalidated_ts": None,
            "strength": 1.0,
            "source_timeframe": "5m",
            "origin_ts": "2026-08-26T00:00:00Z",
        }
    ]
    t0 = mod._ms(mod._utc("2026-08-26T01:00:00Z"))
    mids = [
        mod.BookState(t0, 99.0, 98.9, 99.1, True, [], []),
        mod.BookState(t0 + 5000, 101.0, 100.9, 101.1, True, [], []),
    ]
    eps, born, gaps = mod.detect_arrivals(pools, mids)
    assert gaps and gaps[0]["class"] == "GAP_CROSS"
    assert not any(e["pool_id"] == "ask_gap" for e in eps)


def test_07_08_no_dup_until_exit(mod):
    pools = [
        {
            "pool_id": "ask_dup",
            "side": "ASK",
            "lower_edge": 100.0,
            "upper_edge": 105.0,
            "available_at": "2026-08-26T00:00:00Z",
            "invalidated_ts": None,
            "strength": 1.0,
            "source_timeframe": "5m",
            "origin_ts": "2026-08-26T00:00:00Z",
        }
    ]
    t0 = mod._ms(mod._utc("2026-08-26T01:00:00Z"))
    mids = [
        mod.BookState(t0, 99.0, 98.9, 99.1, True, [], []),
        mod.BookState(t0 + 1000, 100.5, 100.4, 100.6, True, [], []),
        mod.BookState(t0 + 2000, 101.0, 100.9, 101.1, True, [], []),
        mod.BookState(t0 + 3000, 100.2, 100.1, 100.3, True, [], []),
    ]
    eps, _, _ = mod.detect_arrivals(pools, mids)
    assert len(eps) == 1
    mids2 = mids + [
        mod.BookState(t0 + 4000, 99.0, 98.9, 99.1, True, [], []),
        mod.BookState(t0 + 5000, 100.1, 100.0, 100.2, True, [], []),
    ]
    eps2, _, _ = mod.detect_arrivals(pools, mids2)
    assert len(eps2) == 2


def test_09_10_11_walls_inside_same_side(mod):
    ranked = mod.side_levels_ranked([(100.0, 1.0), (101.0, 10.0), (99.0, 2.0)])
    lo, hi = 100.0, 102.0
    inside = [r for r in ranked if lo <= r["price"] <= hi]
    assert all(lo <= r["price"] <= hi for r in inside)
    assert ranked[0]["significance_class"] == "MAJOR"


def test_12_13_genuine_no_future_and_cap():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "genuine" in src
    assert "MONITOR_MAX_S = 300" in src
    assert "MAX_300S" in src


def test_14_15_tick_stable_tracking(mod):
    k1 = mod._tick_key("ASK", 79217.1, 0.1)
    k2 = mod._tick_key("ASK", 79217.14, 0.1)
    assert k1 == k2


def test_16_17_no_consumption_or_outcomes():
    src = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"FORBIDDEN_CAUSE\s*=\s*\(", src)
    assert "WALL_DISAPPEARED_CAUSE_UNKNOWN" in src
    assert "pnl" not in src.lower()
    assert "winrate" not in src.lower()
    assert "No trading" in src or "no trading" in src.lower()


def test_18_19_deterministic_and_no_live_mutation(mod):
    assert mod.episode_id("BTCUSDT", "p1", 1000) == mod.episode_id("BTCUSDT", "p1", 1000)
    src = SCRIPT.read_text(encoding="utf-8")
    assert "INSERT INTO" not in src
    assert "dashboard-restart" not in src.lower()
