"""Unit tests for causal bid-weakening / reversal warning audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orderbook_analyse.orderbook_bid_weakening_reversal_audit import (
    BID_SUPPORT_WEAKENING,
    EXPIRED,
    REVERSAL_CONFIRMED,
    REVERSAL_WARNING,
    TREND_UP_HEALTHY,
    WARNING_FAILED,
    BidWeakeningParams,
    MachineState,
    WarningEvent,
    advance_state,
    compute_features_at,
    run_bid_weakening_audit_from_snapshots,
    simulate_warning_outcomes,
    warning_score_components,
)
from orderbook_analyse.wall_movement_tracker import (
    WALL_PULLED,
    WALL_REPLACED_LOWER,
    SnapshotRecord,
    TransitionRecord,
    WallView,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _wall(side: str, price: str, notional: str = "10000", *, dist: float = 0.5) -> WallView:
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
    bid_notional: str = "10000",
    ask_notional: str = "8000",
    bid_count: int = 2,
    delta: str = "100",
    oi_chg: str | None = "10",
) -> SnapshotRecord:
    nb = None if nearest_bid is None else _wall("bid", nearest_bid, bid_notional)
    na = None if nearest_ask is None else _wall("ask", nearest_ask, ask_notional)
    near_bids = []
    if nb is not None:
        near_bids.append(nb)
        if bid_count > 1:
            near_bids.append(
                _wall(
                    "bid",
                    format(Decimal(nearest_bid) - Decimal("0.002"), "f"),
                    str(Decimal(bid_notional) / 2),
                )
            )
    near_asks = [na] if na is not None else []
    return SnapshotRecord(
        timestamp=ts,
        mid_price=Decimal(mid),
        best_bid=Decimal(mid) - Decimal("0.0001"),
        best_ask=Decimal(mid) + Decimal("0.0001"),
        bucket_size=Decimal("0.001"),
        strongest_bid=nb,
        strongest_ask=na,
        top_bid_walls=near_bids[:3],
        top_ask_walls=near_asks[:3],
        all_bid_buckets={},
        all_ask_buckets={},
        buy_notional_since_prev=Decimal(delta) if Decimal(delta) > 0 else Decimal("0"),
        sell_notional_since_prev=Decimal("0") if Decimal(delta) > 0 else abs(Decimal(delta)),
        trade_delta_notional=Decimal(delta),
        open_interest=Decimal("1000000"),
        oi_change_since_prev=None if oi_chg is None else Decimal(oi_chg),
        nearest_bid=nb,
        nearest_ask=na,
        dominant_bid=nb,
        dominant_ask=na,
        near_bids=near_bids,
        near_asks=near_asks,
        total_near_bid_notional=sum((w.notional for w in near_bids), Decimal("0")),
        total_near_ask_notional=sum((w.notional for w in near_asks), Decimal("0")),
    )


def _tx(
    ts: datetime,
    classification: str,
    *,
    side: str = "bid",
    prev_price: str = "0.622",
    cur_price: str = "0.620",
) -> TransitionRecord:
    return TransitionRecord(
        previous_timestamp=ts - timedelta(seconds=30),
        current_timestamp=ts,
        side=side,
        previous_wall_price=Decimal(prev_price),
        current_wall_price=Decimal(cur_price),
        shift_buckets=-1.0,
        shift_pct=-0.3,
        previous_notional=Decimal("10000"),
        current_notional=Decimal("5000"),
        notional_change=Decimal("-5000"),
        old_wall_remaining_notional=Decimal("1000"),
        old_wall_remaining_ratio=0.1,
        mid_price_change_pct=-0.05,
        trade_delta_notional=Decimal("-200"),
        oi_change=Decimal("-10"),
        classification=classification,
    )


def test_bid_thinning_alone_not_confirmed_reversal() -> None:
    """Single soft bid drop may weaken but must not confirm reversal."""
    params = BidWeakeningParams(
        warning_min_feature_count=5,  # hard to reach
        warning_confirm_snapshots=2,
        bid_notional_drop_pct=50.0,
    )
    snaps = [
        _snap(TS0, "0.630", bid_notional="10000", nearest_bid="0.625"),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.6305",
            bid_notional="9000",
            nearest_bid="0.625",
            delta="50",
        ),
    ]
    feat0 = compute_features_at(index=0, snapshots=snaps, transitions=[], params=params)
    feat1 = compute_features_at(index=1, snapshots=snaps, transitions=[], params=params)
    machine = MachineState(baseline=feat0)
    machine, warning, timeline = advance_state(
        machine=machine, feat=feat1, params=params, warning_seq=1
    )
    assert warning is None
    assert machine.state != REVERSAL_CONFIRMED
    assert timeline["state"] in {BID_SUPPORT_WEAKENING, TREND_UP_HEALTHY}


def test_multiple_weak_snapshots_create_warning() -> None:
    params = BidWeakeningParams(
        warning_min_feature_count=3,
        warning_confirm_snapshots=2,
        bid_notional_drop_pct=20.0,
        nearest_bid_retreat_bps=3.0,
        cooldown_seconds=0,
    )
    snaps = [
        _snap(TS0, "0.630", bid_notional="20000", nearest_bid="0.626", bid_count=3, delta="500"),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.6295",
            bid_notional="12000",
            nearest_bid="0.624",
            bid_count=1,
            delta="-300",
            oi_chg="-20",
        ),
        _snap(
            TS0 + timedelta(seconds=60),
            "0.6290",
            bid_notional="8000",
            nearest_bid="0.623",
            bid_count=1,
            delta="-400",
            oi_chg="-30",
        ),
    ]
    transitions = [
        _tx(snaps[1].timestamp, WALL_PULLED),
        _tx(snaps[2].timestamp, WALL_REPLACED_LOWER),
    ]
    machine = MachineState()
    warning = None
    for i in range(len(snaps)):
        feat = compute_features_at(
            index=i, snapshots=snaps, transitions=transitions, params=params
        )
        machine, w, _ = advance_state(
            machine=machine, feat=feat, params=params, warning_seq=1
        )
        if w is not None:
            warning = w
    assert warning is not None
    assert warning.state == REVERSAL_WARNING
    assert machine.state == REVERSAL_WARNING


def test_new_bid_strength_invalidates_warning() -> None:
    params = BidWeakeningParams(warning_max_age_seconds=600)
    warning = WarningEvent(
        warning_id="W0001",
        warning_time=TS0,
        warning_index=2,
        state=REVERSAL_WARNING,
        score=6,
        feature_count=4,
        features_true=["bid_notional_drop"],
        mid=Decimal("0.629"),
        local_high=Decimal("0.631"),
        nearest_bid=Decimal("0.623"),
        dominant_bid=Decimal("0.623"),
        dominant_bid_notional=Decimal("8000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("8000"),
        nearest_ask=Decimal("0.635"),
        dominant_ask=Decimal("0.635"),
        active_ask_wall_notional_sum=Decimal("7000"),
        bid_ask_notional_ratio=1.1,
        trade_delta=Decimal("-200"),
        oi_change=Decimal("-10"),
        lower_high_confirmed=False,
        support_break_confirmed=False,
        local_support=Decimal("0.623"),
        expire_deadline=TS0 + timedelta(seconds=600),
    )
    snaps = [
        _snap(TS0, "0.629", bid_notional="8000", nearest_bid="0.623"),
        _snap(
            TS0 + timedelta(seconds=30),
            "0.6295",
            bid_notional="15000",
            nearest_bid="0.625",
            bid_count=3,
            delta="400",
        ),
    ]
    feat = compute_features_at(index=1, snapshots=snaps, transitions=[], params=params)
    machine = MachineState(state=REVERSAL_WARNING, active_warning=warning)
    machine, _, timeline = advance_state(
        machine=machine, feat=feat, params=params, warning_seq=1
    )
    assert timeline["state"] == WARNING_FAILED
    assert warning.terminal_state == WARNING_FAILED
    assert machine.active_warning is None


def test_new_high_invalidates_warning() -> None:
    params = BidWeakeningParams(warning_max_age_seconds=600)
    warning = WarningEvent(
        warning_id="W0002",
        warning_time=TS0,
        warning_index=2,
        state=REVERSAL_WARNING,
        score=5,
        feature_count=3,
        features_true=["no_new_high"],
        mid=Decimal("0.629"),
        local_high=Decimal("0.630"),
        nearest_bid=Decimal("0.624"),
        dominant_bid=Decimal("0.624"),
        dominant_bid_notional=Decimal("9000"),
        active_bid_wall_count=2,
        active_bid_wall_notional_sum=Decimal("9000"),
        nearest_ask=Decimal("0.636"),
        dominant_ask=Decimal("0.636"),
        active_ask_wall_notional_sum=Decimal("8000"),
        bid_ask_notional_ratio=1.1,
        trade_delta=Decimal("-100"),
        oi_change=Decimal("0"),
        lower_high_confirmed=False,
        support_break_confirmed=False,
        local_support=Decimal("0.624"),
        expire_deadline=TS0 + timedelta(seconds=600),
    )
    snaps = [
        _snap(TS0, "0.629"),
        _snap(TS0 + timedelta(seconds=30), "0.632"),  # new high vs warning.local_high
    ]
    feat = compute_features_at(index=1, snapshots=snaps, transitions=[], params=params)
    machine = MachineState(state=REVERSAL_WARNING, active_warning=warning)
    machine, _, timeline = advance_state(
        machine=machine, feat=feat, params=params, warning_seq=1
    )
    assert timeline["state"] == WARNING_FAILED
    assert warning.terminal_state == WARNING_FAILED


def test_lower_high_plus_weakness_scores_warning_features() -> None:
    params = BidWeakeningParams(
        bid_notional_drop_pct=20.0,
        nearest_bid_retreat_bps=3.0,
        lower_high_tolerance_bps=5.0,
        local_high_lookback_seconds=600,
    )
    # Build: high at 0.635, then lower high structure around 0.632 with bid drop
    snaps = [
        _snap(TS0, "0.630", bid_notional="20000", nearest_bid="0.626"),
        _snap(TS0 + timedelta(seconds=30), "0.635", bid_notional="20000", nearest_bid="0.628"),
        _snap(TS0 + timedelta(seconds=60), "0.633", bid_notional="15000", nearest_bid="0.627"),
        _snap(
            TS0 + timedelta(seconds=90),
            "0.631",
            bid_notional="10000",
            nearest_bid="0.624",
            bid_count=1,
            delta="-500",
            oi_chg="-40",
        ),
    ]
    transitions = [_tx(snaps[3].timestamp, WALL_PULLED)]
    feat = compute_features_at(
        index=3,
        snapshots=snaps,
        transitions=transitions,
        params=params,
        prior_local_high=Decimal("0.635"),
    )
    comps = warning_score_components(feat, params=params, baseline=None)
    names = [n for n, _ in comps]
    assert "lower_high" in names or feat.lower_high_confirmed
    assert any("bid" in n or "delta" in n or "pull" in n for n in names)


def test_support_break_confirms_reversal() -> None:
    params = BidWeakeningParams(
        support_break_bps=5.0,
        mid_down_confirm_snapshots=2,
        warning_max_age_seconds=600,
    )
    warning = WarningEvent(
        warning_id="W0003",
        warning_time=TS0,
        warning_index=2,
        state=REVERSAL_WARNING,
        score=7,
        feature_count=4,
        features_true=["bid_notional_drop", "lower_high"],
        mid=Decimal("0.630"),
        local_high=Decimal("0.635"),
        nearest_bid=Decimal("0.625"),
        dominant_bid=Decimal("0.625"),
        dominant_bid_notional=Decimal("8000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("8000"),
        nearest_ask=Decimal("0.638"),
        dominant_ask=Decimal("0.638"),
        active_ask_wall_notional_sum=Decimal("9000"),
        bid_ask_notional_ratio=0.9,
        trade_delta=Decimal("-300"),
        oi_change=Decimal("-20"),
        lower_high_confirmed=True,
        support_break_confirmed=False,
        local_support=Decimal("0.625"),
        expire_deadline=TS0 + timedelta(seconds=600),
    )
    # Mid breaks under local support 0.625
    snaps = [
        _snap(TS0, "0.630", nearest_bid="0.625"),
        _snap(TS0 + timedelta(seconds=30), "0.622", nearest_bid="0.620", delta="-800"),
    ]
    feat = compute_features_at(
        index=1,
        snapshots=snaps,
        transitions=[],
        params=params,
        prior_local_high=Decimal("0.635"),
    )
    assert feat.support_break_confirmed
    machine = MachineState(state=REVERSAL_WARNING, active_warning=warning, down_streak=2)
    machine, _, timeline = advance_state(
        machine=machine, feat=feat, params=params, warning_seq=1
    )
    assert timeline["state"] == REVERSAL_CONFIRMED
    assert warning.terminal_state == REVERSAL_CONFIRMED


def test_no_same_snapshot_lookahead_in_outcomes() -> None:
    warning = WarningEvent(
        warning_id="W0004",
        warning_time=TS0 + timedelta(seconds=60),
        warning_index=2,
        state=REVERSAL_WARNING,
        score=5,
        feature_count=3,
        features_true=["x"],
        mid=Decimal("0.630"),
        local_high=Decimal("0.632"),
        nearest_bid=Decimal("0.624"),
        dominant_bid=Decimal("0.624"),
        dominant_bid_notional=Decimal("8000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("8000"),
        nearest_ask=Decimal("0.636"),
        dominant_ask=Decimal("0.636"),
        active_ask_wall_notional_sum=Decimal("7000"),
        bid_ask_notional_ratio=1.1,
        trade_delta=Decimal("-100"),
        oi_change=None,
        lower_high_confirmed=False,
        support_break_confirmed=False,
        local_support=Decimal("0.624"),
        expire_deadline=TS0 + timedelta(seconds=400),
    )
    # Same-timestamp print must be excluded; only later path counts
    path = [
        (TS0 + timedelta(seconds=60), Decimal("0.600")),  # same ts as warning — ignore
        (TS0 + timedelta(seconds=90), Decimal("0.628")),
    ]
    rows = simulate_warning_outcomes(
        warning=warning,
        price_path=path,
        end=TS0 + timedelta(seconds=300),
        params=BidWeakeningParams(),
    )
    session = next(r for r in rows if r["horizon"] == "session_end")
    # If same-ts 0.600 were used, down would be huge; with strict > warning_time, ~0.317%
    assert session["max_favourable_down_pct"] < 1.0


def test_forward_outcomes_start_after_warning() -> None:
    warning = WarningEvent(
        warning_id="W0005",
        warning_time=TS0 + timedelta(seconds=90),
        warning_index=3,
        state=REVERSAL_WARNING,
        score=4,
        feature_count=3,
        features_true=["x"],
        mid=Decimal("0.630"),
        local_high=Decimal("0.633"),
        nearest_bid=Decimal("0.624"),
        dominant_bid=Decimal("0.624"),
        dominant_bid_notional=Decimal("7000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("7000"),
        nearest_ask=Decimal("0.636"),
        dominant_ask=Decimal("0.636"),
        active_ask_wall_notional_sum=Decimal("8000"),
        bid_ask_notional_ratio=0.9,
        trade_delta=Decimal("-200"),
        oi_change=Decimal("-5"),
        lower_high_confirmed=True,
        support_break_confirmed=False,
        local_support=Decimal("0.624"),
        expire_deadline=TS0 + timedelta(seconds=400),
    )
    path = [
        (TS0 + timedelta(seconds=30), Decimal("0.600")),  # before warning
        (TS0 + timedelta(seconds=60), Decimal("0.600"),),
        (TS0 + timedelta(seconds=120), Decimal("0.627")),
    ]
    rows = simulate_warning_outcomes(
        warning=warning,
        price_path=path,
        end=TS0 + timedelta(seconds=300),
        params=BidWeakeningParams(),
    )
    session = next(r for r in rows if r["horizon"] == "session_end")
    assert session["max_favourable_down_pct"] < 5.0  # pre-warning 0.600 excluded


def test_warning_expires() -> None:
    params = BidWeakeningParams(warning_max_age_seconds=60)
    warning = WarningEvent(
        warning_id="W0006",
        warning_time=TS0,
        warning_index=1,
        state=REVERSAL_WARNING,
        score=4,
        feature_count=3,
        features_true=["x"],
        mid=Decimal("0.630"),
        local_high=Decimal("0.632"),
        nearest_bid=Decimal("0.624"),
        dominant_bid=Decimal("0.624"),
        dominant_bid_notional=Decimal("8000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("8000"),
        nearest_ask=Decimal("0.636"),
        dominant_ask=Decimal("0.636"),
        active_ask_wall_notional_sum=Decimal("7000"),
        bid_ask_notional_ratio=1.0,
        trade_delta=Decimal("-50"),
        oi_change=None,
        lower_high_confirmed=False,
        support_break_confirmed=False,
        local_support=Decimal("0.624"),
        expire_deadline=TS0 + timedelta(seconds=60),
    )
    snaps = [
        _snap(TS0, "0.630"),
        _snap(TS0 + timedelta(seconds=90), "0.6295"),  # age 90 > 60
    ]
    feat = compute_features_at(index=1, snapshots=snaps, transitions=[], params=params)
    machine = MachineState(state=REVERSAL_WARNING, active_warning=warning)
    machine, _, timeline = advance_state(
        machine=machine, feat=feat, params=params, warning_seq=1
    )
    assert timeline["state"] == EXPIRED
    assert warning.terminal_state == EXPIRED


def test_no_future_transitions_in_features() -> None:
    params = BidWeakeningParams()
    snaps = [
        _snap(TS0, "0.630"),
        _snap(TS0 + timedelta(seconds=30), "0.631"),
    ]
    future_tx = _tx(TS0 + timedelta(hours=1), WALL_PULLED)
    feat = compute_features_at(
        index=1, snapshots=snaps, transitions=[future_tx], params=params
    )
    assert feat.bid_wall_pull_count == 0


def test_deterministic_outputs(tmp_path: Path) -> None:
    params = BidWeakeningParams(
        warning_min_feature_count=2,
        warning_confirm_snapshots=2,
        bid_notional_drop_pct=15.0,
        nearest_bid_retreat_bps=3.0,
        cooldown_seconds=0,
        warning_max_age_seconds=600,
        false_warning_min_down_pct=0.10,
    )
    snaps: list[SnapshotRecord] = []
    # Uptrend with rising bids
    for i in range(6):
        mid = Decimal("0.620") + Decimal(i) * Decimal("0.002")
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                nearest_bid=format(mid - Decimal("0.004"), "f"),
                bid_notional="20000",
                bid_count=3,
                delta="400",
            )
        )
    # Weakening after local high
    for i in range(6, 12):
        mid = Decimal("0.630") - Decimal(i - 6) * Decimal("0.001")
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                format(mid, "f"),
                nearest_bid=format(mid - Decimal("0.006"), "f"),
                bid_notional=str(12000 - (i - 6) * 1500),
                bid_count=1,
                delta=str(-200 - (i - 6) * 50),
                oi_chg="-25",
            )
        )
    transitions = [
        _tx(snaps[i].timestamp, WALL_PULLED if i % 2 == 0 else WALL_REPLACED_LOWER)
        for i in range(6, 12)
    ]
    price_path = [(s.timestamp, s.mid_price) for s in snaps]
    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    s1 = run_bid_weakening_audit_from_snapshots(
        snapshots=snaps,
        transitions=transitions,
        sequences=[],
        price_path=price_path,
        params=params,
        end=snaps[-1].timestamp,
        output_dir=out1,
    )
    s2 = run_bid_weakening_audit_from_snapshots(
        snapshots=snaps,
        transitions=transitions,
        sequences=[],
        price_path=price_path,
        params=params,
        end=snaps[-1].timestamp,
        output_dir=out2,
    )
    assert s1["warning_count"] == s2["warning_count"]
    assert s1["confirmed_reversal_count"] == s2["confirmed_reversal_count"]
    assert (out1 / "bid_weakening_features.csv").read_text() == (
        out2 / "bid_weakening_features.csv"
    ).read_text()
    assert (out1 / "bid_weakening_warnings.csv").exists()
    assert (out1 / "bid_weakening_forward_outcomes.csv").exists()
    assert (out1 / "bid_weakening_false_warnings.csv").exists()
    assert (out1 / "bid_weakening_confirmed_reversals.csv").exists()
    assert (out1 / "bid_weakening_threshold_summary.csv").exists()
    assert (out1 / "REPORT.md").exists()
    assert (out1 / "strategy_summary.json").exists()


def test_features_use_only_past_snapshots() -> None:
    params = BidWeakeningParams()
    snaps = [
        _snap(TS0, "0.630", bid_notional="10000"),
        _snap(TS0 + timedelta(seconds=30), "0.631", bid_notional="10000"),
        _snap(TS0 + timedelta(seconds=60), "0.700", bid_notional="50000"),  # future relative to idx1
    ]
    feat = compute_features_at(index=1, snapshots=snaps, transitions=[], params=params)
    assert feat.mid == Decimal("0.631")
    assert feat.local_high is not None
    assert feat.local_high <= Decimal("0.631")
