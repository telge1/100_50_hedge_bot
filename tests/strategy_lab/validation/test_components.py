"""P4B component reference and cycle validation tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import (
    ComparisonExpression,
    ComponentReference,
    RuleComponentSpec,
    SideName,
    SideRuleBundle,
)
from orderbook_analyse.strategy_lab.models.enums import Directionality, EvaluationTiming
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion
from orderbook_analyse.strategy_lab.models.signals import RuleBasedSignalSpec
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4b,
)
from tests.strategy_lab.validation.conftest import (
    catalogs,
    edc_features,
    sid,
    valid_rule_based_long_strategy,
    valid_state_machine_long_strategy,
)
from tests.strategy_lab.v2_fixtures import _comparison


def _comparison_trigger() -> ComparisonExpression:
    return _comparison()


def test_valid_component_reference(catalogs) -> None:
    trigger = ComponentReference(component_id=sid("gate"))
    signal = RuleBasedSignalSpec(
        operator_contract_version=ContractVersion(value="catalog/v2"),
        directionality=Directionality.LONG,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        components=(
            RuleComponentSpec(
                component_id=sid("gate"),
                description="gate",
                root=_comparison_trigger(),
            ),
        ),
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        ),
        short=None,
    )
    spec = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=signal,
        features=edc_features(),
    )
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_unknown_component_reference(catalogs) -> None:
    spec = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=dataclasses.replace(
            valid_rule_based_long_strategy().signal,
            long=SideRuleBundle(
                side=SideName.LONG,
                setup=None,
                trigger=ComponentReference(component_id=sid("missing")),
                confirmation=None,
                invalidation=None,
            ),
        ),
    )
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE in {i.code for i in report.issues}


def test_duplicate_component_id(catalogs) -> None:
    component = RuleComponentSpec(
        component_id=sid("gate"),
        description="gate",
        root=_comparison_trigger(),
    )
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        components=(component, component),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.COMPONENT_DUPLICATE_ID in {i.code for i in report.issues}


def test_self_cycle(catalogs) -> None:
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        components=(
            RuleComponentSpec(
                component_id=sid("a"),
                description="self",
                root=ComponentReference(component_id=sid("a")),
            ),
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.COMPONENT_CYCLE in codes
    assert ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE not in codes


def test_indirect_cycle(catalogs) -> None:
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        components=(
            RuleComponentSpec(
                component_id=sid("a"),
                description="a",
                root=ComponentReference(component_id=sid("b")),
            ),
            RuleComponentSpec(
                component_id=sid("b"),
                description="b",
                root=ComponentReference(component_id=sid("a")),
            ),
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.COMPONENT_CYCLE in {i.code for i in report.issues}


def test_two_independent_cycles(catalogs) -> None:
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        components=(
            RuleComponentSpec(
                component_id=sid("a"),
                description="a",
                root=ComponentReference(component_id=sid("b")),
            ),
            RuleComponentSpec(
                component_id=sid("b"),
                description="b",
                root=ComponentReference(component_id=sid("a")),
            ),
            RuleComponentSpec(
                component_id=sid("c"),
                description="c",
                root=ComponentReference(component_id=sid("d")),
            ),
            RuleComponentSpec(
                component_id=sid("d"),
                description="d",
                root=ComponentReference(component_id=sid("c")),
            ),
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    cycle_issues = [
        issue for issue in report.issues if issue.code is ValidationIssueCode.COMPONENT_CYCLE
    ]
    assert len(cycle_issues) == 2


def test_cycle_diagnosis_order_independent(catalogs) -> None:
    components_fwd = (
        RuleComponentSpec(
            component_id=sid("a"),
            description="a",
            root=ComponentReference(component_id=sid("b")),
        ),
        RuleComponentSpec(
            component_id=sid("b"),
            description="b",
            root=ComponentReference(component_id=sid("a")),
        ),
    )
    components_rev = (components_fwd[1], components_fwd[0])
    spec_fwd = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=dataclasses.replace(
            valid_rule_based_long_strategy().signal,
            components=components_fwd,
        ),
    )
    spec_rev = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=dataclasses.replace(
            valid_rule_based_long_strategy().signal,
            components=components_rev,
        ),
    )
    report_fwd = validate_strategy_v2_p4b(spec_fwd, catalogs)
    report_rev = validate_strategy_v2_p4b(spec_rev, catalogs)
    cycles_fwd = [
        issue for issue in report_fwd.issues if issue.code is ValidationIssueCode.COMPONENT_CYCLE
    ]
    cycles_rev = [
        issue for issue in report_rev.issues if issue.code is ValidationIssueCode.COMPONENT_CYCLE
    ]
    assert len(cycles_fwd) == 1
    assert len(cycles_rev) == 1
    assert cycles_fwd[0].context == cycles_rev[0].context


def test_unknown_reference_does_not_emit_cycle(catalogs) -> None:
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        components=(
            RuleComponentSpec(
                component_id=sid("a"),
                description="a",
                root=ComponentReference(component_id=sid("missing")),
            ),
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE in codes
    assert ValidationIssueCode.COMPONENT_CYCLE not in codes


def test_transition_condition_component_reference(catalogs) -> None:
    sm = valid_state_machine_long_strategy().signal
    transition = sm.transitions[0]
    broken_transition = dataclasses.replace(
        transition,
        condition=ComponentReference(component_id=sid("missing")),
    )
    signal = dataclasses.replace(
        sm,
        transitions=(broken_transition, sm.transitions[1]),
    )
    spec = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE in {i.code for i in report.issues}


def test_bundle_invalidation_component_reference(catalogs) -> None:
    signal = dataclasses.replace(
        valid_rule_based_long_strategy().signal,
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=_comparison_trigger(),
            confirmation=None,
            invalidation=ComponentReference(component_id=sid("missing")),
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE
        and "invalidation" in issue.path
        for issue in report.issues
    )
