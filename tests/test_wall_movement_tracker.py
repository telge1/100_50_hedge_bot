"""Unit tests for causal wall movement tracking (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from orderbook_analyse.orderbook_replay import OrderBookState, BookLevelEvent, OrderBookReplayer, replay_until
from orderbook_analyse.wall_movement_tracker import (
    FALLING_BID_FLOOR,
    LIQUIDITY_COMPRESSION,
    MovementParams,
    RISING_BID_FLOOR,
    SnapshotRecord,
    WALL_CHASING_PRICE,
    WALL_REPLACED_HIGHER,
    WALL_STABLE,
    WallView,
    build_sequences,
    build_transition,
    build_transitions,
    classify_transition,
    detect_liquidity_regime,
    detect_wall_chasing,
    match_walls,
)


TS0 = datetime(2026, 7, 26, 9, 16, 29, tzinfo=timezone.utc)


def _wall(side: str, price: str, notional: str, *, dist: float = 1.0, mult: float = 5.0, is_wall: bool = True) -> WallView:
    return WallView(
        side=side,
        price=Decimal(price),
        notional=Decimal(notional),
        wall_multiple=mult,
        distance_pct=dist,
        is_wall=is_wall,
    )


def _snap(
    ts: datetime,
    mid: str,
    bid: WallView | None,
    ask: WallView | None,
    *,
    bucket: str = "0.001",
    bid_map: dict[Decimal, Decimal] | None = None,
    ask_map: dict[Decimal, Decimal] | None = None,
    buy: str = "0",
    sell: str = "0",
    oi: str | None = "100",
    oi_chg: str | None = "0",
) -> SnapshotRecord:
    return SnapshotRecord(
        timestamp=ts,
        mid_price=Decimal(mid),
        best_bid=Decimal(mid) - Decimal("0.001"),
        best_ask=Decimal(mid) + Decimal("0.001"),
        bucket_size=Decimal(bucket),
        strongest_bid=bid,
        strongest_ask=ask,
        top_bid_walls=[bid] if bid else [],
        top_ask_walls=[ask] if ask else [],
        all_bid_buckets=bid_map or ({bid.price: bid.notional} if bid else {}),
        all_ask_buckets=ask_map or ({ask.price: ask.notional} if ask else {}),
        buy_notional_since_prev=Decimal(buy),
        sell_notional_since_prev=Decimal(sell),
        trade_delta_notional=Decimal(buy) - Decimal(sell),
        open_interest=None if oi is None else Decimal(oi),
        oi_change_since_prev=None if oi_chg is None else Decimal(oi_chg),
    )


def test_match_walls_unique_no_double_assign() -> None:
    prev = [_wall("bid", "0.614", "100"), _wall("bid", "0.610", "80")]
    cur = [_wall("bid", "0.615", "95"), _wall("bid", "0.611", "70")]
    matches = match_walls(prev, cur, bucket_size=Decimal("0.001"), max_buckets=1)
    assert len(matches) == 2
    used_prev = {a.price for a, _, _ in matches}
    used_cur = {b.price for _, b, _ in matches}
    assert len(used_prev) == 2
    assert len(used_cur) == 2


def test_constant_bid_wall_stable() -> None:
    params = MovementParams()
    prev_w = _wall("bid", "0.616", "100")
    cur_w = _wall("bid", "0.616", "105")
    prev = _snap(TS0, "0.622", prev_w, _wall("ask", "0.628", "90"))
    cur = _snap(TS0 + timedelta(seconds=30), "0.622", cur_w, _wall("ask", "0.628", "90"))
    tx = build_transition(
        side="bid",
        prev=prev_w,
        cur=cur_w,
        prev_snap=prev,
        cur_snap=cur,
        match_score=1.0,
        params=params,
    )
    assert tx.classification == WALL_STABLE


def test_single_random_higher_wall_no_rising_sequence() -> None:
    params = MovementParams(sequence_min_shifts=2, sample_seconds=30)
    w0 = _wall("bid", "0.614", "100")
    w1 = _wall("bid", "0.615", "110")
    ask = _wall("ask", "0.630", "80")
    snaps = [
        _snap(TS0, "0.620", w0, ask),
        _snap(TS0 + timedelta(seconds=30), "0.6205", w1, ask, bid_map={Decimal("0.614"): Decimal("10"), Decimal("0.615"): Decimal("110")}),
        _snap(TS0 + timedelta(seconds=60), "0.6205", w1, ask),  # stable after one jump
    ]
    transitions = build_transitions(snaps, params)
    sequences = build_sequences(snaps, transitions, params)
    rising = [s for s in sequences if s.classification == RISING_BID_FLOOR]
    assert rising == []


def test_true_sequence_614_615_616() -> None:
    params = MovementParams(sequence_min_shifts=2, sample_seconds=30)
    ask = _wall("ask", "0.630", "80")
    walls = [
        _wall("bid", "0.614", "100"),
        _wall("bid", "0.615", "110"),
        _wall("bid", "0.616", "120"),
    ]
    snaps = []
    for i, w in enumerate(walls):
        prev_price = walls[i - 1].price if i else w.price
        bid_map = {w.price: w.notional}
        if i:
            bid_map[prev_price] = Decimal("5")  # old mostly gone
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(Decimal("0.620") + Decimal("0.0002") * i, "f"),
                w,
                ask,
                bid_map=bid_map,
                oi=str(100 + i),
                oi_chg="1",
            )
        )
    transitions = build_transitions(snaps, params)
    sequences = build_sequences(snaps, transitions, params)
    rising = [s for s in sequences if s.classification == RISING_BID_FLOOR]
    assert rising
    assert rising[0].start_wall_price == Decimal("0.614")
    assert rising[0].end_wall_price == Decimal("0.616")
    assert rising[0].number_of_shifts >= 2


def test_rising_price_plus_rising_bid_wall() -> None:
    params = MovementParams(sequence_min_shifts=2)
    ask = _wall("ask", "0.635", "70")
    snaps = [
        _snap(TS0, "0.620", _wall("bid", "0.614", "100", dist=0.97), ask),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.621",
            _wall("bid", "0.615", "120", dist=0.97),
            ask,
            bid_map={Decimal("0.614"): Decimal("8"), Decimal("0.615"): Decimal("120")},
            oi_chg="10",
        ),
        _snap(
            TS0 + timedelta(seconds=60),
            "0.622",
            _wall("bid", "0.616", "130", dist=0.96),
            ask,
            bid_map={Decimal("0.615"): Decimal("7"), Decimal("0.616"): Decimal("130")},
            oi_chg="8",
        ),
    ]
    sequences = build_sequences(snaps, build_transitions(snaps, params), params)
    rising = [s for s in sequences if s.classification == RISING_BID_FLOOR]
    assert rising
    assert rising[0].wall_mid_beta is not None


def test_falling_price_plus_falling_bid_wall() -> None:
    params = MovementParams(sequence_min_shifts=2)
    ask = _wall("ask", "0.630", "70")
    snaps = [
        _snap(TS0, "0.622", _wall("bid", "0.616", "100"), ask),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.621",
            _wall("bid", "0.615", "90"),
            ask,
            bid_map={Decimal("0.616"): Decimal("5"), Decimal("0.615"): Decimal("90")},
        ),
        _snap(
            TS0 + timedelta(seconds=60),
            "0.620",
            _wall("bid", "0.614", "85"),
            ask,
            bid_map={Decimal("0.615"): Decimal("4"), Decimal("0.614"): Decimal("85")},
        ),
    ]
    sequences = build_sequences(snaps, build_transitions(snaps, params), params)
    falling = [s for s in sequences if s.classification == FALLING_BID_FLOOR]
    assert falling


def test_old_wall_partially_remains() -> None:
    params = MovementParams()
    prev_w = _wall("bid", "0.614", "100")
    cur_w = _wall("bid", "0.615", "120")
    prev = _snap(TS0, "0.620", prev_w, _wall("ask", "0.630", "50"))
    cur = _snap(
        TS0 + timedelta(seconds=30),
        "0.621",
        cur_w,
        _wall("ask", "0.630", "50"),
        bid_map={Decimal("0.614"): Decimal("40"), Decimal("0.615"): Decimal("120")},
    )
    tx = build_transition(
        side="bid", prev=prev_w, cur=cur_w, prev_snap=prev, cur_snap=cur, match_score=0.9, params=params
    )
    assert tx.old_wall_remaining_ratio == 0.4
    assert tx.classification in {RISING_BID_FLOOR, WALL_REPLACED_HIGHER}


def test_old_wall_fully_replaced() -> None:
    params = MovementParams()
    prev_w = _wall("bid", "0.614", "100")
    cur_w = _wall("bid", "0.616", "150")
    prev = _snap(TS0, "0.620", prev_w, _wall("ask", "0.630", "50"))
    cur = _snap(
        TS0 + timedelta(seconds=30),
        "0.621",
        cur_w,
        _wall("ask", "0.630", "50"),
        bid_map={Decimal("0.616"): Decimal("150")},  # 0.614 gone
    )
    tx = build_transition(
        side="bid", prev=prev_w, cur=cur_w, prev_snap=prev, cur_snap=cur, match_score=0.8, params=params
    )
    assert tx.old_wall_remaining_ratio == 0.0
    assert tx.classification in {WALL_REPLACED_HIGHER, RISING_BID_FLOOR}


def test_wall_chasing_constant_distance() -> None:
    params = MovementParams(chase_min_shifts=3, chase_distance_tol_pct=0.2)
    ask = _wall("ask", "0.640", "40", dist=2.0)
    snaps = []
    for i in range(5):
        mid = Decimal("0.620") + Decimal("0.001") * i
        wall_px = mid - Decimal("0.006")
        prev_wall = mid - Decimal("0.007") if i else wall_px
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                _wall("bid", format(wall_px, "f"), "100", dist=0.97),
                ask,
                bid_map={wall_px: Decimal("100"), prev_wall: Decimal("5")},
            )
        )
    chasing = detect_wall_chasing(snaps, params)
    assert chasing
    assert chasing[0].classification == WALL_CHASING_PRICE


def test_liquidity_compression() -> None:
    snaps = [
        _snap(TS0, "0.620", _wall("bid", "0.614", "100"), _wall("ask", "0.630", "100")),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.620",
            _wall("bid", "0.616", "110"),
            _wall("ask", "0.628", "110"),
        ),
    ]
    regimes = detect_liquidity_regime(snaps)
    assert any(r["classification"] == LIQUIDITY_COMPRESSION for r in regimes)


def test_no_lookahead_replay() -> None:
    events = [
        BookLevelEvent(TS0, "bid", Decimal("0.620"), Decimal("1"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0, "ask", Decimal("0.621"), Decimal("1"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0 + timedelta(seconds=10), "bid", Decimal("0.619"), Decimal("9"), "delta", 2, 2, 0),
        BookLevelEvent(TS0 + timedelta(seconds=20), "ask", Decimal("0.630"), Decimal("50"), "delta", 3, 3, 0),
    ]
    book = replay_until(events, as_of=TS0 + timedelta(seconds=10))
    assert Decimal("0.630") not in book.asks
    assert Decimal("0.619") in book.bids


def test_quantity_zero_removes_level() -> None:
    events = [
        BookLevelEvent(TS0, "bid", Decimal("0.620"), Decimal("5"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0, "ask", Decimal("0.621"), Decimal("5"), "snapshot", 1, 1, 0),
        BookLevelEvent(TS0 + timedelta(milliseconds=1), "bid", Decimal("0.620"), Decimal("0"), "delta", 2, 2, 0),
    ]
    book = OrderBookReplayer().replay(events)
    assert Decimal("0.620") not in book.bids


def test_deterministic_transition_outputs() -> None:
    params = MovementParams()
    snaps = [
        _snap(TS0, "0.620", _wall("bid", "0.614", "100"), _wall("ask", "0.630", "80")),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.621",
            _wall("bid", "0.615", "110"),
            _wall("ask", "0.630", "80"),
            bid_map={Decimal("0.614"): Decimal("5"), Decimal("0.615"): Decimal("110")},
        ),
    ]
    a = [t.to_row() for t in build_transitions(snaps, params)]
    b = [t.to_row() for t in build_transitions(snaps, params)]
    assert a == b
