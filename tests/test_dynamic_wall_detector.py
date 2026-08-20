"""Network-free unit tests for dynamic wall detection and orderbook replay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orderbook_analyse.dynamic_wall_detector import (
    StabilitySample,
    WallDetectorParams,
    aggregate_book,
    analyze_resolution,
    assign_bucket_price,
    build_clusters,
    choose_bucket_size,
    compute_wall_stability,
    infer_tick_size,
    match_reference_level,
    score_buckets,
)
from orderbook_analyse.orderbook_replay import (
    BookLevelEvent,
    OrderBookReplayer,
    OrderBookState,
    ReplayError,
    replay_until,
)


TS0 = datetime(2026, 7, 26, 9, 16, 29, tzinfo=timezone.utc)


def _evt(
    *,
    ts: datetime,
    side: str,
    price: str,
    qty: str,
    msg: str,
    u: int,
    seq: int,
    idx: int = 0,
) -> BookLevelEvent:
    return BookLevelEvent(
        exchange_ts=ts,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        message_type=msg,
        update_id=u,
        cross_sequence=seq,
        level_index=idx,
    )


def test_infer_tick_size() -> None:
    prices = [Decimal("0.620"), Decimal("0.621"), Decimal("0.622"), Decimal("0.624")]
    assert infer_tick_size(prices) == Decimal("0.001")


def test_choose_bucket_size_apt_like_10bps() -> None:
    # mid≈0.622, 10 bps → raw≈0.000622 → nice/tick constrained to 0.001
    size = choose_bucket_size(Decimal("0.622"), Decimal("0.001"), 10)
    assert size == Decimal("0.001")


def test_choose_bucket_size_btc_sol_alt_no_hardcode() -> None:
    btc = choose_bucket_size(Decimal("65000"), Decimal("0.1"), 10)
    assert btc >= Decimal("0.1")
    # raw=65 → ceil-nice → 100
    assert btc == Decimal("100")

    sol = choose_bucket_size(Decimal("150"), Decimal("0.01"), 10)
    assert sol >= Decimal("0.01")
    # raw=0.15 → ceil-nice → 0.2
    assert sol == Decimal("0.2")

    tiny = choose_bucket_size(Decimal("0.000012"), Decimal("0.000001"), 10)
    assert tiny >= Decimal("0.000001")


def test_nice_step_never_below_tick() -> None:
    size = choose_bucket_size(Decimal("0.5"), Decimal("0.01"), 1)  # raw=0.00005
    assert size >= Decimal("0.01")


def test_bucket_assignment_bid_floor_ask_ceil() -> None:
    assert assign_bucket_price(Decimal("0.6174"), Decimal("0.001"), "bid") == Decimal("0.617")
    assert assign_bucket_price(Decimal("0.6174"), Decimal("0.001"), "ask") == Decimal("0.618")
    assert assign_bucket_price(Decimal("0.6280"), Decimal("0.001"), "ask") == Decimal("0.628")


def test_snapshot_reconstruction_and_delta_and_qty_zero() -> None:
    events = [
        _evt(ts=TS0, side="bid", price="0.620", qty="10", msg="snapshot", u=1, seq=10, idx=0),
        _evt(ts=TS0, side="bid", price="0.619", qty="5", msg="snapshot", u=1, seq=10, idx=1),
        _evt(ts=TS0, side="ask", price="0.621", qty="8", msg="snapshot", u=1, seq=10, idx=0),
        _evt(
            ts=TS0 + timedelta(milliseconds=100),
            side="bid",
            price="0.620",
            qty="0",
            msg="delta",
            u=2,
            seq=11,
        ),
        _evt(
            ts=TS0 + timedelta(milliseconds=100),
            side="ask",
            price="0.622",
            qty="3",
            msg="delta",
            u=2,
            seq=11,
            idx=1,
        ),
    ]
    book = OrderBookReplayer().replay(events)
    assert Decimal("0.620") not in book.bids
    assert book.bids[Decimal("0.619")] == Decimal("5")
    assert book.asks[Decimal("0.621")] == Decimal("8")
    assert book.asks[Decimal("0.622")] == Decimal("3")
    assert book.best_bid() == Decimal("0.619")
    assert book.best_ask() == Decimal("0.621")
    assert book.mid_price() == Decimal("0.620")


def test_sequence_gap_raises() -> None:
    events = [
        _evt(ts=TS0, side="bid", price="0.62", qty="1", msg="snapshot", u=1, seq=1),
        _evt(
            ts=TS0 + timedelta(milliseconds=1),
            side="bid",
            price="0.62",
            qty="2",
            msg="delta",
            u=3,
            seq=2,
        ),
    ]
    with pytest.raises(ReplayError, match="update_id gap"):
        OrderBookReplayer().replay(events)


def test_missing_snapshot_raises() -> None:
    events = [
        _evt(ts=TS0, side="bid", price="0.62", qty="1", msg="delta", u=2, seq=2),
    ]
    with pytest.raises(ReplayError):
        OrderBookReplayer().replay(events)


def test_snapshot_at_no_lookahead() -> None:
    events = [
        _evt(ts=TS0, side="bid", price="0.620", qty="1", msg="snapshot", u=1, seq=1),
        _evt(ts=TS0, side="ask", price="0.621", qty="1", msg="snapshot", u=1, seq=1),
        _evt(ts=TS0 + timedelta(seconds=10), side="bid", price="0.619", qty="9", msg="delta", u=2, seq=2),
        _evt(ts=TS0 + timedelta(seconds=20), side="ask", price="0.630", qty="50", msg="delta", u=3, seq=3),
    ]
    book = replay_until(events, as_of=TS0 + timedelta(seconds=10))
    assert Decimal("0.619") in book.bids
    assert Decimal("0.630") not in book.asks


def test_local_median_wall_multiple_percentile_and_clusters() -> None:
    mid = Decimal("0.620")
    book = OrderBookState(
        bids={
            Decimal("0.619"): Decimal("10"),
            Decimal("0.618"): Decimal("10"),
            Decimal("0.617"): Decimal("200"),  # wall
            Decimal("0.616"): Decimal("10"),
            Decimal("0.615"): Decimal("10"),
            Decimal("0.614"): Decimal("10"),
            Decimal("0.613"): Decimal("10"),
        },
        asks={
            Decimal("0.621"): Decimal("10"),
            Decimal("0.622"): Decimal("10"),
            Decimal("0.626"): Decimal("80"),
            Decimal("0.627"): Decimal("10"),
            Decimal("0.628"): Decimal("90"),
            Decimal("0.629"): Decimal("85"),
            Decimal("0.630"): Decimal("10"),
        },
        has_snapshot=True,
    )
    params = WallDetectorParams(
        wall_multiple_min=3.0,
        percentile_min=80.0,
        depth_share_min=0.05,
        local_radius=5,
        distance_max_pct=5.0,
    )
    analysis = analyze_resolution(
        book, bucket_size=Decimal("0.001"), resolution="fixed", mid=mid, params=params
    )
    bid_walls = [w for w in analysis["walls"] if w.side == "bid"]
    assert any(w.bucket_price == Decimal("0.617") for w in bid_walls)
    wall_617 = next(w for w in analysis["candidates"] if w.bucket_price == Decimal("0.617"))
    assert wall_617.wall_multiple >= 3
    assert wall_617.percentile >= 80

    ask_walls = [w for w in analysis["walls"] if w.side == "ask"]
    # Direct cluster unit check with known candidates
    synthetic = [
        next(w for w in analysis["candidates"] if w.bucket_price == Decimal("0.626")),
        next(w for w in analysis["candidates"] if w.bucket_price == Decimal("0.628")),
        next(w for w in analysis["candidates"] if w.bucket_price == Decimal("0.629")),
    ]
    for s in synthetic:
        s.is_wall = True
    clusters = build_clusters(synthetic, bucket_size=Decimal("0.001"), max_gap_buckets=1)
    assert any(
        c.start_price <= Decimal("0.626") and c.end_price >= Decimal("0.629") for c in clusters
    )
    # At least one ask wall should be detected in the fixture
    assert len(ask_walls) >= 1

    ref = match_reference_level(Decimal("0.628"), analysis)
    assert ref["nearest_bucket"] == "0.628"


def test_multi_resolution_comparison_shapes() -> None:
    mid = Decimal("0.622")
    book = OrderBookState(
        bids={Decimal(f"0.{620-i:03d}"): Decimal("5") for i in range(10)},
        asks={Decimal(f"0.{623+i:03d}"): Decimal("5") for i in range(10)},
        has_snapshot=True,
    )
    # Inject a wall
    book.bids[Decimal("0.617")] = Decimal("500")
    book.asks[Decimal("0.628")] = Decimal("500")
    params = WallDetectorParams(distance_max_pct=3.0, percentile_min=70.0, depth_share_min=0.05)
    tick = infer_tick_size(list(book.bids) + list(book.asks))
    sizes = {
        "5bps": choose_bucket_size(mid, tick, 5),
        "10bps": choose_bucket_size(mid, tick, 10),
        "25bps": choose_bucket_size(mid, tick, 25),
    }
    assert sizes["10bps"] == Decimal("0.001")
    results = {
        name: analyze_resolution(book, bucket_size=size, resolution=name, mid=mid, params=params)
        for name, size in sizes.items()
    }
    assert set(results) == {"5bps", "10bps", "25bps"}
    assert all("wall_candidate_count" in r for r in results.values())


def test_stability_computation() -> None:
    t0 = TS0
    series = [
        StabilitySample(t0, Decimal("100")),
        StabilitySample(t0 + timedelta(seconds=30), Decimal("80")),
        StabilitySample(t0 + timedelta(seconds=60), None),
        StabilitySample(t0 + timedelta(seconds=90), Decimal("50")),
    ]
    rows = compute_wall_stability({("ask", Decimal("0.628")): series})
    assert len(rows) == 1
    row = rows[0]
    assert row["samples_present"] == 3
    assert row["number_of_zero_or_absent_samples"] == 1
    assert row["presence_ratio"] == 0.75
    assert row["max_notional"] == 100.0
    assert row["final_notional"] == 50.0
    assert row["max_drawdown_from_peak_pct"] == 50.0


def test_aggregate_book_respects_distance() -> None:
    book = OrderBookState(
        bids={Decimal("0.500"): Decimal("999"), Decimal("0.619"): Decimal("1")},
        asks={Decimal("0.621"): Decimal("1")},
        has_snapshot=True,
    )
    agg = aggregate_book(
        book, bucket_size=Decimal("0.001"), mid=Decimal("0.620"), distance_max_pct=3.0
    )
    bid_prices = {b.bucket_price for b in agg["bid"]}
    assert Decimal("0.619") in bid_prices
    assert Decimal("0.500") not in bid_prices


def test_score_buckets_excludes_self_from_local_median() -> None:
    buckets = []
    # Build via aggregate to get BucketStat list
    book = OrderBookState(
        bids={
            Decimal("0.610"): Decimal("1"),
            Decimal("0.611"): Decimal("1"),
            Decimal("0.612"): Decimal("1"),
            Decimal("0.613"): Decimal("1"),
            Decimal("0.614"): Decimal("1"),
            Decimal("0.615"): Decimal("100"),
        },
        asks={Decimal("0.620"): Decimal("1")},
        has_snapshot=True,
    )
    raw = aggregate_book(
        book, bucket_size=Decimal("0.001"), mid=Decimal("0.618"), distance_max_pct=5.0
    )
    scored = score_buckets(raw["bid"], params=WallDetectorParams(local_radius=5), resolution="t")
    wall = next(s for s in scored if s.bucket_price == Decimal("0.615"))
    assert wall.local_median_notional < float(wall.notional)
    assert wall.wall_multiple > 1


def test_filter_keeps_later_snapshot_and_avoids_false_gap() -> None:
    """HYPE-style: delta chain skips IDs that a mid-stream snapshot reseats."""
    from orderbook_analyse.dynamic_wall_detector import filter_events_after_bootstrap
    from orderbook_analyse.orderbook_replay import group_messages

    boot_u, boot_seq = 9266140, 159428999945
    ts0 = TS0
    ts1 = TS0 + timedelta(seconds=1)
    ts2 = TS0 + timedelta(seconds=2)
    events = [
        _evt(ts=ts0, side="bid", price="0.62", qty="1", msg="snapshot", u=boot_u, seq=boot_seq, idx=0),
        _evt(ts=ts0, side="ask", price="0.63", qty="1", msg="snapshot", u=boot_u, seq=boot_seq, idx=1),
        _evt(ts=ts1, side="bid", price="0.62", qty="2", msg="delta", u=9266141, seq=boot_seq + 10, idx=0),
        # Reseat snapshot (9266142..9266145 never appear as deltas)
        _evt(ts=ts2, side="bid", price="0.621", qty="5", msg="snapshot", u=9266146, seq=boot_seq + 20, idx=0),
        _evt(ts=ts2, side="ask", price="0.631", qty="5", msg="snapshot", u=9266146, seq=boot_seq + 20, idx=1),
        _evt(
            ts=ts2 + timedelta(milliseconds=1),
            side="bid",
            price="0.621",
            qty="6",
            msg="delta",
            u=9266147,
            seq=boot_seq + 21,
            idx=0,
        ),
    ]
    filtered = filter_events_after_bootstrap(events, snapshot_u=boot_u, snapshot_seq=boot_seq)
    snap_uids = {e.update_id for e in filtered if e.message_type == "snapshot"}
    assert snap_uids == {boot_u, 9266146}

    # Old filter behavior (bootstrap snap only) fails with the production error.
    old_style = [e for e in filtered if not (e.message_type == "snapshot" and e.update_id != boot_u)]
    with pytest.raises(ReplayError, match="update_id gap: expected 9266142, got 9266147"):
        bad = OrderBookReplayer()
        for message_type, update_id, seq, ts, levels in group_messages(old_style):
            bad.apply_message(message_type, update_id, seq, ts, levels)

    replayer = OrderBookReplayer()
    for message_type, update_id, seq, ts, levels in group_messages(filtered):
        replayer.apply_message(message_type, update_id, seq, ts, levels)
    assert replayer.book.last_update_id == 9266147
    assert replayer.book.has_snapshot


def test_filter_drops_older_stray_snapshot() -> None:
    from orderbook_analyse.dynamic_wall_detector import filter_events_after_bootstrap

    boot_u, boot_seq = 100, 1000
    older = _evt(ts=TS0, side="bid", price="1", qty="1", msg="snapshot", u=50, seq=900, idx=0)
    boot = _evt(ts=TS0, side="bid", price="1", qty="1", msg="snapshot", u=boot_u, seq=boot_seq, idx=0)
    filtered = filter_events_after_bootstrap(
        [older, boot], snapshot_u=boot_u, snapshot_seq=boot_seq
    )
    assert [e.update_id for e in filtered] == [boot_u]


def test_find_bootstrap_snapshot_prefers_newest_in_window() -> None:
    from orderbook_analyse.dynamic_wall_detector import find_bootstrap_snapshot

    class _Result:
        def __init__(self, rows):
            self.result_rows = rows

    class _FakeDb:
        def __init__(self):
            self.queries: list[str] = []

        def query(self, sql, parameters=None):
            self.queries.append(sql)
            if "exchange_ts >= %(start)s" in sql and "exchange_ts <= %(end)s" in sql:
                assert "ORDER BY exchange_ts DESC" in sql
                return _Result(
                    [
                        (
                            datetime(2026, 8, 3, 11, 58, 29, 628000, tzinfo=timezone.utc),
                            9266725,
                            159429576372,
                        )
                    ]
                )
            return _Result([])

    db = _FakeDb()
    start = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    ts, u, seq = find_bootstrap_snapshot(db, symbol="HYPEUSDT", start=start, end=end)
    assert u == 9266725
    assert seq == 159429576372
    assert ts.isoformat().startswith("2026-08-03T11:58:29")
    assert "ORDER BY exchange_ts DESC" in db.queries[0]
    assert "ORDER BY exchange_ts ASC" not in db.queries[0]
