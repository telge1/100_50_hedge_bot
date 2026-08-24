"""Rule-tree and operand typing tests."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    BooleanAndExpression,
    BooleanNotExpression,
    BooleanOrExpression,
    ComparisonExpression,
    ComponentReference,
    ContractVersion,
    DecimalParam,
    Directionality,
    EvaluationTiming,
    FeatureOutputReference,
    IntParam,
    LiteralOperand,
    RateParam,
    RateValue,
    RuleBasedSignalSpec,
    RuleComponentSpec,
    SideName,
    SideRuleBundle,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from tests.strategy_lab.v2_fixtures import sid, state_machine_signal_v2
from tests.strategy_lab.validation.conftest import (
    catalogs,
    edc_features,
    sid,
    valid_cluster_strategy,
    valid_edc_strategy,
)


def _ema_ref(alias: str, output: str = "value") -> FeatureOutputReference:
    return FeatureOutputReference(
        feature_alias=sid(alias),
        output_id=sid(output),
    )


def _rule_based(**kwargs) -> RuleBasedSignalSpec:
    base = {
        "operator_contract_version": ContractVersion(value="catalog/v2"),
        "directionality": Directionality.LONG,
        "evaluation_timing": EvaluationTiming.SIGNAL_BAR_CLOSE,
        "long": None,
        "short": None,
    }
    base.update(kwargs)
    return RuleBasedSignalSpec(**base)


def test_valid_crosses_above(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("crosses_above"),
        left=_ema_ref("ema_fast"),
        right=_ema_ref("ema_slow"),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert report.is_valid


def test_valid_gt_ema_ema(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=_ema_ref("ema_fast"),
        right=_ema_ref("ema_slow"),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_valid_gt_ema_decimal(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=_ema_ref("ema_fast"),
        right=LiteralOperand(value=DecimalParam(value=Decimal("0"))),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_valid_gt_ema_int(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=_ema_ref("ema_fast"),
        right=LiteralOperand(value=IntParam(value=0)),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_rate_param_rejected(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=_ema_ref("ema_fast"),
        right=LiteralOperand(
            value=RateParam(
                value=RateValue(value=Decimal("1"), unit=RateUnit.PERCENT)
            )
        ),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH
        for issue in report.issues
    )


def test_unknown_operator(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("unknown_op"),
        left=_ema_ref("ema_fast"),
        right=_ema_ref("ema_slow"),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(issue.code is ValidationIssueCode.OPERATOR_UNKNOWN for issue in report.issues)


def test_unknown_feature_alias_no_signature_cascade(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("missing"),
            output_id=sid("value"),
        ),
        right=_ema_ref("ema_slow"),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_ALIAS in codes
    assert ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH not in codes


def test_unknown_feature_output(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("ema_fast"),
            output_id=sid("missing"),
        ),
        right=_ema_ref("ema_slow"),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_OUTPUT
        for issue in report.issues
    )


def test_operator_contract_version_required(catalogs) -> None:
    from orderbook_analyse.strategy_lab.validation import validate_strategy_v2_p4a
    from tests.strategy_lab.v2_fixtures import rule_based_signal_v2

    signal = dataclasses.replace(
        rule_based_signal_v2(),
        operator_contract_version=ContractVersion(value="catalog/v1"),
    )
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=signal,
        features=edc_features(),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.OPERATOR_CONTRACT_VERSION
        for issue in report.issues
    )


def test_nested_boolean_operators(catalogs) -> None:
    leaf = ComparisonExpression(
        operator_id=sid("gt"),
        left=_ema_ref("ema_fast"),
        right=_ema_ref("ema_slow"),
    )
    trigger = BooleanNotExpression(
        operand=BooleanOrExpression(
            operands=(
                BooleanAndExpression(operands=(leaf, leaf)),
                leaf,
            )
        )
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_component_reference_accepted(catalogs) -> None:
    trigger = ComponentReference(component_id=sid("setup_gate"))
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        ),
        components=(
            RuleComponentSpec(
                component_id=sid("setup_gate"),
                description="gate",
                root=ComparisonExpression(
                    operator_id=sid("gt"),
                    left=_ema_ref("ema_fast"),
                    right=_ema_ref("ema_slow"),
                ),
            ),
        ),
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_transition_condition_validated(catalogs) -> None:
    sm = state_machine_signal_v2()
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=sm,
        features=edc_features(),
    )
    assert validate_strategy_v2_p4a(spec, catalogs).is_valid


def test_non_comparable_feature_output_type(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("clusters"),
            output_id=sid("snapshots"),
        ),
        right=FeatureOutputReference(
            feature_alias=sid("ema_slow"),
            output_id=sid("value"),
        ),
    )
    signal = _rule_based(
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    )
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH
        for issue in report.issues
    )
    assert not any(
        issue.code is ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_OUTPUT
        for issue in report.issues
    )
