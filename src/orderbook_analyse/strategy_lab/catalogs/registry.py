"""Closed catalog registry and integrity validation for Strategy Lab P3."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Generic, TypeVar

from orderbook_analyse.strategy_lab.catalogs.features import FEATURE_DESCRIPTORS
from orderbook_analyse.strategy_lab.catalogs.models import (
    CATALOG_ID_PATTERN,
    BoundFeatureRequirement,
    CatalogIntegrityIssue,
    CatalogIntegrityReport,
    DataRequirementDescriptor,
    DataRequirementRole,
    FeatureDescriptor,
    InvalidCatalogDefinitionError,
    OperatorDescriptor,
    ParameterDefinition,
    PluginDescriptor,
    ResearchConfirmationPolicy,
    UnknownCatalogEntryError,
    ValueType,
)
from orderbook_analyse.strategy_lab.catalogs.operators import OPERATOR_DESCRIPTORS
from orderbook_analyse.strategy_lab.catalogs.plugins import PLUGIN_DESCRIPTORS
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    IdentifierParam,
    IntParam,
    RateParam,
    StringParam,
)

T = TypeVar("T")

_VALUE_TYPE_TO_PARAM_CLASS = {
    ValueType.BOOLEAN: BoolParam,
    ValueType.INTEGER: IntParam,
    ValueType.DECIMAL: DecimalParam,
    ValueType.RATE: RateParam,
    ValueType.STRING: StringParam,
    ValueType.IDENTIFIER: IdentifierParam,
}


def _validate_catalog_id(entry_id: str, *, context: str) -> None:
    if not entry_id:
        raise InvalidCatalogDefinitionError(f"empty catalog id in {context}")
    if not CATALOG_ID_PATTERN.fullmatch(entry_id):
        raise InvalidCatalogDefinitionError(
            f"invalid catalog id syntax {entry_id!r} in {context}"
        )


class CatalogRegistry(Generic[T]):
    """Immutable closed registry with deterministic iteration and lookup."""

    __slots__ = ("_by_id", "_ids", "_name")

    def __init__(
        self,
        *,
        name: str,
        entries: Sequence[T],
        id_getter: Callable[[T], str],
    ) -> None:
        self._name = name
        ids: list[str] = []
        by_id: dict[str, T] = {}
        for entry in entries:
            entry_id = id_getter(entry)
            _validate_catalog_id(entry_id, context=f"{name}.{entry_id}")
            if entry_id in by_id:
                raise InvalidCatalogDefinitionError(
                    f"duplicate {name} id: {entry_id!r}"
                )
            ids.append(entry_id)
            by_id[entry_id] = entry
        self._ids = tuple(sorted(ids))
        self._by_id = by_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def ids(self) -> tuple[str, ...]:
        return self._ids

    def __iter__(self):
        for entry_id in self._ids:
            yield self._by_id[entry_id]

    def get(self, entry_id: str) -> T:
        try:
            return self._by_id[entry_id]
        except KeyError as exc:
            raise UnknownCatalogEntryError(
                f"unknown {self._name} id: {entry_id!r}"
            ) from exc

    def __len__(self) -> int:
        return len(self._ids)


def build_feature_catalog() -> CatalogRegistry[FeatureDescriptor]:
    return CatalogRegistry(
        name="feature",
        entries=FEATURE_DESCRIPTORS,
        id_getter=lambda d: d.feature_id,
    )


def build_operator_catalog() -> CatalogRegistry[OperatorDescriptor]:
    return CatalogRegistry(
        name="operator",
        entries=OPERATOR_DESCRIPTORS,
        id_getter=lambda d: d.operator_id,
    )


def build_plugin_catalog() -> CatalogRegistry[PluginDescriptor]:
    return CatalogRegistry(
        name="plugin",
        entries=PLUGIN_DESCRIPTORS,
        id_getter=lambda d: d.plugin_id,
    )


FEATURE_CATALOG = build_feature_catalog()
OPERATOR_CATALOG = build_operator_catalog()
PLUGIN_CATALOG = build_plugin_catalog()


def get_feature(feature_id: str) -> FeatureDescriptor:
    return FEATURE_CATALOG.get(feature_id)


def get_operator(operator_id: str) -> OperatorDescriptor:
    return OPERATOR_CATALOG.get(operator_id)


def get_plugin(plugin_id: str) -> PluginDescriptor:
    return PLUGIN_CATALOG.get(plugin_id)


def validate_catalog_integrity(
    *,
    features: CatalogRegistry[FeatureDescriptor] | None = None,
    operators: CatalogRegistry[OperatorDescriptor] | None = None,
    plugins: CatalogRegistry[PluginDescriptor] | None = None,
) -> CatalogIntegrityReport:
    """Validate closed catalog definitions (catalog-only; no StrategySpec)."""
    feature_registry = features or FEATURE_CATALOG
    operator_registry = operators or OPERATOR_CATALOG
    plugin_registry = plugins or PLUGIN_CATALOG
    issues: list[CatalogIntegrityIssue] = []

    feature_by_id = {feature.feature_id: feature for feature in feature_registry}

    for registry in (feature_registry, operator_registry, plugin_registry):
        for entry in registry:
            issues.extend(_validate_entry_contract(entry))

    for feature in feature_registry:
        issues.extend(_validate_parameters(feature.feature_id, feature.parameters))

    for operator in operator_registry:
        if operator.operator_id.startswith("crosses_") and (
            not operator.requires_previous_observation
        ):
            issues.append(
                CatalogIntegrityIssue(
                    code="CROSS_OPERATOR_MISSING_PREVIOUS",
                    message="cross operator must require previous observation",
                    entry_id=operator.operator_id,
                )
            )
        issues.extend(_validate_operator_operand_types(operator))

    for plugin in plugin_registry:
        issues.extend(_validate_plugin_contract(plugin, feature_by_id))

    return CatalogIntegrityReport(issues=tuple(issues))


def _validate_plugin_contract(
    plugin: PluginDescriptor,
    feature_by_id: dict[str, FeatureDescriptor],
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    entry_id = plugin.plugin_id

    if not plugin.contract_version:
        issues.append(
            CatalogIntegrityIssue(
                code="EMPTY_CONTRACT_VERSION",
                message="plugin contract_version must not be empty",
                entry_id=entry_id,
            )
        )
    if plugin.adapter_status.name == "ADAPTER_AVAILABLE":
        issues.append(
            CatalogIntegrityIssue(
                code="UNEXPECTED_ADAPTER_AVAILABLE",
                message="P3 plugins must not be adapter_available yet",
                entry_id=entry_id,
            )
        )
    if not plugin.entry_rule_id:
        issues.append(
            CatalogIntegrityIssue(
                code="MISSING_ENTRY_RULE",
                message="plugin must declare entry_rule_id",
                entry_id=entry_id,
            )
        )
    if not plugin.decision_timing or not plugin.entry_timing:
        issues.append(
            CatalogIntegrityIssue(
                code="MISSING_TIMING",
                message="plugin must declare decision and entry timing",
                entry_id=entry_id,
            )
        )

    if plugin.plugin_id == "edc_m0_strict_sync" and (
        plugin.confirmation_policy is None
    ):
        issues.append(
            CatalogIntegrityIssue(
                code="MISSING_CONFIRMATION_POLICY",
                message="edc_m0_strict_sync must bind confirmation_policy explicitly",
                entry_id=entry_id,
            )
        )

    if plugin.confirmation_policy is not None:
        confirmation_sources = [
            req
            for req in plugin.data_requirements
            if req.role is DataRequirementRole.CONFIRMATION_REQUIRED
        ]
        if not confirmation_sources:
            issues.append(
                CatalogIntegrityIssue(
                    code="POLICY_WITHOUT_CONFIRMATION_SOURCES",
                    message="confirmation_policy requires confirmation_required sources",
                    entry_id=entry_id,
                )
            )
        for requirement in confirmation_sources:
            if requirement.required_for_policy != plugin.confirmation_policy:
                issues.append(
                    CatalogIntegrityIssue(
                        code="POLICY_DATA_MISMATCH",
                        message=(
                            f"{requirement.requirement_id} must declare "
                            f"required_for_policy={plugin.confirmation_policy.value}"
                        ),
                        entry_id=entry_id,
                    )
                )

    issues.extend(_validate_parameters(entry_id, plugin.parameters))

    seen_aliases: set[str] = set()
    for requirement in plugin.required_features:
        if requirement.alias in seen_aliases:
            issues.append(
                CatalogIntegrityIssue(
                    code="DUPLICATE_FEATURE_ALIAS",
                    message=f"duplicate feature alias {requirement.alias!r}",
                    entry_id=entry_id,
                )
            )
        seen_aliases.add(requirement.alias)
        if not CATALOG_ID_PATTERN.fullmatch(requirement.alias):
            issues.append(
                CatalogIntegrityIssue(
                    code="INVALID_FEATURE_ALIAS",
                    message=f"invalid feature alias syntax {requirement.alias!r}",
                    entry_id=entry_id,
                )
            )
        feature = feature_by_id.get(requirement.feature_id)
        if feature is None:
            issues.append(
                CatalogIntegrityIssue(
                    code="UNKNOWN_FEATURE_REFERENCE",
                    message=(
                        f"plugin references unknown feature "
                        f"{requirement.feature_id!r}"
                    ),
                    entry_id=entry_id,
                )
            )
            continue
        issues.extend(
            _validate_bound_feature_requirement(
                plugin_id=entry_id,
                requirement=requirement,
                feature=feature,
            )
        )

    seen_requirement_ids: set[str] = set()
    for data_requirement in plugin.data_requirements:
        issues.extend(
            _validate_data_requirement(entry_id, data_requirement, seen_requirement_ids)
        )

    if plugin.source_loading_padding is not None:
        padding = plugin.source_loading_padding
        if padding.candle_pad_days and padding.candle_pad_days == plugin.signal_warmup.minimum_bar_index:
            issues.append(
                CatalogIntegrityIssue(
                    code="PADDING_WARMUP_CONFUSION",
                    message="source loading pad_days must not mirror signal warmup bars",
                    entry_id=entry_id,
                )
            )

    return issues


def _validate_bound_feature_requirement(
    *,
    plugin_id: str,
    requirement: BoundFeatureRequirement,
    feature: FeatureDescriptor,
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    param_defs = {param.name: param for param in feature.parameters}
    bound_names: set[str] = set()

    for binding in requirement.bindings:
        if binding.name in bound_names:
            issues.append(
                CatalogIntegrityIssue(
                    code="DUPLICATE_BOUND_PARAMETER",
                    message=(
                        f"duplicate bound parameter {binding.name!r} on "
                        f"{requirement.alias!r}"
                    ),
                    entry_id=plugin_id,
                )
            )
        bound_names.add(binding.name)
        param_def = param_defs.get(binding.name)
        if param_def is None:
            issues.append(
                CatalogIntegrityIssue(
                    code="UNKNOWN_BOUND_PARAMETER",
                    message=(
                        f"unknown bound parameter {binding.name!r} for feature "
                        f"{feature.feature_id!r}"
                    ),
                    entry_id=plugin_id,
                )
            )
            continue
        expected_cls = _VALUE_TYPE_TO_PARAM_CLASS.get(param_def.value_type)
        if expected_cls is None or not isinstance(binding.value, expected_cls):
            issues.append(
                CatalogIntegrityIssue(
                    code="BOUND_PARAMETER_TYPE_MISMATCH",
                    message=(
                        f"binding {binding.name!r} on {requirement.alias!r} "
                        f"does not match feature type {param_def.value_type.value}"
                    ),
                    entry_id=plugin_id,
                )
            )
            continue
        issues.extend(
            _validate_bound_value_against_definition(
                plugin_id=plugin_id,
                alias=requirement.alias,
                param_def=param_def,
                binding_value=binding.value,
            )
        )

    for param_def in feature.parameters:
        if param_def.required and param_def.name not in bound_names:
            issues.append(
                CatalogIntegrityIssue(
                    code="MISSING_REQUIRED_BINDING",
                    message=(
                        f"feature {feature.feature_id!r} requires bound parameter "
                        f"{param_def.name!r} for alias {requirement.alias!r}"
                    ),
                    entry_id=plugin_id,
                )
            )

    return issues


def _validate_bound_value_against_definition(
    *,
    plugin_id: str,
    alias: str,
    param_def: ParameterDefinition,
    binding_value: object,
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    if isinstance(binding_value, IntParam):
        value = binding_value.value
        bounds = param_def.int_bounds
        if bounds is not None:
            if bounds.min_value is not None and value < bounds.min_value:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_BOUND_PARAMETER_VALUE",
                        message=(
                            f"{alias}.{param_def.name} below minimum "
                            f"{bounds.min_value}"
                        ),
                        entry_id=plugin_id,
                    )
                )
            if bounds.max_value is not None and value > bounds.max_value:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_BOUND_PARAMETER_VALUE",
                        message=(
                            f"{alias}.{param_def.name} above maximum "
                            f"{bounds.max_value}"
                        ),
                        entry_id=plugin_id,
                    )
                )
    elif isinstance(binding_value, DecimalParam):
        value = binding_value.value
        bounds = param_def.decimal_bounds
        if bounds is not None:
            if bounds.min_value is not None and value < bounds.min_value:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_BOUND_PARAMETER_VALUE",
                        message=(
                            f"{alias}.{param_def.name} below minimum "
                            f"{bounds.min_value}"
                        ),
                        entry_id=plugin_id,
                    )
                )
            if bounds.max_value is not None and value > bounds.max_value:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_BOUND_PARAMETER_VALUE",
                        message=(
                            f"{alias}.{param_def.name} above maximum "
                            f"{bounds.max_value}"
                        ),
                        entry_id=plugin_id,
                    )
                )
    elif isinstance(binding_value, RateParam):
        if param_def.required_rate_unit is not None and (
            binding_value.value.unit is not param_def.required_rate_unit
        ):
            issues.append(
                CatalogIntegrityIssue(
                    code="INVALID_RATE_UNIT",
                    message=(
                        f"{alias}.{param_def.name} requires unit "
                        f"{param_def.required_rate_unit.value}"
                    ),
                    entry_id=plugin_id,
                )
            )
    return issues


def _validate_data_requirement(
    plugin_id: str,
    requirement: DataRequirementDescriptor,
    seen_requirement_ids: set[str],
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    if requirement.requirement_id in seen_requirement_ids:
        issues.append(
            CatalogIntegrityIssue(
                code="DUPLICATE_DATA_REQUIREMENT",
                message=f"duplicate data requirement {requirement.requirement_id!r}",
                entry_id=plugin_id,
            )
        )
    seen_requirement_ids.add(requirement.requirement_id)
    if not CATALOG_ID_PATTERN.fullmatch(requirement.requirement_id):
        issues.append(
            CatalogIntegrityIssue(
                code="INVALID_DATA_REQUIREMENT_ID",
                message=(
                    f"invalid data requirement id {requirement.requirement_id!r}"
                ),
                entry_id=plugin_id,
            )
        )

    optional_roles = {
        DataRequirementRole.ANALYSIS_OPTIONAL,
        DataRequirementRole.VALIDATION_OPTIONAL,
    }
    required_roles = {
        DataRequirementRole.SIGNAL_REQUIRED,
        DataRequirementRole.EXECUTION_REQUIRED,
        DataRequirementRole.CONFIRMATION_REQUIRED,
    }
    if requirement.role in optional_roles and requirement.required:
        issues.append(
            CatalogIntegrityIssue(
                code="CONTRADICTORY_DATA_REQUIREMENT",
                message=(
                    f"{requirement.requirement_id} is optional role but marked required"
                ),
                entry_id=plugin_id,
            )
        )
    if requirement.role in required_roles and not requirement.required:
        issues.append(
            CatalogIntegrityIssue(
                code="CONTRADICTORY_DATA_REQUIREMENT",
                message=(
                    f"{requirement.requirement_id} is required role but marked optional"
                ),
                entry_id=plugin_id,
            )
        )
    if requirement.granularity_minutes <= 0:
        issues.append(
            CatalogIntegrityIssue(
                code="INVALID_GRANULARITY",
                message=(
                    f"{requirement.requirement_id} granularity_minutes must be > 0"
                ),
                entry_id=plugin_id,
            )
        )
    return issues


def _validate_operator_operand_types(
    operator: OperatorDescriptor,
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    logical_ops = {"and", "or", "not"}
    numeric_ops = {"eq", "ne", "gt", "gte", "lt", "lte"}
    if operator.operator_id in logical_ops:
        for operand in operator.operand_types:
            if operand.value_type is not ValueType.BOOLEAN:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_LOGICAL_OPERAND",
                        message=(
                            f"{operator.operator_id} operand must be boolean"
                        ),
                        entry_id=operator.operator_id,
                    )
                )
    if operator.operator_id in numeric_ops:
        for operand in operator.operand_types:
            if operand.value_type not in {ValueType.DECIMAL, ValueType.RATE}:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_NUMERIC_OPERAND",
                        message=(
                            f"{operator.operator_id} operand must be numeric"
                        ),
                        entry_id=operator.operator_id,
                    )
                )
    return issues


def _validate_entry_contract(entry: object) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    if not is_dataclass(entry):
        issues.append(
            CatalogIntegrityIssue(
                code="NOT_DATACLASS",
                message=f"{type(entry).__name__} must be a dataclass",
            )
        )
        return issues
    params = getattr(entry, "__dataclass_params__", None)
    if params is None or not params.frozen:
        issues.append(
            CatalogIntegrityIssue(
                code="NOT_FROZEN",
                message=f"{type(entry).__name__} must be frozen",
            )
        )
    for value in _iter_nested_values(entry):
        if callable(value):
            issues.append(
                CatalogIntegrityIssue(
                    code="CALLABLE_IN_DESCRIPTOR",
                    message=f"callable found in {type(entry).__name__}",
                )
            )
        if isinstance(value, (list, dict, set)):
            issues.append(
                CatalogIntegrityIssue(
                    code="MUTABLE_CONTAINER",
                    message=f"mutable container found in {type(entry).__name__}",
                )
            )
    return issues


def _validate_parameters(
    entry_id: str,
    parameters: Sequence[ParameterDefinition],
) -> list[CatalogIntegrityIssue]:
    issues: list[CatalogIntegrityIssue] = []
    seen: set[str] = set()
    for param in parameters:
        if param.name in seen:
            issues.append(
                CatalogIntegrityIssue(
                    code="DUPLICATE_PARAMETER",
                    message=f"duplicate parameter {param.name!r}",
                    entry_id=entry_id,
                )
            )
        seen.add(param.name)
        if param.int_bounds is not None:
            bounds = param.int_bounds
            if bounds.min_value is not None and type(bounds.min_value) is not int:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_INT_BOUND",
                        message=f"parameter {param.name!r} int bound is not int",
                        entry_id=entry_id,
                    )
                )
            if bounds.max_value is not None and type(bounds.max_value) is not int:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_INT_BOUND",
                        message=f"parameter {param.name!r} int bound is not int",
                        entry_id=entry_id,
                    )
                )
        if param.decimal_bounds is not None:
            bounds = param.decimal_bounds
            if bounds.min_value is not None and type(bounds.min_value) is not Decimal:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_DECIMAL_BOUND",
                        message=(
                            f"parameter {param.name!r} decimal bound is not Decimal"
                        ),
                        entry_id=entry_id,
                    )
                )
            if bounds.max_value is not None and type(bounds.max_value) is not Decimal:
                issues.append(
                    CatalogIntegrityIssue(
                        code="INVALID_DECIMAL_BOUND",
                        message=(
                            f"parameter {param.name!r} decimal bound is not Decimal"
                        ),
                        entry_id=entry_id,
                    )
                )
    return issues


def _iter_nested_values(obj: object) -> Iterable[object]:
    if not is_dataclass(obj):
        return
    for field in fields(obj):
        value = getattr(obj, field.name)
        yield value
        if is_dataclass(value):
            yield from _iter_nested_values(value)
        elif isinstance(value, tuple):
            for item in value:
                if is_dataclass(item):
                    yield from _iter_nested_values(item)
                else:
                    yield item


def assert_production_catalog_integrity() -> CatalogIntegrityReport:
    report = validate_catalog_integrity()
    if not report.ok:
        joined = "; ".join(issue.message for issue in report.issues)
        raise InvalidCatalogDefinitionError(joined)
    return report
