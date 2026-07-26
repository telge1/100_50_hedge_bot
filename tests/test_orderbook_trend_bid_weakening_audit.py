"""Unit tests for integrated trend + bid-weakening audit."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orderbook_analyse.orderbook_trend_bid_weakening_audit import (
    BEARISH,
    FULL_EXIT_OR_SHORT_CONFIRMATION,
    HEDGE_PREPARE,
    LONG_EXIT_WARNING,
    STOP_LONG_ADDS,
    STRONG_WARNING,
    TREND_CONTEXT_UNAVAILABLE,
    VERY_STRONG_WARNING,
    WEAK_WARNING,
    FeatureRow,
    IntegratedParams,
    RegimeRow,
    WarningRow,
    build_episodes,
    classify_warning_quality,
    evaluate_support_break,
    join_regime_as_of,
    map_action,
    run_integrated_audit,
    select_support_level,
    simulate_outcomes,
    variant_passes,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _feat(ts: datetime, mid: str, **kwargs) -> FeatureRow:
    return FeatureRow(
        timestamp=ts,
        index=kwargs.get("index", 0),
        mid=Decimal(mid),
        nearest_bid=Decimal(kwargs.get("nearest_bid", "0.620")),
        dominant_bid=Decimal(kwargs.get("dominant_bid", "0.618")),
        local_high=Decimal(kwargs.get("local_high", mid)),
        local_low=Decimal(kwargs.get("local_low", "0.615")),
        local_support=Decimal(kwargs.get("local_support", "0.620")),
        lower_high_confirmed=kwargs.get("lower_high_confirmed", False),
        nearest_bid_change_bps=kwargs.get("nearest_bid_change_bps"),
        active_bid_wall_notional_change_pct=kwargs.get("notional_chg"),
        bid_wall_shift_higher_count=kwargs.get("shift_higher", 0),
    )


def _warn(
    wid: str,
    ts: datetime,
    *,
    score: int = 8,
    features: str = "bid_notional_drop,trade_delta_negative,nearest_bid_retreat",
    lower_high: bool = True,
    mid: str = "0.630",
    local_support: str = "0.625",
) -> WarningRow:
    return WarningRow(
        warning_id=wid,
        warning_time=ts,
        warning_index=3,
        score=score,
        feature_count=3,
        features_true=[x for x in features.split(",") if x],
        mid=Decimal(mid),
        local_high=Decimal("0.635"),
        nearest_bid=Decimal("0.624"),
        dominant_bid=Decimal("0.624"),
        dominant_bid_notional=Decimal("8000"),
        active_bid_wall_count=1,
        active_bid_wall_notional_sum=Decimal("8000"),
        nearest_ask=Decimal("0.640"),
        dominant_ask=Decimal("0.640"),
        active_ask_wall_notional_sum=Decimal("7000"),
        bid_ask_notional_ratio=1.1,
        trade_delta=Decimal("-200"),
        oi_change=Decimal("-10"),
        lower_high_confirmed=lower_high,
        local_support=Decimal(local_support),
        terminal_state=None,
        terminal_time=None,
    )


def _regime(ts: datetime, combined: str, *, r5: str | None = None, r15: str | None = None, r30: str | None = None) -> RegimeRow:
    return RegimeRow(
        decision_time=ts,
        candle_timestamp=ts - timedelta(minutes=5),
        regime_5m=r5 or combined,
        regime_15m=r15 or combined,
        regime_30m=r30 or combined,
        combined_regime=combined,
        previous_combined_regime=None,
        trend_direction="mixed" if combined == "transition" else "long",
        trend_strength="weak",
        trend_weakness=True,
        transition_detected=combined == "transition",
    )


def test_asof_join_uses_only_past_regime() -> None:
    regimes = [
        _regime(TS0, "bullish_trend"),
        _regime(TS0 + timedelta(minutes=5), "transition"),
        _regime(TS0 + timedelta(minutes=10), "bearish_trend"),
    ]
    j = join_regime_as_of(regimes, as_of=TS0 + timedelta(minutes=7))
    assert j["combined_regime"] == "transition"
    assert j["trend_state_time"] == TS0 + timedelta(minutes=5)
    assert j["trend_state_age_seconds"] == 120
    # future bearish not used
    assert j["combined_regime"] != "bearish_trend"


def test_future_regime_excluded() -> None:
    regimes = [_regime(TS0 + timedelta(hours=1), "bearish_trend")]
    j = join_regime_as_of(regimes, as_of=TS0)
    assert j["trend_data_available"] is False
    assert j["combined_regime"] == TREND_CONTEXT_UNAVAILABLE


def test_missing_trend_explicit() -> None:
    j = join_regime_as_of([], as_of=TS0)
    assert j["trend_join_reason"] == "NO_REGIME_AT_OR_BEFORE_WARNING"
    assert j["combined_regime"] == TREND_CONTEXT_UNAVAILABLE


def test_timeframe_disagreement_flags() -> None:
    regimes = [
        _regime(
            TS0,
            "transition",
            r5="transition",
            r15="bullish_trend_with_trend_weakness",
            r30="bullish_trend",
        )
    ]
    j = join_regime_as_of(regimes, as_of=TS0 + timedelta(seconds=30))
    assert j["disagreement_5m_15m"] is True
    assert j["disagreement_15m_30m"] is True
    assert j["short_term_transition_only"] is True
    assert j["higher_timeframe_bullish_weakness"] is True


def test_support_known_before_break() -> None:
    feats = [
        _feat(TS0, "0.630", nearest_bid="0.625", local_support="0.625"),
        _feat(TS0 + timedelta(seconds=30), "0.629", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=60), "0.622", nearest_bid="0.620"),  # break
        _feat(TS0 + timedelta(seconds=90), "0.621", nearest_bid="0.620"),  # confirm
    ]
    w = _warn("W1", TS0 + timedelta(seconds=30), local_support="0.625")
    sel = select_support_level(w, feats, params=IntegratedParams())
    assert sel["support_identified_time"] < TS0 + timedelta(seconds=60)
    br = evaluate_support_break(
        support_level=sel["support_level"],
        support_source=sel["support_source"],
        support_identified_time=sel["support_identified_time"],
        warning_time=w.warning_time,
        features=feats,
        params=IntegratedParams(support_break_min_depth_bps=5),
    )
    assert br["support_break_valid"] is True
    assert br["support_identified_time"] < br["support_break_start_time"]
    assert br["support_break_confirm_time"] > br["support_break_start_time"]


def test_same_snapshot_break_excluded() -> None:
    """Break cannot confirm on the warning snapshot itself."""
    feats = [
        _feat(TS0, "0.630", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=30), "0.620", nearest_bid="0.625"),  # warning ts deep
    ]
    w = _warn("W1", TS0 + timedelta(seconds=30), local_support="0.625")
    sel = select_support_level(w, feats, params=IntegratedParams())
    br = evaluate_support_break(
        support_level=sel["support_level"],
        support_source=sel["support_source"],
        support_identified_time=sel["support_identified_time"],
        warning_time=w.warning_time,
        features=feats,
        params=IntegratedParams(),
    )
    assert br["support_break_valid"] is False


def test_two_snapshots_required_for_confirm() -> None:
    feats = [
        _feat(TS0, "0.630", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=30), "0.629", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=60), "0.622", nearest_bid="0.620"),  # start only
    ]
    w = _warn("W1", TS0 + timedelta(seconds=30))
    sel = select_support_level(w, feats, params=IntegratedParams())
    br = evaluate_support_break(
        support_level=sel["support_level"],
        support_source=sel["support_source"],
        support_identified_time=sel["support_identified_time"],
        warning_time=w.warning_time,
        features=feats,
        params=IntegratedParams(support_break_confirm_snapshots=2),
    )
    assert br["support_break_valid"] is False
    assert br["support_break_start_time"] is not None


def test_reclaim_invalidates_break() -> None:
    feats = [
        _feat(TS0, "0.630", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=30), "0.629", nearest_bid="0.625"),
        _feat(TS0 + timedelta(seconds=60), "0.622", nearest_bid="0.620"),
        _feat(TS0 + timedelta(seconds=90), "0.626", nearest_bid="0.624"),  # reclaim
    ]
    w = _warn("W1", TS0 + timedelta(seconds=30))
    sel = select_support_level(w, feats, params=IntegratedParams())
    br = evaluate_support_break(
        support_level=sel["support_level"],
        support_source=sel["support_source"],
        support_identified_time=sel["support_identified_time"],
        warning_time=w.warning_time,
        features=feats,
        params=IntegratedParams(),
    )
    assert br["support_break_valid"] is False
    assert br["support_reclaimed"] is True


def test_bullish_weak_warning_is_stop_adds() -> None:
    assert (
        map_action(
            quality=WEAK_WARNING,
            combined_regime="bullish_trend",
            support_break_valid=False,
            trend_available=True,
        )
        == STOP_LONG_ADDS
    )


def test_bullish_strong_warning_exit_warning() -> None:
    assert (
        map_action(
            quality=STRONG_WARNING,
            combined_regime="bullish_trend",
            support_break_valid=False,
            trend_available=True,
        )
        == LONG_EXIT_WARNING
    )


def test_transition_strong_plus_break_hedge() -> None:
    assert (
        map_action(
            quality=STRONG_WARNING,
            combined_regime="transition",
            support_break_valid=True,
            trend_available=True,
        )
        == HEDGE_PREPARE
    )


def test_bearish_strong_plus_break_full_exit() -> None:
    assert (
        map_action(
            quality=STRONG_WARNING,
            combined_regime="bearish_trend",
            support_break_valid=True,
            trend_available=True,
        )
        == FULL_EXIT_OR_SHORT_CONFIRMATION
    )
    assert "bearish_trend" in BEARISH


def test_episode_dedupe_and_one_action() -> None:
    warns = [
        _warn("W1", TS0, score=6),
        _warn("W2", TS0 + timedelta(seconds=60), score=9),
        _warn("W3", TS0 + timedelta(seconds=900), score=8),  # new episode by gap
    ]
    feats = [_feat(TS0 + timedelta(seconds=30 * i), "0.630") for i in range(40)]
    eps = build_episodes(warns, feats, params=IntegratedParams(episode_gap_seconds=300))
    assert len(eps) == 2
    assert len(eps[0]["warnings"]) == 2


def test_outcomes_strictly_after_action_time() -> None:
    action = TS0 + timedelta(seconds=90)
    path = [
        (TS0 + timedelta(seconds=30), Decimal("0.600")),  # before
        (TS0 + timedelta(seconds=120), Decimal("0.628")),
    ]
    rows = simulate_outcomes(
        action_time=action,
        entry_mid=Decimal("0.630"),
        price_path=path,
        end=TS0 + timedelta(seconds=300),
        local_high=Decimal("0.635"),
    )
    sess = next(r for r in rows if r["horizon"] == "session_end")
    assert sess["max_favourable_down_pct"] < 5.0


def test_variant_g0_g5_gates() -> None:
    w = _warn("W1", TS0, score=9, lower_high=True)
    trend = {
        "trend_data_available": True,
        "combined_regime": "transition",
        "regime_5m": "transition",
        "regime_15m": "transition",
        "regime_30m": "bullish_trend_with_trend_weakness",
    }
    params = IntegratedParams()
    assert variant_passes("G0", warning=w, quality=STRONG_WARNING, trend=trend, support_break_valid=False, params=params)
    assert variant_passes("G1", warning=w, quality=STRONG_WARNING, trend=trend, support_break_valid=False, params=params)
    assert not variant_passes("G5", warning=w, quality=STRONG_WARNING, trend=trend, support_break_valid=False, params=params)
    assert variant_passes("G5", warning=w, quality=STRONG_WARNING, trend=trend, support_break_valid=True, params=params)
    assert variant_passes("G6", warning=w, quality=WEAK_WARNING, trend=trend, support_break_valid=True, params=params)


def test_quality_very_strong_needs_break_and_trend() -> None:
    w = _warn("W1", TS0, score=10, lower_high=True)
    q = classify_warning_quality(
        w,
        params=IntegratedParams(),
        support_break_valid=True,
        trend={
            "trend_data_available": True,
            "combined_regime": "transition",
            "all_timeframes_transition": False,
            "short_term_transition_only": False,
            "higher_timeframe_bullish_weakness": False,
        },
    )
    assert q == VERY_STRONG_WARNING


def test_end_to_end_deterministic(tmp_path: Path) -> None:
    # Write minimal CSVs
    warn_path = tmp_path / "bid_weakening_warnings.csv"
    feat_path = tmp_path / "bid_weakening_features.csv"
    reg_path = tmp_path / "regime_snapshots.csv"
    feats = []
    # build path with swing then break
    mids = ["0.628", "0.630", "0.629", "0.631", "0.630", "0.629", "0.622", "0.621", "0.620", "0.619"]
    for i, mid in enumerate(mids):
        ts = TS0 + timedelta(seconds=30 * i)
        feats.append(
            {
                "timestamp": ts.isoformat(),
                "index": i,
                "mid": mid,
                "nearest_bid": "0.625",
                "dominant_bid": "0.624",
                "local_high": "0.631",
                "local_low": "0.619",
                "local_support": "0.625",
                "lower_high_confirmed": "True" if i >= 5 else "False",
                "active_bid_wall_notional_sum": "10000",
                "active_bid_wall_notional_change_pct": "-30",
                "nearest_bid_change_bps": "-8",
                "bid_wall_shift_lower_count": "1",
                "bid_wall_shift_higher_count": "0",
                "trade_delta_60s": "-500",
                "dominant_bid_notional": "8000",
                "dominant_bid_notional_change_pct": "-20",
                "active_bid_wall_count": "1",
                "bid_wall_pull_count": "1",
                "nearest_ask": "0.640",
                "dominant_ask": "0.640",
                "dominant_ask_notional": "7000",
                "active_ask_wall_count": "1",
                "active_ask_wall_notional_sum": "7000",
                "ask_notional_change_pct": "5",
                "bid_ask_notional_ratio": "1.2",
                "trade_delta_30s": "-200",
                "trade_delta_180s": "-800",
                "oi_change_30s": "-10",
                "oi_change_60s": "-20",
                "oi_change_180s": "-30",
                "mid_change_bps_30s": "-5",
                "mid_change_bps_60s": "-10",
                "mid_change_bps_180s": "-15",
                "bars_since_local_high": max(0, i - 3),
                "support_break_confirmed": "False",
            }
        )
    with feat_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(feats[0].keys()))
        w.writeheader()
        w.writerows(feats)

    warn_ts = TS0 + timedelta(seconds=30 * 5)
    with warn_path.open("w", newline="") as fh:
        fields = [
            "warning_id","warning_time","warning_index","state","score","feature_count",
            "features_true","mid","local_high","nearest_bid","dominant_bid","dominant_bid_notional",
            "active_bid_wall_count","active_bid_wall_notional_sum","nearest_ask","dominant_ask",
            "active_ask_wall_notional_sum","bid_ask_notional_ratio","trade_delta","oi_change",
            "lower_high_confirmed","support_break_confirmed","local_support","terminal_state","terminal_time",
        ]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow(
            {
                "warning_id": "W0001",
                "warning_time": warn_ts.isoformat(),
                "warning_index": 5,
                "state": "REVERSAL_WARNING",
                "score": 10,
                "feature_count": 5,
                "features_true": "bid_notional_drop,nearest_bid_retreat,trade_delta_negative,lower_high,bid_wall_shift_lower",
                "mid": "0.629",
                "local_high": "0.631",
                "nearest_bid": "0.625",
                "dominant_bid": "0.624",
                "dominant_bid_notional": "8000",
                "active_bid_wall_count": 1,
                "active_bid_wall_notional_sum": "8000",
                "nearest_ask": "0.640",
                "dominant_ask": "0.640",
                "active_ask_wall_notional_sum": "7000",
                "bid_ask_notional_ratio": "1.1",
                "trade_delta": "-500",
                "oi_change": "-20",
                "lower_high_confirmed": "True",
                "support_break_confirmed": "False",
                "local_support": "0.625",
                "terminal_state": "",
                "terminal_time": "",
            }
        )

    with reg_path.open("w", newline="") as fh:
        fields = [
            "index","decision_time","candle_timestamp","regime_5m","regime_15m","regime_30m",
            "combined_regime","previous_combined_regime","regime_change","trend_direction",
            "trend_strength","trend_weakness","transition_detected","setup_activated","setup_side","setup_type",
        ]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow(
            {
                "index": 1,
                "decision_time": (warn_ts - timedelta(minutes=5)).isoformat(),
                "candle_timestamp": (warn_ts - timedelta(minutes=10)).isoformat(),
                "regime_5m": "transition",
                "regime_15m": "bullish_trend_with_trend_weakness",
                "regime_30m": "bullish_trend_with_trend_weakness",
                "combined_regime": "transition",
                "previous_combined_regime": "bullish_trend_with_trend_weakness",
                "regime_change": "True",
                "trend_direction": "mixed",
                "trend_strength": "weak",
                "trend_weakness": "True",
                "transition_detected": "True",
                "setup_activated": "False",
                "setup_side": "",
                "setup_type": "",
            }
        )

    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    params = IntegratedParams(session_end=(TS0 + timedelta(seconds=30 * 12)).isoformat())
    s1 = run_integrated_audit(
        warnings_path=warn_path,
        features_path=feat_path,
        regimes_path=reg_path,
        output_dir=out1,
        params=params,
    )
    s2 = run_integrated_audit(
        warnings_path=warn_path,
        features_path=feat_path,
        regimes_path=reg_path,
        output_dir=out2,
        params=params,
    )
    assert s1["warning_count"] == s2["warning_count"] == 1
    assert s1["actions_by_variant"] == s2["actions_by_variant"]
    assert (out1 / "integrated_warning_context.csv").read_text() == (
        out2 / "integrated_warning_context.csv"
    ).read_text()
    # reference file exists and marked post-hoc
    ref = (out1 / "integrated_reference_point_audit.csv").read_text()
    assert "POST_HOC_ONLY" in ref
    assert "10:00" in ref or "10:00:00" in ref
