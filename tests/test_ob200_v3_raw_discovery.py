"""Focused tests for OB200 v3 raw discovery (no live collectors, low RAM)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import orjson
import pytest

from orderbook_analyse.ob200_v3_raw_discovery.audit import audit_segment, iter_decompressed_lines
from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef, excluded_tmp_files, list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow, sample_from_book
from orderbook_analyse.ob200_v3_raw_discovery.walls import extract_wall_events
from orderbook_analyse.ob200_v3_raw_discovery.analysis import build_chains, matched_controls
from orderbook_analyse.orderbook_v2.book import apply_delta, apply_snapshot
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    serialize_market_payload,
    serialize_rotation_checkpoint,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.segment import SegmentWriter


def _book_snap(u: int = 1, seq: int = 100):
    bids = [[str(100 - i * 0.1), str(10 + i)] for i in range(200)]
    asks = [[str(100.1 + i * 0.1), str(10 + i)] for i in range(200)]
    return apply_snapshot({"b": bids, "a": asks, "u": u, "seq": seq})


def test_exclude_tmp(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    d = root / "BTCUSDT" / "2026" / "08" / "25"
    d.mkdir(parents=True)
    (d / "BTCUSDT_20260825T010000Z_20260825T020000Z_ob200_v3.zst").write_bytes(b"x")
    (d / "BTCUSDT_20260825T020000Z_open_ob200_v3.zst.tmp").write_bytes(b"y")
    closed = list_closed_segments(root, symbols=("BTCUSDT",))
    assert len(closed) == 1
    assert excluded_tmp_files(root, ("BTCUSDT",))


def test_checkpoint_init_200_levels() -> None:
    book = _book_snap()
    assert len(book.bids) == 200
    assert len(book.asks) == 200
    assert max(book.bids) < min(book.asks)


def test_absolute_delta_update_delete_insert() -> None:
    book = _book_snap()
    # update existing
    book, _ = apply_delta(book, {"b": [["100", "99"]], "a": [], "u": 2, "seq": 101})
    assert book.bids[Decimal("100")] == Decimal("99")
    # delete
    book, _ = apply_delta(book, {"b": [["100", "0"]], "a": [], "u": 3, "seq": 102})
    assert Decimal("100") not in book.bids
    # new level
    book, _ = apply_delta(book, {"b": [["99.95", "5"]], "a": [], "u": 4, "seq": 103})
    assert book.bids[Decimal("99.95")] == Decimal("5")


def test_crossed_book_detection() -> None:
    book = apply_snapshot({"b": [["2", "1"]], "a": [["1", "1"]], "u": 1, "seq": 1})
    assert max(book.bids) >= min(book.asks)
    row = sample_from_book("X", 0, book, source_file="t", warmup=False)
    assert row is None


def test_sequence_backstep_is_gap() -> None:
    book = _book_snap(u=10, seq=10)
    new, warns = apply_delta(book, {"b": [], "a": [], "u": 8, "seq": 11})
    assert any(w.startswith("seq_gap") for w in warns)
    assert not new.is_valid


def test_replay_from_local_checkpoint(tmp_path: Path) -> None:
    book = _book_snap(u=10, seq=1000)
    start = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    writer = SegmentWriter(symbol="BTCUSDT", directory=tmp_path, start_utc=start, compression="none")
    writer.open()
    line = serialize_rotation_checkpoint(
        book,
        "BTCUSDT",
        topic="orderbook.200.BTCUSDT",
        ts_ms=int(start.timestamp() * 1000),
        received_at=start,
    )
    writer.write_line(
        line, kind="rotation_checkpoint", sequence=book.last_seq, update_id=book.last_u
    )
    for i in range(1, 6):
        payload = {
            "topic": "orderbook.200.BTCUSDT",
            "type": "delta",
            "ts": int(start.timestamp() * 1000) + i * 100,
            "cts": None,
            "data": {"s": "BTCUSDT", "b": [["99.5", str(10 + i)]], "a": [], "u": 10 + i, "seq": 1000 + i * 10},
        }
        # seq jumps intentionally (Bybit-like) while u is contiguous
        writer.write_line(
            serialize_market_payload(payload, received_at=start),
            kind="delta",
            sequence=1000 + i * 10,
            update_id=10 + i,
        )
    path, man = writer.close(end_utc=start.replace(hour=7))
    manifest = json.loads(man.read_text())
    assert manifest["replayable"] is True
    assert manifest["continuity_status"] == "contiguous_u"
    assert manifest["completion_status"] == "closed"
    assert manifest["replay_source"] == "rotation_checkpoint"
    ref = SegmentRef(path=path, symbol="BTCUSDT", start_utc=start, end_utc=start.replace(hour=7))
    audit = audit_segment(ref)
    assert audit.u_gaps == 0
    assert audit.seq_jumps >= 1
    assert audit.seq_jump_is_loss == "no"
    assert audit.replay_verdict == "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT"
    assert audit.start_checkpoint_bids == 200


def test_wall_events_no_lookahead() -> None:
    samples = []
    mid = 100.0
    for i in range(200):
        # build dominant bid wall then approach
        wall_qty = 100.0 if i > 50 else 5.0
        samples.append(
            SampleRow(
                symbol="BTCUSDT",
                ts_ms=1_700_000_000_000 + i * 1000,
                best_bid=mid - 0.1,
                best_ask=mid + 0.1,
                mid=mid - (0.01 if i > 120 else 0.0),
                spread=0.2,
                spread_bps=20.0,
                microprice=mid,
                bid_levels=200,
                ask_levels=200,
                bid_qty_l10=50,
                ask_qty_l10=50,
                imbalance_l10=0.0,
                bid_qty_bps10=40,
                ask_qty_bps10=40,
                imbalance_bps10=0.0,
                bid_wall_price=mid - 0.5,
                bid_wall_qty=wall_qty,
                ask_wall_price=mid + 0.5,
                ask_wall_qty=5.0,
                source_file="synth",
                warmup=i < 60,
            )
        )
    events = extract_wall_events(samples, qty_median_mult=3.0, seed=1)
    types = {e.event_type for e in events}
    assert "WALL_APPEAR" in types or "WALL_APPROACH" in types or "WALL_TOUCH" in types
    # all event ts within sample range — no future
    max_ts = samples[-1].ts_ms
    assert all(e.ts_ms <= max_ts for e in events)


def test_controls_exclude_event_windows() -> None:
    samples = [
        SampleRow(
            symbol="BTCUSDT",
            ts_ms=1_700_000_000_000 + i * 1000,
            best_bid=99.9,
            best_ask=100.1,
            mid=100.0,
            spread=0.2,
            spread_bps=20.0,
            microprice=100.0,
            bid_levels=200,
            ask_levels=200,
            bid_qty_l10=10,
            ask_qty_l10=10,
            imbalance_l10=0.0,
            bid_qty_bps10=10,
            ask_qty_bps10=10,
            imbalance_bps10=0.0,
            bid_wall_price=99.5,
            bid_wall_qty=50,
            ask_wall_price=100.5,
            ask_wall_qty=50,
            source_file="s",
            warmup=False,
        )
        for i in range(500)
    ]
    from orderbook_analyse.ob200_v3_raw_discovery.walls import WallEvent

    ev = WallEvent(
        event_id="e1",
        symbol="BTCUSDT",
        side="BID",
        direction="LONG",
        event_type="WALL_TOUCH",
        ts_ms=samples[200].ts_ms,
        wall_price=99.5,
        wall_qty=50,
        wall_dist_bps=1.0,
        mid=100.0,
        best_bid=99.9,
        best_ask=100.1,
        spread_bps=20.0,
        imbalance_l10=0.0,
        qty_vs_median=3.0,
        persistence_s=1.0,
        source_file="s",
        threshold_qty_median_mult=3.0,
    )
    ctrls = matched_controls([ev], {"BTCUSDT": samples}, controls_per_event=5, seed=7)
    assert ctrls, "expected at least one matched control"
    for c in ctrls:
        # excluded primary window: [ts-30s, ts+120s]
        assert not (ev.ts_ms - 30_000 <= c.ts_ms <= ev.ts_ms + 120_000)


def test_chains_long_short_separate() -> None:
    from orderbook_analyse.ob200_v3_raw_discovery.walls import WallEvent

    base = 1_700_000_000_000

    def ev(eid, sym, side, direction, etype, ts):
        return WallEvent(
            event_id=eid,
            symbol=sym,
            side=side,
            direction=direction,
            event_type=etype,
            ts_ms=ts,
            wall_price=1.0,
            wall_qty=1.0,
            wall_dist_bps=0.0,
            mid=1.0,
            best_bid=1.0,
            best_ask=1.1,
            spread_bps=0.0,
            imbalance_l10=0.0,
            qty_vs_median=0.0,
            persistence_s=0.0,
            source_file="s",
            threshold_qty_median_mult=3.0,
        )

    events = [
        ev("a", "BTCUSDT", "BID", "LONG", "WALL_TOUCH", base),
        ev("b", "BTCUSDT", "BID", "LONG", "WALL_ABSORPTION_PROXY", base + 1000),
        ev("c", "BTCUSDT", "BID", "LONG", "WALL_RECLAIM", base + 5000),
        ev("d", "DOGEUSDT", "ASK", "SHORT", "WALL_TOUCH", base),
    ]
    chains = build_chains(events, seed=1)
    assert any(c.direction == "LONG" and c.complete for c in chains)
    assert any(c.symbol == "DOGEUSDT" and c.direction == "SHORT" for c in chains)


def test_zstd_streaming_roundtrip(tmp_path: Path) -> None:
    start = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
    writer = SegmentWriter(symbol="DOGEUSDT", directory=tmp_path, start_utc=start)
    writer.open()
    book = _book_snap()
    writer.write_line(
        serialize_rotation_checkpoint(
            book, "DOGEUSDT", topic="orderbook.200.DOGEUSDT", ts_ms=int(start.timestamp() * 1000), received_at=start
        ),
        kind="rotation_checkpoint",
        sequence=100,
        update_id=book.last_u,
    )
    path, mp = writer.close(end_utc=start.replace(hour=2))
    man = json.loads(mp.read_text())
    assert man["completion_status"] == "closed"
    assert man["replay_source"] == "rotation_checkpoint"
    lines = list(iter_decompressed_lines(path))
    assert len(lines) == 1
    assert lines[0][1]["type"] == "rotation_checkpoint"


def test_v2_lifecycles_non_overlapping_primary() -> None:
    from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import (
        build_chains_v2,
        build_wall_lifecycles,
        audit_v1_chain_overcount,
    )
    from orderbook_analyse.ob200_v3_raw_discovery.analysis import build_chains
    from orderbook_analyse.ob200_v3_raw_discovery.walls import WallEvent

    base = 1_700_000_000_000

    def ev(eid, etype, ts, price=100.0):
        return WallEvent(
            event_id=eid,
            symbol="BTCUSDT",
            side="BID",
            direction="LONG",
            event_type=etype,
            ts_ms=ts,
            wall_price=price,
            wall_qty=10.0,
            wall_dist_bps=1.0,
            mid=100.05,
            best_bid=100.0,
            best_ask=100.1,
            spread_bps=10.0,
            imbalance_l10=0.0,
            qty_vs_median=3.0,
            persistence_s=1.0,
            source_file="s",
            threshold_qty_median_mult=3.0,
        )

    events = [
        ev("a", "WALL_APPEAR", base),
        ev("t1", "WALL_TOUCH", base + 1000),
        ev("t2", "WALL_TOUCH", base + 2000),
        ev("t3", "WALL_TOUCH", base + 3000),
        ev("ab", "WALL_ABSORPTION_PROXY", base + 4000),
        ev("rc", "WALL_RECLAIM", base + 5000),
    ]
    v1 = build_chains(events, seed=1)
    v1_complete = sum(1 for c in v1 if c.complete)
    assert v1_complete >= 3
    lcs = build_wall_lifecycles(events, seed=1, cooldown_ms=60_000)
    assert len(lcs) == 1
    chains = build_chains_v2(lcs, seed=1)
    primary_complete = [
        c for c in chains if c.is_primary and c.completion_class == "COMPLETE_PRIMARY"
    ]
    assert len(primary_complete) == 1
    audit = audit_v1_chain_overcount(v1, events, lcs, chains)
    assert audit[0].v1_chains_complete >= audit[0].complete_primary_v2
