"""Feature binding validation and resolution index for P4A."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.catalogs.v2.models import FeatureDescriptorV2
from orderbook_analyse.strategy_lab.models.contracts_v2.feature import (
    FeatureOutputDescriptorV2,
)
from orderbook_analyse.strategy_lab.models.features import FeatureBindingSpec
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    ExpectedActualVersionContext,
    FeatureAliasContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)
from orderbook_analyse.strategy_lab.validation.parameters import validate_parameter_bindings


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedFeatureOutput:
    binding: FeatureBindingSpec
    feature: FeatureDescriptorV2
    output: FeatureOutputDescriptorV2


class FeatureResolutionIndex:
    """Read-only alias lookup built from validated feature bindings."""

    __slots__ = ("_aliases",)

    def __init__(self, aliases: dict[str, tuple[FeatureBindingSpec, FeatureDescriptorV2]]) -> None:
        self._aliases = aliases

    def has_alias(self, alias: StableIdentifier) -> bool:
        return alias.value in self._aliases

    def get_binding_and_feature(
        self,
        alias: StableIdentifier,
    ) -> tuple[FeatureBindingSpec, FeatureDescriptorV2] | None:
        return self._aliases.get(alias.value)

    def resolve_output(
        self,
        alias: StableIdentifier,
        output_id: StableIdentifier,
    ) -> ResolvedFeatureOutput | None:
        entry = self._aliases.get(alias.value)
        if entry is None:
            return None
        binding, feature = entry
        for output in feature.outputs:
            if output.output_id.value == output_id.value:
                return ResolvedFeatureOutput(
                    binding=binding,
                    feature=feature,
                    output=output,
                )
        return None


def validate_features(
    features: tuple[FeatureBindingSpec, ...],
    catalogs: CatalogBundleV2,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    seen_aliases: set[str] = set()
    alias_map: dict[str, tuple[FeatureBindingSpec, FeatureDescriptorV2]] = {}

    for index, binding in enumerate(features):
        path = f"features[{index}]"
        alias_value = binding.alias.value
        if alias_value in seen_aliases:
            issues.append(
                _error(
                    ValidationIssueCode.FEATURE_DUPLICATE_ALIAS,
                    path=f"{path}.alias",
                    message=f"duplicate feature alias {alias_value!r}",
                    context=FeatureAliasContext(feature_alias=binding.alias),
                )
            )
        seen_aliases.add(alias_value)

        try:
            feature = catalogs.features.get(binding.catalog_feature_id.value)
        except Exception:
            issues.append(
                _error(
                    ValidationIssueCode.FEATURE_UNKNOWN_ID,
                    path=f"{path}.catalog_feature_id",
                    message=f"unknown feature id {binding.catalog_feature_id.value!r}",
                    context=UnknownIdentifierContext(
                        identifier=binding.catalog_feature_id
                    ),
                )
            )
            continue

        if binding.catalog_contract_version.value != feature.contract_version.value:
            issues.append(
                _error(
                    ValidationIssueCode.FEATURE_CONTRACT_VERSION,
                    path=f"{path}.catalog_contract_version",
                    message=(
                        f"feature contract version must be "
                        f"{feature.contract_version.value!r}, got "
                        f"{binding.catalog_contract_version.value!r}"
                    ),
                    context=ExpectedActualVersionContext(
                        expected=feature.contract_version.value,
                        actual=binding.catalog_contract_version.value,
                    ),
                )
            )

        binding_pairs = tuple((item.name, item.value) for item in binding.bindings)
        issues.extend(
            validate_parameter_bindings(
                path_prefix=f"{path}.bindings",
                definitions=feature.parameters,
                bindings=binding_pairs,
                code_prefix="FEATURE",
            )
        )

        if alias_value not in alias_map:
            alias_map[alias_value] = (binding, feature)

    return tuple(issues)


def build_feature_resolution_index(
    features: tuple[FeatureBindingSpec, ...],
    catalogs: CatalogBundleV2,
) -> FeatureResolutionIndex:
    alias_map: dict[str, tuple[FeatureBindingSpec, FeatureDescriptorV2]] = {}
    for binding in features:
        try:
            feature = catalogs.features.get(binding.catalog_feature_id.value)
        except Exception:
            continue
        if binding.catalog_contract_version.value != feature.contract_version.value:
            continue
        alias_map[binding.alias.value] = (binding, feature)
    return FeatureResolutionIndex(alias_map)


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
