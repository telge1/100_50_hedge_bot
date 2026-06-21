from __future__ import annotations

from .config import (
    EMERGENCY_INSTABILITY_MIN,
    REBOUND_PARTICIPATION_CONFIRM,
    REBOUND_PRESSURE_CONFIRM,
    TREND_EXHAUSTION_MAX,
    TREND_EXHAUSTION_STRONG_MAX,
    TREND_INSTABILITY_MAX,
    TREND_PARTICIPATION_MIN,
    TREND_PARTICIPATION_STRONG_MIN,
    TREND_PRESSURE_MAX,
    TREND_PRESSURE_MIN,
    TREND_STRONG_PRESSURE_MIN,
)
from .models import PrimitiveEvents, RegimeSnapshot, ScoreSnapshot


def compute_candidate_regimes(
    events: PrimitiveEvents,
    scores: ScoreSnapshot,
    previous_state: str,
) -> RegimeSnapshot:
    pressure_score = scores.pressure_score
    participation_score = scores.participation_score
    instability_score = scores.instability_score
    exhaustion_score = scores.exhaustion_score

    strong_trend_long = (
        pressure_score >= TREND_STRONG_PRESSURE_MIN
        and participation_score >= TREND_PARTICIPATION_STRONG_MIN
        and (events.price_impulse_up or events.orderflow_push_long)
    )
    strong_trend_short = (
        pressure_score <= -TREND_STRONG_PRESSURE_MIN
        and participation_score >= TREND_PARTICIPATION_STRONG_MIN
        and (events.price_impulse_down or events.orderflow_push_short)
    )
    trend_long_participation_min = (
        TREND_PARTICIPATION_STRONG_MIN if strong_trend_long else TREND_PARTICIPATION_MIN
    )
    trend_short_participation_min = (
        TREND_PARTICIPATION_STRONG_MIN if strong_trend_short else TREND_PARTICIPATION_MIN
    )
    trend_long_exhaustion_max = TREND_EXHAUSTION_STRONG_MAX if strong_trend_long else TREND_EXHAUSTION_MAX
    trend_short_exhaustion_max = TREND_EXHAUSTION_STRONG_MAX if strong_trend_short else TREND_EXHAUSTION_MAX

    candidate_trend_long = (
        pressure_score >= TREND_PRESSURE_MIN
        and participation_score >= trend_long_participation_min
        and instability_score < TREND_INSTABILITY_MAX
        and exhaustion_score < trend_long_exhaustion_max
    )
    candidate_trend_short = (
        pressure_score <= TREND_PRESSURE_MAX
        and participation_score >= trend_short_participation_min
        and instability_score < TREND_INSTABILITY_MAX
        and exhaustion_score < trend_short_exhaustion_max
    )
    candidate_trend_exhaustion_long = (
        previous_state in {"trend_long", "trend_exhaustion_long"}
        and pressure_score > 15
        and exhaustion_score >= TREND_EXHAUSTION_MAX
    )
    candidate_trend_exhaustion_short = (
        previous_state in {"trend_short", "trend_exhaustion_short"}
        and pressure_score < -15
        and exhaustion_score >= TREND_EXHAUSTION_MAX
    )
    candidate_rebound_start_long = (
        previous_state in {"trend_short", "trend_exhaustion_short"}
        and events.price_flip_long
        and events.orderflow_flip_long
        and events.volume_participation_high
        and (events.oi_flush or events.liq_flush_down or events.microburst_risk)
    )
    candidate_rebound_start_short = (
        previous_state in {"trend_long", "trend_exhaustion_long"}
        and events.price_flip_short
        and events.orderflow_flip_short
        and events.volume_participation_high
        and (events.oi_flush or events.liq_flush_up or events.microburst_risk)
    )
    candidate_rebound_confirmed_long = (
        previous_state in {"rebound_start_long", "rebound_confirmed_long"}
        and pressure_score > REBOUND_PRESSURE_CONFIRM
        and participation_score > REBOUND_PARTICIPATION_CONFIRM
        and events.orderflow_push_long
    )
    candidate_rebound_confirmed_short = (
        previous_state in {"rebound_start_short", "rebound_confirmed_short"}
        and pressure_score < -REBOUND_PRESSURE_CONFIRM
        and participation_score > REBOUND_PARTICIPATION_CONFIRM
        and events.orderflow_push_short
    )
    hf_rebound_participation_long = (
        candidate_rebound_start_long
        and events.rebound_participation_surge_long
        and events.orderflow_push_long
        and scores.pressure_score > 0
    )
    hf_rebound_participation_short = (
        candidate_rebound_start_short
        and events.rebound_participation_surge_short
        and events.orderflow_push_short
        and scores.pressure_score < 0
    )
    emergency_trigger = (
        instability_score >= EMERGENCY_INSTABILITY_MIN
        and (
            events.spread_explosion
            or events.microburst_extreme
            or events.liq_cluster_event
            or events.liquidity_vacuum
            or events.price_extreme_up
            or events.price_extreme_down
        )
    )

    candidate_flags = {
        "trend_long": candidate_trend_long,
        "trend_short": candidate_trend_short,
        "trend_exhaustion_long": candidate_trend_exhaustion_long,
        "trend_exhaustion_short": candidate_trend_exhaustion_short,
        "rebound_start_long": candidate_rebound_start_long,
        "rebound_start_short": candidate_rebound_start_short,
        "rebound_confirmed_long": candidate_rebound_confirmed_long,
        "rebound_confirmed_short": candidate_rebound_confirmed_short,
        "emergency": emergency_trigger,
    }
    candidate_states = [name for name, active in candidate_flags.items() if active]

    transition_reason: list[str] = []
    if candidate_trend_long:
        transition_reason.append("trend_long_scores_ok")
        if strong_trend_long:
            transition_reason.append("strong_trend_long_override")
        if events.price_impulse_up:
            transition_reason.append("price_impulse_up")
        if events.orderflow_push_long:
            transition_reason.append("orderflow_push_long")
    if candidate_trend_short:
        transition_reason.append("trend_short_scores_ok")
        if strong_trend_short:
            transition_reason.append("strong_trend_short_override")
        if events.price_impulse_down:
            transition_reason.append("price_impulse_down")
        if events.orderflow_push_short:
            transition_reason.append("orderflow_push_short")
    if candidate_rebound_start_long:
        transition_reason.extend(["price_flip_long", "orderflow_flip_long", "volume_participation_high"])
        if events.oi_flush:
            transition_reason.append("oi_flush")
        if events.liq_flush_down:
            transition_reason.append("liq_flush_down")
        if events.microburst_risk:
            transition_reason.append("microburst_risk")
    if candidate_rebound_start_short:
        transition_reason.extend(["price_flip_short", "orderflow_flip_short", "volume_participation_high"])
        if events.oi_flush:
            transition_reason.append("oi_flush")
        if events.liq_flush_up:
            transition_reason.append("liq_flush_up")
        if events.microburst_risk:
            transition_reason.append("microburst_risk")
    if emergency_trigger:
        if events.spread_explosion:
            transition_reason.append("spread_explosion")
        if events.microburst_extreme:
            transition_reason.append("microburst_extreme")
        if events.liq_cluster_event:
            transition_reason.append("liq_cluster_event")
        if events.liquidity_vacuum:
            transition_reason.append("liquidity_vacuum")

    return RegimeSnapshot(
        candidate_states=candidate_states,
        candidate_flags=candidate_flags,
        active_state=previous_state,
        emergency_trigger=emergency_trigger,
        hf_rebound_participation_flag=hf_rebound_participation_long or hf_rebound_participation_short,
        transition_reason=transition_reason,
    )
