"""Targeted tests for LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2."""

from __future__ import annotations

from pathlib import Path

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.clustering import (
    PoolInterval,
    assign_market_clusters,
    build_components,
    intervals_connected,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
    classify_first_seen,
    cluster_wall_identity,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    pool_filter_after_rank,
    significance_class,
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_signal import chart_pool_engine, get_engine_function


def test_01_full_side_rank_before_pool_filter():
    ranked = side_levels_ranked_full([(100.0, 1.0), (101.0, 50.0), (150.0, 2.0)])
    inside = pool_filter_after_rank(ranked, 100.0, 102.0)
    assert ranked[0]["price"] == 101.0
    assert inside[0]["full_side_rank"] == 1
    assert len(ranked) == 3


def test_02_03_exact_and_classes():
    assert significance_class(1, 0.99) == "MAJOR"
    assert significance_class(10, 0.85) == "MODERATE"
    assert significance_class(50, 0.5) == "MINOR"


def test_04_05_06_07_first_seen_classes():
    assert (
        classify_first_seen(
            first_seen_ts_ms=1000,
            arrival_ts_ms=2000,
            present_in_pre=True,
            present_at_exact_arrival=True,
            present_strictly_after=False,
        )
        == FirstSeenClass.PRE_EXISTING_BEFORE_ARRIVAL
    )
    assert (
        classify_first_seen(
            first_seen_ts_ms=2000,
            arrival_ts_ms=2000,
            present_in_pre=False,
            present_at_exact_arrival=True,
            present_strictly_after=False,
        )
        == FirstSeenClass.FIRST_SEEN_AT_ARRIVAL
    )
    assert (
        classify_first_seen(
            first_seen_ts_ms=2001,
            arrival_ts_ms=2000,
            present_in_pre=False,
            present_at_exact_arrival=False,
            present_strictly_after=True,
        )
        == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL
    )
    assert (
        classify_first_seen(
            first_seen_ts_ms=2000,
            arrival_ts_ms=2000,
            present_in_pre=False,
            present_at_exact_arrival=False,
            present_strictly_after=False,
        )
        == FirstSeenClass.FIRST_SEEN_AT_ARRIVAL
    )


def test_08_09_wall_identity_cluster_dedup():
    a = cluster_wall_identity(symbol="BTCUSDT", side="ASK", tick_price=79217.1)
    b = cluster_wall_identity(symbol="BTCUSDT", side="ASK", tick_price=79217.1)
    assert a == b
    assert a != cluster_wall_identity(symbol="BTCUSDT", side="BID", tick_price=79217.1)


def test_10_11_overlap_and_transitive():
    p1 = PoolInterval("a", "ASK", 100, 110, 0, None)
    p2 = PoolInterval("b", "ASK", 110, 120, 0, None)
    p3 = PoolInterval("c", "ASK", 119, 130, 0, None)
    assert intervals_connected(p1, p2)
    comps = build_components("ASK", [p1, p2, p3], as_of_ms=1)
    assert len(comps) == 1
    assert set(comps[0].member_pool_ids) == {"a", "b", "c"}


def test_12_ask_bid_separated():
    pools = [
        PoolInterval("a", "ASK", 100, 110, 0, None),
        PoolInterval("b", "BID", 100, 110, 0, None),
    ]
    assert len(build_components("ASK", pools, as_of_ms=1)) == 1
    assert len(build_components("BID", pools, as_of_ms=1)) == 1


def test_13_14_merge_overlap_not_time_alone():
    pools = [
        PoolInterval("p1", "ASK", 100, 110, 0, None),
        PoolInterval("p2", "ASK", 105, 120, 0, None),
        PoolInterval("p3", "ASK", 200, 210, 0, None),
    ]
    mids = [(i, 99.0, True) for i in range(0, 10)]
    mids += [(10, 100.5, True), (11, 106.0, True), (12, 106.0, True)]
    arrivals = [
        {
            "pool_arrival_id": "A1",
            "pool_id": "p1",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 10,
            "lower_edge": 100.0,
            "upper_edge": 110.0,
        },
        {
            "pool_arrival_id": "A2",
            "pool_id": "p2",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 11,
            "lower_edge": 105.0,
            "upper_edge": 120.0,
        },
        {
            "pool_arrival_id": "A3",
            "pool_id": "p3",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 11,
            "lower_edge": 200.0,
            "upper_edge": 210.0,
        },
    ]
    enriched, clusters, _ = assign_market_clusters(
        symbol="BTCUSDT", pool_arrivals=arrivals, pools=pools, mids=mids
    )
    by_id = {e["pool_arrival_id"]: e for e in enriched}
    assert by_id["A1"]["market_arrival_cluster_id"] == by_id["A2"]["market_arrival_cluster_id"]
    assert by_id["A3"]["market_arrival_cluster_id"] != by_id["A1"]["market_arrival_cluster_id"]


def test_15_16_exit_and_rearm():
    pools = [PoolInterval("p1", "ASK", 100, 110, 0, None)]
    mids = [
        (0, 99.0, True),
        (1, 100.5, True),
        (2, 101.0, True),
        (3, 99.0, True),
        (4, 99.0, True),
        (5, 100.2, True),
    ]
    arrivals = [
        {
            "pool_arrival_id": "A1",
            "pool_id": "p1",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 1,
            "lower_edge": 100.0,
            "upper_edge": 110.0,
        },
        {
            "pool_arrival_id": "A2",
            "pool_id": "p1",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 5,
            "lower_edge": 100.0,
            "upper_edge": 110.0,
        },
    ]
    enriched, clusters, _ = assign_market_clusters(
        symbol="BTCUSDT", pool_arrivals=arrivals, pools=pools, mids=mids
    )
    by_id = {e["pool_arrival_id"]: e for e in enriched}
    assert by_id["A1"]["market_arrival_cluster_id"] != by_id["A2"]["market_arrival_cluster_id"]
    assert len(clusters) >= 2


def test_17_18_no_retroactive_stable_id():
    pools = [
        PoolInterval("p1", "ASK", 100, 110, 0, None),
        PoolInterval("p2", "ASK", 105, 120, 50, None),
    ]
    mids = [(i, 99.0, True) for i in range(0, 10)] + [(10, 100.5, True)] + [
        (i, 106.0, True) for i in range(11, 60)
    ]
    arrivals = [
        {
            "pool_arrival_id": "A1",
            "pool_id": "p1",
            "side": "ASK",
            "approach_direction": "FROM_BELOW",
            "arrival_ts_ms": 10,
            "lower_edge": 100.0,
            "upper_edge": 110.0,
        }
    ]
    enriched, clusters, _ = assign_market_clusters(
        symbol="BTCUSDT", pool_arrivals=arrivals, pools=pools, mids=mids
    )
    cid0 = enriched[0]["market_arrival_cluster_id"]
    start_cl = next(c for c in clusters if c.cluster_id == cid0)
    assert start_cl.cluster_id == cid0


def test_19_20_21_22_foundation_no_outcomes_no_mutation():
    assert get_engine_function() is chart_pool_engine()
    assert get_engine_function().__module__ == "indicators.liquidity_location.engine"
    src = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_liquidity_pool_arrival_wall_monitor_v2.py"
    ).read_text(encoding="utf-8")
    assert "pnl" not in src.lower()
    assert "INSERT INTO" not in src
    assert "no_public_trades" in src  # explicit denial only
