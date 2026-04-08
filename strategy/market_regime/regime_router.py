from __future__ import annotations

from .models import FastTriggerSnapshot, MidRegimeSnapshot, RoutedRegimeSnapshot, SlowRegimeSnapshot


def _build_snapshot(
    *,
    slow: SlowRegimeSnapshot,
    mid: MidRegimeSnapshot,
    fast: FastTriggerSnapshot,
    routed_state: str,
    confidence: float,
    transition_reason: list[str],
    bot_hint: str,
    conflict_flags: dict[str, bool] | None = None,
    instability_flags: dict[str, bool] | None = None,
    emergency_trigger: bool = False,
) -> RoutedRegimeSnapshot:
    return RoutedRegimeSnapshot(
        slow_state=slow.state,
        mid_state=mid.state,
        fast_state=fast.state,
        routed_state=routed_state,
        oi_price_state=fast.oi_price_state,
        confidence=confidence,
        conflict_flags=dict(conflict_flags or {}),
        instability_flags=dict(instability_flags or {}),
        transition_reason=transition_reason,
        emergency_trigger=emergency_trigger,
        bot_hint=bot_hint,
        candidate_states=[routed_state],
        candidate_flags={routed_state: True},
    )


def _fast_opposes_long(fast_state: str) -> bool:
    return fast_state in {
        "fast_impulse_short",
        "fast_pullback_short_in_long",
        "fast_reversal_attempt_short",
    }


def _fast_supports_long(fast_state: str) -> bool:
    return fast_state in {
        "fast_neutral",
        "fast_impulse_long",
        "fast_reversal_attempt_long",
        "fast_pullback_long_in_short",
    }


def _fast_ambiguous_long(fast_state: str) -> bool:
    return fast_state == "fast_exhaustion_long"


def _fast_opposes_short(fast_state: str) -> bool:
    return fast_state in {
        "fast_impulse_long",
        "fast_pullback_long_in_short",
        "fast_reversal_attempt_long",
    }


def _fast_supports_short(fast_state: str) -> bool:
    return fast_state in {
        "fast_neutral",
        "fast_impulse_short",
        "fast_reversal_attempt_short",
        "fast_pullback_short_in_long",
    }


def _fast_ambiguous_short(fast_state: str) -> bool:
    return fast_state == "fast_exhaustion_short"


def route_regime(
    slow: SlowRegimeSnapshot,
    mid: MidRegimeSnapshot,
    fast: FastTriggerSnapshot,
) -> RoutedRegimeSnapshot:
    if fast.state == "fast_emergency_instability":
        return _build_snapshot(
            slow=slow,
            mid=mid,
            fast=fast,
            routed_state="emergency",
            confidence=0.95,
            transition_reason=["fast_emergency_instability", f"oi_price_state:{fast.oi_price_state}"],
            bot_hint="emergency_hedge",
            instability_flags={"emergency_instability": True},
            emergency_trigger=True,
        )

    if mid.state is not None:
        bot_hint = "repair"
        if "reversal" in mid.state:
            bot_hint = "pause_adds"
        instability_flags = {
            "fast_exhaustion_ambiguous": fast.state in {"fast_exhaustion_long", "fast_exhaustion_short"},
        }
        return _build_snapshot(
            slow=slow,
            mid=mid,
            fast=fast,
            routed_state=mid.state,
            confidence=0.85,
            transition_reason=list(mid.transition_reason or [f"mid_priority:{mid.state}"]),
            bot_hint=bot_hint,
            instability_flags=instability_flags,
        )

    if slow.state in {"slow_trend_long", "slow_transition_long_to_neutral"}:
        if _fast_ambiguous_long(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="range_unclear",
                confidence=0.40,
                transition_reason=["slow_long_fast_exhaustion_ambiguous", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="hold",
                conflict_flags={"fast_exhaustion_ambiguous": True},
                instability_flags={"fast_exhaustion_ambiguous": True},
            )
        if _fast_opposes_long(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="pullback_in_long_context",
                confidence=0.75,
                transition_reason=["slow_long_context_fast_opposes", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="repair",
                conflict_flags={"slow_fast_direction_conflict": True},
            )
        if _fast_supports_long(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="trend_continuation_long",
                confidence=0.75,
                transition_reason=["slow_long_context_meta_support", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="burn",
            )

    if slow.state in {"slow_trend_short", "slow_transition_short_to_neutral"}:
        if _fast_ambiguous_short(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="range_unclear",
                confidence=0.40,
                transition_reason=["slow_short_fast_exhaustion_ambiguous", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="hold",
                conflict_flags={"fast_exhaustion_ambiguous": True},
                instability_flags={"fast_exhaustion_ambiguous": True},
            )
        if _fast_opposes_short(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="pullback_in_short_context",
                confidence=0.75,
                transition_reason=["slow_short_context_fast_opposes", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="repair",
                conflict_flags={"slow_fast_direction_conflict": True},
            )
        if _fast_supports_short(fast.state):
            return _build_snapshot(
                slow=slow,
                mid=mid,
                fast=fast,
                routed_state="trend_continuation_short",
                confidence=0.75,
                transition_reason=["slow_short_context_meta_support", f"oi_price_state:{fast.oi_price_state}"],
                bot_hint="burn",
            )

    return _build_snapshot(
        slow=slow,
        mid=mid,
        fast=fast,
        routed_state="range_unclear",
        confidence=0.40,
        transition_reason=["true_range_or_unclear_context", f"oi_price_state:{fast.oi_price_state}"],
        bot_hint="hold",
    )
