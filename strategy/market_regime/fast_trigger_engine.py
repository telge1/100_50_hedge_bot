from __future__ import annotations

from .config import (
    FAST_EMERGENCY_INSTABILITY_MIN,
    FAST_EXHAUSTION_MIN,
    FAST_IMPULSE_PRESSURE_MIN,
    FAST_PULLBACK_PRESSURE_MIN,
    FAST_REVERSAL_PARTICIPATION_MIN,
    FAST_REVERSAL_PRESSURE_MIN,
)
from .models import FastTriggerSnapshot, NormalizedSnapshot, PrimitiveEvents


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def _derived(snapshot: NormalizedSnapshot, field_name: str, default: float = 0.0) -> float:
    value = snapshot.value(field_name, default)
    return default if value is None else float(value)


def compute_fast_trigger(
    snapshot: NormalizedSnapshot,
    events: PrimitiveEvents,
    slow_state: str,
) -> FastTriggerSnapshot:
    oi_price_state = snapshot.label("oi_price_state", "neutral")
    long_event_pressure = 0.0
    short_event_pressure = 0.0
    participation_boost = 0.0
    instability_boost = 0.0
    exhaustion_boost = 0.0

    if events.fresh_long_build_up:
        long_event_pressure += 18.0
    if events.fresh_short_build_up:
        short_event_pressure += 18.0
    if events.high_participation_breakout:
        participation_boost += 18.0
        if snapshot.z("price_change_1m") > 0:
            long_event_pressure += 10.0
        elif snapshot.z("price_change_1m") < 0:
            short_event_pressure += 10.0
    if events.weak_move_low_participation:
        participation_boost -= 12.0
        exhaustion_boost += 15.0
    if events.volatility_expansion:
        instability_boost += 12.0
    if events.thin_orderflow_instability:
        instability_boost += 22.0
    if events.spread_stress_phase:
        instability_boost += 18.0
        exhaustion_boost += 6.0
    if events.dirty_breakout_risk:
        instability_boost += 14.0
        participation_boost -= 8.0
    if events.panic_liquidation_phase:
        instability_boost += 16.0
        exhaustion_boost += 18.0
    if events.squeeze_exhaustion_reversal:
        exhaustion_boost += 20.0
        if snapshot.z("price_change_1m") > 0:
            short_event_pressure += 8.0
        elif snapshot.z("price_change_1m") < 0:
            long_event_pressure += 8.0

    pressure_score = (
        0.30 * _clamp(snapshot.z("price_change_1m"), -2.0, 2.0)
        + 0.25 * _clamp(snapshot.z("orderflow_ratio"), -2.0, 2.0)
        + 0.15 * _clamp(snapshot.z("delta_ratio"), -2.0, 2.0)
        + 0.15 * _clamp(snapshot.z("velocity_1m"), -2.0, 2.0)
        + 0.15 * _clamp(_derived(snapshot, "price_move_vs_atr"), -2.0, 2.0)
    ) * 22.0
    pressure_score += long_event_pressure
    pressure_score -= short_event_pressure
    participation_score = max(
        0.0,
        (
            0.25 * max(snapshot.z("trade_volume_1m"), 0.0)
            + 0.25 * max(snapshot.z("trade_count_1m"), 0.0)
            + 0.25 * max(snapshot.z("avg_trade_size"), 0.0)
            + 0.25 * max(_derived(snapshot, "trade_intensity_score"), 0.0)
        )
        * 20.0,
    )
    participation_score += participation_boost
    instability_score = max(
        0.0,
        (
            0.20 * max(snapshot.z("microburst_score"), 0.0)
            + 0.20 * max(snapshot.z("liquidation_density_5m"), 0.0)
            + 0.15 * max(snapshot.z("liquidation_cluster_score"), 0.0)
            + 0.20 * max(snapshot.z("spread_ratio"), 0.0)
            + 0.25 * max(_derived(snapshot, "spread_stress_score"), 0.0)
        )
        * 25.0,
    )
    instability_score += instability_boost

    exhaustion_score = exhaustion_boost
    if events.oi_price_build_long:
        pressure_score += 8.0
        participation_score += 5.0
    elif events.oi_price_short_covering:
        pressure_score -= 8.0
        exhaustion_score += 10.0
    elif events.oi_price_build_short:
        pressure_score -= 8.0
        participation_score += 5.0
    elif events.oi_price_long_flush:
        pressure_score += 8.0
        exhaustion_score += 10.0
    if events.velocity_slowdown_long or events.velocity_slowdown_short:
        exhaustion_score += 30.0
    if events.pressure_divergence_long or events.pressure_divergence_short:
        exhaustion_score += 25.0
    if events.oi_flush:
        exhaustion_score += 20.0
    if events.microburst_risk:
        exhaustion_score += 15.0
    if events.liq_cluster_event:
        exhaustion_score += 10.0
    pressure_score = _clamp(pressure_score, -100.0, 100.0)
    participation_score = _clamp(participation_score, 0.0, 100.0)
    instability_score = _clamp(instability_score, 0.0, 100.0)
    exhaustion_score = _clamp(exhaustion_score, 0.0, 100.0)

    candidate_flags = {
        "fast_emergency_instability": instability_score >= FAST_EMERGENCY_INSTABILITY_MIN,
        "fast_impulse_long": (
            pressure_score >= FAST_IMPULSE_PRESSURE_MIN
            and participation_score >= FAST_REVERSAL_PARTICIPATION_MIN
        ),
        "fast_impulse_short": (
            pressure_score <= -FAST_IMPULSE_PRESSURE_MIN
            and participation_score >= FAST_REVERSAL_PARTICIPATION_MIN
        ),
        "fast_pullback_short_in_long": (
            slow_state == "slow_trend_long" and pressure_score <= -FAST_PULLBACK_PRESSURE_MIN
        ),
        "fast_pullback_long_in_short": (
            slow_state == "slow_trend_short" and pressure_score >= FAST_PULLBACK_PRESSURE_MIN
        ),
        "fast_exhaustion_long": slow_state == "slow_trend_long" and exhaustion_score >= FAST_EXHAUSTION_MIN,
        "fast_exhaustion_short": slow_state == "slow_trend_short" and exhaustion_score >= FAST_EXHAUSTION_MIN,
        "fast_reversal_attempt_long": (
            pressure_score >= FAST_REVERSAL_PRESSURE_MIN
            and participation_score >= FAST_REVERSAL_PARTICIPATION_MIN
            and (events.orderflow_flip_long or events.price_flip_long)
        ),
        "fast_reversal_attempt_short": (
            pressure_score <= -FAST_REVERSAL_PRESSURE_MIN
            and participation_score >= FAST_REVERSAL_PARTICIPATION_MIN
            and (events.orderflow_flip_short or events.price_flip_short)
        ),
    }

    state = "fast_neutral"
    transition_reason: list[str] = []
    if candidate_flags["fast_emergency_instability"]:
        state = "fast_emergency_instability"
        transition_reason.append("instability_extreme")
    elif candidate_flags["fast_pullback_short_in_long"]:
        state = "fast_pullback_short_in_long"
        transition_reason.append("pullback_against_long")
    elif candidate_flags["fast_pullback_long_in_short"]:
        state = "fast_pullback_long_in_short"
        transition_reason.append("pullback_against_short")
    elif candidate_flags["fast_impulse_long"]:
        state = "fast_impulse_long"
        transition_reason.append("fast_long_impulse")
    elif candidate_flags["fast_impulse_short"]:
        state = "fast_impulse_short"
        transition_reason.append("fast_short_impulse")
    elif candidate_flags["fast_exhaustion_long"]:
        state = "fast_exhaustion_long"
        transition_reason.append("fast_long_exhaustion")
    elif candidate_flags["fast_exhaustion_short"]:
        state = "fast_exhaustion_short"
        transition_reason.append("fast_short_exhaustion")
    elif candidate_flags["fast_reversal_attempt_long"]:
        state = "fast_reversal_attempt_long"
        transition_reason.append("fast_long_reversal_attempt")
    elif candidate_flags["fast_reversal_attempt_short"]:
        state = "fast_reversal_attempt_short"
        transition_reason.append("fast_short_reversal_attempt")
    else:
        transition_reason.append("fast_neutral")
    transition_reason.append(f"oi_price_state:{oi_price_state}")

    return FastTriggerSnapshot(
        state=state,
        pressure_score_fast=pressure_score,
        participation_score_fast=participation_score,
        instability_score_fast=instability_score,
        exhaustion_score_fast=exhaustion_score,
        oi_price_state=oi_price_state,
        candidate_flags=candidate_flags,
        transition_reason=transition_reason,
    )
