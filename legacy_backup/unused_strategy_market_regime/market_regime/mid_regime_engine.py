from __future__ import annotations

from .config import (
    MID_EXHAUSTION_MIN_EXHAUSTION,
    MID_EXHAUSTION_PRESSURE_MIN,
    MID_PARTICIPATION_MIN,
    MID_PULLBACK_MAX_EXHAUSTION,
    MID_PULLBACK_PRESSURE_MIN,
    MID_REVERSAL_PARTICIPATION_MIN,
    MID_REVERSAL_EXHAUSTION_MIN,
    MID_REVERSAL_PRESSURE_MIN,
    MID_REVERSAL_SLOW_EXHAUSTION_MIN,
)
from .models import FastTriggerSnapshot, MidRegimeSnapshot, NormalizedSnapshot, PrimitiveEvents, SlowRegimeSnapshot


def compute_mid_state(
    fast: FastTriggerSnapshot,
    slow: SlowRegimeSnapshot,
    events: PrimitiveEvents | None = None,
    snapshot: NormalizedSnapshot | None = None,
) -> MidRegimeSnapshot:
    fast_pressure = fast.pressure_score_fast
    fast_participation = fast.participation_score_fast
    fast_exhaustion = fast.exhaustion_score_fast
    fast_state = fast.state
    oi_price_state = fast.oi_price_state
    slow_state = slow.state
    slow_exhaustion = slow.exhaustion_score_slow
    events = events or PrimitiveEvents()
    spread_stress = snapshot.value("spread_stress_score") if snapshot is not None else 0.0
    atr_regime = snapshot.value("atr_regime_zscore") if snapshot is not None else 0.0

    long_context = slow_state in {"slow_trend_long", "slow_transition_long_to_neutral"}
    short_context = slow_state in {"slow_trend_short", "slow_transition_short_to_neutral"}
    long_transition = slow_state == "slow_transition_long_to_neutral"
    short_transition = slow_state == "slow_transition_short_to_neutral"
    strong_opposition_long = fast_pressure <= -(MID_EXHAUSTION_PRESSURE_MIN + 2.0)
    strong_opposition_short = fast_pressure >= (MID_EXHAUSTION_PRESSURE_MIN + 2.0)
    bearish_build_up = oi_price_state == "price_down_oi_up"
    bearish_deleveraging = oi_price_state == "price_down_oi_down"
    bullish_build_up = oi_price_state == "price_up_oi_up"
    bullish_deleveraging = oi_price_state == "price_up_oi_down"

    pullback_in_long = (
        long_context
        and fast_pressure <= -MID_PULLBACK_PRESSURE_MIN
        and fast_pressure > -MID_EXHAUSTION_PRESSURE_MIN
        and fast_participation >= MID_PARTICIPATION_MIN
        and fast_exhaustion < MID_PULLBACK_MAX_EXHAUSTION
        and not events.spread_stress_phase
        and not events.panic_liquidation_phase
    )
    pullback_in_short = (
        short_context
        and fast_pressure >= MID_PULLBACK_PRESSURE_MIN
        and fast_pressure < MID_EXHAUSTION_PRESSURE_MIN
        and fast_participation >= MID_PARTICIPATION_MIN
        and fast_exhaustion < MID_PULLBACK_MAX_EXHAUSTION
        and not events.spread_stress_phase
        and not events.panic_liquidation_phase
    )

    reversal_setup_long = (
        short_context
        and fast_pressure >= MID_REVERSAL_PRESSURE_MIN
        and fast_participation >= MID_REVERSAL_PARTICIPATION_MIN
        and fast_exhaustion >= MID_REVERSAL_EXHAUSTION_MIN
        and (short_transition or slow_exhaustion >= MID_REVERSAL_SLOW_EXHAUSTION_MIN)
        and fast_state in {"fast_reversal_attempt_long", "fast_impulse_long"}
        and (events.fresh_long_build_up or bullish_build_up or fast_pressure >= (MID_REVERSAL_PRESSURE_MIN + 2.0))
        and (events.squeeze_exhaustion_reversal or fast_exhaustion >= MID_REVERSAL_EXHAUSTION_MIN)
        and not bullish_deleveraging
        and not events.spread_stress_phase
    )
    reversal_setup_short = (
        long_context
        and fast_pressure <= -MID_REVERSAL_PRESSURE_MIN
        and fast_participation >= MID_REVERSAL_PARTICIPATION_MIN
        and fast_exhaustion >= MID_REVERSAL_EXHAUSTION_MIN
        and (long_transition or slow_exhaustion >= MID_REVERSAL_SLOW_EXHAUSTION_MIN)
        and fast_state in {"fast_reversal_attempt_short", "fast_impulse_short"}
        and (events.fresh_short_build_up or bearish_build_up or fast_pressure <= -(MID_REVERSAL_PRESSURE_MIN + 2.0))
        and (events.squeeze_exhaustion_reversal or fast_exhaustion >= MID_REVERSAL_EXHAUSTION_MIN)
        and not bearish_deleveraging
        and not events.spread_stress_phase
    )

    exhaustion_long = (
        long_context
        and not reversal_setup_short
        and fast_pressure <= -MID_EXHAUSTION_PRESSURE_MIN
        and (
            bearish_deleveraging
            or events.panic_liquidation_phase
            or events.spread_stress_phase
            or events.weak_move_low_participation
            or
            fast_exhaustion >= MID_EXHAUSTION_MIN_EXHAUSTION
            or fast_state in {"fast_exhaustion_long", "fast_impulse_short"}
            or (fast_state == "fast_exhaustion_long" and strong_opposition_long)
        )
    )
    exhaustion_short = (
        short_context
        and not reversal_setup_long
        and fast_pressure >= MID_EXHAUSTION_PRESSURE_MIN
        and (
            bullish_deleveraging
            or events.panic_liquidation_phase
            or events.spread_stress_phase
            or events.weak_move_low_participation
            or
            fast_exhaustion >= MID_EXHAUSTION_MIN_EXHAUSTION
            or fast_state in {"fast_exhaustion_short", "fast_impulse_long"}
            or (fast_state == "fast_exhaustion_short" and strong_opposition_short)
        )
    )

    candidate_flags = {
        "mid_pullback_in_long": pullback_in_long,
        "mid_pullback_in_short": pullback_in_short,
        "mid_exhaustion_long": exhaustion_long,
        "mid_exhaustion_short": exhaustion_short,
        "mid_reversal_setup_long": reversal_setup_long,
        "mid_reversal_setup_short": reversal_setup_short,
    }

    state: str | None = None
    transition_reason: list[str] = []
    matched_rule_name: str | None = None
    ordered_states = [
        "mid_pullback_in_long",
        "mid_pullback_in_short",
        "mid_exhaustion_long",
        "mid_exhaustion_short",
        "mid_reversal_setup_long",
        "mid_reversal_setup_short",
    ]
    for candidate in ordered_states:
        if candidate_flags.get(candidate):
            state = candidate
            matched_rule_name = candidate
            transition_reason.append(f"mid_detected:{candidate}")
            if candidate in {"mid_exhaustion_long", "mid_exhaustion_short"}:
                transition_reason.append("classified_as_exhaustion_due_to_pressure")
            if candidate in {"mid_reversal_setup_long", "mid_reversal_setup_short"}:
                transition_reason.append("classified_as_reversal_due_to_pressure_and_exhaustion")
            break
    if state is None:
        transition_reason.append("mid_none")
    transition_reason.append(f"oi_price_state:{oi_price_state}")

    return MidRegimeSnapshot(
        state=state,
        transition_reason=transition_reason,
        candidate_flags=candidate_flags,
        debug={
            "slow_state": slow_state,
            "fast_state": fast_state,
            "fast_pressure": fast_pressure,
            "fast_participation": fast_participation,
            "fast_exhaustion": fast_exhaustion,
            "slow_exhaustion": slow_exhaustion,
            "oi_price_state": oi_price_state,
            "spread_stress_score": spread_stress,
            "atr_regime_zscore": atr_regime,
            "event_fresh_long_build_up": events.fresh_long_build_up,
            "event_fresh_short_build_up": events.fresh_short_build_up,
            "event_panic_liquidation_phase": events.panic_liquidation_phase,
            "event_spread_stress_phase": events.spread_stress_phase,
            "event_squeeze_exhaustion_reversal": events.squeeze_exhaustion_reversal,
            "matched_rule_name": matched_rule_name,
        },
    )
