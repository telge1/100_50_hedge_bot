"""Plugin signal validation for P4A."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.catalogs.v2.models import PluginDescriptorV2
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    PluginModeRequirementV2,
    PluginParameterBindingTargetV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.reserved_config import (
    RESERVED_PLUGIN_CONFIG_KEYS,
)
from orderbook_analyse.strategy_lab.models.enums import Directionality, PluginKind
from orderbook_analyse.strategy_lab.models.features import FeatureBindingSpec
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    ExpectedActualVersionContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)
from orderbook_analyse.strategy_lab.validation.parameters import (
    config_parameter_definitions,
    validate_plugin_config_bindings,
)


def validate_plugin_signal(
    signal: PluginSignalSpec,
    features: tuple[FeatureBindingSpec, ...],
    catalogs: CatalogBundleV2,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    path = "signal.plugin"
    plugin_ref = signal.plugin

    try:
        descriptor = catalogs.plugins.get(plugin_ref.plugin_id.value)
    except Exception:
        issues.append(
            _error(
                ValidationIssueCode.PLUGIN_UNKNOWN,
                path=f"{path}.plugin_id",
                message=f"unknown plugin id {plugin_ref.plugin_id.value!r}",
                context=UnknownIdentifierContext(identifier=plugin_ref.plugin_id),
            )
        )
        return tuple(issues)

    if plugin_ref.contract_version.value != descriptor.contract_version.value:
        issues.append(
            _error(
                ValidationIssueCode.PLUGIN_CONTRACT_VERSION,
                path=f"{path}.contract_version",
                message=(
                    f"plugin contract version must be "
                    f"{descriptor.contract_version.value!r}, got "
                    f"{plugin_ref.contract_version.value!r}"
                ),
                context=ExpectedActualVersionContext(
                    expected=descriptor.contract_version.value,
                    actual=plugin_ref.contract_version.value,
                ),
            )
        )
        return tuple(issues)

    if descriptor.kind is not PluginKind.SIGNAL:
        issues.append(
            _error(
                ValidationIssueCode.PLUGIN_KIND,
                path=path,
                message=f"plugin kind must be signal, got {descriptor.kind.value!r}",
                context=None,
            )
        )

    issues.extend(_validate_plugin_config(signal, descriptor))
    issues.extend(_validate_plugin_mode(signal, descriptor))
    issues.extend(_validate_plugin_policy(signal, descriptor))
    issues.extend(_validate_plugin_direction(signal, descriptor))
    issues.extend(_validate_required_features(signal, features, descriptor))
    return tuple(issues)


def _validate_plugin_config(
    signal: PluginSignalSpec,
    descriptor: PluginDescriptorV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    reserved = {key.value for key in RESERVED_PLUGIN_CONFIG_KEYS}
    signal_spec_param_names = {
        item.definition.name.value
        for item in descriptor.parameters
        if item.binding_target is PluginParameterBindingTargetV2.PLUGIN_SIGNAL_SPEC
    }
    seen_keys: set[str] = set()

    for index, entry in enumerate(signal.plugin.config):
        key_path = f"signal.plugin.config[{index}].key"
        if entry.key in reserved or entry.key in signal_spec_param_names:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_RESERVED_CONFIG_KEY,
                    path=key_path,
                    message=f"reserved plugin config key {entry.key!r}",
                    context=UnknownIdentifierContext(
                        identifier=StableIdentifier(value=entry.key)
                    ),
                )
            )
        if entry.key in seen_keys:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_DUPLICATE_PARAMETER,
                    path=key_path,
                    message=f"duplicate plugin config key {entry.key!r}",
                    context=UnknownIdentifierContext(
                        identifier=StableIdentifier(value=entry.key)
                    ),
                )
            )
        seen_keys.add(entry.key)

    config_definitions = config_parameter_definitions(descriptor.parameters)
    config_pairs = tuple((entry.key, entry.value) for entry in signal.plugin.config)
    issues.extend(
        validate_plugin_config_bindings(
            path_prefix="signal.plugin.config",
            definitions=config_definitions,
            config=config_pairs,
        )
    )
    return issues


def _validate_plugin_mode(
    signal: PluginSignalSpec,
    descriptor: PluginDescriptorV2,
) -> list[ValidationIssue]:
    contract = descriptor.mode_contract
    mode = signal.mode_id

    if contract.requirement is PluginModeRequirementV2.NOT_APPLICABLE:
        if mode is not None:
            return [
                _error(
                    ValidationIssueCode.PLUGIN_MODE_MISMATCH,
                    path="signal.mode_id",
                    message=(
                        f"plugin {descriptor.plugin_id.value!r} does not use mode_id"
                    ),
                    context=None,
                )
            ]
        return []

    allowed = {item.value for item in contract.allowed_modes}

    if contract.requirement is PluginModeRequirementV2.REQUIRED and mode is None:
        return [
            _error(
                ValidationIssueCode.PLUGIN_MODE_MISMATCH,
                path="signal.mode_id",
                message="required mode_id is missing",
                context=None,
            )
        ]

    if mode is None:
        return []

    if mode.value not in allowed:
        return [
            _error(
                ValidationIssueCode.PLUGIN_MODE_MISMATCH,
                path="signal.mode_id",
                message=(
                    f"mode_id {mode.value!r} is not allowed; expected one of "
                    f"{sorted(allowed)!r}"
                ),
                context=UnknownIdentifierContext(identifier=mode),
            )
        ]

    return []


def _validate_plugin_policy(
    signal: PluginSignalSpec,
    descriptor: PluginDescriptorV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if descriptor.confirmation_policy is not None:
        if signal.confirmation_policy is not descriptor.confirmation_policy:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_POLICY_MISMATCH,
                    path="signal.confirmation_policy",
                    message=(
                        f"confirmation policy must be "
                        f"{descriptor.confirmation_policy.value!r}"
                    ),
                    context=None,
                )
            )
    elif signal.confirmation_policy is not None:
        issues.append(
            _error(
                ValidationIssueCode.PLUGIN_POLICY_MISMATCH,
                path="signal.confirmation_policy",
                message="plugin does not declare a confirmation policy",
                context=None,
            )
        )
    return issues


def _validate_plugin_direction(
    signal: PluginSignalSpec,
    descriptor: PluginDescriptorV2,
) -> list[ValidationIssue]:
    if _direction_supported(descriptor, signal.directionality):
        return []
    return [
        _error(
            ValidationIssueCode.PLUGIN_DIRECTION_UNSUPPORTED,
            path="signal.directionality",
            message=(
                f"directionality {signal.directionality.value!r} is not supported by "
                f"plugin {descriptor.plugin_id.value!r}"
            ),
            context=None,
        )
    ]


def _validate_required_features(
    signal: PluginSignalSpec,
    features: tuple[FeatureBindingSpec, ...],
    descriptor: PluginDescriptorV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_alias = {feature.alias.value: feature for feature in features}

    for requirement in descriptor.required_features:
        alias = requirement.alias.value
        strategy_feature = by_alias.get(alias)
        if strategy_feature is None:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISSING,
                    path="signal.plugin",
                    message=(
                        f"required feature alias {alias!r} is missing from strategy "
                        "features"
                    ),
                    context=None,
                )
            )
            continue

        if strategy_feature.catalog_feature_id.value != requirement.feature_id.value:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISMATCH,
                    path="features",
                    message=(
                        f"feature alias {alias!r} must bind feature id "
                        f"{requirement.feature_id.value!r}"
                    ),
                    context=None,
                )
            )

        if (
            strategy_feature.catalog_contract_version.value
            != descriptor.contract_version.value
        ):
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISMATCH,
                    path="features",
                    message=(
                        f"feature alias {alias!r} must use contract version "
                        f"{descriptor.contract_version.value!r}"
                    ),
                    context=None,
                )
            )

        strategy_bindings = {
            binding.name.value: binding.value for binding in strategy_feature.bindings
        }
        required_bindings = {
            binding.name.value: binding.value for binding in requirement.bindings
        }
        if strategy_bindings != required_bindings:
            issues.append(
                _error(
                    ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISMATCH,
                    path="features",
                    message=(
                        f"feature alias {alias!r} bindings do not match plugin "
                        "required feature contract"
                    ),
                    context=None,
                )
            )

    return issues


def _direction_supported(
    descriptor: PluginDescriptorV2,
    directionality: Directionality,
) -> bool:
    supported = descriptor.supported_directions
    if supported is Directionality.BOTH:
        return True
    if directionality is Directionality.BOTH:
        return supported is Directionality.BOTH
    return directionality is supported


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
