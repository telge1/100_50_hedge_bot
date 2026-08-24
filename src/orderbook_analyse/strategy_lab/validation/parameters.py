"""Central parameter binding validation for P4A."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ParameterValueType,
    PluginParameterBindingTargetV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.feature import ParameterDefinitionV2
from orderbook_analyse.strategy_lab.models.contracts_v2.plugin import (
    PluginParameterDefinitionV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.param_mapping import (
    param_value_to_parameter_type,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    IdentifierParam,
    IntParam,
    ParamValue,
    RateParam,
)
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    BoundsContext,
    ExpectedActualTypeContext,
    ParameterNameContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)


def config_parameter_definitions(
    parameters: tuple[PluginParameterDefinitionV2, ...],
) -> tuple[ParameterDefinitionV2, ...]:
    """Return only plugin parameters bound to PluginRefV2.config."""
    return tuple(
        item.definition
        for item in parameters
        if item.binding_target is PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG
    )


def validate_parameter_bindings(
    *,
    path_prefix: str,
    definitions: tuple[ParameterDefinitionV2, ...],
    bindings: tuple[tuple[StableIdentifier, ParamValue], ...],
    code_prefix: str,
) -> tuple[ValidationIssue, ...]:
    """Validate bindings against closed parameter definitions."""
    issues: list[ValidationIssue] = []
    defs_by_name = {definition.name: definition for definition in definitions}
    seen: set[str] = set()

    for index, (name, value) in enumerate(bindings):
        value_path = f"{path_prefix}[{index}].value"
        if name.value in seen:
            issues.append(
                _issue(
                    code=_code(code_prefix, "DUPLICATE_PARAMETER"),
                    path=f"{path_prefix}[{index}].name",
                    message=f"duplicate parameter name {name.value!r}",
                    context=ParameterNameContext(parameter_name=name),
                )
            )
        seen.add(name.value)

        definition = defs_by_name.get(name)
        if definition is None:
            issues.append(
                _issue(
                    code=_code(code_prefix, "UNKNOWN_PARAMETER"),
                    path=f"{path_prefix}[{index}].name",
                    message=f"unknown parameter {name.value!r}",
                    context=ParameterNameContext(parameter_name=name),
                )
            )
            continue

        issues.extend(
            validate_parameter_value(
                path=value_path,
                definition=definition,
                value=value,
                code_prefix=code_prefix,
            )
        )

    for definition in definitions:
        if definition.required and definition.name.value not in seen:
            issues.append(
                _issue(
                    code=_code(code_prefix, "MISSING_PARAMETER"),
                    path=path_prefix,
                    message=f"missing required parameter {definition.name.value!r}",
                    context=ParameterNameContext(parameter_name=definition.name),
                )
            )

    return tuple(issues)


def validate_plugin_config_bindings(
    *,
    path_prefix: str,
    definitions: tuple[ParameterDefinitionV2, ...],
    config: tuple[tuple[str, ParamValue], ...],
) -> tuple[ValidationIssue, ...]:
    """Validate plugin config entries keyed by plain strings."""
    normalized = tuple(
        (StableIdentifier(value=key), value_param) for key, value_param in config
    )
    return validate_parameter_bindings(
        path_prefix=path_prefix,
        definitions=definitions,
        bindings=normalized,
        code_prefix="PLUGIN",
    )


def validate_parameter_value(
    *,
    path: str,
    definition: ParameterDefinitionV2,
    value: ParamValue,
    code_prefix: str,
) -> tuple[ValidationIssue, ...]:
    """Validate one ParamValue against a closed ParameterDefinitionV2."""
    issues: list[ValidationIssue] = []
    try:
        actual_type = param_value_to_parameter_type(value)
    except TypeError:
        issues.append(
            _issue(
                code=_code(code_prefix, "PARAMETER_TYPE"),
                path=path,
                message="parameter value is not a supported ParamValue",
                context=None,
            )
        )
        return tuple(issues)

    if actual_type is not definition.value_type:
        issues.append(
            _issue(
                code=_code(code_prefix, "PARAMETER_TYPE"),
                path=path,
                message=(
                    f"parameter {definition.name.value!r} expects "
                    f"{definition.value_type.value}, got {actual_type.value}"
                ),
                context=ExpectedActualTypeContext(
                    expected=definition.value_type,
                    actual=actual_type,
                ),
            )
        )
        return tuple(issues)

    if isinstance(value, IntParam):
        bounds = definition.int_bounds
        if bounds is not None:
            if bounds.min_value is not None and value.value < bounds.min_value:
                issues.append(
                    _issue(
                        code=_code(code_prefix, "PARAMETER_BOUNDS"),
                        path=path,
                        message=(
                            f"parameter {definition.name.value!r} below minimum "
                            f"{bounds.min_value}"
                        ),
                        context=BoundsContext(
                            parameter_name=definition.name,
                            bound_value=value.value,
                            minimum=bounds.min_value,
                            maximum=bounds.max_value,
                        ),
                    )
                )
            if bounds.max_value is not None and value.value > bounds.max_value:
                issues.append(
                    _issue(
                        code=_code(code_prefix, "PARAMETER_BOUNDS"),
                        path=path,
                        message=(
                            f"parameter {definition.name.value!r} above maximum "
                            f"{bounds.max_value}"
                        ),
                        context=BoundsContext(
                            parameter_name=definition.name,
                            bound_value=value.value,
                            minimum=bounds.min_value,
                            maximum=bounds.max_value,
                        ),
                    )
                )

    if isinstance(value, DecimalParam):
        bounds = definition.decimal_bounds
        if bounds is not None:
            if bounds.min_value is not None and value.value < bounds.min_value:
                issues.append(
                    _issue(
                        code=_code(code_prefix, "PARAMETER_BOUNDS"),
                        path=path,
                        message=(
                            f"parameter {definition.name.value!r} below minimum "
                            f"{bounds.min_value}"
                        ),
                        context=BoundsContext(
                            parameter_name=definition.name,
                            bound_value=value.value,
                            minimum=bounds.min_value,
                            maximum=bounds.max_value,
                        ),
                    )
                )
            if bounds.max_value is not None and value.value > bounds.max_value:
                issues.append(
                    _issue(
                        code=_code(code_prefix, "PARAMETER_BOUNDS"),
                        path=path,
                        message=(
                            f"parameter {definition.name.value!r} above maximum "
                            f"{bounds.max_value}"
                        ),
                        context=BoundsContext(
                            parameter_name=definition.name,
                            bound_value=value.value,
                            minimum=bounds.min_value,
                            maximum=bounds.max_value,
                        ),
                    )
                )

    if isinstance(value, RateParam):
        if (
            definition.required_rate_unit is not None
            and value.value.unit is not definition.required_rate_unit
        ):
            issues.append(
                _issue(
                    code=_code(code_prefix, "RATE_UNIT"),
                    path=path,
                    message=(
                        f"parameter {definition.name.value!r} requires rate unit "
                        f"{definition.required_rate_unit.value}"
                    ),
                    context=ParameterNameContext(parameter_name=definition.name),
                )
            )

    if isinstance(value, IdentifierParam) and definition.allowed_identifiers:
        allowed = {item.value for item in definition.allowed_identifiers}
        if value.value not in allowed:
            issues.append(
                _issue(
                    code=_code(code_prefix, "IDENTIFIER_VALUE"),
                    path=path,
                    message=(
                        f"parameter {definition.name.value!r} identifier "
                        f"{value.value!r} is not allowed"
                    ),
                    context=UnknownIdentifierContext(
                        identifier=StableIdentifier(value=value.value)
                    ),
                )
            )

    if isinstance(value, BoolParam) and definition.value_type is not ParameterValueType.BOOLEAN:
        issues.append(
            _issue(
                code=_code(code_prefix, "PARAMETER_TYPE"),
                path=path,
                message="boolean parameter type mismatch",
                context=None,
            )
        )

    return tuple(issues)


def remap_parameter_issues_to_research(
    issues: tuple[ValidationIssue, ...],
) -> tuple[ValidationIssue, ...]:
    """Map FEATURE_/PLUGIN_ parameter issues onto research candidate codes."""
    remapped: list[ValidationIssue] = []
    saw_type = False
    for issue in issues:
        name = issue.code.name
        if name.endswith("PARAMETER_TYPE"):
            saw_type = True
            remapped.append(
                ValidationIssue(
                    code=ValidationIssueCode.RESEARCH_CANDIDATE_TYPE,
                    severity=issue.severity,
                    path=issue.path,
                    message=issue.message,
                    context=issue.context,
                )
            )
        elif name.endswith(("PARAMETER_BOUNDS", "RATE_UNIT", "IDENTIFIER_VALUE")):
            if saw_type:
                continue
            remapped.append(
                ValidationIssue(
                    code=ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                    severity=issue.severity,
                    path=issue.path,
                    message=issue.message,
                    context=issue.context,
                )
            )
    return tuple(remapped)


def _code(prefix: str, suffix: str) -> ValidationIssueCode:
    return ValidationIssueCode[f"{prefix}_{suffix}"]


def _issue(
    *,
    code: ValidationIssueCode,
    path: str,
    message: str,
    context: object | None,
) -> ValidationIssue:
    from orderbook_analyse.strategy_lab.validation.diagnostics import IssueContext

    return ValidationIssue(
        code=code,
        severity=ValidationSeverity.ERROR,
        path=path,
        message=message,
        context=context if context is None or isinstance(context, IssueContext) else None,
    )
