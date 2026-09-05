"""Tests for RAM-only full orderbook state + UI aggregation."""

from __future__ import annotations

from orderbook_analyse.orderbook_v2_live.full_book_state import (
    FULL_DEPTH,
    FullBookState,
    aggregate_full_book,
    full_orderbook_topic,
    parse_full_orderbook_topic,
)


def test_full_topic_helpers():
    assert full_orderbook_topic("btcusdt") == "orderbook.full.BTCUSDT"
    assert parse_full_orderbook_topic("orderbook.full.BTCUSDT") == "BTCUSDT"
    assert parse_full_orderbook_topic("orderbook.1000.BTCUSDT") is None
    assert FULL_DEPTH == 0


def test_snapshot_and_delta_and_aggregate():
    book = FullBookState(symbol="BTCUSDT")
    # Dense near mid + sparse far.
    mid = 100.0
    bids = [[str(mid - i * 0.1), "1"] for i in range(1, 401)]
    asks = [[str(mid + i * 0.1), "1"] for i in range(1, 401)]
    bids.append(["10", "5"])
    asks.append(["1000", "5"])
    book.apply_snapshot(bids=bids, asks=asks, u=1, seq=10, ts_ms=1_700_000_000_000)
    assert book.snapshot_loaded
    assert book.best_bid() == 99.9
    assert book.best_ask() == 100.1

    book.apply_delta(bids=[["99.9", "0"], ["99.8", "2"]], asks=[["100.1", "3"]], u=2, seq=11, ts_ms=1_700_000_000_100)
    assert 99.9 not in book.bids
    assert book.bids[99.8] == 2.0
    assert book.asks[100.1] == 3.0

    agg = aggregate_full_book(book, max_bars_per_side=50, clip_pct=50)
    assert agg["book_mode"] == "full"
    assert agg["raw_bid_count"] >= 400
    assert agg["raw_ask_count"] >= 400
    assert len(agg["bids"]) <= 50
    assert len(agg["asks"]) <= 50
    assert agg["aggregated"] is True
    assert agg["mid"] is not None
    # Fantasy 1000 clipped from display span at 50%.
    assert agg["display_ask_high"] < 200


def test_stale_delta_rejected():
    from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

    book = FullBookState(symbol="BTCUSDT")
    book.apply_snapshot(bids=[["100", "1"]], asks=[["101", "1"]], u=5, seq=1, ts_ms=1)
    assert book.apply_delta(bids=[], asks=[], u=4, seq=2, ts_ms=2) is DeltaOutcome.IGNORED_STALE_U
    assert book.update_id == 5
