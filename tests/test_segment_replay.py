"""Phase 2 segment replay smoke tests (offline, synthetic events)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orderbook_analyse.orderbook_replay import BookLevelEvent
from orderbook_analyse.replay_segmentation import ReplaySegment
from orderbook_analyse.segment_replay import (
    check_book_invariants,
    decide_phase2,
    filter_events_to_segment,
    replay_segment_events,
    sample_grid,
)

TS0 = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)


def _ev(
    ts: datetime,
    *,
    side: str,
    price: str,
    qty: str,
    message_type: str,
    update_id: int,
    seq: int,
    level_index: int = 0,
) -> BookLevelEvent:
    return BookLevelEvent(
        exchange_ts=ts,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        message_type=message_type,
        update_id=update_id,
        cross_sequence=seq,
        level_index=level_index,
    )


def _snapshot(ts: datetime, u: int, seq: int) -> list[BookLevelEvent]:
    return [
        _ev(ts, side="bid", price="10.0", qty="5", message_type="snapshot", update_id=u, seq=seq, level_index=0),
        _ev(ts, side="ask", price="10.1", qty="5", message_type="snapshot", update_id=u, seq=seq, level_index=0),
    ]


def _delta(
    ts: datetime,
    u: int,
    seq: int,
    *,
    side: str = "bid",
    price: str = "9.9",
    qty: str = "2",
) -> list[BookLevelEvent]:
    return [
        _ev(ts, side=side, price=price, qty=qty, message_type="delta", update_id=u, seq=seq),
    ]


def _segment(
    *,
    start: datetime,
    end: datetime,
    boot_u: int,
    boot_seq: int,
    last_u: int,
    last_seq: int,
    segment_id: str = "seg_001",
    is_replayable: bool = True,
) -> ReplaySegment:
    return ReplaySegment(
        segment_id=segment_id,
        symbol="TEST",
        segment_start_ts=start,
        segment_end_ts=end,
        bootstrap_snapshot_ts=start,
        bootstrap_update_id=boot_u,
        bootstrap_cross_sequence=boot_seq,
        first_delta_update_id=boot_u + 1,
        last_update_id=last_u,
        last_cross_sequence=last_seq,
        message_count=last_u - boot_u + 1,
        delta_message_count=last_u - boot_u,
        snapshot_message_count=1,
        duration_sec=(end - start).total_seconds(),
        bid_snapshot_levels=1,
        ask_snapshot_levels=1,
        is_replayable=is_replayable,
        discard_reason=None if is_replayable else "insufficient_duration",
        end_reason="analysis_end",
    )


def test_successful_replay_end_state_and_determinism() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=10)
    events = (
        _snapshot(start, 100, 1)
        + _delta(start + timedelta(seconds=60), 101, 2)
        + _delta(start + timedelta(seconds=120), 102, 3, side="ask", price="10.2", qty="3")
    )
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=102, last_seq=3)
    r1, e1, s1 = replay_segment_events(events, segment=seg, warmup_seconds=60, sample_interval_seconds=60)
    r2, e2, s2 = replay_segment_events(events, segment=seg, warmup_seconds=60, sample_interval_seconds=60)
    assert r1.replay_status == "REPLAY_OK"
    assert r1.invariants_ok is True
    assert r1.actual_last_update_id == 102
    assert e1 is not None
    assert e1.best_bid == Decimal("10.0") or e1.best_bid == Decimal("9.9")
    assert e1.best_ask is not None and e1.best_bid is not None
    assert e1.best_bid < e1.best_ask
    # Deterministic fachlich (runtime_sec may differ)
    row1 = {k: v for k, v in r1.to_row().items() if k != "runtime_sec"}
    row2 = {k: v for k, v in r2.to_row().items() if k != "runtime_sec"}
    assert row1 == row2
    assert e1.to_row() == e2.to_row()
    assert s1 == s2


def test_filter_keeps_last_update_at_segment_end() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=5, milliseconds=21)
    events = (
        _snapshot(start, 10, 1)
        + _delta(start + timedelta(seconds=1), 11, 2)
        + _delta(end, 12, 3)
        + _delta(end + timedelta(milliseconds=500), 13, 4)
        + _snapshot(end + timedelta(seconds=1), 50, 99)
    )
    filtered = filter_events_to_segment(
        events,
        bootstrap_update_id=10,
        bootstrap_cross_sequence=1,
        segment_end_ts=end,
        last_update_id=12,
    )
    assert {ev.update_id for ev in filtered} == {10, 11, 12}
    assert all(ev.exchange_ts <= end for ev in filtered)


def test_segment_bounds_filter() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=5)
    boot = _snapshot(start, 10, 1)
    before = _delta(start - timedelta(seconds=1), 9, 0)
    after = _delta(end + timedelta(seconds=1), 15, 6)
    gap_zone = _delta(end + timedelta(seconds=30), 20, 10)
    inside = _delta(start + timedelta(seconds=30), 11, 2)
    next_boot = _snapshot(end + timedelta(minutes=1), 30, 20)
    filtered = filter_events_to_segment(
        before + boot + inside + after + gap_zone + next_boot,
        bootstrap_update_id=10,
        bootstrap_cross_sequence=1,
        segment_end_ts=end,
        last_update_id=11,
    )
    assert all(ev.update_id in {10, 11} for ev in filtered)
    assert all(ev.exchange_ts <= end for ev in filtered)
    assert not any(ev.update_id == 30 for ev in filtered)


def test_two_segments_independent_state() -> None:
    s1_start = TS0
    s1_end = TS0 + timedelta(minutes=6)
    s2_start = TS0 + timedelta(minutes=10)
    s2_end = TS0 + timedelta(minutes=16)
    e1 = (
        _snapshot(s1_start, 1, 1)
        + _delta(s1_start + timedelta(seconds=30), 2, 2, price="1.0", qty="100")
    )
    e2 = _snapshot(s2_start, 50, 50) + _delta(s2_start + timedelta(seconds=30), 51, 51)
    seg1 = _segment(
        start=s1_start, end=s1_end, boot_u=1, boot_seq=1, last_u=2, last_seq=2, segment_id="a"
    )
    seg2 = _segment(
        start=s2_start, end=s2_end, boot_u=50, boot_seq=50, last_u=51, last_seq=51, segment_id="b"
    )
    r1, end1, _ = replay_segment_events(e1, segment=seg1, warmup_seconds=60)
    r2, end2, _ = replay_segment_events(e2, segment=seg2, warmup_seconds=60)
    assert r1.replay_status == "REPLAY_OK"
    assert r2.replay_status == "REPLAY_OK"
    assert end1 is not None and end2 is not None
    assert end1.last_update_id == 2
    assert end2.last_update_id == 51
    assert end2.best_bid != Decimal("1.0")  # first segment state not carried over


def test_bootstrap_missing() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = _delta(start + timedelta(seconds=1), 101, 2)
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=101, last_seq=2)
    r, e, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_BOOTSTRAP"
    assert e is None


def test_update_id_gap_inside_segment() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = (
        _snapshot(start, 100, 1)
        + _delta(start + timedelta(seconds=10), 101, 2)
        + _delta(start + timedelta(seconds=20), 103, 4)  # gap: skipped 102
    )
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=103, last_seq=4)
    r, e, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_GAP"
    assert e is None


def test_cross_sequence_backwards() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = (
        _snapshot(start, 100, 10)
        + _delta(start + timedelta(seconds=10), 101, 9)
    )
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=10, last_u=101, last_seq=9)
    r, _, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_SEQUENCE"


def test_crossed_book_invariant() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = (
        _snapshot(start, 100, 1)
        + _delta(start + timedelta(seconds=10), 101, 2, side="bid", price="10.5", qty="1")
    )
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=101, last_seq=2)
    r, e, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_INVARIANT"
    assert "crossed" in (r.error_message or "")
    assert e is None


def test_empty_side_invariant() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = [
        _ev(start, side="bid", price="10.0", qty="5", message_type="snapshot", update_id=100, seq=1),
        # no ask
    ]
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=100, last_seq=1)
    r, _, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_INVARIANT"
    assert "empty ask" in (r.error_message or "")


def test_end_update_id_mismatch() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = _snapshot(start, 100, 1) + _delta(start + timedelta(seconds=10), 101, 2)
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=999, last_seq=2)
    r, _, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_INVARIANT"
    assert "end update_id mismatch" in (r.error_message or "")


def test_unexpected_mid_segment_snapshot() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=6)
    events = (
        _snapshot(start, 100, 1)
        + _delta(start + timedelta(seconds=10), 101, 2)
        + _snapshot(start + timedelta(seconds=20), 102, 3)
    )
    seg = _segment(start=start, end=end, boot_u=100, boot_seq=1, last_u=102, last_seq=3)
    r, _, _ = replay_segment_events(events, segment=seg, warmup_seconds=60)
    assert r.replay_status == "REPLAY_FAILED_INVARIANT"
    assert "snapshot" in (r.error_message or "").lower()


def test_warmup_longer_than_segment() -> None:
    start = TS0
    end = TS0 + timedelta(seconds=120)
    events = _snapshot(start, 1, 1) + _delta(start + timedelta(seconds=30), 2, 2)
    seg = _segment(start=start, end=end, boot_u=1, boot_seq=1, last_u=2, last_seq=2)
    r, e, _ = replay_segment_events(events, segment=seg, warmup_seconds=300)
    assert r.replay_status == "REPLAY_OK_NO_POST_WARMUP"
    assert r.feature_emission_start_ts is None
    assert e is not None
    assert r.actual_last_update_id == 2


def test_warmup_exact_and_longer() -> None:
    start = TS0
    end_exact = TS0 + timedelta(seconds=300)
    events = _snapshot(start, 1, 1) + _delta(start + timedelta(seconds=100), 2, 2)
    seg = _segment(start=start, end=end_exact, boot_u=1, boot_seq=1, last_u=2, last_seq=2)
    r, _, _ = replay_segment_events(events, segment=seg, warmup_seconds=300)
    assert r.replay_status == "REPLAY_OK_NO_POST_WARMUP"

    end_long = TS0 + timedelta(seconds=400)
    seg2 = _segment(start=start, end=end_long, boot_u=1, boot_seq=1, last_u=2, last_seq=2)
    r2, _, _ = replay_segment_events(events, segment=seg2, warmup_seconds=300)
    assert r2.replay_status == "REPLAY_OK"
    assert r2.feature_emission_start_ts == start + timedelta(seconds=300)
    assert r2.post_warmup_duration_sec == pytest.approx(100.0)


def test_sample_grid_deterministic() -> None:
    start = TS0
    end = TS0 + timedelta(seconds=180)
    g1 = sample_grid(start, end, interval_seconds=60)
    g2 = sample_grid(start, end, interval_seconds=60)
    assert g1 == g2
    assert g1 == [start, start + timedelta(seconds=60), start + timedelta(seconds=120), end]


def test_decide_phase2() -> None:
    assert (
        decide_phase2(
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE",
            gap_count=0,
            stats={"segments_replayable": 2, "segments_replay_ok": 2, "segments_replay_failed": 0},
        )
        == "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE"
    )
    assert (
        decide_phase2(
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS",
            gap_count=2,
            stats={"segments_replayable": 3, "segments_replay_ok": 3, "segments_replay_failed": 0},
        )
        == "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE_WITH_GAPS"
    )
    assert (
        decide_phase2(
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE",
            gap_count=0,
            stats={"segments_replayable": 3, "segments_replay_ok": 2, "segments_replay_failed": 1},
        )
        == "FULL_HISTORY_SEGMENT_REPLAY_PARTIAL"
    )
    assert (
        decide_phase2(
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE",
            gap_count=0,
            stats={"segments_replayable": 2, "segments_replay_ok": 0, "segments_replay_failed": 2},
        )
        == "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
    )


def test_check_book_invariants_empty() -> None:
    from orderbook_analyse.orderbook_replay import OrderBookState

    book = OrderBookState(has_snapshot=True)
    errs = check_book_invariants(book)
    assert any("empty bid" in e for e in errs)
    assert any("empty ask" in e for e in errs)
