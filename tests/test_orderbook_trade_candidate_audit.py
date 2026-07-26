"""Unit tests for causal orderbook trade-candidate audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orderbook_analyse.liquidation_analysis import (
    LIQUIDATED_LONG,
    LIQUIDATED_SHORT,
    LiquidationEvent,
    PRICE_TYPE_BANKRUPTCY,
    SIDE_SEMANTICS_STATUS,
)
from orderbook_analyse.near_liquidity import (
    NEAR_ASK_MOVING_HIGHER,
    NEAR_ASK_MOVING_LOWER,
    NearAskTransition,
    NearSnapshotView,
)
from orderbook_analyse.orderbook_trade_candidate_audit import (
    AMBIGUOUS_TP_SL_ORDER,
    LONG,
    NO_TRADE,
    NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER,
    NO_TRADE_CONFLICTING_STRUCTURE,
    NO_TRADE_RETEST_BROKEN,
    NO_TRADE_RETEST_SL_TOO_WIDE,
    NO_TRADE_INSUFFICIENT_CRV,
    NO_TRADE_TOO_WIDE_SL,
    SHORT,
    SUPPORT_RETEST_RECLAIM_LONG,
    SL_HIT,
    TP1_HIT,
    TP2_HIT,
    AuditParams,
    CandidateDecision,
    RetestTracker,
    aged_sequence,
    apply_cooldown_filter,
    build_accepted_candidate,
    compute_retest_stop_loss,
    compute_stop_loss,
    compute_take_profits,
    evaluate_candidate_at,
    resolve_entry_price,
    run_audit_from_snapshots,
    simulate_trade_outcome,
)
from orderbook_analyse.wall_movement_tracker import (
    FALLING_ASK_CEILING,
    FALLING_BID_FLOOR,
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


def _near_tx(ts: datetime, classification: str, prev: str, cur: str) -> NearAskTransition:
    return NearAskTransition(
        previous_timestamp=ts - timedelta(seconds=30),
        current_timestamp=ts,
        previous_nearest_ask_price=Decimal(prev),
        current_nearest_ask_price=Decimal(cur),
        shift_buckets=1.0,
        previous_nearest_ask_notional=Decimal("1000"),
        current_nearest_ask_notional=Decimal("900"),
        notional_change_pct=-10.0,
        previous_total_near_ask_notional=Decimal("2000"),
        current_total_near_ask_notional=Decimal("1800"),
        aggressive_buy_notional=Decimal("50"),
        mid_price_change_pct=0.05,
        classification=classification,
        confidence=0.7,
    )


def _liq(ts: datetime, side: str, price: str = "0.636") -> LiquidationEvent:
    interpreted = LIQUIDATED_LONG if side == "Buy" else LIQUIDATED_SHORT
    return LiquidationEvent(
        event_key=f"k|{ts.isoformat()}|{side}",
        exchange_timestamp=ts,
        received_timestamp=ts,
        symbol="APTUSDT",
        raw_side=side,
        interpreted_position_side=interpreted,
        bankruptcy_price=Decimal(price),
        liquidation_qty=Decimal("10"),
        liquidation_notional=Decimal("6"),
        price_type=PRICE_TYPE_BANKRUPTCY,
        side_semantics_status=SIDE_SEMANTICS_STATUS,
    )


def test_no_lookahead_entry_uses_only_past_sequences() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630") for i in range(6)]
    future_seq = _seq(RISING_BID_FLOOR, TS0 + timedelta(hours=1), shifts=3)
    near_views = [_near_view(s) for s in snaps]
    d = evaluate_candidate_at(
        index=5,
        snapshots=snaps,
        near_views=near_views,
        sequences=[future_seq],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=1),
    )
    assert d.rising_bid_shifts == 0


def test_next_snapshot_mid_entry() -> None:
    snaps = [_snap(TS0, "0.630"), _snap(TS0 + timedelta(seconds=30), "0.631")]
    entry = resolve_entry_price(mode="next-snapshot-mid", signal_index=0, snapshots=snaps, trades=[])
    assert entry is not None
    assert entry[0] == snaps[1].timestamp
    assert entry[1] == Decimal("0.631")


def test_rising_bid_and_ask_higher_can_long() -> None:
    snaps = []
    for i in range(8):
        mid = Decimal("0.620") + Decimal(i) * Decimal("0.001")
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                nearest_bid="0.615",
                nearest_ask="0.640",
                dominant_ask="0.650",
                delta="2000",
                oi_chg="100",
            )
        )
    near_views = [_near_view(s) for s in snaps]
    for i in range(1, len(near_views)):
        near_views[i] = NearSnapshotView(
            nearest_bid=_wall("bid", "0.618"),
            nearest_ask=_wall("ask", "0.642"),
            dominant_bid=near_views[i].dominant_bid,
            dominant_ask=near_views[i].dominant_ask,
            near_bids=near_views[i].near_bids,
            near_asks=near_views[i].near_asks,
            total_near_bid_notional=near_views[i].total_near_bid_notional * Decimal("1.2"),
            total_near_ask_notional=near_views[i].total_near_ask_notional,
        )
    seqs = [_seq(RISING_BID_FLOOR, snaps[-1].timestamp, shifts=3, end_price="0.618")]
    near_tx = [_near_tx(snaps[-1].timestamp, NEAR_ASK_MOVING_HIGHER, "0.639", "0.641")]
    d = evaluate_candidate_at(
        index=len(snaps) - 1,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=5, flow_lookback_snapshots=3),
    )
    assert d.side == LONG


def test_falling_ask_and_bid_lower_can_short() -> None:
    snaps = []
    for i in range(8):
        mid = Decimal("0.640") - Decimal(i) * Decimal("0.001")
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                nearest_bid="0.610",
                nearest_ask="0.645",
                dominant_bid="0.600",
                delta="-2000",
                oi_chg="50",
            )
        )
    near_views = [_near_view(s) for s in snaps]
    for i in range(1, len(near_views)):
        near_views[i] = NearSnapshotView(
            nearest_bid=_wall("bid", "0.608"),
            nearest_ask=_wall("ask", "0.643"),
            dominant_bid=near_views[i].dominant_bid,
            dominant_ask=near_views[i].dominant_ask,
            near_bids=near_views[i].near_bids,
            near_asks=near_views[i].near_asks,
            total_near_bid_notional=Decimal("500"),
            total_near_ask_notional=Decimal("2000"),
        )
    seqs = [
        _seq(FALLING_ASK_CEILING, snaps[-1].timestamp, shifts=2, start_price="0.650", end_price="0.643"),
        _seq(FALLING_BID_FLOOR, snaps[-1].timestamp, shifts=2, start_price="0.615", end_price="0.608"),
    ]
    near_tx = [_near_tx(snaps[-1].timestamp, NEAR_ASK_MOVING_LOWER, "0.646", "0.643")]
    d = evaluate_candidate_at(
        index=len(snaps) - 1,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=5, flow_lookback_snapshots=3),
    )
    assert d.side == SHORT


def test_contradiction_no_trade() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630", delta="100") for i in range(5)]
    near_views = [_near_view(s) for s in snaps]
    seqs = [
        _seq(RISING_BID_FLOOR, snaps[-1].timestamp, shifts=3),
        _seq(FALLING_BID_FLOOR, snaps[-1].timestamp, shifts=2),
    ]
    near_tx = [_near_tx(snaps[-1].timestamp, NEAR_ASK_MOVING_HIGHER, "0.639", "0.641")]
    d = evaluate_candidate_at(
        index=4,
        snapshots=snaps,
        near_views=near_views,
        sequences=seqs,
        transitions=[],
        near_tx=near_tx,
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=1),
    )
    assert d.side == NO_TRADE


def test_long_sl_below_bid_wall() -> None:
    params = AuditParams(sl_buffer_bps=8, min_sl_distance_pct=0.05, max_sl_distance_pct=5.0)
    out = compute_stop_loss(
        side=LONG,
        entry=Decimal("0.630"),
        nearest_bid=Decimal("0.620"),
        nearest_ask=Decimal("0.640"),
        dominant_bid=Decimal("0.615"),
        dominant_ask=Decimal("0.650"),
        params=params,
    )
    assert not isinstance(out, str)
    ref_type, ref, sl, dist, risk = out
    assert ref_type == "nearest_bid_wall"
    assert ref == Decimal("0.620")
    assert sl < Decimal("0.620")


def test_short_sl_above_ask_wall() -> None:
    params = AuditParams(sl_buffer_bps=8, min_sl_distance_pct=0.05, max_sl_distance_pct=5.0)
    out = compute_stop_loss(
        side=SHORT,
        entry=Decimal("0.630"),
        nearest_bid=Decimal("0.620"),
        nearest_ask=Decimal("0.640"),
        dominant_bid=None,
        dominant_ask=None,
        params=params,
    )
    assert not isinstance(out, str)
    _, ref, sl, _, _ = out
    assert ref == Decimal("0.640")
    assert sl > Decimal("0.640")


def test_sl_minimum_widens() -> None:
    params = AuditParams(sl_buffer_bps=1, min_sl_distance_pct=0.50, max_sl_distance_pct=5.0)
    out = compute_stop_loss(
        side=LONG,
        entry=Decimal("1.00"),
        nearest_bid=Decimal("0.999"),
        nearest_ask=Decimal("1.01"),
        dominant_bid=None,
        dominant_ask=None,
        params=params,
    )
    assert not isinstance(out, str)
    _, _, sl, dist, _ = out
    assert abs(dist - 0.50) < 1e-9
    assert sl == Decimal("0.995")


def test_sl_maximum_rejects() -> None:
    params = AuditParams(sl_buffer_bps=8, min_sl_distance_pct=0.10, max_sl_distance_pct=0.50)
    out = compute_stop_loss(
        side=LONG,
        entry=Decimal("1.00"),
        nearest_bid=Decimal("0.90"),
        nearest_ask=Decimal("1.05"),
        dominant_bid=None,
        dominant_ask=None,
        params=params,
    )
    assert out == NO_TRADE_TOO_WIDE_SL


def test_tp_front_of_ask_wall() -> None:
    params = AuditParams(tp_front_run_bps=5, min_crv_tp1=0.5, min_crv_tp2=0.5)
    fields, reject = compute_take_profits(
        side=LONG,
        entry=Decimal("0.630"),
        stop_loss=Decimal("0.620"),
        nearest_bid=Decimal("0.620"),
        nearest_ask=Decimal("0.650"),
        dominant_bid=None,
        dominant_ask=Decimal("0.660"),
        params=params,
    )
    assert reject is None and fields is not None
    assert fields["take_profit_1"] < Decimal("0.650")


def test_short_tp_front_of_bid_wall() -> None:
    params = AuditParams(tp_front_run_bps=5, min_crv_tp1=0.5, min_crv_tp2=0.5)
    fields, reject = compute_take_profits(
        side=SHORT,
        entry=Decimal("0.630"),
        stop_loss=Decimal("0.640"),
        nearest_bid=Decimal("0.610"),
        nearest_ask=Decimal("0.640"),
        dominant_bid=Decimal("0.600"),
        dominant_ask=None,
        params=params,
    )
    assert reject is None and fields is not None
    assert fields["take_profit_1"] > Decimal("0.610")


def test_crv_gate_rejects() -> None:
    params = AuditParams(tp_front_run_bps=0, min_crv_tp1=5.0, min_crv_tp2=5.0)
    fields, reject = compute_take_profits(
        side=LONG,
        entry=Decimal("0.630"),
        stop_loss=Decimal("0.620"),
        nearest_bid=Decimal("0.620"),
        nearest_ask=Decimal("0.632"),
        dominant_bid=None,
        dominant_ask=Decimal("0.633"),
        params=params,
    )
    assert fields is None
    assert reject == NO_TRADE_INSUFFICIENT_CRV


def test_tp1_first() -> None:
    out = simulate_trade_outcome(
        side=LONG,
        entry_time=TS0,
        entry_price=Decimal("1.00"),
        stop_loss=Decimal("0.99"),
        take_profit_1=Decimal("1.02"),
        take_profit_2=None,
        price_path=[(TS0 + timedelta(seconds=10), Decimal("1.02"))],
        end=TS0 + timedelta(minutes=10),
    )
    assert out["outcome"] == TP1_HIT


def test_tp2_first() -> None:
    out = simulate_trade_outcome(
        side=LONG,
        entry_time=TS0,
        entry_price=Decimal("1.00"),
        stop_loss=Decimal("0.99"),
        take_profit_1=Decimal("1.02"),
        take_profit_2=Decimal("1.05"),
        price_path=[(TS0 + timedelta(seconds=10), Decimal("1.05"))],
        end=TS0 + timedelta(minutes=10),
    )
    assert out["outcome"] == TP2_HIT


def test_sl_first() -> None:
    out = simulate_trade_outcome(
        side=LONG,
        entry_time=TS0,
        entry_price=Decimal("1.00"),
        stop_loss=Decimal("0.99"),
        take_profit_1=Decimal("1.02"),
        take_profit_2=None,
        price_path=[(TS0 + timedelta(seconds=10), Decimal("0.99"))],
        end=TS0 + timedelta(minutes=10),
    )
    assert out["outcome"] == SL_HIT


def test_ambiguous_tp_sl_same_timestamp() -> None:
    out = simulate_trade_outcome(
        side=LONG,
        entry_time=TS0,
        entry_price=Decimal("1.00"),
        stop_loss=Decimal("0.99"),
        take_profit_1=Decimal("1.02"),
        take_profit_2=None,
        price_path=[(TS0 + timedelta(seconds=10), Decimal("1.02")), (TS0 + timedelta(seconds=10), Decimal("0.99"))],
        end=TS0 + timedelta(minutes=10),
    )
    assert out["outcome"] in {TP1_HIT, AMBIGUOUS_TP_SL_ORDER, SL_HIT}


def test_liquidation_alone_no_signal() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630", delta="0", oi_chg="0") for i in range(5)]
    near_views = [_near_view(s) for s in snaps]
    d = evaluate_candidate_at(
        index=4,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[_liq(snaps[-1].timestamp, "Sell", "0.640")],
        params=AuditParams(minimum_entry_score=1),
    )
    assert d.side == NO_TRADE


def test_short_liq_only_confirms_long_not_creates() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630", delta="-10") for i in range(4)]
    near_views = [_near_view(s) for s in snaps]
    d = evaluate_candidate_at(
        index=3,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[_liq(snaps[-1].timestamp, "Sell")],
        params=AuditParams(minimum_entry_score=1),
    )
    assert d.side == NO_TRADE


def test_long_liq_only_confirms_short_not_creates() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630", delta="10") for i in range(4)]
    near_views = [_near_view(s) for s in snaps]
    d = evaluate_candidate_at(
        index=3,
        snapshots=snaps,
        near_views=near_views,
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[_liq(snaps[-1].timestamp, "Buy")],
        params=AuditParams(minimum_entry_score=1),
    )
    assert d.side == NO_TRADE


def test_episodes_deduped() -> None:
    decisions = [
        CandidateDecision(signal_time=TS0, side=LONG, reason="x", score=6, components=[], snapshot_index=0),
        CandidateDecision(signal_time=TS0 + timedelta(minutes=1), side=LONG, reason="x", score=6, components=[], snapshot_index=1),
        CandidateDecision(signal_time=TS0 + timedelta(minutes=2), side=NO_TRADE, reason="neutral", score=0, components=[], snapshot_index=2),
        CandidateDecision(signal_time=TS0 + timedelta(minutes=3), side=LONG, reason="x", score=6, components=[], snapshot_index=3),
    ]
    accepted, rejected, episodes = apply_cooldown_filter(decisions, cooldown_minutes=5)
    assert len(accepted) == 1
    assert any(r.reason == "DEDUPED_SAME_EPISODE" for r in rejected)
    assert any(r.reason == "NO_TRADE_COOLDOWN" for r in rejected)


def test_deterministic_outputs(tmp_path: Path) -> None:
    snaps = []
    for i in range(10):
        mid = Decimal("0.620") + Decimal(i) * Decimal("0.001")
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                nearest_bid="0.615",
                nearest_ask="0.645",
                dominant_ask="0.660",
                delta="3000",
                oi_chg="20",
            )
        )
    near_views = [_near_view(s) for s in snaps]
    for i in range(1, len(near_views)):
        near_views[i] = NearSnapshotView(
            nearest_bid=_wall("bid", "0.617"),
            nearest_ask=_wall("ask", "0.646"),
            dominant_bid=near_views[i].dominant_bid,
            dominant_ask=near_views[i].dominant_ask,
            near_bids=near_views[i].near_bids,
            near_asks=near_views[i].near_asks,
            total_near_bid_notional=Decimal("2000"),
            total_near_ask_notional=Decimal("1500"),
        )
    seqs = [_seq(RISING_BID_FLOOR, snaps[-1].timestamp, shifts=3, end_price="0.617")]
    near_tx = [_near_tx(snaps[i].timestamp, NEAR_ASK_MOVING_HIGHER, "0.644", "0.646") for i in range(5, 10)]
    path = [(s.timestamp, s.mid_price) for s in snaps]
    path += [(snaps[-1].timestamp + timedelta(seconds=s), Decimal("0.650")) for s in range(30, 600, 30)]
    params = AuditParams(
        minimum_entry_score=5,
        entry_mode="next-snapshot-mid",
        min_crv_tp1=0.5,
        min_crv_tp2=0.5,
        min_sl_distance_pct=0.10,
        max_sl_distance_pct=5.0,
        cooldown_minutes=5,
    )
    a = tmp_path / "a"
    b = tmp_path / "b"
    s1 = run_audit_from_snapshots(
        snapshots=snaps, near_views=near_views, sequences=seqs, transitions=[], near_tx=near_tx,
        ladder_seqs=[], liquidations=[], price_path=path, params=params,
        end=snaps[-1].timestamp + timedelta(minutes=30), output_dir=a,
    )
    s2 = run_audit_from_snapshots(
        snapshots=snaps, near_views=near_views, sequences=seqs, transitions=[], near_tx=near_tx,
        ladder_seqs=[], liquidations=[], price_path=path, params=params,
        end=snaps[-1].timestamp + timedelta(minutes=30), output_dir=b,
    )
    assert s1["accepted"] == s2["accepted"]
    assert (a / "trade_candidates.csv").read_text() == (b / "trade_candidates.csv").read_text()


def test_old_falling_ask_expires_after_max_age() -> None:
    seq = _seq(FALLING_ASK_CEILING, TS0, shifts=7)
    found, age, expired = aged_sequence(
        [seq], FALLING_ASK_CEILING, TS0 + timedelta(seconds=121), 120
    )
    assert found is seq
    assert age == 121
    assert expired


def _bullish_decision_with_falling_ask(age_seconds: int) -> CandidateDecision:
    snaps = [
        _snap(
            TS0 + timedelta(seconds=30 * i),
            str(Decimal("0.630") + Decimal(i) * Decimal("0.001")),
            delta="1000",
        )
        for i in range(6)
    ]
    as_of = snaps[-1].timestamp
    return evaluate_candidate_at(
        index=5,
        snapshots=snaps,
        near_views=[_near_view(s) for s in snaps],
        sequences=[
            _seq(RISING_BID_FLOOR, as_of, shifts=3),
            _seq(
                FALLING_ASK_CEILING,
                as_of - timedelta(seconds=age_seconds),
                shifts=7,
            ),
        ],
        transitions=[],
        near_tx=[_near_tx(as_of, NEAR_ASK_MOVING_HIGHER, "0.639", "0.641")],
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=1),
    )


def test_active_falling_ask_blocks_long() -> None:
    decision = _bullish_decision_with_falling_ask(30)
    assert decision.side == NO_TRADE
    assert decision.reason == NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER
    assert decision.active_falling_ask_shifts == 7


def test_expired_falling_ask_does_not_block_or_zero_score() -> None:
    decision = _bullish_decision_with_falling_ask(121)
    assert not decision.falling_ask_blocker_active
    assert decision.expired_falling_ask_shifts == 7
    assert decision.score > 0
    assert decision.reason != NO_TRADE_ACTIVE_FALLING_ASK_BLOCKER


def _retest_snaps(*, second_reclaim: bool = True, broken: bool = False) -> list[SnapshotRecord]:
    mids = ["0.9990", "1.0020", "1.0005"]
    if broken:
        mids.append("0.9980")
    elif second_reclaim:
        mids.append("1.0006")
    deltas = ["0", "-200", "-100", "100"]
    return [
        _snap(
            TS0 + timedelta(seconds=30 * i),
            mid,
            nearest_bid="0.9995",
            nearest_ask="1.0000" if i == 0 else "1.0100",
            dominant_bid="0.9500",
            dominant_ask="1.0300",
            delta=deltas[i],
            oi_chg="10",
        )
        for i, mid in enumerate(mids)
    ]


def test_reclaim_requires_two_stable_snapshots() -> None:
    params = AuditParams(reclaim_confirm_snapshots=2)
    tracker = RetestTracker(params)
    snaps = _retest_snaps()
    tracker.process(snaps, index=1)
    one = tracker.process(snaps, index=2)
    assert one is not None and one.reclaim_snapshot_count == 1
    assert one.reclaim_confirm_time is None
    two = tracker.process(snaps, index=3)
    assert two is not None and two.reclaim_confirm_time == snaps[3].timestamp


def test_support_retest_level_is_causally_identified() -> None:
    tracker = RetestTracker(AuditParams())
    snaps = _retest_snaps()
    state = tracker.process(snaps, index=1)
    assert state is not None
    assert state.reference_level == Decimal("1.0000")
    assert state.identified_time == snaps[1].timestamp


def test_retest_undershoot_within_tolerance() -> None:
    tracker = RetestTracker(AuditParams(retest_max_undershoot_bps=10))
    snaps = _retest_snaps()
    tracker.process(snaps, index=1)
    state = tracker.process(snaps, index=2)
    assert state is not None and not state.broken
    assert state.undershoot_bps == 0


def test_retest_breaks_on_excessive_undershoot() -> None:
    tracker = RetestTracker(AuditParams(retest_max_undershoot_bps=10))
    snaps = _retest_snaps(broken=True)
    tracker.process(snaps, index=1)
    tracker.process(snaps, index=2)
    state = tracker.process(snaps, index=3)
    assert state is not None and state.broken
    decision = evaluate_candidate_at(
        index=3,
        snapshots=snaps,
        near_views=[_near_view(s) for s in snaps],
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=1),
        retest_state=state,
    )
    assert decision.reason == NO_TRADE_RETEST_BROKEN


def test_support_retest_reclaim_long_forms_not_on_first_snapshot() -> None:
    params = AuditParams(minimum_entry_score=5, reclaim_confirm_snapshots=2)
    tracker = RetestTracker(params)
    snaps = _retest_snaps()
    tracker.process(snaps, index=1)
    first = tracker.process(snaps, index=2)
    first_decision = evaluate_candidate_at(
        index=2,
        snapshots=snaps,
        near_views=[_near_view(s) for s in snaps],
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
        retest_state=first,
    )
    assert first_decision.side == NO_TRADE
    confirmed = tracker.process(snaps, index=3)
    decision = evaluate_candidate_at(
        index=3,
        snapshots=snaps,
        near_views=[_near_view(s) for s in snaps],
        sequences=[],
        transitions=[],
        near_tx=[],
        ladder_seqs=[],
        liquidations=[],
        params=params,
        retest_state=confirmed,
    )
    assert decision.side == LONG
    assert decision.entry_setup_type == SUPPORT_RETEST_RECLAIM_LONG


def test_retest_local_sl_priority_and_deep_floor_not_preferred() -> None:
    params = AuditParams(
        retest_sl_buffer_bps=5,
        retest_max_sl_distance_pct=5,
    )
    low = compute_retest_stop_loss(
        entry=Decimal("1.001"),
        retest_low=Decimal("0.9990"),
        local_near_bid=Decimal("0.9995"),
        reclaim_level=Decimal("1.0000"),
        deeper_bid_floor=Decimal("0.9500"),
        params=params,
    )
    assert not isinstance(low, str) and low[0] == "RETEST_LOW"
    near = compute_retest_stop_loss(
        entry=Decimal("1.001"),
        retest_low=Decimal("1.0005"),
        local_near_bid=Decimal("0.9995"),
        reclaim_level=Decimal("1.0000"),
        deeper_bid_floor=Decimal("0.9500"),
        params=params,
    )
    assert not isinstance(near, str) and near[0] == "LOCAL_NEAR_BID"
    assert near[1] != Decimal("0.9500")


def test_retest_sl_too_wide_rejects() -> None:
    out = compute_retest_stop_loss(
        entry=Decimal("1.00"),
        retest_low=Decimal("0.99"),
        local_near_bid=None,
        reclaim_level=None,
        deeper_bid_floor=None,
        params=AuditParams(retest_max_sl_distance_pct=0.60),
    )
    assert out == NO_TRADE_RETEST_SL_TOO_WIDE


def test_concrete_conflicting_structure_reason() -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), "0.630") for i in range(5)]
    as_of = snaps[-1].timestamp
    decision = evaluate_candidate_at(
        index=4,
        snapshots=snaps,
        near_views=[_near_view(s) for s in snaps],
        sequences=[
            _seq(RISING_BID_FLOOR, as_of, shifts=3),
            _seq(FALLING_BID_FLOOR, as_of, shifts=2),
        ],
        transitions=[],
        near_tx=[_near_tx(as_of, NEAR_ASK_MOVING_HIGHER, "0.639", "0.641")],
        ladder_seqs=[],
        liquidations=[],
        params=AuditParams(minimum_entry_score=1),
    )
    assert decision.reason == NO_TRADE_CONFLICTING_STRUCTURE
    assert decision.score != 0
