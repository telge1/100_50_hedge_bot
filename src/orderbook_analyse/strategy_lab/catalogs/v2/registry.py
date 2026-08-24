"""Closed catalog registry for Strategy Lab catalog/v2."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

from orderbook_analyse.strategy_lab.catalogs.v2.features import FEATURE_DESCRIPTORS_V2
from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CATALOG_ID_PATTERN,
    CatalogIntegrityIssueV2,
    CatalogIntegrityReportV2,
    FeatureDescriptorV2,
    InvalidCatalogDefinitionError,
    OperatorDescriptorV2,
    PluginDescriptorV2,
    UnknownCatalogEntryError,
)
from orderbook_analyse.strategy_lab.catalogs.v2.operators import OPERATOR_DESCRIPTORS_V2
from orderbook_analyse.strategy_lab.catalogs.v2.plugins import PLUGIN_DESCRIPTORS_V2
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    DataRequirementRoleV2,
    PluginModeRequirementV2,
    PluginParameterBindingTargetV2,
    SelectedSignalTimeframeGranularityV2,
    SignalTimeframeModeV2,
    SnapshotGranularityV2,
    TimeframeGranularityV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    ParameterValueType,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    IntParam,
    RateParam,
    StringParam,
)

T = TypeVar("T")

_PARAM_CLASS_BY_TYPE = {
    ParameterValueType.BOOLEAN: BoolParam,
    ParameterValueType.INTEGER: IntParam,
    ParameterValueType.DECIMAL: DecimalParam,
    ParameterValueType.RATE: RateParam,
    ParameterValueType.STRING: StringParam,
}


def _stable_id_value(identifier: StableIdentifier) -> str:
    return identifier.value


def _validate_catalog_id(entry_id: str, *, context: str) -> None:
    if not entry_id:
        raise InvalidCatalogDefinitionError(f"empty catalog id in {context}")
    if not CATALOG_ID_PATTERN.fullmatch(entry_id):
        raise InvalidCatalogDefinitionError(
            f"invalid catalog id syntax {entry_id!r} in {context}"
        )


class CatalogRegistryV2(Generic[T]):
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


def build_feature_catalog_v2() -> CatalogRegistryV2[FeatureDescriptorV2]:
    return CatalogRegistryV2(
        name="feature",
        entries=FEATURE_DESCRIPTORS_V2,
        id_getter=lambda d: _stable_id_value(d.feature_id),
    )


def build_operator_catalog_v2() -> CatalogRegistryV2[OperatorDescriptorV2]:
    return CatalogRegistryV2(
        name="operator",
        entries=OPERATOR_DESCRIPTORS_V2,
        id_getter=lambda d: _stable_id_value(d.operator_id),
    )


def build_plugin_catalog_v2() -> CatalogRegistryV2[PluginDescriptorV2]:
    return CatalogRegistryV2(
        name="plugin",
        entries=PLUGIN_DESCRIPTORS_V2,
        id_getter=lambda d: _stable_id_value(d.plugin_id),
    )


FEATURE_CATALOG_V2 = build_feature_catalog_v2()
OPERATOR_CATALOG_V2 = build_operator_catalog_v2()
PLUGIN_CATALOG_V2 = build_plugin_catalog_v2()


def get_feature_v2(feature_id: str) -> FeatureDescriptorV2:
    return FEATURE_CATALOG_V2.get(feature_id)


def get_operator_v2(operator_id: str) -> OperatorDescriptorV2:
    return OPERATOR_CATALOG_V2.get(operator_id)


def get_plugin_v2(plugin_id: str) -> PluginDescriptorV2:
    return PLUGIN_CATALOG_V2.get(plugin_id)


def validate_catalog_integrity_v2() -> CatalogIntegrityReportV2:
    """Validate closed catalog/v2 definitions."""
    issues: list[CatalogIntegrityIssueV2] = []
    feature_by_id = {
        _stable_id_value(feature.feature_id): feature for feature in FEATURE_CATALOG_V2
    }

    for feature in FEATURE_CATALOG_V2:
        output_ids: set[str] = set()
        for output in feature.outputs:
            oid = _stable_id_value(output.output_id)
            if oid in output_ids:
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="DUPLICATE_OUTPUT_ID",
                        message=f"duplicate output id {oid!r}",
                        entry_id=_stable_id_value(feature.feature_id),
                    )
                )
            output_ids.add(oid)

    logical_ops = {"and", "or", "not"}
    for operator in OPERATOR_CATALOG_V2:
        if _stable_id_value(operator.operator_id) in logical_ops:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="FORBIDDEN_LOGICAL_OPERATOR",
                    message="catalog/v2 excludes and/or/not operators",
                    entry_id=_stable_id_value(operator.operator_id),
                )
            )
        issues.extend(_validate_operator_signatures(operator))

    for plugin in PLUGIN_CATALOG_V2:
        pid = _stable_id_value(plugin.plugin_id)
        if plugin.contract_version.value != "catalog/v2":
            issues.append(
                CatalogIntegrityIssueV2(
                    code="INVALID_CONTRACT_VERSION",
                    message="plugin must use catalog/v2 contract version",
                    entry_id=pid,
                )
            )
        if plugin.signal_warmup.timeframe_basis.value == "selected_signal_timeframe":
            if plugin.signal_warmup.fixed_timeframe is not None:
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="INVALID_WARMUP_TIMEFRAME",
                        message="fixed_timeframe must be None for selected_signal_timeframe",
                        entry_id=pid,
                    )
                )

        issues.extend(_validate_plugin_entry_contract(plugin))
        issues.extend(_validate_plugin_parameters(plugin))
        issues.extend(_validate_plugin_features(plugin, feature_by_id))
        issues.extend(_validate_plugin_data_requirements(plugin))

    return CatalogIntegrityReportV2(issues=tuple(issues))


def _validate_operator_signatures(
    operator: OperatorDescriptorV2,
) -> list[CatalogIntegrityIssueV2]:
    oid = _stable_id_value(operator.operator_id)
    issues: list[CatalogIntegrityIssueV2] = []
    seen: set[tuple[object, ...]] = set()

    for signature in operator.signatures:
        if len(signature.operands) != 2:
            continue
        key = (
            signature.operands[0].origin,
            signature.operands[0].type_constraint,
            signature.operands[1].origin,
            signature.operands[1].type_constraint,
        )
        if key in seen:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="AMBIGUOUS_OPERATOR_SIGNATURE",
                    message=(
                        f"duplicate operand signature overload for operator {oid!r}"
                    ),
                    entry_id=oid,
                )
            )
        seen.add(key)

    return issues


def _validate_plugin_entry_contract(
    plugin: PluginDescriptorV2,
) -> list[CatalogIntegrityIssueV2]:
    pid = _stable_id_value(plugin.plugin_id)
    issues: list[CatalogIntegrityIssueV2] = []
    if not isinstance(plugin.entry_reference_rule, EntryReferenceRuleV2):
        issues.append(
            CatalogIntegrityIssueV2(
                code="INVALID_ENTRY_RULE_TYPE",
                message="entry_reference_rule must be EntryReferenceRuleV2",
                entry_id=pid,
            )
        )
    if not isinstance(plugin.entry_timing_anchor, EntryTimingAnchorV2):
        issues.append(
            CatalogIntegrityIssueV2(
                code="INVALID_ENTRY_ANCHOR_TYPE",
                message="entry_timing_anchor must be EntryTimingAnchorV2",
                entry_id=pid,
            )
        )
    if plugin.entry_price_reference is not EntryPriceReferenceV2.BAR_OPEN:
        issues.append(
            CatalogIntegrityIssueV2(
                code="INVALID_ENTRY_PRICE_REFERENCE",
                message="entry_price_reference must be bar_open",
                entry_id=pid,
            )
        )
    return issues


def _validate_plugin_parameters(
    plugin: PluginDescriptorV2,
) -> list[CatalogIntegrityIssueV2]:
    pid = _stable_id_value(plugin.plugin_id)
    issues: list[CatalogIntegrityIssueV2] = []
    seen_names: set[str] = set()
    config_names: set[str] = set()
    signal_names: set[str] = set()

    for parameter in plugin.parameters:
        if type(parameter.binding_target) is not PluginParameterBindingTargetV2:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="INVALID_BINDING_TARGET",
                    message="plugin parameter binding_target must be PluginParameterBindingTargetV2",
                    entry_id=pid,
                )
            )
            continue
        pname = _stable_id_value(parameter.definition.name)
        if pname in seen_names:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="DUPLICATE_PLUGIN_PARAMETER",
                    message=f"duplicate plugin parameter {pname!r}",
                    entry_id=pid,
                )
            )
        seen_names.add(pname)
        if parameter.binding_target is PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG:
            config_names.add(pname)
        else:
            signal_names.add(pname)

    overlap = config_names & signal_names
    for pname in sorted(overlap):
        issues.append(
            CatalogIntegrityIssueV2(
                code="AMBIGUOUS_BINDING_TARGET",
                message=f"parameter {pname!r} has conflicting binding targets",
                entry_id=pid,
            )
        )

    mode_contract = plugin.mode_contract
    if type(mode_contract.requirement) is not PluginModeRequirementV2:
        issues.append(
            CatalogIntegrityIssueV2(
                code="INVALID_MODE_CONTRACT",
                message="mode_contract.requirement must be PluginModeRequirementV2",
                entry_id=pid,
            )
        )
    elif mode_contract.requirement is PluginModeRequirementV2.NOT_APPLICABLE:
        if mode_contract.allowed_modes:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="INVALID_MODE_CONTRACT",
                    message="not_applicable mode_contract must have empty allowed_modes",
                    entry_id=pid,
                )
            )
    elif not mode_contract.allowed_modes:
        issues.append(
            CatalogIntegrityIssueV2(
                code="INVALID_MODE_CONTRACT",
                message="required/optional mode_contract must declare allowed_modes",
                entry_id=pid,
            )
        )

    return issues


def _validate_plugin_features(
    plugin: PluginDescriptorV2,
    feature_by_id: dict[str, FeatureDescriptorV2],
) -> list[CatalogIntegrityIssueV2]:
    pid = _stable_id_value(plugin.plugin_id)
    issues: list[CatalogIntegrityIssueV2] = []
    seen_aliases: set[str] = set()
    for requirement in plugin.required_features:
        alias = _stable_id_value(requirement.alias)
        if alias in seen_aliases:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="DUPLICATE_FEATURE_ALIAS",
                    message=f"duplicate feature alias {alias!r}",
                    entry_id=pid,
                )
            )
        seen_aliases.add(alias)
        feature_id = _stable_id_value(requirement.feature_id)
        feature = feature_by_id.get(feature_id)
        if feature is None:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="UNKNOWN_FEATURE_REFERENCE",
                    message=f"unknown feature reference {feature_id!r}",
                    entry_id=pid,
                )
            )
            continue
        param_defs = {
            _stable_id_value(param.name): param for param in feature.parameters
        }
        bound_names: set[str] = set()
        for binding in requirement.bindings:
            bname = _stable_id_value(binding.name)
            if bname in bound_names:
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="DUPLICATE_BOUND_PARAMETER",
                        message=f"duplicate bound parameter {bname!r}",
                        entry_id=pid,
                    )
                )
            bound_names.add(bname)
            param_def = param_defs.get(bname)
            if param_def is None:
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="UNKNOWN_BOUND_PARAMETER",
                        message=f"unknown bound parameter {bname!r}",
                        entry_id=pid,
                    )
                )
                continue
            expected_cls = _PARAM_CLASS_BY_TYPE.get(param_def.value_type)
            if expected_cls is None or not isinstance(binding.value, expected_cls):
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="BOUND_PARAMETER_TYPE_MISMATCH",
                        message=(
                            f"{alias}.{bname} does not match {param_def.value_type.value}"
                        ),
                        entry_id=pid,
                    )
                )
            elif isinstance(binding.value, RateParam):
                if (
                    param_def.required_rate_unit is not None
                    and binding.value.value.unit is not param_def.required_rate_unit
                ):
                    issues.append(
                        CatalogIntegrityIssueV2(
                            code="INVALID_RATE_UNIT",
                            message=f"{alias}.{bname} has wrong rate unit",
                            entry_id=pid,
                        )
                    )
        for param_def in feature.parameters:
            pname = _stable_id_value(param_def.name)
            if param_def.required and pname not in bound_names:
                issues.append(
                    CatalogIntegrityIssueV2(
                        code="MISSING_REQUIRED_BINDING",
                        message=f"missing required binding {pname!r} for {alias!r}",
                        entry_id=pid,
                    )
                )
    return issues


def _validate_plugin_data_requirements(
    plugin: PluginDescriptorV2,
) -> list[CatalogIntegrityIssueV2]:
    pid = _stable_id_value(plugin.plugin_id)
    issues: list[CatalogIntegrityIssueV2] = []
    seen_ids: set[str] = set()
    optional_roles = {
        DataRequirementRoleV2.ANALYSIS_OPTIONAL,
        DataRequirementRoleV2.VALIDATION_OPTIONAL,
    }
    required_roles = {
        DataRequirementRoleV2.SIGNAL_REQUIRED,
        DataRequirementRoleV2.EXECUTION_REQUIRED,
        DataRequirementRoleV2.CONFIRMATION_REQUIRED,
    }
    allows_multiple_signal_tf = (
        plugin.signal_timeframe.mode is SignalTimeframeModeV2.ALLOWED_SET
        and len(plugin.signal_timeframe.allowed_minutes) > 1
    )
    reference_minutes = plugin.signal_timeframe.reference_minutes

    for requirement in plugin.data_requirements:
        rid = _stable_id_value(requirement.requirement_id)
        if rid in seen_ids:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="DUPLICATE_DATA_REQUIREMENT",
                    message=f"duplicate data requirement {rid!r}",
                    entry_id=pid,
                )
            )
        seen_ids.add(rid)
        if requirement.role in optional_roles and requirement.required:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="CONTRADICTORY_DATA_REQUIREMENT",
                    message=f"{rid} is optional role but marked required",
                    entry_id=pid,
                )
            )
        if requirement.role in required_roles and not requirement.required:
            issues.append(
                CatalogIntegrityIssueV2(
                    code="CONTRADICTORY_DATA_REQUIREMENT",
                    message=f"{rid} is required role but marked optional",
                    entry_id=pid,
                )
            )
        if allows_multiple_signal_tf:
            if isinstance(requirement.granularity, TimeframeGranularityV2):
                if (
                    requirement.role is DataRequirementRoleV2.SIGNAL_REQUIRED
                    and requirement.granularity.timeframe.value == reference_minutes
                ):
                    issues.append(
                        CatalogIntegrityIssueV2(
                            code="FIXED_SIGNAL_TF_GRANULARITY",
                            message=(
                                f"{rid} fixes signal granularity to reference minutes "
                                f"while plugin allows multiple signal timeframes"
                            ),
                            entry_id=pid,
                        )
                    )
            if isinstance(requirement.granularity, SnapshotGranularityV2):
                if (
                    requirement.role is DataRequirementRoleV2.SIGNAL_REQUIRED
                    and requirement.granularity.aligned_timeframe.value
                    == reference_minutes
                ):
                    issues.append(
                        CatalogIntegrityIssueV2(
                            code="FIXED_SIGNAL_TF_SNAPSHOT",
                            message=(
                                f"{rid} fixes snapshot alignment to reference minutes "
                                f"while plugin allows multiple signal timeframes"
                            ),
                            entry_id=pid,
                        )
                    )
        if (
            plugin.signal_timeframe.mode is SignalTimeframeModeV2.FIXED
            and isinstance(requirement.granularity, SelectedSignalTimeframeGranularityV2)
        ):
            issues.append(
                CatalogIntegrityIssueV2(
                    code="SELECTED_TF_ON_FIXED_PLUGIN",
                    message=f"{rid} uses selected_signal_timeframe on fixed-TF plugin",
                    entry_id=pid,
                )
            )
    return issues


def assert_production_catalog_integrity_v2() -> CatalogIntegrityReportV2:
    report = validate_catalog_integrity_v2()
    if not report.ok:
        joined = "; ".join(issue.message for issue in report.issues)
        raise InvalidCatalogDefinitionError(joined)
    return report
