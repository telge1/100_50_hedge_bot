"""Phase 4 wall history unit tests (synthetic books, no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orderbook_analyse.orderbook_replay import BookLevelEvent, OrderBookReplayer
from orderbook_analyse.replay_segmentation import ReplayGap, ReplaySegment
from orderbook_analyse.wall_history import (
    WallHistoryParams,
    apply_test_break_flags,
    build_sequences_and_transitions,
    check_wall_history_integrity,
    decide_full_analysis,
    decide_phase4_wall,
    join_timeline_with_walls,
    match_observations,
    observe_book_walls,
    parse_wall_resolutions,
    replay_segment_wall_history,
    wall_sample_times,
)

TS0 = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)


def _ev(ts, *, side, price, qty, message_type, update_id, seq, level_index=0):
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


def _book_with_walls() -> OrderBookReplayer:
    """Book with a dominant bid wall far from mid and ask wall."""
    ts = TS0
    events = []
    # mid ~ 10.05: bids around 10.0, asks around 10.1; add huge walls
    u = 1
    levels = []
    for i, (side, price, qty) in enumerate(
        [
            ("bid", "9.90", "1"),
            ("bid", "9.95", "1"),
            ("bid", "10.00", "100"),  # wall
            ("ask", "10.10", "1"),
            ("ask", "10.15", "1"),
            ("ask", "10.20", "100"),  # wall
        ]
    ):
        levels.append(
            _ev(ts, side=side, price=price, qty=qty, message_type="snapshot", update_id=u, seq=1, level_index=i)
        )
    # fill more normal levels
    for i in range(10):
        levels.append(
            _ev(ts, side="bid", price=str(9.80 - i * 0.01), qty="1", message_type="snapshot", update_id=u, seq=1, level_index=100 + i)
        )
        levels.append(
            _ev(ts, side="ask", price=str(10.30 + i * 0.01), qty="1", message_type="snapshot", update_id=u, seq=1, level_index=200 + i)
        )
    r = OrderBookReplayer()
    from orderbook_analyse.orderbook_replay import group_messages

    for mt, uid, seq, t, lv in group_messages(levels):
        r.apply_message(mt, uid, seq, t, lv)
    return r


def _segment(**kwargs) -> ReplaySegment:
    base = dict(
        segment_id="S0001",
        symbol="TEST",
        segment_start_ts=TS0,
        segment_end_ts=TS0 + timedelta(minutes=20),
        bootstrap_snapshot_ts=TS0,
        bootstrap_update_id=1,
        bootstrap_cross_sequence=1,
        first_delta_update_id=2,
        last_update_id=10,
        last_cross_sequence=10,
        message_count=10,
        delta_message_count=9,
        snapshot_message_count=1,
        duration_sec=1200,
        bid_snapshot_levels=20,
        ask_snapshot_levels=20,
        is_replayable=True,
        discard_reason=None,
        end_reason="analysis_end",
    )
    base.update(kwargs)
    return ReplaySegment(**base)


def test_parse_wall_resolutions() -> None:
    assert parse_wall_resolutions(None) == [5.0, 10.0, 20.0, 50.0]
    assert parse_wall_resolutions("10,20") == [10.0, 20.0]
    with pytest.raises(ValueError):
        parse_wall_resolutions("0,10")


def test_wall_sample_times_warmup_and_bounds() -> None:
    start = TS0
    end = TS0 + timedelta(minutes=12)
    samples = wall_sample_times(start, end, interval_seconds=60, warmup_seconds=300)
    assert samples[0] == start + timedelta(seconds=300)
    assert samples[-1] <= end
    assert all(t >= start + timedelta(seconds=300) for t in samples)
    short = wall_sample_times(start, start + timedelta(seconds=200), interval_seconds=60, warmup_seconds=300)
    assert short == []


def test_observe_bid_and_ask_walls_multi_resolution() -> None:
    book = _book_with_walls().book
    params = WallHistoryParams(resolutions_bps=(10.0, 20.0), distance_max_pct=5.0, wall_multiple_min=2.0, percentile_min=80.0)
    obs, clusters, oid = observe_book_walls(
        book, symbol="TEST", segment_id="S0001", sample_ts=TS0 + timedelta(minutes=6), params=params
    )
    assert oid > 1
    assert any(o["side"] == "bid" and o["is_wall"] for o in obs)
    assert any(o["side"] == "ask" and o["is_wall"] for o in obs)
    assert {o["resolution"] for o in obs} >= {"auto_10bps", "auto_20bps"}
    assert all(o.get("distance_to_mid_bps") is not None for o in obs if o["is_wall"])
    # no hard-coded APT reference levels
    assert all(o["wall_price"] not in {"0.617", "0.628"} for o in obs)


def test_match_observations_and_tie_break() -> None:
    mid = Decimal("10")
    prev = [
        {
            "side": "ask",
            "resolution": "auto_10bps",
            "_price_dec": Decimal("10.20"),
            "_notional_dec": Decimal("100"),
            "_mid_dec": mid,
            "cluster_id": "c1",
            "distance_to_mid_bps": 200,
        }
    ]
    cur_near = {
        "side": "ask",
        "resolution": "auto_10bps",
        "_price_dec": Decimal("10.21"),
        "_notional_dec": Decimal("110"),
        "_mid_dec": mid,
        "cluster_id": "c1",
        "distance_to_mid_bps": 210,
    }
    cur_far = {
        "side": "ask",
        "resolution": "auto_10bps",
        "_price_dec": Decimal("10.50"),
        "_notional_dec": Decimal("100"),
        "_mid_dec": mid,
        "cluster_id": "c2",
        "distance_to_mid_bps": 500,
    }
    matches = match_observations(prev, [cur_near, cur_far], match_distance_bps=10.0)
    assert len(matches) == 1
    assert matches[0][1] is cur_near
    # outside match distance → no match
    assert match_observations(prev, [cur_far], match_distance_bps=10.0) == []


def test_no_double_assignment_in_match() -> None:
    mid = Decimal("10")
    prev = [
        {"side": "bid", "resolution": "auto_10bps", "_price_dec": Decimal("9.9"), "_notional_dec": Decimal("50"), "_mid_dec": mid, "cluster_id": "a"},
        {"side": "bid", "resolution": "auto_10bps", "_price_dec": Decimal("9.91"), "_notional_dec": Decimal("40"), "_mid_dec": mid, "cluster_id": "b"},
    ]
    cur = [
        {"side": "bid", "resolution": "auto_10bps", "_price_dec": Decimal("9.905"), "_notional_dec": Decimal("45"), "_mid_dec": mid, "cluster_id": "a"},
    ]
    matches = match_observations(prev, cur, match_distance_bps=20.0)
    assert len(matches) == 1
    used_prev = {id(m[0]) for m in matches}
    used_cur = {id(m[1]) for m in matches}
    assert len(used_prev) == 1 and len(used_cur) == 1


def test_lifecycle_appeared_grew_shrank_disappeared() -> None:
    mid = Decimal("10")
    t1 = TS0 + timedelta(minutes=6)
    t2 = TS0 + timedelta(minutes=7)
    t3 = TS0 + timedelta(minutes=8)

    def obs(ts, price, notion, oid):
        return {
            "wall_observation_id": oid,
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": ts.isoformat(),
            "resolution": "auto_10bps",
            "side": "ask",
            "is_wall": True,
            "wall_multiple": 5.0,
            "percentile": 95.0,
            "depth_share": 0.2,
            "distance_to_mid_bps": float(abs(Decimal(price) - mid) / mid * 10000),
            "cluster_id": "c1",
            "_price_dec": Decimal(price),
            "_notional_dec": Decimal(notion),
            "_mid_dec": mid,
        }

    observations = [
        obs(t1, "10.20", "100", "o1"),
        obs(t2, "10.20", "150", "o2"),  # grew
        # t3 has no wall observation → DISAPPEARED (empty sample implied by gap in times)
        # Force disappearance by adding only a different wall at t3
        {
            **obs(t3, "10.50", "80", "o3"),
            "cluster_id": "other",
            "distance_to_mid_bps": 500.0,
        },
    ]
    params = WallHistoryParams(notional_change_threshold_pct=20.0, match_distance_bps=10.0)
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=params,
    )
    types = {tr["transition_type"] for tr in transitions}
    assert "APPEARED" in types
    assert "GREW" in types
    assert "DISAPPEARED" in types
    assert any(s["end_reason"] == "DISAPPEARED" for s in sequences)
    assert all(s["sample_count"] >= 1 for s in sequences)
    assert all(s["age_seconds"] >= 0 for s in sequences)
    seq_ids = [s["wall_sequence_id"] for s in sequences]
    assert len(seq_ids) == len(set(seq_ids))


def test_sequences_do_not_cross_segments() -> None:
    mid = Decimal("10")
    params = WallHistoryParams()
    obs_a = [
        {
            "wall_observation_id": "a1",
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "resolution": "auto_10bps",
            "side": "bid",
            "is_wall": True,
            "wall_multiple": 4.0,
            "percentile": 90.0,
            "depth_share": 0.1,
            "distance_to_mid_bps": 50.0,
            "_price_dec": Decimal("9.95"),
            "_notional_dec": Decimal("80"),
            "_mid_dec": mid,
        }
    ]
    seq1, _ = build_sequences_and_transitions(
        obs_a, symbol="TEST", segment_id="S0001", segment_end_ts=TS0 + timedelta(minutes=10), params=params
    )
    seq2, _ = build_sequences_and_transitions(
        [
            {
                **obs_a[0],
                "segment_id": "S0002",
                "wall_observation_id": "b1",
                "sample_ts": (TS0 + timedelta(minutes=16)).isoformat(),
            }
        ],
        symbol="TEST",
        segment_id="S0002",
        segment_end_ts=TS0 + timedelta(minutes=30),
        params=params,
    )
    assert seq1[0]["wall_sequence_id"].startswith("TEST:S0001:")
    assert seq2[0]["wall_sequence_id"].startswith("TEST:S0002:")
    assert seq1[0]["wall_sequence_id"] != seq2[0]["wall_sequence_id"]


def test_apply_test_and_break_ask_wall() -> None:
    sequences = [
        {
            "symbol": "TEST",
            "segment_id": "S0001",
            "wall_sequence_id": "TEST:S0001:ASK:W000001",
            "side": "ask",
            "resolution": "auto_10bps",
            "first_seen_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "last_seen_ts": (TS0 + timedelta(minutes=8)).isoformat(),
            "closed_ts": (TS0 + timedelta(minutes=8)).isoformat(),
            "first_price": "10.20",
            "last_price": "10.20",
            "min_price": "10.20",
            "max_price": "10.20",
            "first_notional": "100",
            "last_notional": "100",
            "was_tested": False,
            "was_broken": False,
            "end_reason": "SEGMENT_END",
            "max_distance_bps": 150,
            "min_distance_bps": 150,
        }
    ]
    transitions: list = []
    # 3 bps touch then confirmed break close beyond wall + 5 bps buffer
    wall = Decimal("10.20")
    high_touch = wall * (Decimal("1") - Decimal("3") / Decimal("10000"))
    path = [
        (TS0 + timedelta(minutes=6), Decimal("10.05"), high_touch, Decimal("10.00")),
        (TS0 + timedelta(minutes=8), Decimal("10.25"), Decimal("10.30"), Decimal("10.20")),
    ]
    apply_test_break_flags(sequences, transitions, sample_mids=path, params=WallHistoryParams())
    assert sequences[0]["was_tested"] is True
    assert sequences[0]["touched"] is True
    assert sequences[0]["was_broken"] is True
    assert sequences[0]["confirmed_broken"] is True
    assert sequences[0]["min_test_distance_bps"] is not None
    assert sequences[0]["test_price_source"] == "segment_replay_mid_high_low"
    assert any(t["transition_type"] == "TESTED" for t in transitions)
    assert any(t["transition_type"] == "BROKEN" for t in transitions)


def test_disappeared_before_test_flag() -> None:
    sequences = [
        {
            "symbol": "TEST",
            "segment_id": "S0001",
            "wall_sequence_id": "TEST:S0001:BID:W000001",
            "side": "bid",
            "resolution": "auto_10bps",
            "first_seen_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "last_seen_ts": (TS0 + timedelta(minutes=7)).isoformat(),
            "first_price": "9.90",
            "last_price": "9.90",
            "min_price": "9.90",
            "max_price": "9.90",
            "first_notional": "50",
            "last_notional": "50",
            "was_tested": False,
            "was_broken": False,
            "end_reason": "DISAPPEARED",
            "max_distance_bps": 100,
            "min_distance_bps": 100,
        }
    ]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), Decimal("10"), Decimal("10.01"), Decimal("9.99"))],
        params=WallHistoryParams(),
    )
    assert sequences[0]["disappeared_before_test"] is True
    assert sequences[0]["was_tested"] is False


def test_timeline_join_no_future_and_stale() -> None:
    preferred_obs = [
        {
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "resolution": "auto_10bps",
            "side": "bid",
            "is_wall": True,
            "wall_price": "9.95",
            "wall_notional": "100",
            "wall_multiple": 4.0,
            "percentile": 95.0,
            "depth_share": 0.2,
            "distance_to_mid_bps": 50.0,
        }
    ]
    seg = _segment()
    timeline = [
        {
            "symbol": "TEST",
            "bucket_start": (TS0 + timedelta(minutes=5)).isoformat(),
            "bucket_end": (TS0 + timedelta(minutes=6)).isoformat(),
            "close_price": "10",
        },
        {
            "symbol": "TEST",
            "bucket_start": (TS0 + timedelta(minutes=10)).isoformat(),
            "bucket_end": (TS0 + timedelta(minutes=11)).isoformat(),
            "close_price": "10",
        },
    ]
    params = WallHistoryParams(sample_interval_sec=60, stale_sample_intervals=2.0)
    rows = join_timeline_with_walls(
        timeline,
        observations=preferred_obs,
        sequences=[],
        segments=[seg],
        gaps=[],
        params=params,
    )
    assert rows[0]["wall_data_present"] is True
    assert rows[0]["wall_sample_ts"] is not None
    sample_ts = datetime.fromisoformat(rows[0]["wall_sample_ts"])
    assert sample_ts <= datetime.fromisoformat(timeline[0]["bucket_end"])
    # 5 minutes later → stale (> 2*60)
    assert rows[1]["wall_data_stale"] is True


def test_timeline_gap_prevents_wall_carry() -> None:
    preferred_obs = [
        {
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "resolution": "auto_10bps",
            "side": "ask",
            "is_wall": True,
            "wall_price": "10.2",
            "wall_notional": "100",
            "wall_multiple": 4.0,
            "percentile": 95.0,
            "depth_share": 0.2,
            "distance_to_mid_bps": 150.0,
        }
    ]
    gap = ReplayGap(
        gap_id="G1",
        symbol="TEST",
        gap_start_ts=TS0 + timedelta(minutes=7),
        gap_end_ts=TS0 + timedelta(minutes=8),
        previous_update_id=1,
        next_update_id=10,
        missing_update_count=8,
        previous_cross_sequence=1,
        next_cross_sequence=10,
        next_message_type="snapshot",
        next_snapshot_complete=True,
        recovered_at_snapshot_ts=TS0 + timedelta(minutes=8),
        discarded_duration_sec=60,
        reason="update_id_gap",
    )
    timeline = [
        {
            "symbol": "TEST",
            "bucket_start": (TS0 + timedelta(minutes=7)).isoformat(),
            "bucket_end": (TS0 + timedelta(minutes=7, seconds=30)).isoformat(),
            "close_price": "10",
        }
    ]
    rows = join_timeline_with_walls(
        timeline,
        observations=preferred_obs,
        sequences=[],
        segments=[_segment()],
        gaps=[gap],
        params=WallHistoryParams(),
    )
    assert rows[0]["wall_data_present"] is False


def test_imbalance_and_integrity() -> None:
    obs = [
        {
            "wall_observation_id": "o1",
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "resolution": "auto_10bps",
            "side": "bid",
            "is_wall": True,
            "wall_price": "9.9",
            "wall_notional": "200",
            "wall_multiple": 5.0,
            "distance_to_mid_bps": 100.0,
        },
        {
            "wall_observation_id": "o2",
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(minutes=6)).isoformat(),
            "resolution": "auto_10bps",
            "side": "ask",
            "is_wall": True,
            "wall_price": "10.2",
            "wall_notional": "50",
            "wall_multiple": 4.0,
            "distance_to_mid_bps": 150.0,
        },
    ]
    from orderbook_analyse.wall_history import _obs_snapshot_features

    feats = _obs_snapshot_features(obs, preferred_resolution="auto_10bps")
    assert feats["wall_notional_imbalance"] == pytest.approx((200 - 50) / 250)
    seg = _segment()
    sequences = [
        {
            "wall_sequence_id": "TEST:S0001:BID:W000001",
            "segment_id": "S0001",
            "first_seen_ts": obs[0]["sample_ts"],
            "last_seen_ts": obs[0]["sample_ts"],
            "age_seconds": 0,
            "sample_count": 1,
            "disappeared_before_test": False,
            "was_tested": False,
        }
    ]
    ok = check_wall_history_integrity(
        observations=obs,
        sequences=sequences,
        transitions=[{"wall_sequence_id": "TEST:S0001:BID:W000001"}],
        segments=[seg],
        warmup_seconds=300,
    )
    assert ok["ok"] is True


def test_integrity_rejects_warmup_and_future_timeline() -> None:
    seg = _segment()
    bad_obs = [
        {
            "wall_observation_id": "bad",
            "symbol": "TEST",
            "segment_id": "S0001",
            "sample_ts": (TS0 + timedelta(seconds=30)).isoformat(),  # during warmup
            "resolution": "auto_10bps",
            "side": "bid",
            "is_wall": True,
            "wall_multiple": 3.0,
            "distance_to_mid_bps": 10.0,
        }
    ]
    bad = check_wall_history_integrity(
        observations=bad_obs,
        sequences=[],
        transitions=[],
        segments=[seg],
        warmup_seconds=300,
    )
    assert bad["ok"] is False
    future = check_wall_history_integrity(
        observations=[],
        sequences=[],
        transitions=[],
        segments=[seg],
        warmup_seconds=300,
        timelines_with_walls={
            "1m": [
                {
                    "bucket_end": (TS0 + timedelta(minutes=6)).isoformat(),
                    "wall_sample_ts": (TS0 + timedelta(minutes=7)).isoformat(),
                }
            ]
        },
    )
    assert future["ok"] is False


def test_decide_phase4_and_full_analysis() -> None:
    assert decide_phase4_wall(ok=True, gap_count=2, has_failures=False, has_success=True).endswith("WITH_GAPS")
    assert decide_phase4_wall(ok=False, gap_count=0, has_failures=True, has_success=False).endswith("FAILED")
    assert decide_phase4_wall(ok=True, gap_count=0, has_failures=True, has_success=True).endswith("PARTIAL")
    assert (
        decide_full_analysis(
            integrity_ok=True,
            gap_count=1,
            module_decisions=[
                "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE_WITH_GAPS",
                "FULL_HISTORY_MARKET_CONTEXT_COMPLETE",
                "FULL_HISTORY_WALL_HISTORY_COMPLETE_WITH_GAPS",
            ],
        )
        == "FULL_HISTORY_ANALYSIS_COMPLETE_WITH_GAPS"
    )
    assert (
        decide_full_analysis(
            integrity_ok=False,
            gap_count=0,
            module_decisions=["FULL_HISTORY_WALL_HISTORY_COMPLETE"],
        )
        == "FULL_HISTORY_ANALYSIS_FAILED"
    )


def test_replay_segment_wall_history_short_warmup() -> None:
    # Build continuous tiny book events over 2 minutes — shorter than warmup
    events = []
    ts = TS0
    levels = [
        _ev(ts, side="bid", price="10.0", qty="5", message_type="snapshot", update_id=1, seq=1, level_index=0),
        _ev(ts, side="ask", price="10.1", qty="5", message_type="snapshot", update_id=1, seq=1, level_index=0),
    ]
    for i in range(2, 6):
        levels.append(
            _ev(ts + timedelta(seconds=20 * (i - 1)), side="bid", price="9.9", qty="1", message_type="delta", update_id=i, seq=i)
        )
    seg = _segment(
        segment_end_ts=TS0 + timedelta(seconds=120),
        last_update_id=5,
        last_cross_sequence=5,
        duration_sec=120,
    )
    params = WallHistoryParams(sample_interval_sec=60, warmup_seconds=300, resolutions_bps=(10.0,))
    out = replay_segment_wall_history(levels, segment=seg, params=params)
    assert out["summary"]["wall_history_status"] == "WALL_HISTORY_OK_NO_POST_WARMUP"
    assert out["observations"] == []


def _ask_obs(ts: datetime, price: str, notion: str, oid: str, *, mid: Decimal = Decimal("10")) -> dict:
    return {
        "wall_observation_id": oid,
        "symbol": "TEST",
        "segment_id": "S0001",
        "sample_ts": ts.isoformat(),
        "resolution": "auto_10bps",
        "side": "ask",
        "is_wall": True,
        "wall_multiple": 5.0,
        "percentile": 95.0,
        "depth_share": 0.2,
        "distance_to_mid_bps": float(abs(Decimal(price) - mid) / mid * 10000),
        "cluster_id": "c1",
        "_price_dec": Decimal(price),
        "_notional_dec": Decimal(notion),
        "_mid_dec": mid,
    }


def test_five_samples_one_sequence_many_transitions() -> None:
    mid = Decimal("10")
    times = [TS0 + timedelta(minutes=6 + i) for i in range(5)]
    observations = [_ask_obs(t, "10.20", "100", f"o{i}", mid=mid) for i, t in enumerate(times)]
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(),
    )
    assert len(sequences) == 1
    assert sequences[0]["sample_count"] == 5
    assert sequences[0]["end_reason"] == "SEGMENT_END"
    assert sum(1 for t in transitions if t["transition_type"] == "PERSISTED") >= 4
    ids = [s["wall_sequence_id"] for s in sequences]
    assert len(ids) == len(set(ids))


def test_disappeared_not_reexported_at_segment_end() -> None:
    mid = Decimal("10")
    t1 = TS0 + timedelta(minutes=6)
    t2 = TS0 + timedelta(minutes=7)
    observations = [
        _ask_obs(t1, "10.20", "100", "o1", mid=mid),
        # far wall at t2 → first disappears
        {**_ask_obs(t2, "10.50", "80", "o2", mid=mid), "cluster_id": "far", "distance_to_mid_bps": 500.0},
    ]
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(match_distance_bps=10.0),
    )
    first = [s for s in sequences if s["sample_count"] >= 1 and s["first_price"] == "10.20"]
    assert len(first) == 1
    assert first[0]["end_reason"] == "DISAPPEARED"
    assert not any(
        t["wall_sequence_id"] == first[0]["wall_sequence_id"] and t["transition_type"] == "SEGMENT_ENDED"
        for t in transitions
    )
    ids = [s["wall_sequence_id"] for s in sequences]
    assert len(ids) == len(set(ids))


def test_active_until_segment_end_one_sequence() -> None:
    observations = [_ask_obs(TS0 + timedelta(minutes=6), "10.20", "100", "o1")]
    sequences, _ = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(),
    )
    assert len(sequences) == 1
    assert sequences[0]["end_reason"] == "SEGMENT_END"


def test_multiple_walls_unique_ids() -> None:
    mid = Decimal("10")
    t = TS0 + timedelta(minutes=6)
    observations = [
        _ask_obs(t, "10.20", "100", "a1", mid=mid),
        {
            **_ask_obs(t, "10.40", "90", "a2", mid=mid),
            "side": "bid",
            "_price_dec": Decimal("9.80"),
            "cluster_id": "bid1",
            "distance_to_mid_bps": 200.0,
        },
    ]
    sequences, _ = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(),
    )
    ids = [s["wall_sequence_id"] for s in sequences]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))


def test_finalize_export_idempotent_unique_ids() -> None:
    observations = [
        _ask_obs(TS0 + timedelta(minutes=6 + i), "10.20", "100", f"o{i}") for i in range(3)
    ]
    sequences, _ = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(),
    )
    # Re-build is independent; within one build map export is unique
    assert len(sequences) == 1
    seq_ids = [s["wall_sequence_id"] for s in sequences]
    assert len(seq_ids) == len(set(seq_ids))
    # Disappeared wall must not also appear as SEGMENT_END
    t1 = TS0 + timedelta(minutes=6)
    t2 = TS0 + timedelta(minutes=7)
    seq2, tr2 = build_sequences_and_transitions(
        [
            _ask_obs(t1, "10.20", "100", "x1"),
            {**_ask_obs(t2, "10.55", "70", "x2"), "cluster_id": "z", "distance_to_mid_bps": 550.0},
        ],
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(match_distance_bps=5.0),
    )
    for s in seq2:
        if s["end_reason"] == "DISAPPEARED":
            assert not any(
                t["wall_sequence_id"] == s["wall_sequence_id"] and t["transition_type"] == "SEGMENT_ENDED"
                for t in tr2
            )


def _seq_ask(**kwargs):
    base = {
        "symbol": "TEST",
        "segment_id": "S0001",
        "wall_sequence_id": "TEST:S0001:ASK:W000001",
        "side": "ask",
        "resolution": "auto_10bps",
        "first_seen_ts": (TS0 + timedelta(minutes=6)).isoformat(),
        "last_seen_ts": (TS0 + timedelta(minutes=7)).isoformat(),
        "closed_ts": (TS0 + timedelta(minutes=7)).isoformat(),
        "first_price": "10.20",
        "last_price": "10.20",
        "min_price": "10.20",
        "max_price": "10.20",
        "first_notional": "100",
        "last_notional": "100",
        "was_tested": False,
        "was_broken": False,
        "end_reason": "SEGMENT_END",
        "max_distance_bps": 150,
        "min_distance_bps": 150,
    }
    base.update(kwargs)
    return base


def test_ask_tested_at_3bps_threshold_5() -> None:
    wall = Decimal("10.20")
    high = wall * (Decimal("1") - Decimal("3") / Decimal("10000"))
    sequences = [_seq_ask()]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), Decimal("10.10"), high, Decimal("10.00"))],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is True
    assert sequences[0]["min_test_distance_bps"] == pytest.approx(3.0, abs=0.05)


def test_ask_not_tested_at_8bps_threshold_5() -> None:
    wall = Decimal("10.20")
    high = wall * (Decimal("1") - Decimal("8") / Decimal("10000"))
    sequences = [_seq_ask()]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), Decimal("10.10"), high, Decimal("10.00"))],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is False


def test_bid_tested_mirror() -> None:
    wall = Decimal("9.80")
    low = wall * (Decimal("1") + Decimal("3") / Decimal("10000"))
    sequences = [
        _seq_ask(
            wall_sequence_id="TEST:S0001:BID:W000001",
            side="bid",
            first_price="9.80",
            last_price="9.80",
            min_price="9.80",
            max_price="9.80",
        )
    ]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), Decimal("10.00"), Decimal("10.05"), low)],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is True


def test_touch_without_break() -> None:
    wall = Decimal("10.20")
    high = wall * (Decimal("1") - Decimal("2") / Decimal("10000"))  # tested
    mid_close = wall - Decimal("0.01")  # still below wall
    sequences = [_seq_ask()]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), mid_close, high, Decimal("10.00"))],
        params=WallHistoryParams(test_distance_bps=5.0, break_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is True
    assert sequences[0]["traded_through"] is False
    assert sequences[0]["confirmed_broken"] is False
    assert sequences[0]["was_broken"] is False


def test_traded_through_without_confirmed_close() -> None:
    wall = Decimal("10.20")
    break_pad = wall * Decimal("5") / Decimal("10000")
    high = wall + break_pad + Decimal("0.01")
    mid_close = wall  # close not beyond break
    sequences = [_seq_ask()]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), mid_close, high, Decimal("10.00"))],
        params=WallHistoryParams(break_distance_bps=5.0),
    )
    assert sequences[0]["traded_through"] is True
    assert sequences[0]["confirmed_broken"] is False
    assert sequences[0]["was_broken"] is False


def test_confirmed_break() -> None:
    wall = Decimal("10.20")
    break_pad = wall * Decimal("5") / Decimal("10000")
    high = wall + break_pad + Decimal("0.01")
    mid_close = wall + break_pad + Decimal("0.01")
    sequences = [_seq_ask()]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[(TS0 + timedelta(minutes=6), mid_close, high, Decimal("10.00"))],
        params=WallHistoryParams(break_distance_bps=5.0),
    )
    assert sequences[0]["traded_through"] is True
    assert sequences[0]["confirmed_broken"] is True
    assert sequences[0]["was_broken"] is True


def test_disappear_same_interval_after_test() -> None:
    wall = Decimal("10.20")
    high = wall * (Decimal("1") - Decimal("2") / Decimal("10000"))
    t_close = TS0 + timedelta(minutes=7)
    sequences = [
        _seq_ask(
            end_reason="DISAPPEARED",
            last_seen_ts=(TS0 + timedelta(minutes=6)).isoformat(),
            closed_ts=t_close.isoformat(),
        )
    ]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[
            (TS0 + timedelta(minutes=6), Decimal("10.10"), Decimal("10.10"), Decimal("10.00")),
            (t_close, Decimal("10.10"), high, Decimal("10.00")),
        ],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is True
    assert sequences[0]["disappeared_before_test"] is False


def test_no_future_price_interval_used() -> None:
    wall = Decimal("10.20")
    future_high = wall  # would test if looked ahead
    sequences = [
        _seq_ask(
            last_seen_ts=(TS0 + timedelta(minutes=6)).isoformat(),
            closed_ts=(TS0 + timedelta(minutes=6)).isoformat(),
        )
    ]
    apply_test_break_flags(
        sequences,
        [],
        sample_mids=[
            (TS0 + timedelta(minutes=6), Decimal("10.00"), Decimal("10.00"), Decimal("9.90")),
            (TS0 + timedelta(minutes=8), Decimal("10.10"), future_high, Decimal("10.00")),
        ],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is False


def _bid_obs(ts: datetime, price: str, notion: str, oid: str, *, mid: Decimal = Decimal("0.634")) -> dict:
    return {
        "wall_observation_id": oid,
        "symbol": "APTUSDT",
        "segment_id": "S0002",
        "sample_ts": ts.isoformat(),
        "resolution": "auto_10bps",
        "side": "bid",
        "is_wall": True,
        "wall_price": price,
        "wall_notional": notion,
        "wall_multiple": 5.0,
        "percentile": 95.0,
        "depth_share": 0.2,
        "distance_to_mid_bps": float(abs(Decimal(price) - mid) / mid * 10000),
        "cluster_id": "bid_wall",
        "_price_dec": Decimal(price),
        "_notional_dec": Decimal(notion),
        "_mid_dec": mid,
    }


def test_causal_timeline_no_lookahead_break_at_t4() -> None:
    """Wall appears t0, break at t4 — earlier timeline buckets stay false."""
    mid = Decimal("0.634")
    t0 = datetime(2026, 7, 26, 16, 40, 29, tzinfo=timezone.utc)
    samples = [t0 + timedelta(minutes=i) for i in range(5)]
    observations = [
        _bid_obs(samples[0], "0.628", "367000", "o0", mid=mid),
        _bid_obs(samples[1], "0.628", "380000", "o1", mid=mid),
        _bid_obs(samples[2], "0.628", "400000", "o2", mid=mid),
        _bid_obs(samples[3], "0.628", "423000", "o3", mid=mid),
        # t4: far wall → disappear of 0.628
        {
            **_bid_obs(samples[4], "0.610", "100000", "o4", mid=mid),
            "cluster_id": "other",
            "distance_to_mid_bps": 400.0,
        },
    ]
    params = WallHistoryParams(match_distance_bps=10.0, test_distance_bps=5.0, break_distance_bps=5.0)
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="APTUSDT",
        segment_id="S0002",
        segment_end_ts=t0 + timedelta(minutes=30),
        params=params,
    )
    # Break in disappearance interval: low trades through 0.628
    wall = Decimal("0.628")
    break_pad = wall * Decimal("5") / Decimal("10000")
    path = [
        (samples[0], mid, mid + Decimal("0.001"), mid - Decimal("0.001")),
        (samples[1], mid, mid + Decimal("0.001"), mid - Decimal("0.001")),
        (samples[2], mid, mid + Decimal("0.001"), mid - Decimal("0.001")),
        (samples[3], mid, mid + Decimal("0.001"), mid - Decimal("0.001")),
        # interval ending at t4: traded through + confirmed close below wall
        (samples[4], wall - break_pad - Decimal("0.001"), mid, wall - break_pad - Decimal("0.002")),
    ]
    apply_test_break_flags(sequences, transitions, sample_mids=path, params=params)
    target = [s for s in sequences if s["first_price"] == "0.628"][0]
    assert target["was_broken"] is True
    assert target["end_reason"] == "BROKEN"
    assert target["first_test_ts"] is not None
    assert "TESTED" in [
        t["transition_type"] for t in transitions if t["wall_sequence_id"] == target["wall_sequence_id"]
    ]
    assert "BROKEN" in [
        t["transition_type"] for t in transitions if t["wall_sequence_id"] == target["wall_sequence_id"]
    ]
    assert "TRADED_THROUGH" in [
        t["transition_type"] for t in transitions if t["wall_sequence_id"] == target["wall_sequence_id"]
    ]
    assert not any(
        t["transition_type"] == "DISAPPEARED" and t["wall_sequence_id"] == target["wall_sequence_id"]
        for t in transitions
    )

    seg = _segment(
        segment_id="S0002",
        symbol="APTUSDT",
        segment_start_ts=t0 - timedelta(minutes=10),
        segment_end_ts=t0 + timedelta(minutes=30),
    )
    timeline = []
    for i in range(5):
        bs = datetime(2026, 7, 26, 16, 40 + i, 0, tzinfo=timezone.utc)
        timeline.append(
            {
                "symbol": "APTUSDT",
                "bucket_start": bs.isoformat(),
                "bucket_end": (bs + timedelta(minutes=1)).isoformat(),
                "close_price": "0.634",
            }
        )
    rows = join_timeline_with_walls(
        timeline,
        observations=observations,
        sequences=sequences,
        segments=[seg],
        gaps=[],
        params=params,
        transitions=transitions,
    )
    break_ts = datetime.fromisoformat(target["confirmed_break_ts"])
    for r in rows:
        bend = datetime.fromisoformat(r["bucket_end"])
        if r.get("nearest_bid_wall_price") != "0.628":
            continue
        if bend < break_ts:
            assert r["nearest_bid_wall_tested"] is False
            assert r["nearest_bid_wall_broken"] is False


def test_timeline_as_of_uses_transitions_not_final_sequence() -> None:
    """Same wall_sample across buckets; only transitions <= bucket_end flip flags."""
    t0 = datetime(2026, 7, 26, 16, 40, 29, tzinfo=timezone.utc)
    t_break = datetime(2026, 7, 26, 16, 44, 29, tzinfo=timezone.utc)
    sid = "APTUSDT:S0002:BID:W000344"
    observations = [
        {
            **_bid_obs(t0, "0.628", "423000", "o0"),
            "wall_sequence_id": sid,
        }
    ]
    sequences = [
        {
            "wall_sequence_id": sid,
            "segment_id": "S0002",
            "side": "bid",
            "first_seen_ts": t0.isoformat(),
            "last_seen_ts": t0.isoformat(),
            "closed_ts": t_break.isoformat(),
            "was_tested": True,
            "was_broken": True,
            "end_reason": "BROKEN",
            "last_price": "0.628",
        }
    ]
    transitions = [
        {
            "wall_sequence_id": sid,
            "transition_ts": t_break.isoformat(),
            "transition_type": "TESTED",
            "side": "bid",
        },
        {
            "wall_sequence_id": sid,
            "transition_ts": t_break.isoformat(),
            "transition_type": "BROKEN",
            "side": "bid",
        },
    ]
    seg = _segment(
        segment_id="S0002",
        symbol="APTUSDT",
        segment_start_ts=t0 - timedelta(minutes=5),
        segment_end_ts=t0 + timedelta(hours=1),
    )
    timeline = []
    for i in range(5):
        bs = datetime(2026, 7, 26, 16, 40 + i, 0, tzinfo=timezone.utc)
        timeline.append(
            {
                "symbol": "APTUSDT",
                "bucket_start": bs.isoformat(),
                "bucket_end": (bs + timedelta(minutes=1)).isoformat(),
                "close_price": "0.634",
            }
        )
    rows = join_timeline_with_walls(
        timeline,
        observations=observations,
        sequences=sequences,
        segments=[seg],
        gaps=[],
        params=WallHistoryParams(),
        transitions=transitions,
    )
    for r in rows:
        bend = datetime.fromisoformat(r["bucket_end"])
        assert r["nearest_bid_wall_price"] == "0.628"
        if bend < t_break:
            assert r["nearest_bid_wall_tested"] is False
            assert r["nearest_bid_wall_broken"] is False
        else:
            assert r["nearest_bid_wall_tested"] is True
            assert r["nearest_bid_wall_broken"] is True

    t0 = TS0 + timedelta(minutes=6)
    observations = [_ask_obs(t0 + timedelta(minutes=i), "10.20", "100", f"o{i}") for i in range(3)]
    # disappear at t3
    observations.append(
        {**_ask_obs(t0 + timedelta(minutes=3), "10.50", "80", "ox"), "cluster_id": "z", "distance_to_mid_bps": 500.0}
    )
    params = WallHistoryParams(match_distance_bps=5.0)
    sequences, transitions = build_sequences_and_transitions(
        observations, symbol="TEST", segment_id="S0001", segment_end_ts=TS0 + timedelta(minutes=40), params=params
    )
    wall = Decimal("10.20")
    pad = wall * Decimal("5") / Decimal("10000")
    apply_test_break_flags(
        sequences,
        transitions,
        sample_mids=[
            (t0, Decimal("10.05"), Decimal("10.05"), Decimal("10.00")),
            (t0 + timedelta(minutes=1), Decimal("10.05"), Decimal("10.05"), Decimal("10.00")),
            (t0 + timedelta(minutes=2), Decimal("10.05"), Decimal("10.05"), Decimal("10.00")),
            (t0 + timedelta(minutes=3), wall + pad + Decimal("0.01"), wall + pad + Decimal("0.02"), Decimal("10.00")),
        ],
        params=WallHistoryParams(test_distance_bps=5.0, break_distance_bps=5.0),
    )
    assert any(s.get("was_broken") for s in sequences)
    seg = _segment(segment_end_ts=TS0 + timedelta(minutes=40))
    timeline = [
        {
            "symbol": "TEST",
            "bucket_start": (t0 + timedelta(minutes=i)).isoformat(),
            "bucket_end": (t0 + timedelta(minutes=i + 1)).isoformat(),
            "close_price": "10",
        }
        for i in range(3)
    ]
    rows = join_timeline_with_walls(
        timeline,
        observations=observations,
        sequences=sequences,
        segments=[seg],
        gaps=[],
        params=params,
        transitions=transitions,
    )
    # first three bucket ends are before break at t0+3m
    for r in rows[:3]:
        if r.get("nearest_ask_wall_price"):
            assert r["nearest_ask_wall_broken"] is False


def test_test_without_break_causal_transition() -> None:
    sequences = [_seq_ask(end_reason="SEGMENT_END")]
    wall = Decimal("10.20")
    high = wall * (Decimal("1") - Decimal("2") / Decimal("10000"))
    transitions: list = []
    apply_test_break_flags(
        sequences,
        transitions,
        sample_mids=[(TS0 + timedelta(minutes=6), Decimal("10.10"), high, Decimal("10.00"))],
        params=WallHistoryParams(test_distance_bps=5.0, break_distance_bps=5.0),
    )
    assert sequences[0]["was_tested"] is True
    assert sequences[0]["was_broken"] is False
    assert any(t["transition_type"] == "TESTED" for t in transitions)
    assert not any(t["transition_type"] == "BROKEN" for t in transitions)


def test_break_in_disappearance_interval_not_only_disappeared() -> None:
    t1 = TS0 + timedelta(minutes=6)
    t2 = TS0 + timedelta(minutes=7)
    observations = [
        _ask_obs(t1, "10.20", "100", "o1"),
        {**_ask_obs(t2, "10.50", "80", "o2"), "cluster_id": "far", "distance_to_mid_bps": 500.0},
    ]
    params = WallHistoryParams(match_distance_bps=5.0)
    sequences, transitions = build_sequences_and_transitions(
        observations, symbol="TEST", segment_id="S0001", segment_end_ts=TS0 + timedelta(minutes=20), params=params
    )
    wall = Decimal("10.20")
    pad = wall * Decimal("5") / Decimal("10000")
    apply_test_break_flags(
        sequences,
        transitions,
        sample_mids=[
            (t1, Decimal("10.05"), Decimal("10.05"), Decimal("10.00")),
            (t2, wall + pad + Decimal("0.01"), wall + pad + Decimal("0.02"), Decimal("10.00")),
        ],
        params=WallHistoryParams(break_distance_bps=5.0, test_distance_bps=5.0),
    )
    s0 = [s for s in sequences if s["first_price"] == "10.20"][0]
    assert s0["end_reason"] == "BROKEN"
    assert s0["was_tested"] is True
    assert s0["was_broken"] is True
    sid = s0["wall_sequence_id"]
    assert not any(t["wall_sequence_id"] == sid and t["transition_type"] == "DISAPPEARED" for t in transitions)
    ordered = [t["transition_type"] for t in transitions if t["wall_sequence_id"] == sid and t["transition_type"] in {"TESTED", "TRADED_THROUGH", "BROKEN"}]
    assert ordered == ["TESTED", "TRADED_THROUGH", "BROKEN"]


def test_true_disappearance_without_price_approach() -> None:
    t1 = TS0 + timedelta(minutes=6)
    t2 = TS0 + timedelta(minutes=7)
    observations = [
        _ask_obs(t1, "10.20", "100", "o1"),
        {**_ask_obs(t2, "10.50", "80", "o2"), "cluster_id": "far", "distance_to_mid_bps": 500.0},
    ]
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="TEST",
        segment_id="S0001",
        segment_end_ts=TS0 + timedelta(minutes=20),
        params=WallHistoryParams(match_distance_bps=5.0),
    )
    apply_test_break_flags(
        sequences,
        transitions,
        sample_mids=[
            (t1, Decimal("10.00"), Decimal("10.01"), Decimal("9.99")),
            (t2, Decimal("10.00"), Decimal("10.01"), Decimal("9.99")),
        ],
        params=WallHistoryParams(test_distance_bps=5.0),
    )
    s0 = [s for s in sequences if s["first_price"] == "10.20"][0]
    assert s0["end_reason"] == "DISAPPEARED"
    assert s0["was_tested"] is False
    assert any(t["wall_sequence_id"] == s0["wall_sequence_id"] and t["transition_type"] == "DISAPPEARED" for t in transitions)


def test_apt_w000344_style_regression_fixture() -> None:
    """Bid wall 0.628 visible 4 samples; break only in 16:43:29–16:44:29 interval."""
    mid = Decimal("0.634")
    t0 = datetime(2026, 7, 26, 16, 40, 29, tzinfo=timezone.utc)
    notionals = ["367000", "385000", "405000", "423000"]
    observations = [
        _bid_obs(t0 + timedelta(minutes=i), "0.628", notionals[i], f"w{i}", mid=mid) for i in range(4)
    ]
    # disappear sample at 16:44:29
    t_break = t0 + timedelta(minutes=4)
    observations.append(
        {
            **_bid_obs(t_break, "0.620", "50000", "gone", mid=mid),
            "cluster_id": "elsewhere",
            "distance_to_mid_bps": 220.0,
        }
    )
    params = WallHistoryParams(match_distance_bps=15.0, test_distance_bps=5.0, break_distance_bps=5.0)
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol="APTUSDT",
        segment_id="S0002",
        segment_end_ts=t0 + timedelta(hours=1),
        params=params,
    )
    wall = Decimal("0.628")
    pad = wall * Decimal("5") / Decimal("10000")
    path = [(t0 + timedelta(minutes=i), mid, mid + Decimal("0.001"), mid - Decimal("0.001")) for i in range(4)]
    path.append((t_break, wall - pad - Decimal("0.001"), mid, wall - pad - Decimal("0.002")))
    apply_test_break_flags(sequences, transitions, sample_mids=path, params=params)
    seq = [s for s in sequences if s["last_price"] == "0.628"][0]
    assert seq["end_reason"] == "BROKEN"
    assert seq["was_tested"] is True
    assert seq["was_broken"] is True
    assert seq["first_test_ts"] == t_break.isoformat()
    assert seq["confirmed_break_ts"] == t_break.isoformat()
    ids = [s["wall_sequence_id"] for s in sequences]
    assert len(ids) == len(set(ids))

    seg = _segment(
        segment_id="S0002",
        symbol="APTUSDT",
        segment_start_ts=t0 - timedelta(minutes=5),
        segment_end_ts=t0 + timedelta(hours=1),
    )
    # 1m buckets 16:40 .. 16:44
    timeline_1m = []
    for i in range(5):
        bs = datetime(2026, 7, 26, 16, 40 + i, 0, tzinfo=timezone.utc)
        timeline_1m.append(
            {
                "symbol": "APTUSDT",
                "bucket_start": bs.isoformat(),
                "bucket_end": (bs + timedelta(minutes=1)).isoformat(),
                "close_price": "0.634",
            }
        )
    rows_1m = join_timeline_with_walls(
        timeline_1m,
        observations=observations,
        sequences=sequences,
        segments=[seg],
        gaps=[],
        params=params,
        transitions=transitions,
    )
    for r in rows_1m:
        bend = datetime.fromisoformat(r["bucket_end"])
        if r.get("nearest_bid_wall_price") != "0.628":
            continue
        if bend < t_break:
            assert r["nearest_bid_wall_tested"] is False
            assert r["nearest_bid_wall_broken"] is False

    # 5m bucket ending before break: still causal false
    timeline_5m = [
        {
            "symbol": "APTUSDT",
            "bucket_start": datetime(2026, 7, 26, 16, 40, 0, tzinfo=timezone.utc).isoformat(),
            "bucket_end": datetime(2026, 7, 26, 16, 44, 0, tzinfo=timezone.utc).isoformat(),
            "close_price": "0.634",
        }
    ]
    rows_5m = join_timeline_with_walls(
        timeline_5m,
        observations=observations,
        sequences=sequences,
        segments=[seg],
        gaps=[],
        params=params,
        transitions=transitions,
    )
    if rows_5m[0].get("nearest_bid_wall_price") == "0.628":
        assert rows_5m[0]["nearest_bid_wall_tested"] is False
        assert rows_5m[0]["nearest_bid_wall_broken"] is False

    # 5m bucket ending after break: transitions <= bucket_end may mark tested/broken
    # when the last wall_sample still points at 0.628 (no later sample replacing it).
    # Here the disappear sample replaces nearest — sequence-level BROKEN is the source of truth.
    assert seq["was_broken"] is True
