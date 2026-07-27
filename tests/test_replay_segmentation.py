"""Unit tests for replay segment discovery (synthetic messages, no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orderbook_analyse.replay_segmentation import (
    OrderbookMessage,
    discover_replay_segments,
    missing_update_count,
    segmentation_integrity_checks,
)

TS0 = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)


def _msg(
    i: int,
    *,
    update_id: int,
    seq: int,
    message_type: str = "delta",
    bid: int = 200,
    ask: int = 200,
    step_seconds: int = 1,
) -> OrderbookMessage:
    return OrderbookMessage(
        exchange_ts=TS0 + timedelta(seconds=step_seconds * i),
        update_id=update_id,
        cross_sequence=seq,
        message_type=message_type,
        bid_level_count=bid,
        ask_level_count=ask,
        total_level_count=bid + ask,
    )


def test_missing_update_count_vanry_examples() -> None:
    assert missing_update_count(1546781, 1546830) == 48
    assert missing_update_count(1560824, 1560902) == 77
    assert missing_update_count(10, 11) == 0


def test_snapshot_plus_continuous_deltas() -> None:
    msgs = [
        _msg(0, update_id=100, seq=1, message_type="snapshot"),
        _msg(1, update_id=101, seq=2),
        _msg(2, update_id=102, seq=3),
        _msg(3, update_id=103, seq=4),
    ]
    # stretch duration >= 5 min
    msgs = [
        _msg(0, update_id=100, seq=1, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=101, seq=2, step_seconds=60),
        _msg(2, update_id=102, seq=3, step_seconds=60),
        _msg(3, update_id=103, seq=4, step_seconds=60),
        _msg(4, update_id=104, seq=5, step_seconds=60),
        _msg(5, update_id=105, seq=6, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=5, min_snapshot_levels_per_side=150
    )
    assert len(res.segments) == 1
    assert res.segments[0].is_replayable is True
    assert res.segments[0].bootstrap_update_id == 100
    assert res.segments[0].last_update_id == 105
    assert res.segments[0].end_reason == "analysis_end"
    assert len(res.gaps) == 0


def test_complete_snapshot_thresholds() -> None:
    msgs = [
        _msg(0, update_id=1, seq=1, message_type="snapshot", bid=200, ask=200),
        _msg(1, update_id=2, seq=2, message_type="snapshot", bid=150, ask=150),
        _msg(2, update_id=3, seq=3, message_type="snapshot", bid=149, ask=200),
        _msg(3, update_id=4, seq=4, message_type="snapshot", bid=200, ask=10),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=0, min_snapshot_levels_per_side=150
    )
    assert res.complete_snapshot_count == 2
    assert res.incomplete_snapshot_count == 2


def test_update_id_gap_then_complete_snapshot() -> None:
    # VANRY-like: last continuous id 1546781, next snapshot 1546830 → missing 48
    msgs = [
        _msg(0, update_id=1546781, seq=10, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=1546781 + 1, seq=11, step_seconds=60),  # keep segment alive a bit
        _msg(5, update_id=1546781 + 5, seq=15, step_seconds=60),
    ]
    # rebuild continuous chain ending at 1546781 for exact missing=48 example
    msgs = [
        _msg(0, update_id=1546780, seq=9, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=1546781, seq=10, step_seconds=60),
        _msg(10, update_id=1546830, seq=20, message_type="snapshot", step_seconds=60),
        _msg(11, update_id=1546831, seq=21, step_seconds=60),
        _msg(20, update_id=1546840, seq=30, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="VANRYUSDT", segment_minutes_min=1, min_snapshot_levels_per_side=150
    )
    assert len(res.segments) >= 2
    assert res.segments[0].last_update_id == 1546781
    assert res.segments[1].bootstrap_update_id == 1546830
    gap = next(g for g in res.gaps if g.next_update_id == 1546830)
    assert gap.previous_update_id == 1546781
    assert gap.missing_update_count == 48


def test_gap_followed_by_deltas_without_snapshot() -> None:
    msgs = [
        _msg(0, update_id=10, seq=1, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=11, seq=2, step_seconds=60),
        _msg(2, update_id=20, seq=3, step_seconds=60),  # gap delta — discarded
        _msg(3, update_id=21, seq=4, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=1, min_snapshot_levels_per_side=150
    )
    assert len(res.segments) == 1
    assert res.segments[0].last_update_id == 11
    assert any(g.reason == "update_id_gap" for g in res.gaps)
    # no recovery snapshot → trailing discard gap
    assert any(g.recovered_at_snapshot_ts is None for g in res.gaps)


def test_multiple_segments() -> None:
    msgs = []
    # segment A
    for i in range(0, 6):
        msgs.append(
            _msg(
                i,
                update_id=100 + i,
                seq=i + 1,
                message_type="snapshot" if i == 0 else "delta",
                step_seconds=60,
            )
        )
    # gap + segment B
    for i in range(0, 6):
        msgs.append(
            _msg(
                20 + i,
                update_id=200 + i,
                seq=100 + i,
                message_type="snapshot" if i == 0 else "delta",
                step_seconds=60,
            )
        )
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=3, min_snapshot_levels_per_side=150
    )
    assert len([s for s in res.segments if s.is_replayable]) == 2


def test_snapshot_reset_without_update_gap() -> None:
    # Unusual but allowed: next snapshot has update_id = last+1
    msgs = [
        _msg(0, update_id=50, seq=1, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=51, seq=2, step_seconds=60),
        _msg(2, update_id=52, seq=3, message_type="snapshot", step_seconds=60),
        _msg(3, update_id=53, seq=4, step_seconds=60),
        _msg(8, update_id=58, seq=9, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=1, min_snapshot_levels_per_side=150
    )
    assert len(res.segments) == 2
    assert res.segments[0].end_reason == "next_snapshot_reset"
    assert res.gaps[0].missing_update_count == 0
    assert res.gaps[0].reason == "next_snapshot_reset"


def test_cross_sequence_backwards() -> None:
    msgs = [
        _msg(0, update_id=1, seq=10, message_type="snapshot", step_seconds=60),
        _msg(1, update_id=2, seq=11, step_seconds=60),
        _msg(2, update_id=3, seq=5, step_seconds=60),  # backwards
        _msg(10, update_id=100, seq=20, message_type="snapshot", step_seconds=60),
        _msg(15, update_id=105, seq=25, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=1, min_snapshot_levels_per_side=150
    )
    assert res.backwards_sequence_count >= 1
    assert any(g.reason == "cross_sequence_backwards" for g in res.gaps)
    assert any(s.bootstrap_update_id == 100 for s in res.segments)


def test_short_segment_insufficient_duration() -> None:
    msgs = [
        _msg(0, update_id=1, seq=1, message_type="snapshot", step_seconds=1),
        _msg(1, update_id=2, seq=2, step_seconds=1),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=5, min_snapshot_levels_per_side=150
    )
    assert len(res.segments) == 1
    assert res.segments[0].is_replayable is False
    assert res.segments[0].end_reason == "insufficient_duration"


def test_segment_starts_at_snapshot_time() -> None:
    msgs = [
        _msg(0, update_id=7, seq=1, message_type="snapshot", step_seconds=60),
        _msg(10, update_id=17, seq=11, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=5, min_snapshot_levels_per_side=150
    )
    assert res.segments[0].segment_start_ts == msgs[0].exchange_ts
    assert res.segments[0].bootstrap_snapshot_ts == msgs[0].exchange_ts


def test_incomplete_bid_only_snapshot_not_bootstrap() -> None:
    msgs = [
        _msg(0, update_id=1, seq=1, message_type="snapshot", bid=200, ask=0),
        _msg(1, update_id=2, seq=2),
        _msg(10, update_id=50, seq=10, message_type="snapshot", bid=200, ask=200, step_seconds=60),
        _msg(20, update_id=60, seq=20, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=5, min_snapshot_levels_per_side=150
    )
    assert all(s.bootstrap_update_id == 50 for s in res.segments)


def test_integrity_non_overlapping() -> None:
    msgs = [
        _msg(0, update_id=1, seq=1, message_type="snapshot", step_seconds=60),
        _msg(5, update_id=6, seq=6, step_seconds=60),
        _msg(20, update_id=100, seq=50, message_type="snapshot", step_seconds=60),
        _msg(30, update_id=110, seq=60, step_seconds=60),
    ]
    res = discover_replay_segments(
        msgs, symbol="TEST", segment_minutes_min=3, min_snapshot_levels_per_side=150
    )
    integ = segmentation_integrity_checks(res, min_snapshot_levels_per_side=150)
    assert integ["ok"] is True


def test_grouped_snapshot_level_counts() -> None:
    # Message already aggregated; total = bid+ask
    m = _msg(0, update_id=1, seq=1, message_type="snapshot", bid=200, ask=200)
    assert m.total_level_count == 400
    assert m.is_complete(150) is True
    assert m.is_complete(201) is False
