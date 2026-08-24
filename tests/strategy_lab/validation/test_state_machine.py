"""P4B state-machine graph validation tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import (
    ResetRule,
    SideName,
    SignalEmissionSpec,
    StateSpec,
    TimeoutTransitionSpec,
    TransitionSpec,
)
from orderbook_analyse.strategy_lab.models.enums import (
    Directionality,
    ResetEvent,
    TransitionPurpose,
)
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4b,
)
from tests.strategy_lab.validation.conftest import (
    catalogs,
    sid,
    valid_state_machine_long_strategy,
)
from tests.strategy_lab.v2_fixtures import _comparison, state_machine_signal_v2


def _long_sm():
    return state_machine_signal_v2()


def _short_sm():
    sm = _long_sm()
    idle = sid("idle")
    armed = sid("armed")
    return dataclasses.replace(
        sm,
        directionality=Directionality.SHORT,
        transitions=(
            dataclasses.replace(
                sm.transitions[0],
                emission=SignalEmissionSpec(
                    side=SideName.SHORT,
                    emission_id=sid("entry_short"),
                ),
            ),
            sm.transitions[1],
        ),
    )


def _both_sm():
    sm = _long_sm()
    short_transition = TransitionSpec(
        transition_id=sid("short_entry"),
        from_state=sid("armed"),
        to_state=sid("idle"),
        condition=_comparison(),
        priority=3,
        purpose=TransitionPurpose.NORMAL,
        emission=SignalEmissionSpec(
            side=SideName.SHORT,
            emission_id=sid("entry_short"),
        ),
    )
    return dataclasses.replace(
        sm,
        directionality=Directionality.BOTH,
        transitions=sm.transitions + (short_transition,),
    )


def test_valid_long_graph(catalogs) -> None:
    spec = valid_state_machine_long_strategy()
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_valid_short_graph(catalogs) -> None:
    spec = dataclasses.replace(
        valid_state_machine_long_strategy(),
        signal=_short_sm(),
    )
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_valid_both_graph(catalogs) -> None:
    spec = dataclasses.replace(
        valid_state_machine_long_strategy(),
        signal=_both_sm(),
    )
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_duplicate_state_id(catalogs) -> None:
    sm = _long_sm()
    duplicate = StateSpec(state_id=sid("idle"), description="dup")
    signal = dataclasses.replace(sm, states=(sm.states[0], duplicate))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_DUPLICATE_STATE_ID in {i.code for i in report.issues}


def test_unknown_initial_state(catalogs) -> None:
    sm = dataclasses.replace(_long_sm(), initial_state=sid("missing"))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.SM_INITIAL_STATE_UNKNOWN in codes
    assert ValidationIssueCode.SM_UNREACHABLE_STATE not in codes


def test_duplicate_transition_id(catalogs) -> None:
    sm = _long_sm()
    duplicate = sm.transitions[0]
    signal = dataclasses.replace(sm, transitions=(duplicate, duplicate))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_DUPLICATE_TRANSITION_ID in {i.code for i in report.issues}


def test_duplicate_timeout_id(catalogs) -> None:
    sm = _long_sm()
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("t1"),
        in_state=sid("idle"),
        after_bars=5,
        to_state=sid("armed"),
        priority=3,
    )
    signal = dataclasses.replace(sm, timeouts=(timeout, timeout))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_DUPLICATE_TIMEOUT_ID in {i.code for i in report.issues}


def test_transition_and_timeout_ids_share_no_namespace(catalogs) -> None:
    sm = _long_sm()
    shared_id = sid("shared")
    transition = dataclasses.replace(sm.transitions[0], transition_id=shared_id)
    timeout = TimeoutTransitionSpec(
        timeout_id=shared_id,
        in_state=sid("idle"),
        after_bars=5,
        to_state=sid("armed"),
        priority=3,
    )
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]), timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.SM_DUPLICATE_TRANSITION_ID not in codes
    assert ValidationIssueCode.SM_DUPLICATE_TIMEOUT_ID not in codes


def test_unknown_from_state(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(sm.transitions[0], from_state=sid("missing"))
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNKNOWN_FROM_STATE in {i.code for i in report.issues}


def test_unknown_to_state(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(sm.transitions[0], to_state=sid("missing"))
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNKNOWN_TO_STATE in {i.code for i in report.issues}


def test_unknown_timeout_state(catalogs) -> None:
    sm = _long_sm()
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("t1"),
        in_state=sid("missing"),
        after_bars=5,
        to_state=sid("armed"),
        priority=3,
    )
    signal = dataclasses.replace(sm, timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNKNOWN_TIMEOUT_STATE in {i.code for i in report.issues}


def test_unknown_timeout_target(catalogs) -> None:
    sm = _long_sm()
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("t1"),
        in_state=sid("idle"),
        after_bars=5,
        to_state=sid("missing"),
        priority=3,
    )
    signal = dataclasses.replace(sm, timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNKNOWN_TIMEOUT_TARGET in {i.code for i in report.issues}


def test_unreachable_state(catalogs) -> None:
    sm = _long_sm()
    orphan = StateSpec(state_id=sid("orphan"), description="orphan")
    signal = dataclasses.replace(sm, states=sm.states + (orphan,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNREACHABLE_STATE in {i.code for i in report.issues}


def test_combined_transition_timeout_priority_conflict(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(sm.transitions[0], priority=5)
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("t1"),
        in_state=sid("idle"),
        after_bars=5,
        to_state=sid("armed"),
        priority=5,
    )
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]), timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_DUPLICATE_PRIORITY in {i.code for i in report.issues}


def test_unknown_from_state_no_priority_issue(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(
        sm.transitions[0],
        from_state=sid("missing"),
        priority=5,
    )
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("t1"),
        in_state=sid("idle"),
        after_bars=5,
        to_state=sid("armed"),
        priority=5,
    )
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]), timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.SM_UNKNOWN_FROM_STATE in codes
    assert ValidationIssueCode.SM_DUPLICATE_PRIORITY not in codes


def test_invalidation_with_emission(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(
        sm.transitions[1],
        emission=SignalEmissionSpec(side=SideName.LONG, emission_id=sid("bad")),
    )
    signal = dataclasses.replace(sm, transitions=(sm.transitions[0], transition))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_INVALIDATION_WITH_EMISSION in {i.code for i in report.issues}


def test_unsupported_emission_side(catalogs) -> None:
    sm = _long_sm()
    transition = dataclasses.replace(
        sm.transitions[0],
        emission=SignalEmissionSpec(side=SideName.SHORT, emission_id=sid("bad")),
    )
    signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.SM_EMISSION_SIDE_UNSUPPORTED in codes
    assert ValidationIssueCode.SM_LONG_EMISSION_MISSING in codes


def test_missing_long_emission(catalogs) -> None:
    sm = dataclasses.replace(
        _long_sm(),
        transitions=(
            dataclasses.replace(_long_sm().transitions[0], emission=None),
            _long_sm().transitions[1],
        ),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_LONG_EMISSION_MISSING in {i.code for i in report.issues}


def test_missing_short_emission(catalogs) -> None:
    sm = _both_sm()
    short_transition = next(
        transition
        for transition in sm.transitions
        if transition.emission is not None and transition.emission.side is SideName.SHORT
    )
    broken_short = dataclasses.replace(short_transition, emission=None)
    signal = dataclasses.replace(
        sm,
        transitions=tuple(
            broken_short if transition.transition_id == short_transition.transition_id else transition
            for transition in sm.transitions
        ),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_SHORT_EMISSION_MISSING in {i.code for i in report.issues}


def test_duplicate_reset_event(catalogs) -> None:
    sm = dataclasses.replace(
        _long_sm(),
        reset_rules=(
            ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("idle")),
            ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("idle")),
        ),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_DUPLICATE_RESET_EVENT in {i.code for i in report.issues}


def test_unknown_reset_target(catalogs) -> None:
    sm = dataclasses.replace(
        _long_sm(),
        reset_rules=(ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("missing")),),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.SM_UNKNOWN_RESET_TARGET in codes
    assert ValidationIssueCode.SM_UNREACHABLE_STATE not in {
        issue.code for issue in report.issues if "missing" in issue.message
    }


def test_reset_event_without_source(catalogs) -> None:
    sm = dataclasses.replace(
        _long_sm(),
        reset_rules=(ResetRule(event=ResetEvent.TIMEOUT, target_state=sid("idle")),),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_RESET_EVENT_WITHOUT_SOURCE in {i.code for i in report.issues}


def test_timeout_is_reachability_edge(catalogs) -> None:
    sm = _long_sm()
    isolated = StateSpec(state_id=sid("isolated"), description="isolated")
    timeout = TimeoutTransitionSpec(
        timeout_id=sid("to_isolated"),
        in_state=sid("armed"),
        after_bars=5,
        to_state=sid("isolated"),
        priority=3,
    )
    signal = dataclasses.replace(sm, states=sm.states + (isolated,), timeouts=(timeout,))
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNREACHABLE_STATE not in {i.code for i in report.issues}


def test_reset_not_reachability_edge(catalogs) -> None:
    sm = _long_sm()
    unreachable = StateSpec(state_id=sid("reset_only"), description="reset only")
    signal = dataclasses.replace(
        sm,
        states=sm.states + (unreachable,),
        reset_rules=(
            ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("reset_only")),
            *_long_sm().reset_rules,
        ),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.SM_UNREACHABLE_STATE in {i.code for i in report.issues}
