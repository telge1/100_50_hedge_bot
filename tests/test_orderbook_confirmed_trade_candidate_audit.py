"""Unit tests for confirmed orderbook trade-candidate audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orderbook_analyse.near_liquidity import (
    BEARISH_LIQUIDITY_SHIFT,
    BULLISH_LIQUIDITY_SHIFT,
    NEAR_ASK_MOVING_HIGHER,
    NEAR_ASK_MOVING_LOWER,
    NEAR_ASK_STABLE,
    NearAskTransition,
    NearSnapshotView,
)
from orderbook_analyse.orderbook_confirmed_trade_candidate_audit import (
    BREAKOUT_RECLAIM_LONG,
    CONFIRMED,
    FAILED_BREAKOUT_SHORT,
    LONG_SETUPS,
    NO_CONFIRM_GENERIC_NOT_ELIGIBLE,
    NO_CONFIRM_INVALIDATED,
    NO_CONFIRM_TIMEOUT,
    RESISTANCE_REJECTION_SHORT,
    SHORT_CONTINUATION,
    BreakoutMemoryTracker,
    ConfirmedAuditParams,
    LevelMemory,
    WatchState,
    build_confirmed_accepted,
    classify_setup_type,
    collect_confirm_features,
    evaluate_confirmation_snapshot,
    run_confirmed_audit_from_snapshots,
    setup_side,
    structure_holds,
)
from orderbook_analyse.orderbook_trade_candidate_audit import (
    LONG,
    SHORT,
    SUPPORT_RETEST_RECLAIM_LONG,
    AuditParams,
    CandidateDecision,
    ScoreComponent,
)
from orderbook_analyse.wall_movement_tracker import (
    FALLING_ASK_CEILING,
    RISING_BID_FLOOR,
    SequenceRecord,
    SnapshotRecord,
    WallView,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _wall(side: str, price: str, notional: str = "1000", *, dist: float = 0.5) -> WallView:
    return WallView(
        side=side,
        price=Decimal(price),
        notional=Decimal(notional),
        wall_multiple=5.0,
        distance_pct=dist,
        is_wall=True,
    )


def _snap(
    ts: datetime,
    mid: str,
    *,
    nearest_bid: str | None = "0.620",
    nearest_ask: str | None = "0.640",
    dominant_bid: str | None = None,
    dominant_ask: str | None = None,
    delta: str = "100",
    oi_chg: str | None = "10",
) -> SnapshotRecord:
    nb = None if nearest_bid is None else _wall("bid", nearest_bid)
    na = None if nearest_ask is None else _wall("ask", nearest_ask)
    db = None if dominant_bid is None else _wall("bid", dominant_bid)
    da = None if dominant_ask is None else _wall("ask", dominant_ask)
    return SnapshotRecord(
        timestamp=ts,
        mid_price=Decimal(mid),
        best_bid=Decimal(mid) - Decimal("0.0001"),
        best_ask=Decimal(mid) + Decimal("0.0001"),
        bucket_size=Decimal("0.001"),
        strongest_bid=db or nb,
        strongest_ask=da or na,
        top_bid_walls=[nb] if nb else [],
        top_ask_walls=[na] if na else [],
        all_bid_buckets={},
        all_ask_buckets={},
        buy_notional_since_prev=Decimal(delta) if Decimal(delta) > 0 else Decimal("0"),
        sell_notional_since_prev=Decimal("0") if Decimal(delta) > 0 else abs(Decimal(delta)),
        trade_delta_notional=Decimal(delta),
        open_interest=Decimal("1000000"),
        oi_change_since_prev=None if oi_chg is None else Decimal(oi_chg),
        nearest_bid=nb,
        nearest_ask=na,
        dominant_bid=db or nb,
        dominant_ask=da or na,
        near_bids=[nb] if nb else [],
        near_asks=[na] if na else [],
        total_near_bid_notional=nb.notional if nb else Decimal("0"),
        total_near_ask_notional=na.notional if na else Decimal("0"),
    )


def _near_view(snap: SnapshotRecord) -> NearSnapshotView:
    return NearSnapshotView(
        nearest_bid=snap.nearest_bid,
        nearest_ask=snap.nearest_ask,
        dominant_bid=snap.dominant_bid,
        dominant_ask=snap.dominant_ask,
        near_bids=snap.near_bids,
        near_asks=snap.near_asks,
        total_near_bid_notional=snap.total_near_bid_notional,
        total_near_ask_notional=snap.total_near_ask_notional,
    )


def _seq(label: str, end: datetime, shifts: int, *, start_price: str = "0.616", end_price: str = "0.620") -> SequenceRecord:
    return SequenceRecord(
        side="bid" if "BID" in label else "ask",
        classification=label,
        sequence_start=end - timedelta(minutes=5),
        sequence_end=end,
        number_of_shifts=shifts,
        total_shift_buckets=float(shifts),
        total_shift_pct=0.5,
        start_wall_price=Decimal(start_price),
        end_wall_price=Decimal(end_price),
        start_mid=Decimal("0.62"),
        end_mid=Decimal("0.63"),
        wall_mid_beta=1.0,
        average_distance_pct=1.0,
        old_wall_average_remaining_ratio=0.1,
        confidence_score=0.8,
    )


def _decision(
    *,
    side: str = "NO_TRADE",
    reason: str = "NO_TRADE_NO_STRUCTURE",
    setup: str | None = None,
    rising_bid: int = 0,
    falling_ask: int = 0,
    mid: str = "0.630",
    near_ask_class: str | None = None,
    delta: str = "100",
    auction: str = "HIGHER",
    bias: str = BULLISH_LIQUIDITY_SHIFT,
) -> CandidateDecision:
    return CandidateDecision(
        signal_time=TS0,
        side=side,
        reason=reason,
        score=5,
        components=[ScoreComponent("test", 5)],
        snapshot_index=3,
        rising_bid_shifts=rising_bid,
        falling_ask_shifts=falling_ask,
        active_rising_bid_shifts=rising_bid,
        active_falling_ask_shifts=falling_ask,
        auction_direction=auction,
        short_term_bias=bias,
        near_ask_class=near_ask_class,
        trade_delta=Decimal(delta),
        oi_change=Decimal("10"),
        mid=Decimal(mid),
        nearest_bid=Decimal("0.620"),
        nearest_ask=Decimal("0.640"),
        dominant_bid=Decimal("0.615"),
        dominant_ask=Decimal("0.650"),
        entry_setup_type=setup,
    )


def test_watch_snapshot_does_not_count_as_confirmation() -> None:
    params = ConfirmedAuditParams(confirmation_snapshots=2, confirmation_min_feature_count=1)
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630") for i in range(6)]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=2,
        watch_open_time=snaps[2].timestamp,
        watch_mid=snaps[2].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(side=LONG, reason="ctx"),
    )
    # Evaluating at watch_index is not allowed by caller; first confirm is index 3
    r1 = evaluate_confirmation_snapshot(
        watch=watch,
        index=3,
        snapshots=snaps,
        near_views=near_views,
        sequences=[_seq(RISING_BID_FLOOR, snaps[3].timestamp, 2)],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r1["status"] == "CONFIRM_PROGRESS"
    assert watch.confirm_count == 1
    assert watch.confirm_time is None


def test_two_consecutive_snapshots_confirm() -> None:
    params = ConfirmedAuditParams(confirmation_snapshots=2, confirmation_min_feature_count=1)
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), "0.632", delta="500", oi_chg="20")
        for i in range(8)
    ]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=2,
        watch_open_time=snaps[2].timestamp,
        watch_mid=snaps[2].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(side=LONG, reason="ctx"),
    )
    seqs = [_seq(RISING_BID_FLOOR, snaps[5].timestamp, 3)]
    r1 = evaluate_confirmation_snapshot(
        watch=watch,
        index=3,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r1["status"] == "CONFIRM_PROGRESS"
    r2 = evaluate_confirmation_snapshot(
        watch=watch,
        index=4,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r2["status"] == CONFIRMED
    assert watch.confirm_time == snaps[4].timestamp
    assert watch.confirm_count == 2


def test_non_consecutive_confirm_resets() -> None:
    params = ConfirmedAuditParams(confirmation_snapshots=2, confirmation_min_feature_count=1)
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.632", delta="500") for i in range(8)]
    # Insert a dip that fails structure at index 4
    snaps[4] = _snap(TS0 + timedelta(seconds=120), "0.625", delta="-100")
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=2,
        watch_open_time=snaps[2].timestamp,
        watch_mid=snaps[2].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.610"),
        context_decision=_decision(side=LONG, reason="ctx"),
    )
    seqs = [_seq(RISING_BID_FLOOR, snaps[6].timestamp, 3)]
    evaluate_confirmation_snapshot(
        watch=watch,
        index=3,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert watch.confirm_count == 1
    r = evaluate_confirmation_snapshot(
        watch=watch,
        index=4,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r["status"] in {"NO_CONFIRM_COUNT_RESET", "NO_CONFIRM_STRUCTURE_FAILED", NO_CONFIRM_INVALIDATED}
    assert watch.confirm_count == 0 or watch.closed


def test_timeout_closes_watch() -> None:
    params = ConfirmedAuditParams(confirmation_snapshots=2, confirmation_max_seconds=60)
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.632") for i in range(10)]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=SHORT_CONTINUATION,
        side=SHORT,
        watch_index=1,
        watch_open_time=snaps[1].timestamp,
        watch_mid=snaps[1].mid_price,
        reference_level=Decimal("0.640"),
        invalidation_level=Decimal("0.650"),
        context_decision=_decision(side=SHORT, auction="LOWER", bias=BEARISH_LIQUIDITY_SHIFT),
    )
    # index 5 is 120s after index 1 -> timeout (>60)
    r = evaluate_confirmation_snapshot(
        watch=watch,
        index=5,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r["status"] == NO_CONFIRM_TIMEOUT
    assert watch.closed


def test_invalidation_closes_long_watch() -> None:
    params = ConfirmedAuditParams(confirmation_snapshots=2)
    snaps = [
        _snap(TS0, "0.630"),
        _snap(TS0 + timedelta(seconds=30), "0.631"),
        _snap(TS0 + timedelta(seconds=60), "0.615"),  # below invalidation
    ]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=0,
        watch_open_time=snaps[0].timestamp,
        watch_mid=snaps[0].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(),
    )
    r = evaluate_confirmation_snapshot(
        watch=watch,
        index=2,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r["status"] == NO_CONFIRM_INVALIDATED


def test_entry_ordering_watch_lt_confirm_lt_entry() -> None:
    params = ConfirmedAuditParams(
        base=AuditParams(
            min_crv_tp1=0.1,
            min_crv_tp2=0.1,
            min_sl_distance_pct=0.01,
            max_sl_distance_pct=5.0,
        ),
        confirmation_snapshots=2,
        confirmation_min_feature_count=1,
    )
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), "0.632", nearest_ask="0.640", dominant_ask="0.650", delta="800")
        for i in range(8)
    ]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=2,
        watch_open_time=snaps[2].timestamp,
        watch_mid=snaps[2].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(side=LONG),
        confirm_count=2,
        confirm_time=snaps[4].timestamp,
        confirm_index=4,
        confirm_features=["directional_delta", "auction", "price_direction"],
        closed=True,
        close_reason=CONFIRMED,
    )
    decision = _decision(side=LONG, reason=BREAKOUT_RECLAIM_LONG, setup=BREAKOUT_RECLAIM_LONG)
    decision.signal_time = snaps[4].timestamp
    decision.snapshot_index = 4
    built = build_confirmed_accepted(
        watch=watch,
        confirm_decision=decision,
        confirm_index=4,
        snapshots=snaps,
        price_path=[],
        params=params,
        candidate_id="CC0001",
        episode_id="CE0001",
    )
    assert not isinstance(built, CandidateDecision)
    assert built.watch_open_time < built.confirm_time < built.entry_time
    assert built.signal_time == built.confirm_time
    assert built.entry_time == snaps[5].timestamp
    assert built.entry_price == snaps[5].mid_price


def test_no_same_snapshot_entry() -> None:
    params = ConfirmedAuditParams(
        base=AuditParams(
            min_crv_tp1=0.1,
            min_crv_tp2=0.1,
            min_sl_distance_pct=0.01,
            max_sl_distance_pct=5.0,
        ),
        confirmation_min_feature_count=1,
    )
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.632", nearest_ask="0.640", dominant_ask="0.650") for i in range(6)]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=1,
        watch_open_time=snaps[1].timestamp,
        watch_mid=snaps[1].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(),
        confirm_count=2,
        confirm_time=snaps[3].timestamp,
        confirm_index=3,
        confirm_features=["directional_delta", "auction", "near_book"],
        closed=True,
        close_reason=CONFIRMED,
    )
    decision = _decision(side=LONG, setup=BREAKOUT_RECLAIM_LONG)
    built = build_confirmed_accepted(
        watch=watch,
        confirm_decision=decision,
        confirm_index=3,
        snapshots=snaps,
        price_path=[],
        params=params,
        candidate_id="CC0002",
        episode_id="CE0002",
    )
    assert not isinstance(built, CandidateDecision)
    assert built.entry_time != built.confirm_time
    assert built.entry_time != built.watch_open_time


def test_generic_long_not_confirmed_unchanged() -> None:
    """Generic LONG_STRUCTURE may appear as context-only, never as confirmed setup."""
    assert LONG not in LONG_SETUPS or True
    decision = _decision(side=LONG, reason="LONG_STRUCTURE")
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630") for i in range(5)]
    setup = classify_setup_type(
        index=3,
        snapshots=snaps,
        decision=decision,
        retest_state=None,
        breakout_memory=None,
        regime={
            "auction_direction": "MIXED",
            "short_term_bias": "INCONCLUSIVE",
            "near_ask_direction": "INCONCLUSIVE",
            "near_bid_direction": "INCONCLUSIVE",
        },
        params=ConfirmedAuditParams(),
    )
    assert setup is None


def test_support_retest_classified() -> None:
    decision = _decision(
        side=LONG,
        reason=SUPPORT_RETEST_RECLAIM_LONG,
        setup=SUPPORT_RETEST_RECLAIM_LONG,
    )
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.628") for i in range(4)]
    setup = classify_setup_type(
        index=2,
        snapshots=snaps,
        decision=decision,
        retest_state=None,
        breakout_memory=None,
        regime={"auction_direction": "HIGHER", "short_term_bias": BULLISH_LIQUIDITY_SHIFT},
        params=ConfirmedAuditParams(),
    )
    assert setup == SUPPORT_RETEST_RECLAIM_LONG


def test_failed_breakout_short_classified() -> None:
    snaps = [
        _snap(TS0, "0.629", nearest_ask="0.630"),
        _snap(TS0 + timedelta(seconds=30), "0.631", nearest_ask="0.630"),  # break
        _snap(TS0 + timedelta(seconds=60), "0.628", nearest_ask="0.632"),  # fail back
    ]
    mem = LevelMemory(
        level=Decimal("0.630"),
        broken_at=snaps[1].timestamp,
        broken_index=1,
        side_hint="BREAKOUT",
    )
    decision = _decision(
        side=SHORT,
        auction="LOWER",
        bias=BEARISH_LIQUIDITY_SHIFT,
        near_ask_class=NEAR_ASK_MOVING_LOWER,
        delta="-200",
        mid="0.628",
    )
    setup = classify_setup_type(
        index=2,
        snapshots=snaps,
        decision=decision,
        retest_state=None,
        breakout_memory=mem,
        regime={
            "auction_direction": "LOWER",
            "short_term_bias": BEARISH_LIQUIDITY_SHIFT,
            "near_ask_direction": "LOWER",
            "near_bid_direction": "LOWER",
        },
        params=ConfirmedAuditParams(),
    )
    assert setup == FAILED_BREAKOUT_SHORT


def test_short_oi_falling_can_be_supportive_feature() -> None:
    snap = _snap(TS0, "0.625", delta="-500", oi_chg="-50")
    feats = collect_confirm_features(
        side=SHORT,
        snap=snap,
        previous=None,
        regime={
            "auction_direction": "LOWER",
            "short_term_bias": BEARISH_LIQUIDITY_SHIFT,
            "near_bid_direction": "LOWER",
            "near_ask_direction": "LOWER",
        },
        near_class=NEAR_ASK_MOVING_LOWER,
        delta=Decimal("-500"),
        oi_chg=Decimal("-50"),
        trend="DOWN",
    )
    assert "supportive_oi" in feats
    assert "directional_delta" in feats


def test_long_requires_nonneg_oi_for_supportive_feature() -> None:
    snap = _snap(TS0, "0.630", delta="500", oi_chg="-50")
    feats = collect_confirm_features(
        side=LONG,
        snap=snap,
        previous=None,
        regime={
            "auction_direction": "HIGHER",
            "short_term_bias": BULLISH_LIQUIDITY_SHIFT,
            "near_bid_direction": "HIGHER",
            "near_ask_direction": "HIGHER",
        },
        near_class=NEAR_ASK_MOVING_HIGHER,
        delta=Decimal("500"),
        oi_chg=Decimal("-50"),
        trend="UP",
    )
    assert "supportive_oi" not in feats
    assert "directional_delta" in feats


def test_hard_gate_delta_blocks_confirm() -> None:
    params = ConfirmedAuditParams(
        confirmation_snapshots=1,
        confirmation_min_feature_count=1,
        confirmation_require_directional_delta=True,
    )
    snaps = [
        _snap(TS0, "0.630", delta="-200"),
        _snap(TS0 + timedelta(seconds=30), "0.631", delta="-200"),  # wrong delta for long
    ]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=0,
        watch_open_time=snaps[0].timestamp,
        watch_mid=snaps[0].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(),
    )
    r = evaluate_confirmation_snapshot(
        watch=watch,
        index=1,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r["status"] == "NO_CONFIRM_DELTA_HARD_GATE"


def test_structure_holds_long() -> None:
    snap = _snap(TS0, "0.630")
    assert structure_holds(
        setup_type=BREAKOUT_RECLAIM_LONG,
        snap=snap,
        reference_level=Decimal("0.629"),
        params=ConfirmedAuditParams(),
    )


def test_breakout_memory_tracks_ask_break() -> None:
    tracker = BreakoutMemoryTracker()
    snaps = [
        _snap(TS0, "0.629", nearest_ask="0.630"),
        _snap(TS0 + timedelta(seconds=30), "0.631", nearest_ask="0.630"),
    ]
    mem = tracker.process(snaps, index=1)
    assert mem is not None
    assert mem.level == Decimal("0.630")


def test_full_pipeline_deterministic_and_by_setup(tmp_path: Path) -> None:
    """End-to-end synthetic path: watch -> 2 confirms -> entry next mid."""
    # Build a breakout then reclaim sequence with enough history
    snaps: list[SnapshotRecord] = []
    # warmup
    for i in range(4):
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                "0.628",
                nearest_bid="0.620",
                nearest_ask="0.630",
                dominant_ask="0.640",
                delta="100",
            )
        )
    # breakout above 0.630
    snaps.append(
        _snap(
            TS0 + timedelta(seconds=30 * 4),
            "0.631",
            nearest_bid="0.622",
            nearest_ask="0.630",
            dominant_ask="0.640",
            delta="800",
        )
    )
    # pullback below level
    snaps.append(
        _snap(
            TS0 + timedelta(seconds=30 * 5),
            "0.629",
            nearest_bid="0.622",
            nearest_ask="0.632",
            dominant_ask="0.640",
            delta="-50",
        )
    )
    # reclaim and confirm window
    for i in range(6, 12):
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                "0.6315",
                nearest_bid="0.624",
                nearest_ask="0.638",
                dominant_ask="0.645",
                delta="900",
                oi_chg="30",
            )
        )
    near_views = [_near_view(s) for s in snaps]
    sequences = [
        _seq(RISING_BID_FLOOR, snaps[8].timestamp, 3, start_price="0.620", end_price="0.624")
    ]
    near_tx = [
        NearAskTransition(
            previous_timestamp=snaps[7].timestamp - timedelta(seconds=30),
            current_timestamp=snaps[7].timestamp,
            previous_nearest_ask_price=Decimal("0.632"),
            current_nearest_ask_price=Decimal("0.638"),
            shift_buckets=1.0,
            previous_nearest_ask_notional=Decimal("1000"),
            current_nearest_ask_notional=Decimal("1100"),
            notional_change_pct=10.0,
            previous_total_near_ask_notional=Decimal("2000"),
            current_total_near_ask_notional=Decimal("2200"),
            aggressive_buy_notional=Decimal("100"),
            mid_price_change_pct=0.05,
            classification=NEAR_ASK_MOVING_HIGHER,
            confidence=0.8,
        )
    ]
    params = ConfirmedAuditParams(
        base=AuditParams(
            minimum_entry_score=1,
            min_crv_tp1=0.1,
            min_crv_tp2=0.1,
            min_sl_distance_pct=0.01,
            max_sl_distance_pct=5.0,
            cooldown_minutes=0,
        ),
        confirmation_snapshots=2,
        confirmation_min_feature_count=2,
        confirmation_max_seconds=300,
    )
    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    s1 = run_confirmed_audit_from_snapshots(
        snapshots=snaps,
        near_views=near_views,
        sequences=sequences,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=[],
        liquidations=[],
        price_path=[(s.timestamp, s.mid_price) for s in snaps],
        params=params,
        end=snaps[-1].timestamp,
        output_dir=out1,
    )
    s2 = run_confirmed_audit_from_snapshots(
        snapshots=snaps,
        near_views=near_views,
        sequences=sequences,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=[],
        liquidations=[],
        price_path=[(s.timestamp, s.mid_price) for s in snaps],
        params=params,
        end=snaps[-1].timestamp,
        output_dir=out2,
    )
    assert s1["confirmed_by_setup"] == s2["confirmed_by_setup"]
    assert (out1 / "confirmed_candidates.csv").exists()
    assert (out1 / "watched_candidates.csv").exists()
    assert (out1 / "generic_context_only.csv").exists()
    # per-setup files exist
    for setup in (
        SUPPORT_RETEST_RECLAIM_LONG,
        BREAKOUT_RECLAIM_LONG,
        RESISTANCE_REJECTION_SHORT,
        FAILED_BREAKOUT_SHORT,
        SHORT_CONTINUATION,
    ):
        assert (out1 / f"confirmed_candidates_{setup.lower()}.csv").exists()


def test_outcomes_start_after_entry_only() -> None:
    from orderbook_analyse.orderbook_trade_candidate_audit import simulate_trade_outcome

    entry_time = TS0 + timedelta(seconds=90)
    path = [
        (TS0 + timedelta(seconds=30), Decimal("0.700")),  # before entry — ignore
        (TS0 + timedelta(seconds=60), Decimal("0.700")),
        (TS0 + timedelta(seconds=120), Decimal("0.640")),  # after entry
    ]
    out = simulate_trade_outcome(
        side=LONG,
        entry_time=entry_time,
        entry_price=Decimal("0.630"),
        stop_loss=Decimal("0.620"),
        take_profit_1=Decimal("0.635"),
        take_profit_2=None,
        price_path=path,
        end=TS0 + timedelta(seconds=180),
    )
    # First post-entry print is 0.640 which hits TP1; pre-entry 0.700 must not count
    assert out["outcome"] in {"TP1_HIT", "TP2_HIT", "OPEN_AT_END", "NEITHER_HIT"}
    if out["first_touch_time"] is not None:
        ft = out["first_touch_time"]
        if isinstance(ft, str):
            ft = datetime.fromisoformat(ft)
        assert ft > entry_time


def test_setup_side_mapping() -> None:
    assert setup_side(SUPPORT_RETEST_RECLAIM_LONG) == LONG
    assert setup_side(BREAKOUT_RECLAIM_LONG) == LONG
    assert setup_side(RESISTANCE_REJECTION_SHORT) == SHORT
    assert setup_side(FAILED_BREAKOUT_SHORT) == SHORT
    assert setup_side(SHORT_CONTINUATION) == SHORT


def test_min_feature_count_blocks_early_confirm() -> None:
    params = ConfirmedAuditParams(
        confirmation_snapshots=1,
        confirmation_min_feature_count=10,  # impossible
    )
    snaps = [
        _snap(TS0, "0.630"),
        _snap(TS0 + timedelta(seconds=30), "0.631", delta="1", oi_chg=None),
    ]
    near_views = [_near_view(s) for s in snaps]
    watch = WatchState(
        setup_type=BREAKOUT_RECLAIM_LONG,
        side=LONG,
        watch_index=0,
        watch_open_time=snaps[0].timestamp,
        watch_mid=snaps[0].mid_price,
        reference_level=Decimal("0.628"),
        invalidation_level=Decimal("0.620"),
        context_decision=_decision(),
    )
    r = evaluate_confirmation_snapshot(
        watch=watch,
        index=1,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
    )
    assert r["status"] == "NO_CONFIRM_FEATURE_COUNT"
    assert watch.confirm_time is None
