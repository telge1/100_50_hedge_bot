"""Unit tests for absorption / exhaustion audit (synthetic fixtures, no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from orderbook_analyse.orderbook_absorption_features import (
    JOIN_QUALITY_HIGH,
    JOIN_QUALITY_INSUFFICIENT,
    TradeTick,
    WallLevel,
    classify_join_quality,
    compute_orderflow_window,
    estimated_cancel_or_pull,
    estimated_refill_notional,
    join_trades_to_levels,
    match_trade_to_wall,
    normalize_trade_ticks,
    observed_depletion,
    sort_trade_ticks,
    trades_in_window,
    wall_trade_coverage_ratio,
)
from orderbook_analyse.orderbook_absorption_exhaustion_audit import (
    A1,
    A2,
    A4,
    A5,
    AbsorptionParams,
    FailedBreakState,
    REFERENCE_TIMES,
    advance_failed_breakout,
    cluster_episodes,
    detect_a1_buyer_exhaustion,
    detect_a2_ask_absorption,
    detect_a5_migration_stall,
    detect_a6_divergence,
    run_absorption_audit_from_state,
    simulate_mid_outcomes,
    update_swing_tracker,
    variant_match,
)
from orderbook_analyse.orderbook_trend_bid_weakening_audit import (
    RegimeRow,
    join_regime_as_of,
)
from orderbook_analyse.wall_movement_tracker import WallView

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _wall(side: str, price: str, notional: str) -> WallView:
    return WallView(
        side=side,
        price=Decimal(price),
        notional=Decimal(notional),
        wall_multiple=3.0,
        distance_pct=0.2,
        is_wall=True,
    )


def _snap(
    ts: datetime,
    mid: str,
    *,
    nearest_ask: str = "0.640",
    ask_n: str = "5000",
    nearest_bid: str = "0.630",
    bid_n: str = "4000",
) -> SimpleNamespace:
    na = _wall("Ask", nearest_ask, ask_n)
    nb = _wall("Bid", nearest_bid, bid_n)
    return SimpleNamespace(
        timestamp=ts,
        mid_price=Decimal(mid),
        best_bid=Decimal(nearest_bid),
        best_ask=Decimal(nearest_ask),
        nearest_ask=na,
        nearest_bid=nb,
        dominant_ask=na,
        dominant_bid=nb,
        top_ask_walls=[na],
        top_bid_walls=[nb],
        near_asks=[na],
        near_bids=[nb],
        all_ask_buckets={Decimal(nearest_ask): Decimal(ask_n)},
        all_bid_buckets={Decimal(nearest_bid): Decimal(bid_n)},
    )


def _tick(
    ts: datetime,
    side: str,
    price: str,
    notional: float,
    tid: str,
) -> TradeTick:
    return TradeTick(
        trade_ts=ts,
        side=side,
        price=float(price),
        quantity=notional / float(price),
        notional=notional,
        trade_id=tid,
    )


def test_trade_ticks_sorted_deterministically() -> None:
    rows = [
        {
            "trade_ts": TS0 + timedelta(seconds=2),
            "side": "Buy",
            "price": 1.0,
            "quantity": 1.0,
            "notional": 1.0,
            "trade_id": "b",
        },
        {
            "trade_ts": TS0 + timedelta(seconds=1),
            "side": "Sell",
            "price": 1.0,
            "quantity": 1.0,
            "notional": 1.0,
            "trade_id": "a",
        },
        {
            "trade_ts": TS0 + timedelta(seconds=1),
            "side": "Buy",
            "price": 1.0,
            "quantity": 1.0,
            "notional": 1.0,
            "trade_id": "c",
        },
    ]
    ticks, diag = normalize_trade_ticks(rows)
    assert [t.trade_id for t in ticks] == ["a", "c", "b"]
    assert diag.trade_tick_count == 3


def test_window_excludes_future_trades() -> None:
    ticks = [
        _tick(TS0 - timedelta(seconds=5), "Buy", "0.64", 100, "1"),
        _tick(TS0, "Buy", "0.64", 100, "2"),
        _tick(TS0 + timedelta(seconds=1), "Buy", "0.64", 100, "3"),
    ]
    win = trades_in_window(ticks, decision_time=TS0, window_seconds=30)
    assert [t.trade_id for t in win] == ["1", "2"]


def test_buy_matches_ask_only_sell_matches_bid_only() -> None:
    asks = [WallLevel("Ask", 0.640, 5000)]
    bids = [WallLevel("Bid", 0.630, 4000)]
    buy = _tick(TS0, "Buy", "0.640", 1000, "b")
    sell = _tick(TS0, "Sell", "0.630", 1000, "s")
    w_buy, _, _ = match_trade_to_wall(buy, asks + bids, level_join_bps=3)
    w_sell, _, _ = match_trade_to_wall(sell, asks + bids, level_join_bps=3)
    assert w_buy is not None and w_buy.side == "Ask"
    assert w_sell is not None and w_sell.side == "Bid"
    # buy should not match bid
    w_bad, _, _ = match_trade_to_wall(buy, bids, level_join_bps=3)
    assert w_bad is None


def test_one_trade_one_wall_nearest_wins() -> None:
    walls = [
        WallLevel("Ask", 0.640, 1000),
        WallLevel("Ask", 0.641, 9000),
    ]
    tick = _tick(TS0, "Buy", "0.6401", 500, "1")
    w, d, _ = match_trade_to_wall(tick, walls, level_join_bps=20)
    assert w is not None
    assert abs(w.price - 0.640) < 1e-9


def test_outside_join_bps_no_match() -> None:
    walls = [WallLevel("Ask", 0.650, 5000)]
    tick = _tick(TS0, "Buy", "0.640", 500, "1")
    w, _, _ = match_trade_to_wall(tick, walls, level_join_bps=3)
    assert w is None


def test_buy_at_wall_not_equal_total_buy() -> None:
    walls = [WallLevel("Ask", 0.640, 5000)]
    ticks = [
        _tick(TS0 - timedelta(seconds=5), "Buy", "0.640", 2000, "1"),
        _tick(TS0 - timedelta(seconds=4), "Buy", "0.700", 8000, "2"),  # far
    ]
    of = compute_orderflow_window(
        ticks,
        decision_time=TS0,
        window_seconds=30,
        walls=walls,
        nearest_ask=0.640,
        nearest_bid=0.630,
        level_join_bps=3,
        near_level_bps=8,
    )
    assert of["aggressive_buy_total_notional"] == 10000
    assert of["aggressive_buy_at_wall_notional"] == 2000
    assert of["aggressive_buy_at_wall_notional"] < of["aggressive_buy_total_notional"]


def test_pull_proxy_separated_from_coverage() -> None:
    deplete = observed_depletion(5000, 2000)
    matched = 1000.0
    raw, capped = wall_trade_coverage_ratio(matched, deplete)
    cancel = estimated_cancel_or_pull(deplete, matched)
    assert deplete == 3000
    assert abs(raw - 1000 / 3000) < 1e-9
    assert cancel == 2000
    assert cancel != matched


def test_refill_same_vs_near_level() -> None:
    refill = estimated_refill_notional(
        wall_notional_before=5000,
        wall_notional_after=4500,
        aggressive_buy_at_level=2000,
    )
    # expected min = max(5000-2000,0)=3000; refill = max(4500-3000,0)=1500
    assert refill == 1500


def test_low_join_quality_blocks_high_confidence_a2() -> None:
    feat = {
        "w30_aggressive_buy_at_wall_notional": 5000,
        "w30_aggressive_buy_total_notional": 5000,
        "w30_level_join_quality": JOIN_QUALITY_INSUFFICIENT,
        "w30_upside_progress_bps": 1.0,
        "w30_delta_ratio": 0.4,
        "w30_delta_notional": 2000,
        "nearest_ask_notional": 8000,
        "nearest_ask": 0.64,
        "buy_efficiency_bps_per_1k_notional": 0.1,
    }
    params = AbsorptionParams(min_buy_notional=1500, max_progress_bps=8)
    det = detect_a2_ask_absorption(feat, params=params)
    assert det is not None
    assert det["pattern_type"] == "A2_LOW_CONFIDENCE"
    assert det["valid"] is False


def test_a2_valid_with_join_and_low_progress() -> None:
    feat = {
        "w30_aggressive_buy_at_wall_notional": 5000,
        "w30_aggressive_buy_total_notional": 7000,
        "w30_level_join_quality": JOIN_QUALITY_HIGH,
        "w30_upside_progress_bps": 2.0,
        "w30_delta_ratio": 0.3,
        "w30_delta_notional": 3000,
        "nearest_ask_notional": 8000,
        "nearest_ask": 0.64,
        "depletion_ask_wall_notional_after": 7500,
        "buy_efficiency_bps_per_1k_notional": 0.2,
    }
    params = AbsorptionParams(min_buy_notional=1500, max_progress_bps=8)
    det = detect_a2_ask_absorption(feat, params=params)
    assert det is not None
    assert det["pattern_type"] == A2
    assert det["valid"] is True


def test_strong_buy_with_progress_not_absorption() -> None:
    feat = {
        "w30_aggressive_buy_at_wall_notional": 8000,
        "w30_aggressive_buy_total_notional": 9000,
        "w30_level_join_quality": JOIN_QUALITY_HIGH,
        "w30_upside_progress_bps": 40.0,
        "w30_delta_ratio": 0.5,
        "nearest_ask_notional": 8000,
    }
    assert detect_a2_ask_absorption(feat, params=AbsorptionParams()) is None


def test_failed_breakout_causal_confirm() -> None:
    params = AbsorptionParams(failed_break_confirm_snapshots=2, follow_through_min_bps=50)
    st = FailedBreakState()
    # approach with absorption
    f0 = {"timestamp": TS0.isoformat(), "mid": 0.639, "nearest_ask": 0.640, "depletion_absorption_level": 0.640}
    st, sig = advance_failed_breakout(st, f0, params=params, absorption_active=True)
    assert st.state == "LEVEL_APPROACH" and sig is None
    # break
    f1 = {"timestamp": (TS0 + timedelta(seconds=30)).isoformat(), "mid": 0.641, "nearest_ask": 0.640, "depletion_absorption_level": 0.640}
    st, sig = advance_failed_breakout(st, f1, params=params, absorption_active=True)
    assert st.state in {"BREAK_ATTEMPT", "PEAK_RECORDED"}
    # peak
    f2 = {"timestamp": (TS0 + timedelta(seconds=60)).isoformat(), "mid": 0.642, "nearest_ask": 0.640, "depletion_absorption_level": 0.640}
    st, sig = advance_failed_breakout(st, f2, params=params, absorption_active=True)
    # reentry
    f3 = {"timestamp": (TS0 + timedelta(seconds=90)).isoformat(), "mid": 0.639, "nearest_ask": 0.640, "depletion_absorption_level": 0.640}
    st, sig = advance_failed_breakout(st, f3, params=params, absorption_active=True)
    assert st.state == "REENTRY_PENDING"
    assert sig is None  # need 2 under snaps
    f4 = {"timestamp": (TS0 + timedelta(seconds=120)).isoformat(), "mid": 0.638, "nearest_ask": 0.640, "depletion_absorption_level": 0.640}
    st, sig = advance_failed_breakout(st, f4, params=params, absorption_active=True)
    assert sig is not None
    assert sig["pattern_type"] == A4


def test_same_snapshot_break_and_fail_excluded() -> None:
    params = AbsorptionParams(failed_break_confirm_snapshots=2)
    st = FailedBreakState(
        state="BREAK_ATTEMPT",
        level_price=0.64,
        break_time=TS0,
        peak_time=TS0,
        peak_price=0.641,
        setup_start=TS0,
    )
    # same timestamp as break — should reset / not confirm
    f = {"timestamp": TS0.isoformat(), "mid": 0.639, "nearest_ask": 0.64, "depletion_absorption_level": 0.64}
    st2, sig = advance_failed_breakout(st, f, params=params, absorption_active=True)
    assert sig is None


def test_new_breakout_invalidates() -> None:
    params = AbsorptionParams(follow_through_min_bps=5)
    st = FailedBreakState(
        state="PEAK_RECORDED",
        level_price=0.64,
        break_time=TS0,
        peak_time=TS0,
        peak_price=0.6405,
        setup_start=TS0,
    )
    f = {
        "timestamp": (TS0 + timedelta(seconds=30)).isoformat(),
        "mid": 0.6415,
        "nearest_ask": 0.64,
        "depletion_absorption_level": 0.64,
    }
    st2, sig = advance_failed_breakout(st, f, params=params, absorption_active=True)
    assert st2.state == "INVALIDATED"
    assert sig is None


def test_a5_requires_prior_migration_up() -> None:
    params = AbsorptionParams(migration_stall_min_seconds=60)
    hist = [
        {"ask_shift_higher_count_lookback": 0, "ask_shift_lower_count_lookback": 0, "mid": 1.0},
        {"ask_shift_higher_count_lookback": 0, "ask_shift_lower_count_lookback": 0, "mid": 1.0},
    ]
    feat = {
        "ask_shift_higher_count_lookback": 0,
        "ask_shift_lower_count_lookback": 1,
        "nearest_ask": 0.64,
        "mid": 1.0,
    }
    assert detect_a5_migration_stall(feat, hist, params=params) is None
    hist2 = [
        {"ask_shift_higher_count_lookback": 3, "ask_shift_lower_count_lookback": 0, "mid": 1.0},
        {"ask_shift_higher_count_lookback": 0, "ask_shift_lower_count_lookback": 0, "mid": 1.0},
        {"ask_shift_higher_count_lookback": 0, "ask_shift_lower_count_lookback": 0, "mid": 1.0},
        feat,
    ]
    assert detect_a5_migration_stall(feat, hist2, params=params) is not None


def test_a6_needs_two_completed_swings() -> None:
    params = AbsorptionParams(divergence_min_change_pct=10)
    assert detect_a6_divergence({}, [], params=params) is None
    swings = [
        {"high": 1.0, "buy_notional": 10000, "buy_eff": 5.0, "confirm_time": TS0.isoformat()},
        {
            "high": 1.002,
            "buy_notional": 5000,
            "buy_eff": 2.0,
            "confirm_time": (TS0 + timedelta(minutes=5)).isoformat(),
        },
    ]
    det = detect_a6_divergence({}, swings, params=params)
    assert det is not None
    assert det["pattern_type"] == "PRICE_ORDERFLOW_DIVERGENCE" or "PRICE_HIGHER" in str(
        det.get("features_true")
    )


def test_episode_dedupe() -> None:
    params = AbsorptionParams(episode_gap_seconds=300, episode_level_bps=10)
    signals = [
        {
            "signal_id": "S1",
            "pattern_type": A2,
            "signal_time": TS0.isoformat(),
            "action_time": TS0.isoformat(),
            "level": 0.64,
            "score": 3,
        },
        {
            "signal_id": "S2",
            "pattern_type": A2,
            "signal_time": (TS0 + timedelta(seconds=60)).isoformat(),
            "action_time": (TS0 + timedelta(seconds=60)).isoformat(),
            "level": 0.6401,
            "score": 4,
        },
        {
            "signal_id": "S3",
            "pattern_type": A2,
            "signal_time": (TS0 + timedelta(seconds=900)).isoformat(),
            "action_time": (TS0 + timedelta(seconds=900)).isoformat(),
            "level": 0.64,
            "score": 3,
        },
    ]
    eps = cluster_episodes(signals, params=params)
    assert len(eps) == 2


def test_regime_asof_no_future() -> None:
    regimes = [
        RegimeRow(
            decision_time=TS0,
            candle_timestamp=TS0 - timedelta(minutes=5),
            regime_5m="transition",
            regime_15m="transition",
            regime_30m="bullish_trend",
            combined_regime="transition",
            previous_combined_regime=None,
            trend_direction="mixed",
            trend_strength="weak",
            trend_weakness=True,
            transition_detected=True,
        ),
        RegimeRow(
            decision_time=TS0 + timedelta(minutes=5),
            candle_timestamp=TS0,
            regime_5m="bullish_trend",
            regime_15m="bullish_trend",
            regime_30m="bullish_trend",
            combined_regime="bullish_trend",
            previous_combined_regime="transition",
            trend_direction="long",
            trend_strength="normal",
            trend_weakness=False,
            transition_detected=False,
        ),
    ]
    joined = join_regime_as_of(regimes, as_of=TS0 + timedelta(minutes=1))
    assert joined["combined_regime"] == "transition"


def test_outcomes_strictly_after_action() -> None:
    mids = [
        (TS0, 1.0),
        (TS0 + timedelta(seconds=30), 1.0),
        (TS0 + timedelta(seconds=60), 0.995),
        (TS0 + timedelta(seconds=120), 0.990),
    ]
    oc = simulate_mid_outcomes(
        action_time=TS0 + timedelta(seconds=30),
        entry_mid=1.0,
        mids=mids,
    )
    assert oc["hit_down_0_25"] is True
    assert oc["hit_down_0_50"] is True


def test_reference_times_not_in_params() -> None:
    p = AbsorptionParams()
    blob = str(p.__dict__)
    for ref in REFERENCE_TIMES:
        assert ref not in blob


def test_variants_deterministic() -> None:
    assert variant_match("A2", patterns_present={A2}, a2_valid=True, a4_confirmed=False, control_flags={})
    assert not variant_match(
        "A2", patterns_present={A2}, a2_valid=False, a4_confirmed=False, control_flags={}
    )
    assert variant_match(
        "A9", patterns_present={A2, A4}, a2_valid=True, a4_confirmed=True, control_flags={}
    )
    assert variant_match(
        "C1", patterns_present=set(), a2_valid=False, a4_confirmed=False, control_flags={"c1_high_buy": True}
    )


def test_end_to_end_outputs(tmp_path: Path) -> None:
    snaps = []
    ticks = []
    for i in range(12):
        ts = TS0 + timedelta(seconds=30 * i)
        # mid rises then stalls under ask pressure
        mid = 0.635 + i * 0.0003 if i < 6 else 0.6368 - (i - 6) * 0.0002
        ask = 0.640
        ask_n = "8000" if i % 2 == 0 else "7500"
        snaps.append(_snap(ts, f"{mid:.6f}", nearest_ask=f"{ask:.3f}", ask_n=ask_n))
        # buys at ask with little progress later
        ticks.append(_tick(ts - timedelta(seconds=5), "Buy", "0.640", 3000 + i * 100, f"b{i}"))
        ticks.append(_tick(ts - timedelta(seconds=3), "Buy", "0.700", 500, f"x{i}"))  # unmatched

    transitions = [
        SimpleNamespace(
            current_timestamp=TS0 + timedelta(seconds=30),
            side="Ask",
            classification="WALL_REPLACED_HIGHER",
        ),
        SimpleNamespace(
            current_timestamp=TS0 + timedelta(seconds=60),
            side="Ask",
            classification="WALL_REPLACED_HIGHER",
        ),
        SimpleNamespace(
            current_timestamp=TS0 + timedelta(seconds=180),
            side="Ask",
            classification="WALL_REPLACED_LOWER",
        ),
    ]
    out = tmp_path / "abs"
    summary = run_absorption_audit_from_state(
        snapshots=snaps,
        transitions=transitions,
        ticks=sort_trade_ticks(ticks),
        output_dir=out,
        params=AbsorptionParams(min_buy_notional=1000, max_progress_bps=15),
        regimes=[],
        g5_actions=[],
        trade_diag={"trade_tick_count": len(ticks), "duplicate_trade_count": 0, "invalid_trade_count": 0},
        symbol="APTUSDT",
        start=TS0,
        end=TS0 + timedelta(seconds=330),
    )
    assert summary["decision"] in {
        "ABSORPTION_INCREMENTAL_VALUE_FOUND",
        "ABSORPTION_CONFIRMATION_VALUE_ONLY",
        "ABSORPTION_PROXY_QUALITY_INSUFFICIENT",
        "NO_INCREMENTAL_VALUE_VS_G5",
        "AUDIT_INVALID",
    }
    required = [
        "REPORT.md",
        "integrity.json",
        "config.json",
        "trade_loader_diagnostics.json",
        "snapshot_features.csv",
        "pattern_raw_signals.csv",
        "pattern_episodes.csv",
        "pattern_actions.csv",
        "pattern_outcomes.csv",
        "pattern_g5_ablation.csv",
        "pattern_reference_point_audit.csv",
    ]
    for name in required:
        assert (out / name).exists(), name


def test_buyer_exhaustion_vs_absorption_separated() -> None:
    hist = []
    for i in range(5):
        hist.append(
            {
                "timestamp": (TS0 + timedelta(seconds=30 * i)).isoformat(),
                "mid": 1.0 + i * 0.001,
                "w60_aggressive_buy_total_notional": 10000 - i * 2000,
                "w60_delta_ratio": 0.2,
                "w60_delta_notional": 1000,
                "w60_upside_progress_bps": 2.0,
                "buy_efficiency_bps_per_1k_notional": 5.0 - i,
                "bid_shift_higher_count_lookback": 0,
                "bid_shift_lower_count_lookback": 1,
                "nearest_ask": 1.02,
            }
        )
    a1 = detect_a1_buyer_exhaustion(hist[-1], hist, params=AbsorptionParams())
    assert a1 is not None
    assert a1["pattern_type"] == A1
    # absorption needs wall join buy — not triggered by exhaustion alone
    feat_a2 = {
        **hist[-1],
        "w30_aggressive_buy_at_wall_notional": 100,
        "w30_aggressive_buy_total_notional": 2000,
        "w30_level_join_quality": JOIN_QUALITY_HIGH,
        "w30_upside_progress_bps": 2,
        "w30_delta_ratio": 0.2,
        "nearest_ask_notional": 5000,
    }
    assert detect_a2_ask_absorption(feat_a2, params=AbsorptionParams()) is None


def test_swing_tracker_causal() -> None:
    pending = None
    completed: list = []
    feats = [
        {"timestamp": (TS0 + timedelta(seconds=30 * i)).isoformat(), "mid": m, "w60_aggressive_buy_total_notional": 1000, "buy_efficiency_bps_per_1k_notional": 1.0}
        for i, m in enumerate([1.0, 1.02, 1.01, 1.005, 1.03, 1.02, 1.01])
    ]
    for f in feats:
        pending, completed = update_swing_tracker(pending, completed, f, confirm_snapshots=2)
    assert len(completed) >= 1
