"""Causal breakout state machine for a known reference level."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .models import BreakoutState, EdgeSide


@dataclass
class StateTransition:
    state: BreakoutState
    at_utc: datetime
    mid: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "at_utc": self.at_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mid": self.mid,
            "note": self.note,
        }


@dataclass
class StateMachineResult:
    transitions: list[StateTransition] = field(default_factory=list)
    final_state: BreakoutState = BreakoutState.UNRESOLVED
    accepted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "transitions": [t.to_dict() for t in self.transitions],
            "final_state": self.final_state.value,
            "accepted": self.accepted,
        }


def run_breakout_state_machine(
    *,
    side: EdgeSide,
    reference_price: float,
    mid_series: Sequence[tuple[datetime, float]],
    touch_tolerance: float = 5.0,
    break_persist_samples: int = 30,
    accept_persist_samples: int = 50,
) -> StateMachineResult:
    """Drive APPROACH→… using 100ms-like mid samples.

    A single $5 excursion is not enough for ACCEPTED_BREAK — persistence required.
    """
    result = StateMachineResult()
    if not mid_series:
        return result

    beyond_count = 0
    reclaim_seen = False
    broken = False

    def beyond(mid: float) -> bool:
        if side == EdgeSide.UPPER:
            return mid > reference_price + touch_tolerance
        return mid < reference_price - touch_tolerance

    def touching(mid: float) -> bool:
        return abs(mid - reference_price) <= touch_tolerance

    def inside(mid: float) -> bool:
        if side == EdgeSide.UPPER:
            return mid < reference_price - touch_tolerance
        return mid > reference_price + touch_tolerance

    state = BreakoutState.APPROACH
    result.transitions.append(
        StateTransition(state, mid_series[0][0], mid_series[0][1], "init")
    )

    for ts, mid in mid_series:
        if state == BreakoutState.APPROACH:
            if touching(mid):
                state = BreakoutState.TOUCH
                result.transitions.append(StateTransition(state, ts, mid, "touch"))
            elif abs(mid - reference_price) <= touch_tolerance * 4:
                state = BreakoutState.ATTACK
                result.transitions.append(StateTransition(state, ts, mid, "attack"))
        elif state == BreakoutState.ATTACK:
            if touching(mid):
                state = BreakoutState.TOUCH
                result.transitions.append(StateTransition(state, ts, mid, "touch"))
        elif state == BreakoutState.TOUCH:
            if beyond(mid):
                beyond_count = 1
                state = BreakoutState.BREAK
                broken = True
                result.transitions.append(StateTransition(state, ts, mid, "break"))
            elif inside(mid):
                state = BreakoutState.HOLD
                result.transitions.append(StateTransition(state, ts, mid, "hold_inside"))
        elif state in {BreakoutState.HOLD, BreakoutState.RECLAIM}:
            if beyond(mid):
                beyond_count = 1
                state = BreakoutState.BREAK
                broken = True
                result.transitions.append(StateTransition(state, ts, mid, "break"))
            elif touching(mid) and state == BreakoutState.HOLD:
                pass
            elif inside(mid) and broken:
                state = BreakoutState.RECLAIM
                reclaim_seen = True
                result.transitions.append(StateTransition(state, ts, mid, "reclaim"))
        elif state == BreakoutState.BREAK:
            if beyond(mid):
                beyond_count += 1
                if beyond_count >= accept_persist_samples:
                    state = BreakoutState.ACCEPTED_BREAK
                    result.transitions.append(
                        StateTransition(state, ts, mid, f"persist>={accept_persist_samples}")
                    )
            elif touching(mid) or inside(mid):
                if beyond_count < break_persist_samples:
                    state = BreakoutState.FAILED_BREAK
                    result.transitions.append(
                        StateTransition(state, ts, mid, "failed_short_persist")
                    )
                else:
                    state = BreakoutState.RECLAIM
                    reclaim_seen = True
                    result.transitions.append(StateTransition(state, ts, mid, "reclaim_after_break"))
        elif state == BreakoutState.ACCEPTED_BREAK:
            if inside(mid):
                state = BreakoutState.RECLAIM
                reclaim_seen = True
                result.transitions.append(StateTransition(state, ts, mid, "reclaim_after_accept"))
        elif state == BreakoutState.FAILED_BREAK:
            if beyond(mid):
                beyond_count = 1
                state = BreakoutState.BREAK
                result.transitions.append(StateTransition(state, ts, mid, "rebreak"))

    result.final_state = state
    result.accepted = state == BreakoutState.ACCEPTED_BREAK and not reclaim_seen
    return result
