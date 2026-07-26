"""Tests for near vs dominant liquidity and ask-ladder tracking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from orderbook_analyse.near_liquidity import (
    ASK_LADDER_COMPRESSION,
    ASK_LADDER_MOVING_HIGHER,
    ASK_LADDER_MOVING_LOWER,
    BULLISH_LIQUIDITY_SHIFT,
    COMPRESSION,
    NEAR_ASK_BUILDING,
    NEAR_ASK_CONSUMED,
    NEAR_ASK_MOVING_HIGHER,
    NEAR_ASK_MOVING_LOWER,
    NEAR_ASK_PULLED,
    NEAR_ASK_THINNING,
    NearParams,
    NearSnapshotView,
    build_near_ask_transitions,
    classify_near_ask_transition,
    detect_ask_ladder_sequences,
    select_near_and_dominant,
    summarize_near_regime,
    weighted_price,
)
from orderbook_analyse.orderbook_replay import BookLevelEvent, replay_until
from orderbook_analyse.wall_movement_tracker import SnapshotRecord, WallView, match_walls


TS0 = datetime(2026, 7, 26, 11, 19, 7, tzinfo=timezone.utc)


def _w(side: str, price: str, notional: str, *, dist: float, mult: float = 5.0, is_wall: bool = True) -> WallView:
    return WallView(
        side=side,
        price=Decimal(price),
        notional=Decimal(notional),
        wall_multiple=mult,
        distance_pct=dist,
        is_wall=is_wall,
    )


def test_far_dominant_ask_does_not_override_nearest() -> None:
    mid = Decimal("0.630")
    near = NearParams(near_min_distance_pct=0.10, near_max_distance_pct=1.50, near_top_n=3)
    asks = [
        _w("ask", "0.639", "200", dist=1.43),
        _w("ask", "0.640", "180", dist=1.59),  # outside near_max 1.50
        _w("ask", "0.652", "900", dist=3.49),  # far dominant
    ]
    bids = [_w("bid", "0.625", "150", dist=0.79)]
    view = select_near_and_dominant(
        bid_candidates=bids,
        ask_candidates=asks,
        mid=mid,
        best_bid=Decimal("0.629"),
        best_ask=Decimal("0.631"),
        bucket_size=Decimal("0.001"),
        near=near,
    )
    assert view.nearest_ask is not None
    assert view.nearest_ask.price == Decimal("0.639")
    assert view.dominant_ask is not None
    assert view.dominant_ask.price == Decimal("0.652")
    assert view.nearest_ask.price != view.dominant_ask.price


def test_nearest_ask_639_dominant_652() -> None:
    view = select_near_and_dominant(
        bid_candidates=[_w("bid", "0.624", "100", dist=0.95)],
        ask_candidates=[
            _w("ask", "0.639", "220", dist=1.4),
            _w("ask", "0.652", "800", dist=3.5),
        ],
        mid=Decimal("0.630"),
        best_bid=Decimal("0.629"),
        best_ask=Decimal("0.631"),
        bucket_size=Decimal("0.001"),
        near=NearParams(near_max_distance_pct=1.5),
    )
    assert view.nearest_ask.price == Decimal("0.639")
    assert view.dominant_ask.price == Decimal("0.652")


def _near_snap(ts: datetime, mid: str, near_asks: list[WallView], near_bids: list[WallView] | None = None) -> tuple[SnapshotRecord, NearSnapshotView]:
    near_bids = near_bids or [_w("bid", "0.624", "100", dist=0.9)]
    wa = weighted_price(near_asks)
    wb = weighted_price(near_bids)
    nv = NearSnapshotView(
        nearest_bid=near_bids[0] if near_bids else None,
        nearest_ask=near_asks[0] if near_asks else None,
        dominant_bid=near_bids[0] if near_bids else None,
        dominant_ask=max(near_asks, key=lambda w: w.notional) if near_asks else None,
        near_bids=near_bids,
        near_asks=near_asks,
        total_near_bid_notional=sum((w.notional for w in near_bids), Decimal("0")),
        total_near_ask_notional=sum((w.notional for w in near_asks), Decimal("0")),
        near_bid_weighted_price=wb,
        near_ask_weighted_price=wa,
        nearest_bid_ask_gap=(near_asks[0].price - near_bids[0].price) if near_asks and near_bids else None,
    )
    snap = SnapshotRecord(
        timestamp=ts,
        mid_price=Decimal(mid),
        best_bid=Decimal(mid) - Decimal("0.001"),
        best_ask=Decimal(mid) + Decimal("0.001"),
        bucket_size=Decimal("0.001"),
        strongest_bid=near_bids[0] if near_bids else None,
        strongest_ask=nv.dominant_ask,
        top_bid_walls=near_bids[:3],
        top_ask_walls=near_asks[:3],
        all_bid_buckets={w.price: w.notional for w in near_bids},
        all_ask_buckets={w.price: w.notional for w in near_asks},
        buy_notional_since_prev=Decimal("0"),
        sell_notional_since_prev=Decimal("0"),
        trade_delta_notional=Decimal("0"),
        open_interest=Decimal("1"),
        oi_change_since_prev=Decimal("0"),
        nearest_ask=nv.nearest_ask,
        nearest_bid=nv.nearest_bid,
        dominant_ask=nv.dominant_ask,
        dominant_bid=nv.dominant_bid,
        near_asks=near_asks,
        near_bids=near_bids,
        total_near_ask_notional=nv.total_near_ask_notional,
        total_near_bid_notional=nv.total_near_bid_notional,
        near_ask_weighted_price=wa,
        near_bid_weighted_price=wb,
        nearest_bid_ask_gap=nv.nearest_bid_ask_gap,
    )
    return snap, nv


def test_ask_ladder_moving_higher_634_635_636() -> None:
    near = NearParams(sequence_min_shifts=2)
    snaps = []
    nears = []
    for i, px in enumerate(["0.634", "0.635", "0.636"]):
        s, n = _near_snap(
            TS0 + timedelta(seconds=30 * i),
            "0.630",
            [
                _w("ask", px, "200", dist=0.7 + i * 0.1),
                _w("ask", format(Decimal(px) + Decimal("0.001"), "f"), "150", dist=0.9 + i * 0.1),
                _w("ask", format(Decimal(px) + Decimal("0.002"), "f"), "120", dist=1.1 + i * 0.1),
            ],
        )
        snaps.append(s)
        nears.append(n)
    seqs = detect_ask_ladder_sequences(snaps, nears, near)
    assert any(s.classification in {NEAR_ASK_MOVING_HIGHER, ASK_LADDER_MOVING_HIGHER} for s in seqs)


def test_ask_ladder_moving_lower() -> None:
    near = NearParams(sequence_min_shifts=2)
    snaps, nears = [], []
    for i, px in enumerate(["0.640", "0.639", "0.638"]):
        s, n = _near_snap(
            TS0 + timedelta(seconds=30 * i),
            "0.630",
            [
                _w("ask", px, "200", dist=1.4),
                _w("ask", format(Decimal(px) + Decimal("0.001"), "f"), "160", dist=1.5),
            ],
        )
        snaps.append(s)
        nears.append(n)
    seqs = detect_ask_ladder_sequences(snaps, nears, near)
    assert any(s.classification in {NEAR_ASK_MOVING_LOWER, ASK_LADDER_MOVING_LOWER} for s in seqs)


def test_near_ask_building_and_thinning() -> None:
    near = NearParams(build_notional_pct=20, thin_notional_pct=20)
    prev = NearSnapshotView(
        nearest_ask=_w("ask", "0.639", "100", dist=1.4),
        total_near_ask_notional=Decimal("100"),
        near_asks=[_w("ask", "0.639", "100", dist=1.4)],
    )
    stronger = NearSnapshotView(
        nearest_ask=_w("ask", "0.639", "140", dist=1.4),
        total_near_ask_notional=Decimal("160"),
        near_asks=[_w("ask", "0.639", "140", dist=1.4)],
    )
    thinner = NearSnapshotView(
        nearest_ask=_w("ask", "0.639", "60", dist=1.4),
        total_near_ask_notional=Decimal("60"),
        near_asks=[_w("ask", "0.639", "60", dist=1.4)],
    )
    t_build = classify_near_ask_transition(
        prev, stronger, prev_ts=TS0, cur_ts=TS0 + timedelta(seconds=30),
        mid_prev=Decimal("0.63"), mid_cur=Decimal("0.63"),
        bucket_size=Decimal("0.001"), aggressive_buy=Decimal("0"), near=near,
    )
    t_thin = classify_near_ask_transition(
        stronger, thinner, prev_ts=TS0, cur_ts=TS0 + timedelta(seconds=30),
        mid_prev=Decimal("0.63"), mid_cur=Decimal("0.63"),
        bucket_size=Decimal("0.001"), aggressive_buy=Decimal("0"), near=near,
    )
    assert t_build.classification == NEAR_ASK_BUILDING
    assert t_thin.classification == NEAR_ASK_THINNING


def test_near_ask_consumed_and_pulled() -> None:
    near = NearParams(pull_drop_pct=50, consume_trade_coverage=0.35, thin_notional_pct=80)
    # Keep total drop below thin threshold so pull/consume path is used
    prev = NearSnapshotView(
        nearest_ask=_w("ask", "0.639", "100", dist=1.4),
        total_near_ask_notional=Decimal("100"),
        near_asks=[_w("ask", "0.639", "100", dist=1.4)],
    )
    cur = NearSnapshotView(
        nearest_ask=_w("ask", "0.639", "30", dist=1.4),
        total_near_ask_notional=Decimal("70"),  # -30% < 80% thin threshold
        near_asks=[_w("ask", "0.639", "30", dist=1.4)],
    )
    consumed = classify_near_ask_transition(
        prev, cur, prev_ts=TS0, cur_ts=TS0 + timedelta(seconds=30),
        mid_prev=Decimal("0.63"), mid_cur=Decimal("0.63"),
        bucket_size=Decimal("0.001"), aggressive_buy=Decimal("50"), near=near,
    )
    pulled = classify_near_ask_transition(
        prev, cur, prev_ts=TS0, cur_ts=TS0 + timedelta(seconds=30),
        mid_prev=Decimal("0.63"), mid_cur=Decimal("0.63"),
        bucket_size=Decimal("0.001"), aggressive_buy=Decimal("5"), near=near,
    )
    assert consumed.classification == NEAR_ASK_CONSUMED
    assert pulled.classification == NEAR_ASK_PULLED


def test_top3_ask_matching_unique() -> None:
    prev = [
        _w("ask", "0.634", "100", dist=0.7),
        _w("ask", "0.636", "90", dist=0.9),
        _w("ask", "0.638", "80", dist=1.1),
    ]
    cur = [
        _w("ask", "0.635", "105", dist=0.8),
        _w("ask", "0.637", "95", dist=1.0),
        _w("ask", "0.639", "85", dist=1.2),
    ]
    matches = match_walls(prev, cur, bucket_size=Decimal("0.001"), max_buckets=1)
    assert len(matches) == 3
    assert len({a.price for a, _, _ in matches}) == 3
    assert len({b.price for _, b, _ in matches}) == 3


def test_weighted_ask_level_rises() -> None:
    a = [_w("ask", "0.634", "100", dist=0.7), _w("ask", "0.635", "100", dist=0.9)]
    b = [_w("ask", "0.635", "100", dist=0.8), _w("ask", "0.636", "100", dist=1.0)]
    assert weighted_price(b) > weighted_price(a)


def test_auction_higher_lower_compression() -> None:
    # higher
    snaps_h, nears_h = [], []
    for i, (bp, ap) in enumerate([("0.624", "0.634"), ("0.625", "0.635"), ("0.626", "0.636")]):
        s, n = _near_snap(TS0 + timedelta(seconds=30 * i), "0.630", [_w("ask", ap, "200", dist=1.0)], [_w("bid", bp, "200", dist=1.0)])
        snaps_h.append(s)
        nears_h.append(n)
    summary_h = summarize_near_regime(snaps_h, nears_h, [], [])
    assert summary_h["auction_direction"] == "HIGHER"

    # lower
    snaps_l, nears_l = [], []
    for i, (bp, ap) in enumerate([("0.626", "0.636"), ("0.625", "0.635"), ("0.624", "0.634")]):
        s, n = _near_snap(TS0 + timedelta(seconds=30 * i), "0.630", [_w("ask", ap, "200", dist=1.0)], [_w("bid", bp, "200", dist=1.0)])
        snaps_l.append(s)
        nears_l.append(n)
    summary_l = summarize_near_regime(snaps_l, nears_l, [], [])
    assert summary_l["auction_direction"] == "LOWER"

    # compression
    snaps_c, nears_c = [], []
    for i, (bp, ap) in enumerate([("0.624", "0.640"), ("0.626", "0.638")]):
        s, n = _near_snap(TS0 + timedelta(seconds=30 * i), "0.630", [_w("ask", ap, "200", dist=1.0)], [_w("bid", bp, "200", dist=1.0)])
        snaps_c.append(s)
        nears_c.append(n)
    summary_c = summarize_near_regime(snaps_c, nears_c, [], [])
    assert summary_c["auction_direction"] == COMPRESSION


def test_no_lookahead_and_deterministic_near_transitions() -> None:
    events = [
        BookLevelEvent(TS0, "bid", Decimal("0.620"), Decimal("1"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0, "ask", Decimal("0.621"), Decimal("1"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0 + timedelta(seconds=10), "ask", Decimal("0.630"), Decimal("50"), "delta", 2, 2, 0),
    ]
    book = replay_until(events, as_of=TS0 + timedelta(seconds=5))
    assert Decimal("0.630") not in book.asks

    snaps, nears = [], []
    for i, px in enumerate(["0.634", "0.635"]):
        s, n = _near_snap(TS0 + timedelta(seconds=30 * i), "0.630", [_w("ask", px, "100", dist=0.8)])
        snaps.append(s)
        nears.append(n)
    a = [t.to_row() for t in build_near_ask_transitions(snaps, nears, NearParams())]
    b = [t.to_row() for t in build_near_ask_transitions(snaps, nears, NearParams())]
    assert a == b
