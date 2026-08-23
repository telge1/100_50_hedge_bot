"""State machine signal tests for StrategySpec V2."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.models import (
    ResetEvent,
    ResetRule,
    StateMachineSignalSpec,
    StateSpec,
    TimeoutTransitionSpec,
    TransitionConflictPolicy,
    TransitionExecutionPolicy,
    TransitionPurpose,
    TransitionSpec,
)
from tests.strategy_lab.v2_fixtures import _comparison, sid, state_machine_signal_v2


def test_state_machine_requires_at_least_one_state() -> None:
    with pytest.raises(ValueError, match="at least one state"):
        StateMachineSignalSpec(
            directionality=state_machine_signal_v2().directionality,
            evaluation_timing=state_machine_signal_v2().evaluation_timing,
            initial_state=sid("idle"),
            states=(),
            transitions=(),
            transition_execution_policy=TransitionExecutionPolicy.ONE_PER_EVALUATION_BAR,
            transition_conflict_policy=TransitionConflictPolicy.ERROR_ON_MULTIPLE,
            reset_rules=(),
        )


def test_condition_transition_with_emission() -> None:
    signal = state_machine_signal_v2()
    normal = signal.transitions[0]
    assert normal.purpose is TransitionPurpose.NORMAL
    assert normal.emission is not None


def test_invalidation_transition() -> None:
    signal = state_machine_signal_v2()
    invalidation = signal.transitions[1]
    assert invalidation.purpose is TransitionPurpose.INVALIDATION
    assert invalidation.emission is None


def test_timeout_has_no_purpose_emission_or_reset_fields() -> None:
    names = {f.name for f in dataclasses.fields(TimeoutTransitionSpec)}
    assert "purpose" not in names
    assert "emission" not in names
    assert "reset" not in names
    assert "reset_machine" not in names


def test_timeout_after_bars_zero_rejected() -> None:
    with pytest.raises(ValueError, match="after_bars"):
        TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("armed"),
            after_bars=0,
            to_state=sid("idle"),
            priority=1,
        )


def test_timeout_after_bars_bool_rejected() -> None:
    with pytest.raises(TypeError, match="exact int"):
        TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("armed"),
            after_bars=True,  # type: ignore[arg-type]
            to_state=sid("idle"),
            priority=1,
        )


def test_transition_priority_zero_rejected() -> None:
    with pytest.raises(ValueError, match="priority"):
        TransitionSpec(
            transition_id=sid("t"),
            from_state=sid("a"),
            to_state=sid("b"),
            condition=_comparison(),
            priority=0,
            purpose=TransitionPurpose.NORMAL,
            emission=None,
        )


def test_transition_priority_bool_rejected() -> None:
    with pytest.raises(TypeError, match="exact int"):
        TransitionSpec(
            transition_id=sid("t"),
            from_state=sid("a"),
            to_state=sid("b"),
            condition=_comparison(),
            priority=True,  # type: ignore[arg-type]
            purpose=TransitionPurpose.NORMAL,
            emission=None,
        )


def test_both_conflict_policies() -> None:
    sm = state_machine_signal_v2()
    assert sm.transition_conflict_policy is TransitionConflictPolicy.PRIORITY_WINS
    sm2 = StateMachineSignalSpec(
        **{
            **{
                f.name: getattr(sm, f.name)
                for f in dataclasses.fields(StateMachineSignalSpec)
                if f.name != "transition_conflict_policy"
            },
            "transition_conflict_policy": TransitionConflictPolicy.ERROR_ON_MULTIPLE,
        }
    )
    assert sm2.transition_conflict_policy is TransitionConflictPolicy.ERROR_ON_MULTIPLE


def test_only_one_per_evaluation_bar_policy() -> None:
    assert list(TransitionExecutionPolicy) == [
        TransitionExecutionPolicy.ONE_PER_EVALUATION_BAR
    ]


def test_multiple_reset_rules() -> None:
    signal = state_machine_signal_v2()
    events = {rule.event for rule in signal.reset_rules}
    assert ResetEvent.SIGNAL_EMITTED in events
    assert ResetEvent.INVALIDATED in events


def test_no_other_reset_field_on_state_machine() -> None:
    names = {f.name for f in dataclasses.fields(StateMachineSignalSpec)}
    assert "reset_rules" in names
    assert "reset_machine" not in names
    assert "reset" not in names


def test_timeout_transition_constructible() -> None:
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("timeout_armed"),
        in_state=sid("armed"),
        after_bars=3,
        to_state=sid("idle"),
        priority=1,
    )
    assert timeout.after_bars == 3


def test_transition_priority_negative_rejected() -> None:
    with pytest.raises(ValueError, match="priority"):
        TransitionSpec(
            transition_id=sid("t"),
            from_state=sid("a"),
            to_state=sid("b"),
            condition=_comparison(),
            priority=-1,
            purpose=TransitionPurpose.NORMAL,
            emission=None,
        )


def test_timeout_after_bars_negative_rejected() -> None:
    with pytest.raises(ValueError, match="after_bars"):
        TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("armed"),
            after_bars=-1,
            to_state=sid("idle"),
            priority=1,
        )
