from __future__ import annotations

from .config import (
    SLOW_BIAS_CONFIRMATIONS,
    SLOW_BIAS_THRESHOLD,
    SLOW_TRANSITION_EXHAUSTION_MIN,
    SLOW_TRANSITION_PRESSURE_FADE,
    SLOW_TREND_EXHAUSTION_MAX,
    SLOW_TREND_PARTICIPATION_MIN,
    SLOW_TREND_PRESSURE_MIN,
)
from .models import NormalizedSnapshot, PrimitiveEvents, SlowRegimeSnapshot, StateMachineSnapshot


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def _derived(snapshot: NormalizedSnapshot, field_name: str, default: float = 0.0) -> float:
    value = snapshot.value(field_name, default)
    return default if value is None else float(value)


def compute_slow_regime(
    snapshot: NormalizedSnapshot,
    events: PrimitiveEvents | None = None,
    previous_state: StateMachineSnapshot | None = None,
) -> SlowRegimeSnapshot:
    events = events or PrimitiveEvents()
    oi_price_state = snapshot.label("oi_price_state", "neutral")
    pressure_score = (
        0.35 * _clamp(snapshot.z("price_change_15m"), -2.0, 2.0)
        + 0.25 * _clamp(snapshot.z("price_change_5m"), -2.0, 2.0)
        + 0.15 * _clamp(snapshot.z("orderflow_ratio"), -2.0, 2.0)
        + 0.10 * _clamp(snapshot.z("oi_change_ratio"), -2.0, 2.0)
        + 0.15 * _clamp(_derived(snapshot, "oi_abs_zscore"), -2.0, 2.0)
    ) * 25.0
    participation_score = max(
        0.0,
        (
            0.30 * max(snapshot.z("trade_volume_1m"), 0.0)
            + 0.25 * max(snapshot.z("volume_spike_ratio"), 0.0)
            + 0.20 * max(snapshot.z("trade_count_1m"), 0.0)
            + 0.25 * max(_derived(snapshot, "atr_regime_zscore"), 0.0)
        )
        * 20.0,
    )

    exhaustion_score = 0.0
    velocity_5m = snapshot.value("velocity_5m")
    velocity_15m = snapshot.value("velocity_15m")
    acceleration_5m = snapshot.value("acceleration_5m")
    if velocity_5m > 0 and acceleration_5m < 0:
        exhaustion_score += 30.0
    if velocity_15m > 0 and snapshot.z("oi_change_ratio") < -0.5:
        exhaustion_score += 20.0
    if velocity_5m < 0 and acceleration_5m > 0:
        exhaustion_score += 30.0
    if velocity_15m < 0 and snapshot.z("oi_change_ratio") > 0.5:
        exhaustion_score += 20.0

    if events.fresh_long_build_up:
        pressure_score += 10.0
    if events.fresh_short_build_up:
        pressure_score -= 10.0
    if events.squeeze_exhaustion_reversal:
        exhaustion_score += 15.0
    if events.spread_stress_phase:
        exhaustion_score += 10.0
        participation_score -= 5.0
    if events.panic_liquidation_phase:
        exhaustion_score += 12.0

    if pressure_score > 0:
        if oi_price_state == "price_up_oi_up":
            pressure_score += 5.0
        elif oi_price_state == "price_up_oi_down":
            pressure_score -= 5.0
            exhaustion_score += 10.0
    elif pressure_score < 0:
        if oi_price_state == "price_down_oi_up":
            pressure_score -= 5.0
        elif oi_price_state == "price_down_oi_down":
            pressure_score += 5.0
            exhaustion_score += 10.0

    pressure_score = _clamp(pressure_score, -100.0, 100.0)
    exhaustion_score = _clamp(exhaustion_score, 0.0, 100.0)

    candidate_flags = {
        "slow_trend_long": (
            pressure_score >= SLOW_TREND_PRESSURE_MIN
            and participation_score >= SLOW_TREND_PARTICIPATION_MIN
            and exhaustion_score < SLOW_TREND_EXHAUSTION_MAX
        ),
        "slow_trend_short": (
            pressure_score <= -SLOW_TREND_PRESSURE_MIN
            and participation_score >= SLOW_TREND_PARTICIPATION_MIN
            and exhaustion_score < SLOW_TREND_EXHAUSTION_MAX
        ),
        "slow_transition_long_to_neutral": (
            pressure_score > SLOW_TRANSITION_PRESSURE_FADE
            and exhaustion_score >= SLOW_TRANSITION_EXHAUSTION_MIN
        ),
        "slow_transition_short_to_neutral": (
            pressure_score < -SLOW_TRANSITION_PRESSURE_FADE
            and exhaustion_score >= SLOW_TRANSITION_EXHAUSTION_MIN
        ),
    }

    prior_bias = previous_state.slow_bias if previous_state is not None else 0
    prior_counter = previous_state.slow_transition_counter if previous_state is not None else 0
    prior_memory = (
        previous_state.slow_state_memory
        if previous_state is not None and previous_state.slow_state_memory
        else "slow_range_neutral"
    )
    if pressure_score > SLOW_BIAS_THRESHOLD:
        new_bias = 1
    elif pressure_score < -SLOW_BIAS_THRESHOLD:
        new_bias = -1
    else:
        new_bias = 0

    if new_bias != prior_bias:
        transition_counter = prior_counter + 1
    else:
        transition_counter = 0

    effective_bias = prior_bias
    bias_changed = False
    if transition_counter >= SLOW_BIAS_CONFIRMATIONS:
        effective_bias = new_bias
        bias_changed = True

    state_memory = prior_memory
    if effective_bias == 1:
        state_memory = "slow_trend_long"
    elif effective_bias == -1:
        state_memory = "slow_trend_short"
    elif bias_changed:
        state_memory = "slow_range_neutral"

    state = state_memory
    transition_reason: list[str] = []
    if state_memory == "slow_trend_long" and candidate_flags["slow_transition_long_to_neutral"]:
        state = "slow_transition_long_to_neutral"
        transition_reason.append("slow_long_exhausting")
    elif state_memory == "slow_trend_short" and candidate_flags["slow_transition_short_to_neutral"]:
        state = "slow_transition_short_to_neutral"
        transition_reason.append("slow_short_exhausting")
    elif state_memory == "slow_trend_long":
        transition_reason.append("slow_long_structure_memory")
    elif state_memory == "slow_trend_short":
        transition_reason.append("slow_short_structure_memory")
    else:
        transition_reason.append("slow_range_unclear")
    transition_reason.append(f"oi_price_state:{oi_price_state}")
    transition_reason.append(f"slow_transition_counter:{transition_counter}")
    transition_reason.append(f"slow_bias:{effective_bias}")
    if bias_changed:
        transition_reason.append("slow_bias_changed")

    return SlowRegimeSnapshot(
        state=state,
        pressure_score_slow=pressure_score,
        participation_score_slow=participation_score,
        exhaustion_score_slow=exhaustion_score,
        oi_price_state=oi_price_state,
        state_memory=state_memory,
        transition_counter=transition_counter,
        bias=effective_bias,
        candidate_flags=candidate_flags,
        transition_reason=transition_reason,
    )
