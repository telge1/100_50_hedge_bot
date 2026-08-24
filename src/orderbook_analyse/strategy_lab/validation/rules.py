"""Rule-tree operand typing and operator signature validation for P4A."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from orderbook_analyse.strategy_lab.catalogs.v2.models import OperatorDescriptorV2
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    CollectionShape,
    FeatureOutputValueType,
    OperandOriginV2,
    OperandTypeConstraintV2,
    ParameterValueType,
    TemporalShape,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.param_mapping import (
    param_value_to_parameter_type,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.rules import (
    BooleanAndExpression,
    BooleanExpression,
    BooleanNotExpression,
    BooleanOrExpression,
    ComparisonExpression,
    ComponentReference,
    FeatureOutputReference,
    LiteralOperand,
    Operand,
    RuleComponentSpec,
)
from orderbook_analyse.strategy_lab.models.signals import (
    RuleBasedSignalSpec,
    SideRuleBundle,
    StateMachineSignalSpec,
)
from orderbook_analyse.strategy_lab.models.strategy import (
    DecimalParam,
    IntParam,
    ParamValue,
    RateParam,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    FeatureAliasContext,
    OperatorSignatureContext,
)
from orderbook_analyse.strategy_lab.validation.invariants import ValidationInvariantError
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)


class _ResolutionState(Enum):
    RESOLVED = auto()
    UNRESOLVED = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class _ResolvedOperand:
    state: _ResolutionState
    origin: OperandOriginV2 | None
    type_constraint: OperandTypeConstraintV2 | None
    temporal_shape: TemporalShape | None
    collection_shape: CollectionShape | None


def validate_rule_trees(
    *,
    signal: RuleBasedSignalSpec | StateMachineSignalSpec,
    index: FeatureResolutionIndex,
    catalogs: CatalogBundleV2,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    expected_version = _expected_operator_contract_version(catalogs)
    if signal.operator_contract_version.value != expected_version:
        issues.append(
            _error(
                ValidationIssueCode.OPERATOR_CONTRACT_VERSION,
                path="signal.operator_contract_version",
                message=(
                    f"operator contract version must be {expected_version!r}, got "
                    f"{signal.operator_contract_version.value!r}"
                ),
                context=None,
            )
        )
        return tuple(issues)

    if isinstance(signal, RuleBasedSignalSpec):
        if signal.long is not None:
            issues.extend(
                _validate_side_bundle("signal.long", signal.long, index, catalogs)
            )
        if signal.short is not None:
            issues.extend(
                _validate_side_bundle("signal.short", signal.short, index, catalogs)
            )
        for comp_index, component in enumerate(signal.components):
            issues.extend(
                _validate_component_root(
                    f"signal.components[{comp_index}]",
                    component,
                    index,
                    catalogs,
                )
            )

    if isinstance(signal, StateMachineSignalSpec):
        for transition_index, transition in enumerate(signal.transitions):
            issues.extend(
                _validate_expression(
                    f"signal.transitions[{transition_index}].condition",
                    transition.condition,
                    index,
                    catalogs,
                )
            )
        for comp_index, component in enumerate(signal.components):
            issues.extend(
                _validate_component_root(
                    f"signal.components[{comp_index}]",
                    component,
                    index,
                    catalogs,
                )
            )

    return tuple(issues)


def _validate_side_bundle(
    prefix: str,
    bundle: SideRuleBundle,
    index: FeatureResolutionIndex,
    catalogs: CatalogBundleV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if bundle.setup is not None:
        issues.extend(
            _validate_expression(f"{prefix}.setup", bundle.setup, index, catalogs)
        )
    issues.extend(
        _validate_expression(f"{prefix}.trigger", bundle.trigger, index, catalogs)
    )
    if bundle.confirmation is not None:
        issues.extend(
            _validate_expression(
                f"{prefix}.confirmation",
                bundle.confirmation,
                index,
                catalogs,
            )
        )
    if bundle.invalidation is not None:
        issues.extend(
            _validate_expression(
                f"{prefix}.invalidation",
                bundle.invalidation,
                index,
                catalogs,
            )
        )
    return issues


def _validate_component_root(
    prefix: str,
    component: RuleComponentSpec,
    index: FeatureResolutionIndex,
    catalogs: CatalogBundleV2,
) -> list[ValidationIssue]:
    return list(
        _validate_expression(f"{prefix}.root", component.root, index, catalogs)
    )


def _validate_expression(
    path: str,
    expression: BooleanExpression,
    index: FeatureResolutionIndex,
    catalogs: CatalogBundleV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if isinstance(expression, ComparisonExpression):
        left = _resolve_operand(
            f"{path}.left",
            expression.left,
            index,
            issues,
        )
        right = _resolve_operand(
            f"{path}.right",
            expression.right,
            index,
            issues,
        )
        if (
            left.state is _ResolutionState.UNRESOLVED
            or right.state is _ResolutionState.UNRESOLVED
        ):
            return issues

        try:
            operator = catalogs.operators.get(expression.operator_id.value)
        except Exception:
            issues.append(
                _error(
                    ValidationIssueCode.OPERATOR_UNKNOWN,
                    path=f"{path}.operator_id",
                    message=f"unknown operator {expression.operator_id.value!r}",
                    context=None,
                )
            )
            return issues

        issues.extend(
            _validate_operator_signature(
                path=path,
                operator=operator,
                left=left,
                right=right,
            )
        )
        return issues

    if isinstance(expression, BooleanAndExpression):
        for operand_index, operand in enumerate(expression.operands):
            issues.extend(
                _validate_expression(
                    f"{path}.operands[{operand_index}]",
                    operand,
                    index,
                    catalogs,
                )
            )
        return issues

    if isinstance(expression, BooleanOrExpression):
        for operand_index, operand in enumerate(expression.operands):
            issues.extend(
                _validate_expression(
                    f"{path}.operands[{operand_index}]",
                    operand,
                    index,
                    catalogs,
                )
            )
        return issues

    if isinstance(expression, BooleanNotExpression):
        issues.extend(
            _validate_expression(f"{path}.operand", expression.operand, index, catalogs)
        )
        return issues

    if isinstance(expression, ComponentReference):
        return issues

    return issues


def _resolve_operand(
    path: str,
    operand: Operand,
    index: FeatureResolutionIndex,
    issues: list[ValidationIssue],
) -> _ResolvedOperand:
    if isinstance(operand, LiteralOperand):
        return _resolve_literal(path, operand.value, issues)

    if isinstance(operand, FeatureOutputReference):
        if not index.has_alias(operand.feature_alias):
            issues.append(
                _error(
                    ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_ALIAS,
                    path=f"{path}.feature_alias",
                    message=f"unknown feature alias {operand.feature_alias.value!r}",
                    context=FeatureAliasContext(feature_alias=operand.feature_alias),
                )
            )
            return _unresolved()

        resolved = index.resolve_output(operand.feature_alias, operand.output_id)
        if resolved is None:
            issues.append(
                _error(
                    ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_OUTPUT,
                    path=f"{path}.output_id",
                    message=(
                        f"unknown output {operand.output_id.value!r} for feature alias "
                        f"{operand.feature_alias.value!r}"
                    ),
                    context=FeatureAliasContext(feature_alias=operand.feature_alias),
                )
            )
            return _unresolved()

        constraint = _feature_output_constraint(resolved.output.value_type)
        if constraint is None:
            issues.append(
                _error(
                    ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH,
                    path=path,
                    message=(
                        f"feature output type {resolved.output.value_type.value!r} "
                        "is not accepted by comparison operators"
                    ),
                    context=None,
                )
            )
            return _unresolved()

        return _ResolvedOperand(
            state=_ResolutionState.RESOLVED,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=constraint,
            temporal_shape=resolved.output.temporal_shape,
            collection_shape=resolved.output.collection_shape,
        )

    raise ValidationInvariantError(f"unsupported operand type at {path}")


def _resolve_literal(
    path: str,
    value: ParamValue,
    issues: list[ValidationIssue],
) -> _ResolvedOperand:
    if isinstance(value, RateParam):
        issues.append(
            _error(
                ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH,
                path=f"{path}.value",
                message="rate literal is not accepted by comparison operators",
                context=None,
            )
        )
        return _unresolved()

    try:
        parameter_type = param_value_to_parameter_type(value)
    except TypeError as exc:
        raise ValidationInvariantError(
            f"unsupported literal parameter type at {path}"
        ) from exc

    constraint = _literal_constraint(parameter_type, value)
    if constraint is None:
        issues.append(
            _error(
                ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH,
                path=f"{path}.value",
                message="literal type is not accepted by comparison operators",
                context=None,
            )
        )
        return _unresolved()

    return _ResolvedOperand(
        state=_ResolutionState.RESOLVED,
        origin=OperandOriginV2.LITERAL_PARAM,
        type_constraint=constraint,
        temporal_shape=TemporalShape.INSTANT,
        collection_shape=CollectionShape.SINGLE,
    )


def _validate_operator_signature(
    *,
    path: str,
    operator: OperatorDescriptorV2,
    left: _ResolvedOperand,
    right: _ResolvedOperand,
) -> list[ValidationIssue]:
    matches: list[OperandTypeConstraintV2] = []
    for signature in operator.signatures:
        if len(signature.operands) != 2:
            continue
        left_spec = signature.operands[0]
        right_spec = signature.operands[1]
        if left.type_constraint is None or right.type_constraint is None:
            continue
        if left.origin is not left_spec.origin:
            continue
        if right.origin is not right_spec.origin:
            continue
        if left.type_constraint is not left_spec.type_constraint:
            continue
        if right.type_constraint is not right_spec.type_constraint:
            continue
        if signature.result_type is not OperandTypeConstraintV2.BOOLEAN_LITERAL:
            return [
                _error(
                    ValidationIssueCode.OPERATOR_RESULT_NOT_BOOLEAN,
                    path=path,
                    message=(
                        f"operator {operator.operator_id.value!r} does not produce "
                        "a boolean result"
                    ),
                    context=OperatorSignatureContext(
                        operator_id=operator.operator_id,
                        left_constraint=left.type_constraint,
                        right_constraint=right.type_constraint,
                    ),
                )
            ]
        matches.append(signature.result_type)

    if not matches:
        return [
            _error(
                ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH,
                path=path,
                message=(
                    f"no operator signature matches operands for "
                    f"{operator.operator_id.value!r}"
                ),
                context=OperatorSignatureContext(
                    operator_id=operator.operator_id,
                    left_constraint=left.type_constraint,
                    right_constraint=right.type_constraint,
                ),
            )
        ]

    if len(matches) > 1:
        raise ValidationInvariantError(
            f"ambiguous operator signature match for {operator.operator_id.value!r} at {path}"
        )

    return []


def _feature_output_constraint(
    value_type: FeatureOutputValueType,
) -> OperandTypeConstraintV2 | None:
    if value_type is FeatureOutputValueType.DECIMAL:
        return OperandTypeConstraintV2.DECIMAL_SERIES
    if value_type is FeatureOutputValueType.BOOLEAN:
        return OperandTypeConstraintV2.BOOLEAN_SERIES
    return None


def _literal_constraint(
    parameter_type: ParameterValueType,
    value: ParamValue,
) -> OperandTypeConstraintV2 | None:
    if parameter_type is ParameterValueType.INTEGER and isinstance(value, IntParam):
        return OperandTypeConstraintV2.INTEGER_LITERAL
    if parameter_type is ParameterValueType.DECIMAL and isinstance(value, DecimalParam):
        return OperandTypeConstraintV2.DECIMAL_LITERAL
    return None


def _unresolved() -> _ResolvedOperand:
    return _ResolvedOperand(
        state=_ResolutionState.UNRESOLVED,
        origin=None,
        type_constraint=None,
        temporal_shape=None,
        collection_shape=None,
    )


def _expected_operator_contract_version(catalogs: CatalogBundleV2) -> str:
    for operator in catalogs.operators:
        return operator.contract_version.value
    raise RuntimeError("operator catalog must not be empty")


def _error(
    code: ValidationIssueCode,
    *,
    path: str,
    message: str,
    context: object | None,
) -> ValidationIssue:
    from orderbook_analyse.strategy_lab.validation.diagnostics import IssueContext

    typed_context = context if isinstance(context, IssueContext) else None
    return ValidationIssue(
        code=code,
        severity=ValidationSeverity.ERROR,
        path=path,
        message=message,
        context=typed_context,
    )
