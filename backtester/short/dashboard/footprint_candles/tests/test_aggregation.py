"""Aggregation, imbalance, vPOC, dedup folding."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.aggregation import (  # noqa: E402
    RawLevelAgg,
    accumulate_trade,
    build_candle,
    build_levels_from_raw,
    compute_imbalances,
    mark_stacked,
    pick_vpoc_bucket,
)
from footprint_candles.contracts import bucket_index_for_price  # noqa: E402


def test_buy_sell_mapping_and_deltas():
    levels: dict[int, RawLevelAgg] = {}
    # price 100002 → bucket 20000
    accumulate_trade(levels, price="100002", side="Buy", size=1.0, notional=100002)
    accumulate_trade(levels, price="100002", side="Sell", size=0.4, notional=40000)
    lv, d_s, d_n, _, _ = build_levels_from_raw(levels, highlight_imbalances=True)
    assert len(lv) == 1
    assert lv[0].ask_size == 1.0
    assert lv[0].bid_size == 0.4
    assert abs(lv[0].delta_size - 0.6) < 1e-9
    assert abs(d_s - 0.6) < 1e-9
    assert abs(d_n - 60002) < 1e-6


def test_vpoc_tie_break_lower_index():
    by = {
        10: RawLevelAgg(10, bid_size=1.0, ask_size=1.0),
        11: RawLevelAgg(11, bid_size=1.0, ask_size=1.0),
        9: RawLevelAgg(9, bid_size=0.5, ask_size=0.5),
    }
    assert pick_vpoc_bucket(by) == 10  # equal 2.0 at 10 and 11 → lower index


def test_ask_imbalance_diagonal():
    by = {
        5: RawLevelAgg(5, bid_size=1.0, ask_size=0.1),
        6: RawLevelAgg(6, bid_size=0.1, ask_size=3.0),  # ask vs bid[5]=1 → 3.0
    }
    ask, bid = compute_imbalances([5, 6], by)
    assert ask[6] is True
    assert ask[5] is False
    assert bid[5] is False


def test_bid_imbalance_diagonal():
    by = {
        5: RawLevelAgg(5, bid_size=3.0, ask_size=0.1),
        6: RawLevelAgg(6, bid_size=0.1, ask_size=1.0),  # bid[5]/ask[6]=3
    }
    ask, bid = compute_imbalances([5, 6], by)
    assert bid[5] is True
    assert bid[6] is False


def test_zero_division_and_missing_neighbour():
    by = {
        5: RawLevelAgg(5, bid_size=0.0, ask_size=10.0),
        6: RawLevelAgg(6, bid_size=10.0, ask_size=0.0),
    }
    ask, bid = compute_imbalances([5, 6], by)
    # ask[6] vs bid[5]=0 → no imbalance
    assert ask[6] is False
    # bid[5] vs ask[6]=0 → no imbalance
    assert bid[5] is False
    # lone bucket
    alone = {8: RawLevelAgg(8, bid_size=5.0, ask_size=5.0)}
    ask2, bid2 = compute_imbalances([8], alone)
    assert ask2[8] is False and bid2[8] is False


def test_min_compare_size():
    by = {
        5: RawLevelAgg(5, bid_size=1.0, ask_size=0.0),
        6: RawLevelAgg(6, bid_size=0.0, ask_size=0.009),  # below 0.01
    }
    ask, _ = compute_imbalances([5, 6], by)
    assert ask[6] is False


def test_stacked_separate_buy_sell():
    # Ask run of 3
    by = {}
    for i in range(10, 13):
        by[i] = RawLevelAgg(i, bid_size=1.0 if i > 10 else 1.0, ask_size=3.0)
    by[9] = RawLevelAgg(9, bid_size=1.0, ask_size=0.1)
    # Force ask imbalance on 10,11,12 by giving lower bids
    by[9] = RawLevelAgg(9, bid_size=1.0, ask_size=0.0)
    by[10] = RawLevelAgg(10, bid_size=1.0, ask_size=3.0)
    by[11] = RawLevelAgg(11, bid_size=1.0, ask_size=3.0)
    by[12] = RawLevelAgg(12, bid_size=1.0, ask_size=3.0)
    indices = sorted(by)
    ask, bid = compute_imbalances(indices, by)
    assert ask[10] and ask[11] and ask[12]
    stacked = mark_stacked(indices, ask)
    assert stacked[10] and stacked[11] and stacked[12]

    # Bid stack separate — do not mix with ask
    by2 = {
        20: RawLevelAgg(20, bid_size=3.0, ask_size=0.1),
        21: RawLevelAgg(21, bid_size=3.0, ask_size=1.0),
        22: RawLevelAgg(22, bid_size=3.0, ask_size=1.0),
        23: RawLevelAgg(23, bid_size=0.1, ask_size=1.0),
    }
    idx2 = sorted(by2)
    _, bid2 = compute_imbalances(idx2, by2)
    assert bid2[20] and bid2[21] and bid2[22]
    st_bid = mark_stacked(idx2, bid2)
    assert st_bid[20] and st_bid[21] and st_bid[22]
    st_ask = mark_stacked(idx2, {i: False for i in idx2})
    assert not any(st_ask.values())


def test_dedup_live_archive_same_trade_id_via_accumulate_once():
    """Simulate post-dedup single trade (live wins) — only one fold."""
    levels: dict[int, RawLevelAgg] = {}
    accumulate_trade(levels, price=Decimal("100001"), side="Buy", size=2.0, notional=200002)
    # If incorrectly double-counted, size would be 4
    assert levels[bucket_index_for_price(100001)].ask_size == 2.0


def test_build_candle_suppresses_imbalance_when_not_complete():
    by = {
        5: RawLevelAgg(5, bid_size=1.0, ask_size=0.1),
        6: RawLevelAgg(6, bid_size=0.1, ask_size=3.0),
    }
    c = build_candle(
        time=1,
        open_=1,
        high=2,
        low=1,
        close=2,
        by_idx=by,
        coverage="UNKNOWN",
    )
    assert all(not lv.ask_imbalance for lv in c.levels)
    assert c.incomplete is True

    c2 = build_candle(
        time=1,
        open_=1,
        high=2,
        low=1,
        close=2,
        by_idx=by,
        coverage="COMPLETE",
    )
    assert any(lv.ask_imbalance for lv in c2.levels)
