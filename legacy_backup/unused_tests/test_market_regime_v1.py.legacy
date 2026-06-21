import unittest
from datetime import datetime, timezone

from strategy.market_regime import (
    classify_range_unclear_diagnosis,
    CoinProfile,
    evaluate_entry_decision,
    FastTriggerSnapshot,
    FeatureProfileStats,
    MidRegimeSnapshot,
    PrimitiveEvents,
    RawMarketSnapshot,
    RegimeSnapshot,
    RoutedRegimeSnapshot,
    SlowRegimeSnapshot,
    StateMachineSnapshot,
    apply_routed_state_machine,
    apply_state_machine,
    compute_candidate_regimes,
    compute_fast_trigger,
    compute_mid_state,
    compute_primitive_events,
    compute_slow_regime,
    compute_scores,
    normalize_snapshot,
    route_regime,
)


def make_profile() -> CoinProfile:
    feature_names = [
        "price_change_1m",
        "price_change_5m",
        "price_change_15m",
        "oi_change_ratio",
        "trade_volume_1m",
        "volume_spike_ratio",
        "orderflow_ratio",
        "delta_ratio",
        "microburst_score",
        "liquidation_density_5m",
        "liquidation_cluster_score",
        "spread_ratio",
        "trade_count_1m",
        "avg_trade_size",
    ]
    features = {
        name: FeatureProfileStats(
            mean=0.0,
            std=1.0,
            p50=0.0,
            p75=0.75,
            p90=0.9,
            p95=1.5,
            p99=2.0,
            n95=-1.5 if name.startswith("price_change") else None,
            n99=-2.0 if name.startswith("price_change") else None,
        )
        for name in feature_names
    }
    return CoinProfile(symbol="BTCUSDT", features=features)


