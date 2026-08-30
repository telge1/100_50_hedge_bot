"""Tests for liquidity_pool_signal foundation (chart engine direct reuse)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.liquidity_pool_signal import (
    MarketPoolLocation,
    chart_lookback_start,
    chart_pool_engine,
    classify_market_pool_location,
    export_snapshot,
    get_engine_function,
    nearest_front,
    parity_pair,
)
from orderbook_analyse.liquidity_pool_signal import chart_pool_adapter as adapter

PKG_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/src/orderbook_analyse/liquidity_pool_signal"
)
CLI = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/scripts/"
    "run_liquidity_location_chart_pool_export.py"
)


def test_direct_import_same_chart_engine():
    eng = get_engine_function()
    assert eng is chart_pool_engine()
    assert eng.__module__ == "indicators.liquidity_location.engine"
    assert eng.__name__ == "run_liquidity_location"
    # Same binding Research Charts uses
    import sys

    dash = "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard"
    if dash not in sys.path:
        sys.path.insert(0, dash)
    from research_charts.trp_import import load_trp

    assert eng is load_trp()["run_liquidity_location"]


def test_no_nested_logic_in_package_or_cli():
    for path in list(PKG_ROOT.glob("*.py")) + [CLI]:
        src = path.read_text(encoding="utf-8")
        assert "a_plus_nested_ask_pool_edge_short_v1" not in src
        assert "rank_nested_ask" not in src
        assert "APPROACH_MAX_ATR" not in src


def test_ask_bid_and_asof_snapshot():
    as_of = datetime(2026, 8, 26, 4, 48, tzinfo=timezone.utc)
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc),
        as_of=as_of,
    )
    assert snap["n_ask_active"] >= 1
    assert snap["n_bid_active"] >= 1
    for p in snap["active_pools"]:
        assert p["available_at"] <= snap["as_of"]
        assert p["side"] in ("ASK", "BID")
        inv = p.get("invalidated_ts")
        assert inv is None or inv > snap["as_of"]
        assert p["source_timeframe"] == "5m"


def test_market_inside_ask_bid_between_and_overlap():
    ask = {
        "pool_id": "ask1",
        "side": "ASK",
        "lower_edge": 100.0,
        "upper_edge": 110.0,
        "source_timeframe": "5m",
    }
    bid = {
        "pool_id": "bid1",
        "side": "BID",
        "lower_edge": 90.0,
        "upper_edge": 95.0,
        "source_timeframe": "5m",
    }
    overlap_bid = {
        "pool_id": "bid2",
        "side": "BID",
        "lower_edge": 105.0,
        "upper_edge": 115.0,
        "source_timeframe": "5m",
    }

    assert classify_market_pool_location([ask, bid], 105.0) == MarketPoolLocation.INSIDE_ASK_POOL
    assert classify_market_pool_location([ask, bid], 92.0) == MarketPoolLocation.INSIDE_BID_POOL
    assert classify_market_pool_location([ask, bid], 97.0) == MarketPoolLocation.BETWEEN_POOLS
    assert (
        classify_market_pool_location([ask, overlap_bid], 107.0)
        == MarketPoolLocation.INSIDE_OVERLAPPING_POOLS
    )
    assert classify_market_pool_location([], 100.0) == MarketPoolLocation.NO_ACTIVE_POOLS

    inside_ask = nearest_front([ask, bid], 105.0)
    assert inside_ask["market_inside_pool"] is True
    assert inside_ask["market_pool_location"] == MarketPoolLocation.INSIDE_ASK_POOL.value
    assert inside_ask["nearest_ask_pool_above_market"] == "MARKET_INSIDE_POOL"

    inside_bid = nearest_front([ask, bid], 92.0)
    assert inside_bid["market_pool_location"] == MarketPoolLocation.INSIDE_BID_POOL.value

    between = nearest_front([ask, bid], 97.0)
    assert between["market_inside_pool"] is False
    assert between["nearest_ask_pool_above_market"]["pool_id"] == "ask1"
    assert between["nearest_bid_pool_below_market"]["pool_id"] == "bid1"


def test_parity_fingerprint_identical_and_deterministic():
    as_of = datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc)
    start = chart_lookback_start(as_of, "5m")
    pr1 = parity_pair(symbol="BTCUSDT", timeframe="5m", start=start, end=as_of)
    pr2 = parity_pair(symbol="BTCUSDT", timeframe="5m", start=start, end=as_of)
    assert pr1["parity_pass"] is True
    assert pr1["chart_payload_sha256"] == pr1["cli_payload_sha256"]
    assert pr1["chart_payload_sha256"] == pr2["chart_payload_sha256"]
    assert pr1["cli_payload_sha256"] == pr2["cli_payload_sha256"]
    assert pr1["chart_payload_normalized"]["pools"] == pr1["cli_payload_normalized"]["pools"]
    assert adapter.get_engine_function() is adapter.chart_pool_engine()
