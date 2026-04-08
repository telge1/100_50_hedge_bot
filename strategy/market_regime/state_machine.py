from __future__ import annotations

from datetime import datetime

from .config import ALLOWED_ROUTED_TRANSITIONS, DEFAULT_ROUTED_COOLDOWN_FAST_UPDATES, ROUTED_REQUIRED_CONFIRMATIONS, ROUTED_STATES
from .models import RegimeSnapshot, RoutedRegimeSnapshot, StateMachineSnapshot


LEGACY_TO_ROUTED = {
    "neutral": "range_unclear",
    "trend_long": "trend_continuation_long",
    "trend_short": "trend_continuation_short",
    "trend_exhaustion_long": "mid_exhaustion_long",
    "trend_exhaustion_short": "mid_exhaustion_short",
    "rebound_start_long": "reversal_building_long",
    "rebound_start_short": "reversal_building_short",
    "rebound_confirmed_long": "reversal_confirmed_long",
    "rebound_confirmed_short": "reversal_confirmed_short",
    "emergency": "emergency",
}


def _sanitize_routed_state(state: str | None) -> str:
    raw = str(state or "").strip()
    if raw in ROUTED_STATES:
        return raw
    return LEGACY_TO_ROUTED.get(raw, "range_unclear")


def _allowed_transition(current_state: str, next_state: str) -> bool:
    current = _sanitize_routed_state(current_state)
    nxt = _sanitize_routed_state(next_state)
    if current == nxt:
        return True
    return nxt in ALLOWED_ROUTED_TRANSITIONS.get(current, set())


def _bump_counters(
    current_counters: dict[str, int] | None,
    candidate_state: str,
    current_state: str,
    freeze_increment: bool,
) -> dict[str, int]:
    counters = dict(current_counters or {})
    for state in ROUTED_REQUIRED_CONFIRMATIONS:
        if state == candidate_state:
            if not freeze_increment:
                counters[state] = counters.get(state, 0) + 1
        elif state != current_state:
            counters[state] = 0
    return counters


