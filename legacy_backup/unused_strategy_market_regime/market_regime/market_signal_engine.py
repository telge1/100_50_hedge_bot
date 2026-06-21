from __future__ import annotations

import logging

from .db import MarketRegimeStore
from .decision_policy import classify_range_unclear_diagnosis, evaluate_entry_decision
from .event_engine import compute_primitive_events
from .fast_trigger_engine import compute_fast_trigger
from .feature_normalizer import normalize_snapshot
from .models import MarketSignalResult, RawMarketSnapshot, RegimeSnapshot, ScoreSnapshot, StateMachineSnapshot
from .mid_regime_engine import compute_mid_state
from .regime_router import route_regime
from .slow_regime_engine import compute_slow_regime
from .state_machine import apply_routed_state_machine


class MarketSignalEngine:
    def __init__(
        self,
        store: MarketRegimeStore,
        *,
        engine_version: int = 1,
        logger: logging.Logger | None = None,
        strict_missing_profile: bool = False,
    ) -> None:
        self.store = store
        self.engine_version = engine_version
        self.logger = logger or logging.getLogger("market_regime.engine")
        self.strict_missing_profile = strict_missing_profile

    def process_symbol(
        self,
        symbol: str,
        current_raw: RawMarketSnapshot,
        previous_raw: RawMarketSnapshot | None,
        previous_state: StateMachineSnapshot | None,
        persist: bool = True,
    ) -> MarketSignalResult:
        clean_symbol = symbol.upper()
        profile = self.store.load_coin_profile(clean_symbol)
        if profile is None:
            message = f"Coin profile missing for symbol {clean_symbol}"
            if self.strict_missing_profile:
                raise ValueError(message)
            return MarketSignalResult(
                symbol=clean_symbol,
                ts=current_raw.ts,
                profile_found=False,
                skipped=True,
                skip_reason=message,
                debug={"persist_requested": persist},
            )

        previous_normalized = normalize_snapshot(previous_raw, None, profile) if previous_raw is not None else None
        current_normalized = normalize_snapshot(current_raw, previous_raw, profile)
        events = compute_primitive_events(current_normalized, previous_normalized, profile)
        slow_regime = compute_slow_regime(current_normalized, events=events, previous_state=previous_state)
        fast_trigger = compute_fast_trigger(current_normalized, events, slow_regime.state)
        mid_regime = compute_mid_state(fast_trigger, slow_regime, events=events, snapshot=current_normalized)
        routed_regime = route_regime(slow_regime, mid_regime, fast_trigger)
        state_machine = apply_routed_state_machine(
            previous_state=previous_state,
            routed_regime=routed_regime,
            current_ts=current_raw.ts,
        )
        state_machine.slow_state = slow_regime.state
        state_machine.slow_state_memory = slow_regime.state_memory
        state_machine.slow_transition_counter = slow_regime.transition_counter
        state_machine.slow_bias = slow_regime.bias
        state_machine.mid_state = mid_regime.state
        state_machine.fast_state = fast_trigger.state
        scores = ScoreSnapshot(
            pressure_score=fast_trigger.pressure_score_fast,
            participation_score=fast_trigger.participation_score_fast,
            instability_score=fast_trigger.instability_score_fast,
            exhaustion_score=fast_trigger.exhaustion_score_fast,
            debug={
                "slow": {
                    "pressure": slow_regime.pressure_score_slow,
                    "participation": slow_regime.participation_score_slow,
                    "exhaustion": slow_regime.exhaustion_score_slow,
                    "oi_price_state": slow_regime.oi_price_state,
                    "transition_counter": slow_regime.transition_counter,
                    "bias": slow_regime.bias,
                },
                "fast": {
                    "pressure": fast_trigger.pressure_score_fast,
                    "participation": fast_trigger.participation_score_fast,
                    "instability": fast_trigger.instability_score_fast,
                    "exhaustion": fast_trigger.exhaustion_score_fast,
                    "oi_price_state": fast_trigger.oi_price_state,
                },
                "mid": {
                    "state": mid_regime.state,
                    **mid_regime.debug,
                },
            },
        )
        regime = RegimeSnapshot(
            candidate_states=list(routed_regime.candidate_states),
            candidate_flags=dict(routed_regime.candidate_flags),
            active_state=state_machine.current_state,
            emergency_trigger=routed_regime.emergency_trigger,
            transition_reason=list(routed_regime.transition_reason),
        )
        range_unclear_diagnosis = classify_range_unclear_diagnosis(
            transition_reason=state_machine.transition_reason,
            routed_transition_reason=routed_regime.transition_reason,
        )
        decision_result = evaluate_entry_decision(
            state=state_machine.current_state,
            confidence=routed_regime.confidence,
            confidence_source="stored",
            range_unclear_diagnosis=range_unclear_diagnosis,
        )

        persisted = False
        if persist:
            self.store.insert_market_state_live(
                symbol=clean_symbol,
                ts=current_raw.ts,
                raw_snapshot=current_raw,
                normalized_snapshot=current_normalized,
                events=events,
                scores=scores,
                slow_regime=slow_regime,
                mid_regime=mid_regime,
                fast_trigger=fast_trigger,
                routed_regime=routed_regime,
                regime=regime,
                state_machine=state_machine,
                decision=decision_result,
                engine_version=self.engine_version,
            )
            persisted = True

        result = MarketSignalResult(
            symbol=clean_symbol,
            ts=current_raw.ts,
            profile_found=True,
            skipped=False,
            profile=profile,
            normalized_snapshot=current_normalized,
            previous_normalized_snapshot=previous_normalized,
            events=events,
            scores=scores,
            slow_regime=slow_regime,
            mid_regime=mid_regime,
            fast_trigger=fast_trigger,
            routed_regime=routed_regime,
            regime=regime,
            state_machine=state_machine,
            persisted=persisted,
            decision=decision_result.decision,
            decision_reason=decision_result.decision_reason,
            entry_allowed=decision_result.entry_allowed,
            confidence_source=decision_result.confidence_source,
            range_unclear_diagnosis=decision_result.range_unclear_diagnosis,
            debug={
                "persist_requested": persist,
                "engine_version": self.engine_version,
                "transition_reason": state_machine.transition_reason,
                "candidate_states": routed_regime.candidate_states,
                "oi_price_state": current_normalized.label("oi_price_state", "neutral"),
                "slow_state": slow_regime.state,
                "slow_transition_counter": slow_regime.transition_counter,
                "slow_bias": slow_regime.bias,
                "mid_state": mid_regime.state,
                "mid_debug": dict(mid_regime.debug),
                "fast_state": fast_trigger.state,
                "routed_state": state_machine.current_state,
                "confidence": routed_regime.confidence,
                "confidence_source": decision_result.confidence_source,
                "conflict_flags": dict(routed_regime.conflict_flags),
                "instability_flags": dict(routed_regime.instability_flags),
                "routed_transition_reason": list(routed_regime.transition_reason),
                "decision": decision_result.decision,
                "decision_reason": decision_result.decision_reason,
                "entry_allowed": decision_result.entry_allowed,
                "range_unclear_diagnosis": decision_result.range_unclear_diagnosis,
            },
        )
        self.logger.debug(
            "Processed market regime symbol=%s slow=%s mid=%s fast=%s routed=%s decision=%s reason=%s oi_price=%s confidence=%s confidence_source=%s range_diag=%s conflicts=%s instability=%s bias=%s slow_counter=%s emergency=%s persisted=%s",
            clean_symbol,
            slow_regime.state,
            mid_regime.state,
            fast_trigger.state,
            state_machine.current_state,
            decision_result.decision,
            decision_result.decision_reason,
            current_normalized.label("oi_price_state", "neutral"),
            routed_regime.confidence,
            decision_result.confidence_source,
            decision_result.range_unclear_diagnosis,
            sorted(routed_regime.conflict_flags.keys()),
            sorted(routed_regime.instability_flags.keys()),
            slow_regime.bias,
            slow_regime.transition_counter,
            routed_regime.emergency_trigger,
            persisted,
        )
        return result
