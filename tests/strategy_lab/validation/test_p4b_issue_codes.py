"""P4B ValidationIssueCode coverage tests."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.models import (
    ComponentReference,
    ResetRule,
    RuleComponentSpec,
    SideName,
    SideRuleBundle,
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
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from tests.strategy_lab.validation.conftest import (
    catalogs,
    sid,
    valid_rule_based_long_strategy,
    valid_state_machine_long_strategy,
)
from tests.strategy_lab.v2_fixtures import _comparison, rule_based_signal_v2, state_machine_signal_v2


P4B_ISSUE_CODES: frozenset[ValidationIssueCode] = frozenset(
    code
    for code in ValidationIssueCode
    if code.name.startswith(("COMPONENT_", "RULE_", "SM_"))
)


def test_p4b_issue_code_inventory() -> None:
    assert len(P4B_ISSUE_CODES) == 26


def _emit(code: ValidationIssueCode, catalogs: CatalogBundleV2) -> None:
    if code is ValidationIssueCode.COMPONENT_DUPLICATE_ID:
        component = RuleComponentSpec(
            component_id=sid("gate"),
            description="gate",
            root=_comparison(),
        )
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.LONG),
            components=(component, component),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE:
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.LONG),
            long=SideRuleBundle(
                side=SideName.LONG,
                setup=None,
                trigger=ComponentReference(component_id=sid("missing")),
                confirmation=None,
                invalidation=None,
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.COMPONENT_CYCLE:
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.LONG),
            components=(
                RuleComponentSpec(
                    component_id=sid("a"),
                    description="a",
                    root=ComponentReference(component_id=sid("a")),
                ),
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_LONG_BUNDLE_MISSING:
        signal = dataclasses.replace(rule_based_signal_v2(Directionality.LONG), long=None)
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_LONG_BUNDLE_UNEXPECTED:
        signal = rule_based_signal_v2(Directionality.SHORT)
        signal = dataclasses.replace(
            signal,
            long=SideRuleBundle(
                side=SideName.LONG,
                setup=None,
                trigger=_comparison(),
                confirmation=None,
                invalidation=None,
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_SHORT_BUNDLE_MISSING:
        signal = dataclasses.replace(rule_based_signal_v2(Directionality.BOTH), short=None)
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_SHORT_BUNDLE_UNEXPECTED:
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.LONG),
            short=SideRuleBundle(
                side=SideName.SHORT,
                setup=None,
                trigger=_comparison(),
                confirmation=None,
                invalidation=None,
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_LONG_SIDE_MISMATCH:
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.LONG),
            long=SideRuleBundle(
                side=SideName.SHORT,
                setup=None,
                trigger=_comparison(),
                confirmation=None,
                invalidation=None,
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.RULE_SHORT_SIDE_MISMATCH:
        signal = dataclasses.replace(
            rule_based_signal_v2(Directionality.SHORT),
            short=SideRuleBundle(
                side=SideName.LONG,
                setup=None,
                trigger=_comparison(),
                confirmation=None,
                invalidation=None,
            ),
        )
        spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_DUPLICATE_STATE_ID:
        sm = state_machine_signal_v2()
        signal = dataclasses.replace(sm, states=(sm.states[0], sm.states[0]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_INITIAL_STATE_UNKNOWN:
        signal = dataclasses.replace(state_machine_signal_v2(), initial_state=sid("missing"))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_DUPLICATE_TRANSITION_ID:
        sm = state_machine_signal_v2()
        duplicate = sm.transitions[0]
        signal = dataclasses.replace(sm, transitions=(duplicate, duplicate))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_DUPLICATE_TIMEOUT_ID:
        sm = state_machine_signal_v2()
        timeout = TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("idle"),
            after_bars=5,
            to_state=sid("armed"),
            priority=3,
        )
        signal = dataclasses.replace(sm, timeouts=(timeout, timeout))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_UNKNOWN_FROM_STATE:
        sm = state_machine_signal_v2()
        transition = dataclasses.replace(sm.transitions[0], from_state=sid("missing"))
        signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_UNKNOWN_TO_STATE:
        sm = state_machine_signal_v2()
        transition = dataclasses.replace(sm.transitions[0], to_state=sid("missing"))
        signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_UNKNOWN_TIMEOUT_STATE:
        sm = state_machine_signal_v2()
        timeout = TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("missing"),
            after_bars=5,
            to_state=sid("armed"),
            priority=3,
        )
        signal = dataclasses.replace(sm, timeouts=(timeout,))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_UNKNOWN_TIMEOUT_TARGET:
        sm = state_machine_signal_v2()
        timeout = TimeoutTransitionSpec(
            timeout_id=sid("t1"),
            in_state=sid("idle"),
            after_bars=5,
            to_state=sid("missing"),
            priority=3,
        )
        signal = dataclasses.replace(sm, timeouts=(timeout,))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_UNREACHABLE_STATE:
        sm = state_machine_signal_v2()
        orphan = StateSpec(state_id=sid("orphan"), description="orphan")
        signal = dataclasses.replace(sm, states=sm.states + (orphan,))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_DUPLICATE_PRIORITY:
        sm = state_machine_signal_v2()
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
    elif code is ValidationIssueCode.SM_DUPLICATE_RESET_EVENT:
        sm = dataclasses.replace(
            state_machine_signal_v2(),
            reset_rules=(
                ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("idle")),
                ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("idle")),
            ),
        )
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    elif code is ValidationIssueCode.SM_UNKNOWN_RESET_TARGET:
        sm = dataclasses.replace(
            state_machine_signal_v2(),
            reset_rules=(ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=sid("missing")),),
        )
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    elif code is ValidationIssueCode.SM_INVALIDATION_WITH_EMISSION:
        sm = state_machine_signal_v2()
        transition = dataclasses.replace(
            sm.transitions[1],
            emission=SignalEmissionSpec(side=SideName.LONG, emission_id=sid("bad")),
        )
        signal = dataclasses.replace(sm, transitions=(sm.transitions[0], transition))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_EMISSION_SIDE_UNSUPPORTED:
        sm = state_machine_signal_v2()
        transition = dataclasses.replace(
            sm.transitions[0],
            emission=SignalEmissionSpec(side=SideName.SHORT, emission_id=sid("bad")),
        )
        signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_LONG_EMISSION_MISSING:
        sm = state_machine_signal_v2()
        transition = dataclasses.replace(sm.transitions[0], emission=None)
        signal = dataclasses.replace(sm, transitions=(transition, sm.transitions[1]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_SHORT_EMISSION_MISSING:
        sm = dataclasses.replace(state_machine_signal_v2(), directionality=Directionality.BOTH)
        signal = dataclasses.replace(sm, transitions=(sm.transitions[0], sm.transitions[1]))
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    elif code is ValidationIssueCode.SM_RESET_EVENT_WITHOUT_SOURCE:
        sm = dataclasses.replace(
            state_machine_signal_v2(),
            reset_rules=(ResetRule(event=ResetEvent.TIMEOUT, target_state=sid("idle")),),
        )
        spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=sm)
    else:
        raise AssertionError(f"no emitter for {code}")

    report = validate_strategy_v2_p4b(spec, catalogs)
    assert code in {issue.code for issue in report.issues}


@pytest.mark.parametrize("code", sorted(P4B_ISSUE_CODES, key=lambda item: item.value))
def test_p4b_active_issue_code_is_emitted(code: ValidationIssueCode, catalogs) -> None:
    _emit(code, catalogs)