def apply_routed_state_machine(
    previous_state: StateMachineSnapshot | None,
    routed_regime: RoutedRegimeSnapshot,
    *,
    current_ts: datetime | None,
) -> StateMachineSnapshot:
    previous_routed_state = _sanitize_routed_state(
        previous_state.routed_state if previous_state is not None and previous_state.routed_state else (
            previous_state.current_state if previous_state is not None else "range_unclear"
        )
    )
    freeze_increment = bool(
        previous_state is not None and previous_state.current_ts is not None and current_ts == previous_state.current_ts
    )
    candidate_state = _sanitize_routed_state(routed_regime.routed_state)
    counters = _bump_counters(
        previous_state.confirmation_counters if previous_state is not None else None,
        candidate_state,
        previous_routed_state,
        freeze_increment,
    )

    if routed_regime.emergency_trigger:
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state="emergency",
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=DEFAULT_ROUTED_COOLDOWN_FAST_UPDATES,
            transition_reason=list(routed_regime.transition_reason or ["emergency_override"]),
            transition_applied=previous_routed_state != "emergency",
            slow_state=routed_regime.slow_state,
            slow_state_memory=routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state="emergency",
            current_ts=current_ts,
            last_confirmed_ts=current_ts,
        )

    if routed_regime.mid_state is not None and candidate_state == routed_regime.mid_state:
        current_counter = counters.get(candidate_state, 0)
        required = ROUTED_REQUIRED_CONFIRMATIONS.get(candidate_state, 1)
        transition_reason = list(routed_regime.transition_reason or [f"mid_priority:{candidate_state}"])
        if current_counter < required:
            transition_reason.append(f"mid_signal_preserved:{current_counter}/{required}")
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state=candidate_state,
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=0,
            transition_reason=transition_reason,
            transition_applied=candidate_state != previous_routed_state,
            slow_state=routed_regime.slow_state,
            slow_state_memory=previous_state.slow_state_memory if previous_state is not None else routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state=candidate_state,
            current_ts=current_ts,
            last_confirmed_ts=current_ts if candidate_state != previous_routed_state else (
                previous_state.last_confirmed_ts if previous_state is not None else None
            ),
        )

    if freeze_increment:
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state=previous_routed_state,
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=(
                previous_state.cooldown_remaining_fast_updates if previous_state is not None else 0
            ),
            transition_reason=["same_ts_guard"],
            transition_applied=False,
            slow_state=routed_regime.slow_state,
            slow_state_memory=previous_state.slow_state_memory if previous_state is not None else routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state=previous_routed_state,
            current_ts=current_ts,
            last_confirmed_ts=previous_state.last_confirmed_ts if previous_state is not None else None,
        )

    cooldown_remaining = previous_state.cooldown_remaining_fast_updates if previous_state is not None else 0
    if cooldown_remaining > 0 and candidate_state != previous_routed_state:
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state=previous_routed_state,
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=max(cooldown_remaining - 1, 0),
            transition_reason=["cooldown_active"],
            transition_applied=False,
            slow_state=routed_regime.slow_state,
            slow_state_memory=previous_state.slow_state_memory if previous_state is not None else routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state=previous_routed_state,
            current_ts=current_ts,
            last_confirmed_ts=previous_state.last_confirmed_ts if previous_state is not None else None,
        )

    if not _allowed_transition(previous_routed_state, candidate_state):
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state=previous_routed_state,
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=max(cooldown_remaining - 1, 0) if cooldown_remaining > 0 else 0,
            transition_reason=[f"blocked_transition:{previous_routed_state}->{candidate_state}"],
            transition_applied=False,
            slow_state=routed_regime.slow_state,
            slow_state_memory=previous_state.slow_state_memory if previous_state is not None else routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state=previous_routed_state,
            current_ts=current_ts,
            last_confirmed_ts=previous_state.last_confirmed_ts if previous_state is not None else None,
        )

    required = ROUTED_REQUIRED_CONFIRMATIONS.get(candidate_state, 1)
    current_counter = counters.get(candidate_state, 0)
    if candidate_state != previous_routed_state and current_counter < required:
        return StateMachineSnapshot(
            previous_state=previous_routed_state,
            current_state=previous_routed_state,
            confirmation_counters=counters,
            cooldown_remaining_fast_updates=0,
            transition_reason=[f"awaiting_confirmation:{candidate_state}:{current_counter}/{required}"],
            transition_applied=False,
            slow_state=routed_regime.slow_state,
            slow_state_memory=previous_state.slow_state_memory if previous_state is not None else routed_regime.slow_state,
            slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
            slow_bias=previous_state.slow_bias if previous_state is not None else 0,
            mid_state=routed_regime.mid_state,
            fast_state=routed_regime.fast_state,
            routed_state=previous_routed_state,
            current_ts=current_ts,
            last_confirmed_ts=previous_state.last_confirmed_ts if previous_state is not None else None,
        )

    transition_applied = candidate_state != previous_routed_state
    if transition_applied:
        for state in counters:
            if state != candidate_state:
                counters[state] = 0

    return StateMachineSnapshot(
        previous_state=previous_routed_state,
        current_state=candidate_state,
        confirmation_counters=counters,
        cooldown_remaining_fast_updates=(
            DEFAULT_ROUTED_COOLDOWN_FAST_UPDATES if transition_applied else 0
        ),
        transition_reason=list(routed_regime.transition_reason or [f"candidate:{candidate_state}"]),
        transition_applied=transition_applied,
        slow_state=routed_regime.slow_state,
        slow_state_memory=routed_regime.slow_state,
        slow_transition_counter=previous_state.slow_transition_counter if previous_state is not None else 0,
        slow_bias=previous_state.slow_bias if previous_state is not None else 0,
        mid_state=routed_regime.mid_state,
        fast_state=routed_regime.fast_state,
        routed_state=candidate_state,
        current_ts=current_ts,
        last_confirmed_ts=current_ts if transition_applied else (
            previous_state.last_confirmed_ts if previous_state is not None else None
        ),
    )


def apply_state_machine(
    previous_state: str,
    regime_snapshot: RegimeSnapshot,
    confirmation_counters: dict[str, int] | None = None,
    cooldown_remaining_fast_updates: int = 0,
) -> StateMachineSnapshot:
    candidate_state = regime_snapshot.candidate_states[0] if regime_snapshot.candidate_states else previous_state
    routed = RoutedRegimeSnapshot(
        slow_state="slow_range_neutral",
        mid_state=None,
        fast_state="fast_neutral",
        routed_state=LEGACY_TO_ROUTED.get(candidate_state, "range_unclear"),
        transition_reason=list(regime_snapshot.transition_reason),
        emergency_trigger=regime_snapshot.emergency_trigger,
    )
    previous_snapshot = StateMachineSnapshot(
        previous_state=LEGACY_TO_ROUTED.get(previous_state, "range_unclear"),
        current_state=LEGACY_TO_ROUTED.get(previous_state, "range_unclear"),
        confirmation_counters=confirmation_counters or {},
        cooldown_remaining_fast_updates=cooldown_remaining_fast_updates,
        routed_state=LEGACY_TO_ROUTED.get(previous_state, "range_unclear"),
    )
    return apply_routed_state_machine(previous_snapshot, routed, current_ts=None)