class MarketRegimeV1Tests(unittest.TestCase):
    def test_decision_policy_range_unclear_no_signal_confirmed_skips(self) -> None:
        decision = evaluate_entry_decision(
            state="range_unclear",
            confidence=0.4,
            confidence_source="derived_fallback",
            range_unclear_diagnosis="no_signal_confirmed",
        )

        self.assertEqual(decision.decision, "SKIP")
        self.assertEqual(decision.decision_reason, "range_unclear_no_signal_confirmed")
        self.assertFalse(decision.entry_allowed)

    def test_decision_policy_range_unclear_waiting_for_confirmation_skips(self) -> None:
        decision = evaluate_entry_decision(
            state="range_unclear",
            confidence=0.4,
            confidence_source="derived_fallback",
            range_unclear_diagnosis="waiting_for_confirmation",
        )

        self.assertEqual(decision.decision, "SKIP")
        self.assertEqual(decision.decision_reason, "range_unclear_waiting_for_confirmation")
        self.assertFalse(decision.entry_allowed)

    def test_decision_policy_mid_exhaustion_long_allows(self) -> None:
        decision = evaluate_entry_decision(
            state="mid_exhaustion_long",
            confidence=0.85,
            confidence_source="stored",
            range_unclear_diagnosis=None,
        )

        self.assertEqual(decision.decision, "ALLOW")
        self.assertEqual(decision.decision_reason, "allowed_state_mid_exhaustion_long")
        self.assertTrue(decision.entry_allowed)

    def test_decision_policy_pullback_in_long_context_allows(self) -> None:
        decision = evaluate_entry_decision(
            state="pullback_in_long_context",
            confidence=0.75,
            confidence_source="stored",
            range_unclear_diagnosis=None,
        )

        self.assertEqual(decision.decision, "ALLOW")
        self.assertEqual(decision.decision_reason, "allowed_state_pullback_in_long_context")
        self.assertTrue(decision.entry_allowed)

    def test_decision_policy_trend_continuation_long_watchlists(self) -> None:
        decision = evaluate_entry_decision(
            state="trend_continuation_long",
            confidence=0.75,
            confidence_source="stored",
            range_unclear_diagnosis=None,
        )

        self.assertEqual(decision.decision, "WATCHLIST")
        self.assertEqual(decision.decision_reason, "state_not_whitelisted")
        self.assertFalse(decision.entry_allowed)

    def test_decision_policy_confidence_does_not_change_result(self) -> None:
        low_conf = evaluate_entry_decision(
            state="mid_exhaustion_long",
            confidence=0.1,
            confidence_source="stored",
            range_unclear_diagnosis=None,
        )
        high_conf = evaluate_entry_decision(
            state="mid_exhaustion_long",
            confidence=0.95,
            confidence_source="stored",
            range_unclear_diagnosis=None,
        )

        self.assertEqual(low_conf.decision, "ALLOW")
        self.assertEqual(high_conf.decision, "ALLOW")

    def test_range_unclear_diagnosis_waiting_for_confirmation(self) -> None:
        diagnosis = classify_range_unclear_diagnosis(
            transition_reason=["awaiting_confirmation:trend_continuation_long:1/2"],
            routed_transition_reason=["true_range_or_unclear_context"],
        )

        self.assertEqual(diagnosis, "waiting_for_confirmation")

    def test_fast_trigger_event_first_breakout_and_instability_adjust_scores(self) -> None:
        profile = make_profile()
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=1.2,
            price_change_5m=1.4,
            price_change_15m=1.6,
            oi_change=8.0,
            oi_change_ratio=0.9,
            trade_volume_1m=6.0,
            volume_spike_ratio=2.0,
            buy_volume=90.0,
            sell_volume=10.0,
            delta=80.0,
            orderflow_ratio=1.1,
            trade_count_1m=6.0,
            avg_trade_size=2.0,
            atr_1m=0.6,
            spread=0.03,
        )
        snapshot = normalize_snapshot(current, None, profile)

        clean_events = PrimitiveEvents(
            fresh_long_build_up=True,
            high_participation_breakout=True,
            volatility_expansion=True,
        )
        dirty_events = PrimitiveEvents(
            fresh_long_build_up=True,
            high_participation_breakout=True,
            volatility_expansion=True,
            thin_orderflow_instability=True,
            spread_stress_phase=True,
            dirty_breakout_risk=True,
        )

        clean_fast = compute_fast_trigger(snapshot, clean_events, "slow_trend_long")
        dirty_fast = compute_fast_trigger(snapshot, dirty_events, "slow_trend_long")

        self.assertGreater(clean_fast.pressure_score_fast, 0.0)
        self.assertGreater(clean_fast.participation_score_fast, 0.0)
        self.assertGreater(dirty_fast.instability_score_fast, clean_fast.instability_score_fast)
        self.assertGreater(dirty_fast.exhaustion_score_fast, clean_fast.exhaustion_score_fast)

    def test_zero_trade_snapshot_treats_avg_trade_and_microburst_as_no_signal(self) -> None:
        profile = make_profile()
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=-0.4,
            oi_change=2.0,
            oi_change_ratio=0.1,
            trade_volume_1m=0.0,
            volume_spike_ratio=0.0,
            buy_volume=0.0,
            sell_volume=0.0,
            delta=0.0,
            orderflow_ratio=0.0,
            trade_count_1m=0.0,
            avg_trade_size=None,
            microburst_score=45.0,
            atr_1m=None,
            spread=0.01,
        )

        normalized = normalize_snapshot(current, None, profile)
        events = compute_primitive_events(normalized, None, profile)

        self.assertIn("avg_trade_size", normalized.missing_inputs)
        self.assertIn("microburst_score", normalized.missing_inputs)
        self.assertEqual(normalized.z("avg_trade_size"), 0.0)
        self.assertEqual(normalized.z("microburst_score"), 0.0)
        self.assertFalse(events.large_trade_presence)
        self.assertFalse(events.microburst_risk)

    def test_oi_price_state_all_quadrants(self) -> None:
        profile = make_profile()
        cases = [
            ("price_up_oi_up", 1.0, 10.0, "oi_price_build_long"),
            ("price_up_oi_down", 1.0, -10.0, "oi_price_short_covering"),
            ("price_down_oi_up", -1.0, 10.0, "oi_price_build_short"),
            ("price_down_oi_down", -1.0, -10.0, "oi_price_long_flush"),
        ]

        for expected_state, price_change_1m, oi_change, expected_event in cases:
            with self.subTest(expected_state=expected_state):
                current = RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=100.0,
                    price_change_1m=price_change_1m,
                    oi_change=oi_change,
                    oi_change_ratio=0.5 if oi_change > 0 else -0.5,
                    trade_volume_1m=1.0,
                    volume_spike_ratio=1.0,
                    buy_volume=60.0,
                    sell_volume=40.0,
                    delta=20.0,
                    orderflow_ratio=0.5,
                    trade_count_1m=1.0,
                    avg_trade_size=1.0,
                    spread=0.01,
                )
                normalized = normalize_snapshot(current, None, profile)
                events = compute_primitive_events(normalized, None, profile)

                self.assertEqual(normalized.label("oi_price_state"), expected_state)
                self.assertTrue(getattr(events, expected_event))

    def test_fast_trigger_oi_price_state_strengthens_and_downgrades_pressure(self) -> None:
        profile = make_profile()
        previous = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=0.2,
            oi_change=1.0,
            oi_change_ratio=0.2,
            trade_volume_1m=1.0,
            volume_spike_ratio=1.0,
            buy_volume=55.0,
            sell_volume=45.0,
            delta=10.0,
            orderflow_ratio=0.2,
            trade_count_1m=1.0,
            avg_trade_size=1.0,
            spread=0.01,
        )
        bullish_build = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=101.0,
            price_change_1m=1.8,
            oi_change=12.0,
            oi_change_ratio=1.0,
            trade_volume_1m=2.0,
            volume_spike_ratio=1.8,
            buy_volume=80.0,
            sell_volume=20.0,
            delta=60.0,
            orderflow_ratio=1.2,
            trade_count_1m=2.0,
            avg_trade_size=1.2,
            spread=0.02,
        )
        short_covering = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=101.0,
            price_change_1m=1.8,
            oi_change=-12.0,
            oi_change_ratio=-1.0,
            trade_volume_1m=2.0,
            volume_spike_ratio=1.8,
            buy_volume=80.0,
            sell_volume=20.0,
            delta=60.0,
            orderflow_ratio=1.2,
            trade_count_1m=2.0,
            avg_trade_size=1.2,
            spread=0.02,
        )

        bullish_build_norm = normalize_snapshot(bullish_build, previous, profile)
        bullish_build_events = compute_primitive_events(bullish_build_norm, normalize_snapshot(previous, None, profile), profile)
        bullish_build_fast = compute_fast_trigger(bullish_build_norm, bullish_build_events, "slow_trend_long")

        short_covering_norm = normalize_snapshot(short_covering, previous, profile)
        short_covering_events = compute_primitive_events(short_covering_norm, normalize_snapshot(previous, None, profile), profile)
        short_covering_fast = compute_fast_trigger(short_covering_norm, short_covering_events, "slow_trend_long")

        self.assertEqual(bullish_build_fast.oi_price_state, "price_up_oi_up")
        self.assertEqual(short_covering_fast.oi_price_state, "price_up_oi_down")
        self.assertGreater(bullish_build_fast.pressure_score_fast, short_covering_fast.pressure_score_fast)
        self.assertLess(bullish_build_fast.exhaustion_score_fast, short_covering_fast.exhaustion_score_fast)

    def test_slow_regime_oi_price_consistency_confirms_trend_strength(self) -> None:
        profile = make_profile()
        supportive = normalize_snapshot(
            RawMarketSnapshot(
                symbol="BTCUSDT",
                ts=datetime.now(timezone.utc),
                price=100.0,
                price_change_1m=1.0,
                price_change_5m=1.0,
                price_change_15m=1.2,
                oi_change=8.0,
                oi_change_ratio=0.8,
                trade_volume_1m=1.5,
                volume_spike_ratio=1.4,
                buy_volume=70.0,
                sell_volume=30.0,
                delta=40.0,
                orderflow_ratio=0.8,
                trade_count_1m=1.4,
                spread=0.01,
            ),
            None,
            profile,
        )
        fading = normalize_snapshot(
            RawMarketSnapshot(
                symbol="BTCUSDT",
                ts=datetime.now(timezone.utc),
                price=100.0,
                price_change_1m=1.0,
                price_change_5m=1.0,
                price_change_15m=1.2,
                oi_change=-8.0,
                oi_change_ratio=-0.8,
                trade_volume_1m=1.5,
                volume_spike_ratio=1.4,
                buy_volume=70.0,
                sell_volume=30.0,
                delta=40.0,
                orderflow_ratio=0.8,
                trade_count_1m=1.4,
                spread=0.01,
            ),
            None,
            profile,
        )

        supportive_slow = compute_slow_regime(supportive)
        fading_slow = compute_slow_regime(fading)

        self.assertGreater(supportive_slow.pressure_score_slow, fading_slow.pressure_score_slow)
        self.assertLess(supportive_slow.exhaustion_score_slow, fading_slow.exhaustion_score_slow)

    def test_mid_oi_price_state_distinguishes_reversal_from_exhaustion(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_transition_long_to_neutral",
            pressure_score_slow=12.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=25.0,
            oi_price_state="price_down_oi_up",
            state_memory="slow_trend_long",
            transition_counter=2,
            bias=1,
        )
        reversal_fast = FastTriggerSnapshot(
            state="fast_reversal_attempt_short",
            pressure_score_fast=-11.0,
            participation_score_fast=18.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=35.0,
            oi_price_state="price_down_oi_up",
        )
        exhaustion_fast = FastTriggerSnapshot(
            state="fast_reversal_attempt_short",
            pressure_score_fast=-11.0,
            participation_score_fast=18.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=35.0,
            oi_price_state="price_down_oi_down",
        )

        reversal_mid = compute_mid_state(reversal_fast, slow)
        exhaustion_mid = compute_mid_state(exhaustion_fast, slow)

        self.assertEqual(reversal_mid.state, "mid_reversal_setup_short")
        self.assertEqual(exhaustion_mid.state, "mid_exhaustion_long")
        self.assertEqual(reversal_mid.debug["oi_price_state"], "price_down_oi_up")
        self.assertEqual(exhaustion_mid.debug["oi_price_state"], "price_down_oi_down")

    def test_mid_state_uses_event_context_for_reversal_detection(self) -> None:
        profile = make_profile()
        snapshot = normalize_snapshot(
            RawMarketSnapshot(
                symbol="BTCUSDT",
                ts=datetime.now(timezone.utc),
                price=100.0,
                price_change_1m=-1.1,
                price_change_5m=-1.4,
                price_change_15m=-1.6,
                oi_change=9.0,
                oi_change_ratio=1.0,
                trade_volume_1m=5.0,
                volume_spike_ratio=1.8,
                buy_volume=20.0,
                sell_volume=80.0,
                delta=-60.0,
                orderflow_ratio=-1.1,
                trade_count_1m=5.0,
                avg_trade_size=1.5,
                atr_1m=0.7,
                spread=0.02,
            ),
            None,
            profile,
        )
        slow = SlowRegimeSnapshot(
            state="slow_transition_long_to_neutral",
            pressure_score_slow=10.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=28.0,
            state_memory="slow_trend_long",
            transition_counter=2,
            bias=1,
        )
        fast = FastTriggerSnapshot(
            state="fast_reversal_attempt_short",
            pressure_score_fast=-12.0,
            participation_score_fast=18.0,
            instability_score_fast=6.0,
            exhaustion_score_fast=40.0,
            oi_price_state="price_down_oi_up",
        )
        events = PrimitiveEvents(
            fresh_short_build_up=True,
            squeeze_exhaustion_reversal=True,
        )

        mid = compute_mid_state(fast, slow, events=events, snapshot=snapshot)

        self.assertEqual(mid.state, "mid_reversal_setup_short")
        self.assertTrue(mid.debug["event_fresh_short_build_up"])
        self.assertTrue(mid.debug["event_squeeze_exhaustion_reversal"])

    def test_slow_regime_memory_prevents_immediate_neutralization(self) -> None:
        profile = make_profile()
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=0.1,
            price_change_5m=0.25,
            price_change_15m=0.3,
            oi_change_ratio=0.15,
            trade_volume_1m=0.6,
            volume_spike_ratio=0.7,
            orderflow_ratio=0.1,
            trade_count_1m=0.4,
        )
        normalized = normalize_snapshot(current, None, profile)
        previous_state = StateMachineSnapshot(
            previous_state="trend_continuation_long",
            current_state="trend_continuation_long",
            slow_state_memory="slow_trend_long",
            slow_transition_counter=0,
            slow_bias=1,
        )

        slow = compute_slow_regime(normalized, previous_state=previous_state)

        self.assertEqual(slow.state_memory, "slow_trend_long")
        self.assertEqual(slow.bias, 1)

    def test_slow_regime_uses_event_based_conviction_and_exhaustion_context(self) -> None:
        profile = make_profile()
        snapshot = normalize_snapshot(
            RawMarketSnapshot(
                symbol="BTCUSDT",
                ts=datetime.now(timezone.utc),
                price=100.0,
                price_change_1m=0.7,
                price_change_5m=1.0,
                price_change_15m=1.3,
                oi_change=8.0,
                oi_change_ratio=0.8,
                trade_volume_1m=4.0,
                volume_spike_ratio=1.4,
                buy_volume=75.0,
                sell_volume=25.0,
                delta=50.0,
                orderflow_ratio=0.9,
                trade_count_1m=4.0,
                avg_trade_size=1.0,
                atr_1m=0.5,
                spread=0.01,
            ),
            None,
            profile,
        )
        conviction_events = PrimitiveEvents(fresh_long_build_up=True)
        stressed_events = PrimitiveEvents(
            fresh_long_build_up=True,
            spread_stress_phase=True,
            panic_liquidation_phase=True,
            squeeze_exhaustion_reversal=True,
        )

        conviction_slow = compute_slow_regime(snapshot, events=conviction_events)
        stressed_slow = compute_slow_regime(snapshot, events=stressed_events)

        self.assertGreater(conviction_slow.pressure_score_slow, 0.0)
        self.assertGreater(stressed_slow.exhaustion_score_slow, conviction_slow.exhaustion_score_slow)

    def test_mid_state_and_router_preserve_long_context(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=12.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=15.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-9.0,
            participation_score_fast=12.0,
            instability_score_fast=5.0,
            exhaustion_score_fast=30.0,
        )

        mid = compute_mid_state(fast, slow)
        routed = route_regime(slow, mid, fast)

        self.assertEqual(mid.state, "mid_exhaustion_long")
        self.assertEqual(routed.routed_state, "mid_exhaustion_long")
        self.assertEqual(mid.debug["matched_rule_name"], "mid_exhaustion_long")

    def test_mid_pullback_vs_exhaustion_boundary(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=30.0,
            participation_score_slow=35.0,
            exhaustion_score_slow=10.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        pullback_fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-4.0,
            participation_score_fast=12.0,
            instability_score_fast=5.0,
            exhaustion_score_fast=5.0,
        )
        exhaustion_fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-7.0,
            participation_score_fast=12.0,
            instability_score_fast=5.0,
            exhaustion_score_fast=12.0,
        )

        pullback_mid = compute_mid_state(pullback_fast, slow)
        exhaustion_mid = compute_mid_state(exhaustion_fast, slow)

        self.assertEqual(pullback_mid.state, "mid_pullback_in_long")
        self.assertEqual(exhaustion_mid.state, "mid_exhaustion_long")

    def test_mid_exhaustion_vs_reversal_boundary(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_transition_long_to_neutral",
            pressure_score_slow=12.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=25.0,
            state_memory="slow_trend_long",
            transition_counter=2,
            bias=1,
        )
        exhaustion_fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-9.0,
            participation_score_fast=12.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=25.0,
        )
        reversal_fast = FastTriggerSnapshot(
            state="fast_reversal_attempt_short",
            pressure_score_fast=-11.0,
            participation_score_fast=18.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=35.0,
            oi_price_state="price_down_oi_up",
        )

        exhaustion_mid = compute_mid_state(exhaustion_fast, slow)
        reversal_mid = compute_mid_state(reversal_fast, slow)

        self.assertEqual(exhaustion_mid.state, "mid_exhaustion_long")
        self.assertEqual(reversal_mid.state, "mid_reversal_setup_short")
        self.assertEqual(reversal_mid.debug["matched_rule_name"], "mid_reversal_setup_short")

    def test_mid_pressure_minus_seven_is_exhaustion(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=18.0,
            participation_score_slow=24.0,
            exhaustion_score_slow=18.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-7.0,
            participation_score_fast=10.0,
            instability_score_fast=3.0,
            exhaustion_score_fast=12.0,
        )

        mid = compute_mid_state(fast, slow)

        self.assertEqual(mid.state, "mid_exhaustion_long")
        self.assertIn("classified_as_exhaustion_due_to_pressure", mid.transition_reason)

    def test_mid_pressure_minus_nine_is_still_exhaustion(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_transition_long_to_neutral",
            pressure_score_slow=15.0,
            participation_score_slow=18.0,
            exhaustion_score_slow=25.0,
            state_memory="slow_trend_long",
            transition_counter=1,
            bias=1,
        )
        fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-9.0,
            participation_score_fast=14.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=25.0,
        )

        mid = compute_mid_state(fast, slow)

        self.assertEqual(mid.state, "mid_exhaustion_long")
        self.assertIn("classified_as_exhaustion_due_to_pressure", mid.transition_reason)

    def test_mid_pressure_minus_eleven_with_high_exhaustion_is_reversal(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_transition_long_to_neutral",
            pressure_score_slow=12.0,
            participation_score_slow=22.0,
            exhaustion_score_slow=28.0,
            state_memory="slow_trend_long",
            transition_counter=2,
            bias=1,
        )
        fast = FastTriggerSnapshot(
            state="fast_reversal_attempt_short",
            pressure_score_fast=-11.0,
            participation_score_fast=18.0,
            instability_score_fast=4.0,
            exhaustion_score_fast=35.0,
            oi_price_state="price_down_oi_up",
        )

        mid = compute_mid_state(fast, slow)

        self.assertEqual(mid.state, "mid_reversal_setup_short")
        self.assertIn("classified_as_reversal_due_to_pressure_and_exhaustion", mid.transition_reason)

    def test_router_long_context_fast_exhaustion_is_ambiguous(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=20.0,
            participation_score_slow=18.0,
            exhaustion_score_slow=10.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        mid = MidRegimeSnapshot(state=None)
        fast = FastTriggerSnapshot(
            state="fast_exhaustion_long",
            pressure_score_fast=-5.0,
            participation_score_fast=4.0,
            instability_score_fast=1.0,
            exhaustion_score_fast=60.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "range_unclear")
        self.assertTrue(routed.conflict_flags["fast_exhaustion_ambiguous"])

    def test_router_short_context_fast_exhaustion_is_ambiguous(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_short",
            pressure_score_slow=-20.0,
            participation_score_slow=18.0,
            exhaustion_score_slow=10.0,
            state_memory="slow_trend_short",
            transition_counter=0,
            bias=-1,
        )
        mid = MidRegimeSnapshot(state=None)
        fast = FastTriggerSnapshot(
            state="fast_exhaustion_short",
            pressure_score_fast=5.0,
            participation_score_fast=4.0,
            instability_score_fast=1.0,
            exhaustion_score_fast=60.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "range_unclear")
        self.assertTrue(routed.conflict_flags["fast_exhaustion_ambiguous"])

    def test_router_keeps_trend_continuation_without_fast_warning(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=24.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=5.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        mid = MidRegimeSnapshot(state=None)
        fast = FastTriggerSnapshot(
            state="fast_neutral",
            pressure_score_fast=4.0,
            participation_score_fast=3.0,
            instability_score_fast=0.5,
            exhaustion_score_fast=5.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "trend_continuation_long")
        self.assertGreater(routed.confidence, 0.0)

    def test_router_long_fast_exhaustion_becomes_meta_ambiguous(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=22.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=8.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        mid = MidRegimeSnapshot(state=None)
        fast = FastTriggerSnapshot(
            state="fast_exhaustion_long",
            pressure_score_fast=6.0,
            participation_score_fast=6.0,
            instability_score_fast=1.0,
            exhaustion_score_fast=55.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "range_unclear")
        self.assertTrue(routed.conflict_flags["fast_exhaustion_ambiguous"])
        self.assertTrue(routed.instability_flags["fast_exhaustion_ambiguous"])

    def test_router_short_fast_exhaustion_becomes_meta_ambiguous(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_short",
            pressure_score_slow=-22.0,
            participation_score_slow=20.0,
            exhaustion_score_slow=8.0,
            state_memory="slow_trend_short",
            transition_counter=0,
            bias=-1,
        )
        mid = MidRegimeSnapshot(state=None)
        fast = FastTriggerSnapshot(
            state="fast_exhaustion_short",
            pressure_score_fast=-6.0,
            participation_score_fast=6.0,
            instability_score_fast=1.0,
            exhaustion_score_fast=55.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "range_unclear")
        self.assertTrue(routed.conflict_flags["fast_exhaustion_ambiguous"])
        self.assertTrue(routed.instability_flags["fast_exhaustion_ambiguous"])

    def test_router_mid_priority_preserves_meta_only_contract(self) -> None:
        slow = SlowRegimeSnapshot(
            state="slow_trend_long",
            pressure_score_slow=20.0,
            participation_score_slow=18.0,
            exhaustion_score_slow=10.0,
            state_memory="slow_trend_long",
            transition_counter=0,
            bias=1,
        )
        mid = MidRegimeSnapshot(state="mid_reversal_setup_short", transition_reason=["mid_detected:mid_reversal_setup_short"])
        fast = FastTriggerSnapshot(
            state="fast_impulse_short",
            pressure_score_fast=-20.0,
            participation_score_fast=40.0,
            instability_score_fast=10.0,
            exhaustion_score_fast=35.0,
        )

        routed = route_regime(slow, mid, fast)

        self.assertEqual(routed.routed_state, "mid_reversal_setup_short")
        self.assertGreaterEqual(routed.confidence, 0.85)

    def test_strong_trend_short_can_survive_high_exhaustion(self) -> None:
        events = compute_primitive_events(
            normalize_snapshot(
                RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=99.0,
                    price_change_1m=-1.8,
                    price_change_5m=-1.7,
                    oi_change_ratio=-1.8,
                    trade_volume_1m=2.0,
                    volume_spike_ratio=1.9,
                    buy_volume=10.0,
                    sell_volume=90.0,
                    delta=-80.0,
                    orderflow_ratio=-1.8,
                    trade_count_1m=2.0,
                    avg_trade_size=1.2,
                    spread=0.02,
                ),
                RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=100.0,
                    price_change_1m=-2.2,
                    price_change_5m=-2.0,
                    oi_change_ratio=0.8,
                    trade_volume_1m=0.8,
                    volume_spike_ratio=0.6,
                    buy_volume=20.0,
                    sell_volume=80.0,
                    delta=-60.0,
                    orderflow_ratio=-1.2,
                    trade_count_1m=0.8,
                    avg_trade_size=0.6,
                    spread=0.01,
                ),
                make_profile(),
            ),
            normalize_snapshot(
                RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=100.0,
                    price_change_1m=-2.2,
                    price_change_5m=-2.0,
                    oi_change_ratio=0.8,
                    trade_volume_1m=0.8,
                    volume_spike_ratio=0.6,
                    buy_volume=20.0,
                    sell_volume=80.0,
                    delta=-60.0,
                    orderflow_ratio=-1.2,
                    trade_count_1m=0.8,
                    avg_trade_size=0.6,
                    spread=0.01,
                ),
                None,
                make_profile(),
            ),
            make_profile(),
        )
        scores = compute_scores(
            normalize_snapshot(
                RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=99.0,
                    price_change_1m=-1.8,
                    price_change_5m=-1.7,
                    oi_change_ratio=-1.8,
                    trade_volume_1m=2.0,
                    volume_spike_ratio=1.9,
                    buy_volume=10.0,
                    sell_volume=90.0,
                    delta=-80.0,
                    orderflow_ratio=-1.8,
                    trade_count_1m=2.0,
                    avg_trade_size=1.2,
                    spread=0.02,
                ),
                RawMarketSnapshot(
                    symbol="BTCUSDT",
                    ts=datetime.now(timezone.utc),
                    price=100.0,
                    price_change_1m=-2.2,
                    price_change_5m=-2.0,
                    oi_change_ratio=0.8,
                    trade_volume_1m=0.8,
                    volume_spike_ratio=0.6,
                    buy_volume=20.0,
                    sell_volume=80.0,
                    delta=-60.0,
                    orderflow_ratio=-1.2,
                    trade_count_1m=0.8,
                    avg_trade_size=0.6,
                    spread=0.01,
                ),
                make_profile(),
            ),
            events,
        )
        regime = compute_candidate_regimes(events, scores, previous_state="neutral")

        self.assertGreaterEqual(scores.exhaustion_score, 50.0)
        self.assertIn("trend_short", regime.candidate_states)
        self.assertIn("strong_trend_short_override", regime.transition_reason)

    def test_rebound_start_long_candidate_detected(self) -> None:
        profile = make_profile()
        previous = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=-1.2,
            price_change_5m=-1.6,
            oi_change_ratio=0.8,
            trade_volume_1m=0.2,
            volume_spike_ratio=0.4,
            buy_volume=10.0,
            sell_volume=40.0,
            delta=-30.0,
            orderflow_ratio=-1.0,
            trade_count_1m=0.3,
            spread=0.01,
        )
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=101.0,
            price_change_1m=0.9,
            price_change_5m=0.3,
            oi_change_ratio=-2.0,
            trade_volume_1m=2.0,
            volume_spike_ratio=2.5,
            buy_volume=90.0,
            sell_volume=10.0,
            delta=80.0,
            orderflow_ratio=2.0,
            trade_count_1m=2.5,
            spread=0.02,
        )

        prev_norm = normalize_snapshot(previous, None, profile)
        current_norm = normalize_snapshot(current, previous, profile)
        events = compute_primitive_events(current_norm, prev_norm, profile)
        scores = compute_scores(current_norm, events)
        regime = compute_candidate_regimes(events, scores, previous_state="trend_exhaustion_short")

        self.assertTrue(events.price_flip_long)
        self.assertTrue(events.orderflow_flip_long)
        self.assertTrue(events.volume_participation_high)
        self.assertTrue(events.oi_flush)
        self.assertIn("rebound_start_long", regime.candidate_states)

    def test_state_machine_blocks_direct_trend_flip(self) -> None:
        regime = RegimeSnapshot(
            candidate_states=["trend_long"],
            candidate_flags={"trend_long": True},
            active_state="trend_short",
        )
        snapshot = apply_state_machine(
            previous_state="trend_short",
            regime_snapshot=regime,
            confirmation_counters={"trend_long": 3},
            cooldown_remaining_fast_updates=0,
        )
        self.assertEqual(snapshot.current_state, "trend_continuation_short")

    def test_emergency_override_is_immediate(self) -> None:
        regime = RegimeSnapshot(
            candidate_states=["emergency"],
            candidate_flags={"emergency": True},
            active_state="trend_long",
            emergency_trigger=True,
            transition_reason=["microburst_extreme"],
        )

        snapshot = apply_state_machine("trend_long", regime)

        self.assertEqual(snapshot.current_state, "emergency")
        self.assertTrue(snapshot.transition_applied)

    def test_same_ts_guard_does_not_double_count_confirmation(self) -> None:
        previous = StateMachineSnapshot(
            previous_state="trend_continuation_long",
            current_state="trend_continuation_long",
            routed_state="trend_continuation_long",
            confirmation_counters={"mid_reversal_setup_short": 1},
            current_ts=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state="mid_reversal_setup_short",
            fast_state="fast_impulse_short",
            routed_state="mid_reversal_setup_short",
            transition_reason=["slow_long_but_fast_short_pressure"],
        )

        snapshot = apply_routed_state_machine(
            previous,
            routed,
            current_ts=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot.current_state, "mid_reversal_setup_short")
        self.assertEqual(snapshot.confirmation_counters["mid_reversal_setup_short"], 1)
        self.assertIn("slow_long_but_fast_short_pressure", snapshot.transition_reason)
        self.assertIn("mid_signal_preserved:1/2", snapshot.transition_reason)

    def test_mid_state_is_never_downgraded_to_range_unclear(self) -> None:
        previous = StateMachineSnapshot(
            previous_state="range_unclear",
            current_state="range_unclear",
            routed_state="range_unclear",
            confirmation_counters={},
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state="mid_reversal_setup_short",
            fast_state="fast_impulse_short",
            routed_state="mid_reversal_setup_short",
            transition_reason=["mid_detected:mid_reversal_setup_short"],
        )

        snapshot = apply_routed_state_machine(previous, routed, current_ts=datetime.now(timezone.utc))

        self.assertEqual(snapshot.current_state, "mid_reversal_setup_short")
        self.assertNotEqual(snapshot.current_state, "range_unclear")

    def test_persisted_mid_exhaustion_long_can_relax_to_pullback(self) -> None:
        previous = StateMachineSnapshot(
            previous_state="mid_exhaustion_long",
            current_state="mid_exhaustion_long",
            routed_state="mid_exhaustion_long",
            confirmation_counters={"mid_exhaustion_long": 1},
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state=None,
            fast_state="fast_pullback_short_in_long",
            routed_state="pullback_in_long_context",
            transition_reason=["slow_long_context_fast_negative"],
        )

        snapshot = apply_routed_state_machine(previous, routed, current_ts=datetime.now(timezone.utc))

        self.assertEqual(snapshot.current_state, "pullback_in_long_context")

    def test_persisted_pullback_in_long_can_relax_to_trend_continuation(self) -> None:
        previous = StateMachineSnapshot(
            previous_state="pullback_in_long_context",
            current_state="pullback_in_long_context",
            routed_state="pullback_in_long_context",
            confirmation_counters={"pullback_in_long_context": 1},
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state=None,
            fast_state="fast_neutral",
            routed_state="trend_continuation_long",
            transition_reason=["slow_long_context_default"],
        )

        snapshot = apply_routed_state_machine(previous, routed, current_ts=datetime.now(timezone.utc))

        self.assertEqual(snapshot.current_state, "trend_continuation_long")

    def test_persisted_mid_reversal_setup_short_can_relax_to_mid_exhaustion(self) -> None:
        previous = StateMachineSnapshot(
            previous_state="mid_reversal_setup_short",
            current_state="mid_reversal_setup_short",
            routed_state="mid_reversal_setup_short",
            confirmation_counters={"mid_reversal_setup_short": 2},
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state="mid_exhaustion_long",
            fast_state="fast_impulse_short",
            routed_state="mid_exhaustion_long",
            transition_reason=["mid_detected:mid_exhaustion_long"],
        )

        snapshot = apply_routed_state_machine(previous, routed, current_ts=datetime.now(timezone.utc))

        self.assertEqual(snapshot.current_state, "mid_exhaustion_long")


if __name__ == "__main__":
    unittest.main()
