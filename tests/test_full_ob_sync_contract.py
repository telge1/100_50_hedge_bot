"""Bybit Full-OB sync contract tests."""

from __future__ import annotations

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import (
    AlignStatus,
    BufferState,
    DeltaMeta,
    DeltaOutcome,
    align_snapshot_to_buffer,
    classify_live_delta,
)


def _delta(u: int, seq: int, **extra) -> dict:
    data = {"s": "BTCUSDT", "b": [["100", "1"]], "a": [["101", "1"]], "u": u, "seq": seq}
    data.update(extra)
    return {"topic": "orderbook.full.BTCUSDT", "type": "delta", "ts": 1, "cts": 1, "data": data}


def test_buffer_accepts_contiguous_u():
    buf = BufferState()
    assert buf.push(u=10, seq=100, payload=_delta(10, 100)) == "accepted"
    assert buf.push(u=11, seq=105, payload=_delta(11, 105)) == "accepted"
    assert len(buf.items) == 2


def test_buffer_clears_on_u_gap():
    buf = BufferState()
    buf.push(u=10, seq=100, payload=_delta(10, 100))
    assert buf.push(u=12, seq=110, payload=_delta(12, 110)) == "cleared_gap"
    assert len(buf.items) == 1
    assert buf.items[0].u == 12
    assert buf.cleared_for_gap == 1


def test_buffer_discards_decreasing_seq():
    buf = BufferState()
    buf.push(u=10, seq=100, payload=_delta(10, 100))
    assert buf.push(u=11, seq=99, payload=_delta(11, 99)) == "discarded_seq"
    assert len(buf.items) == 1


def test_align_need_newer_snapshot():
    buffer = [
        DeltaMeta(u=20, seq=200, payload=_delta(20, 200)),
        DeltaMeta(u=21, seq=210, payload=_delta(21, 210)),
    ]
    r = align_snapshot_to_buffer(snap_u=5, snap_seq=50, buffer=buffer)
    assert r.status is AlignStatus.NEED_NEWER_SNAPSHOT


def test_align_seq_u_mismatch():
    buffer = [DeltaMeta(u=20, seq=200, payload=_delta(20, 200))]
    r = align_snapshot_to_buffer(snap_u=19, snap_seq=200, buffer=buffer)
    assert r.status is AlignStatus.SEQ_U_MISMATCH


def test_align_match_and_remaining():
    buffer = [
        DeltaMeta(u=19, seq=190, payload=_delta(19, 190)),
        DeltaMeta(u=20, seq=200, payload=_delta(20, 200)),
        DeltaMeta(u=21, seq=210, payload=_delta(21, 210)),
    ]
    r = align_snapshot_to_buffer(snap_u=20, snap_seq=200, buffer=buffer)
    assert r.status is AlignStatus.ALIGNED
    assert len(r.remaining) == 1
    assert r.remaining[0].u == 21


def test_live_u_plus_one_gap_and_reset():
    assert classify_live_delta(local_u=10, event_u=11, event_seq=20, local_seq=15) is DeltaOutcome.APPLIED
    assert classify_live_delta(local_u=10, event_u=12, event_seq=20, local_seq=15) is DeltaOutcome.GAP
    assert classify_live_delta(local_u=10, event_u=1, event_seq=20, local_seq=15) is DeltaOutcome.U_RESET
    assert classify_live_delta(local_u=10, event_u=9, event_seq=20, local_seq=15) is DeltaOutcome.IGNORED_STALE_U
    assert classify_live_delta(local_u=10, event_u=11, event_seq=10, local_seq=15) is DeltaOutcome.IGNORED_DECREASING_SEQ


def test_book_state_enforces_continuity():
    book = FullBookState(symbol="BTCUSDT")
    book.apply_snapshot(bids=[["100", "1"]], asks=[["101", "1"]], u=10, seq=100, ts_ms=1)
    assert book.book_ready
    assert book.apply_delta(bids=[], asks=[], u=11, seq=110, ts_ms=2) is DeltaOutcome.APPLIED
    assert book.apply_delta(bids=[], asks=[], u=11, seq=111, ts_ms=3) is DeltaOutcome.IGNORED_DUP_U
    assert book.apply_delta(bids=[], asks=[], u=9, seq=112, ts_ms=4) is DeltaOutcome.IGNORED_STALE_U
    assert book.apply_delta(bids=[], asks=[], u=13, seq=113, ts_ms=5) is DeltaOutcome.GAP
    assert book.update_id == 11  # unchanged after gap


def test_stale_delta_does_not_mutate():
    book = FullBookState(symbol="BTCUSDT")
    book.apply_snapshot(bids=[["100", "1"]], asks=[["101", "1"]], u=5, seq=1, ts_ms=1)
    out = book.apply_delta(bids=[["100", "9"]], asks=[], u=4, seq=2, ts_ms=2)
    assert out is DeltaOutcome.IGNORED_STALE_U
    assert book.bids[100.0] == 1.0
    assert book.update_id == 5


def test_snapshot_mismatch_does_not_mark_ready_via_align():
    buffer = [DeltaMeta(u=20, seq=200, payload=_delta(20, 200))]
    r = align_snapshot_to_buffer(snap_u=19, snap_seq=200, buffer=buffer)
    assert r.status is not AlignStatus.ALIGNED
