"""P4A ValidationIssueCode coverage and reservation tests."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    FeatureDescriptorV2,
    OperatorDescriptorV2,
)
from orderbook_analyse.strategy_lab.catalogs.v2.registry import CatalogRegistryV2
from orderbook_analyse.strategy_lab.models import (
    ComparisonExpression,
    ContractVersion,
    FeatureBindingSpec,
    FeatureOutputReference,
    FeatureParameterBinding,
    IdentifierParam,
    IntParam,
    LiteralOperand,
    PluginRefV2,
    RateParam,
    RateValue,
    RuleBasedSignalSpec,
    SideName,
    SideRuleBundle,
)
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    AvailabilityTimingV2,
    CollectionShape,
    FeatureOutputDescriptorV2,
    FeatureOutputValueType,
    FeatureWarmupFormulaKindV2,
    FeatureWarmupFormulaV2,
    MissingValuePolicyV2,
    OperatorOperandSpecV2,
    OperatorSignatureV2,
    ParameterDefinitionV2,
    ParameterValueType,
    TemporalShape,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    EvaluationSemanticsV2,
    NullPolicyV2,
    OperandOriginV2,
    OperandTypeConstraintV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    Directionality,
    EvaluationTiming,
    RateUnit as RateUnitEnum,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from tests.strategy_lab.validation.conftest import (
    catalogs,
    cluster_plugin_signal,
    edc_features,
    edc_plugin_signal,
    sid,
    valid_cluster_strategy,
    valid_edc_strategy,
)

P4A_ACTIVE_CODES: frozenset[ValidationIssueCode] = frozenset(ValidationIssueCode)


def test_issue_code_inventory_is_closed_p4a_set() -> None:
    assert len(P4A_ACTIVE_CODES) == len(ValidationIssueCode)


def _emit(code: ValidationIssueCode, catalogs: CatalogBundleV2) -> None:
    spec = valid_edc_strategy()
    if code is ValidationIssueCode.FEATURE_DUPLICATE_ALIAS:
        broken = dataclasses.replace(
            spec,
            features=(edc_features()[0], edc_features()[0]),
        )
    elif code is ValidationIssueCode.FEATURE_UNKNOWN_ID:
        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("missing"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=edc_features()[0].bindings,
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_CONTRACT_VERSION:
        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("ema"),
                    catalog_contract_version=ContractVersion(value="catalog/v1"),
                    bindings=edc_features()[0].bindings,
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_DUPLICATE_PARAMETER:
        bindings = edc_features()[0].bindings * 2
        broken = dataclasses.replace(
            spec,
            features=(FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=bindings,
            ),),
        )
    elif code is ValidationIssueCode.FEATURE_MISSING_PARAMETER:
        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("ema"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=(),
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_UNKNOWN_PARAMETER:
        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("ema"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=edc_features()[0].bindings + (
                        FeatureParameterBinding(
                            name=sid("extra"),
                            value=IntParam(value=1),
                        ),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_PARAMETER_TYPE:
        from orderbook_analyse.strategy_lab.models import BoolParam

        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("ema"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=(
                        FeatureParameterBinding(
                            name=sid("period"),
                            value=BoolParam(value=True),
                        ),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_PARAMETER_BOUNDS:
        broken = dataclasses.replace(
            spec,
            features=(
                FeatureBindingSpec(
                    alias=sid("ema_fast"),
                    catalog_feature_id=sid("ema"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=(
                        FeatureParameterBinding(
                            name=sid("period"),
                            value=IntParam(value=0),
                        ),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_RATE_UNIT:
        broken = dataclasses.replace(
            valid_cluster_strategy(),
            features=(
                FeatureBindingSpec(
                    alias=sid("clusters"),
                    catalog_feature_id=sid("lld_liquidity_clusters"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=(
                        FeatureParameterBinding(
                            name=sid("gap_pct"),
                            value=RateParam(
                                value=RateValue(
                                    value=Decimal("0.10"),
                                    unit=RateUnitEnum.BASIS_POINTS,
                                )
                            ),
                        ),
                        FeatureParameterBinding(
                            name=sid("minimum_pools"),
                            value=IntParam(value=3),
                        ),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.FEATURE_IDENTIFIER_VALUE:
        bundle = _identifier_feature_bundle(catalogs)
        broken = dataclasses.replace(
            valid_edc_strategy(),
            features=(
                FeatureBindingSpec(
                    alias=sid("id_feature"),
                    catalog_feature_id=sid("test_identifier_param"),
                    catalog_contract_version=ContractVersion(value="catalog/v2"),
                    bindings=(
                        FeatureParameterBinding(
                            name=sid("label"),
                            value=IdentifierParam(value="gamma"),
                        ),
                    ),
                ),
            ),
            signal=edc_plugin_signal(),
        )
        report = validate_strategy_v2_p4a(broken, bundle)
        assert code in {issue.code for issue in report.issues}
        assert any(
            issue.path == "features[0].bindings[0].value" for issue in report.issues
        )
        return
    elif code is ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_ALIAS:
        trigger = ComparisonExpression(
            operator_id=sid("gt"),
            left=FeatureOutputReference(
                feature_alias=sid("missing"),
                output_id=sid("value"),
            ),
            right=FeatureOutputReference(
                feature_alias=sid("ema_slow"),
                output_id=sid("value"),
            ),
        )
        broken = _rule_strategy(trigger)
    elif code is ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_OUTPUT:
        trigger = ComparisonExpression(
            operator_id=sid("gt"),
            left=FeatureOutputReference(
                feature_alias=sid("ema_fast"),
                output_id=sid("missing"),
            ),
            right=FeatureOutputReference(
                feature_alias=sid("ema_slow"),
                output_id=sid("value"),
            ),
        )
        broken = _rule_strategy(trigger)
    elif code is ValidationIssueCode.OPERATOR_UNKNOWN:
        trigger = ComparisonExpression(
            operator_id=sid("missing_op"),
            left=FeatureOutputReference(
                feature_alias=sid("ema_fast"),
                output_id=sid("value"),
            ),
            right=FeatureOutputReference(
                feature_alias=sid("ema_slow"),
                output_id=sid("value"),
            ),
        )
        broken = _rule_strategy(trigger)
    elif code is ValidationIssueCode.OPERATOR_CONTRACT_VERSION:
        signal = _rule_signal(_comparison())
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(
                signal,
                operator_contract_version=ContractVersion(value="catalog/v1"),
            ),
            features=edc_features(),
        )
    elif code is ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH:
        trigger = ComparisonExpression(
            operator_id=sid("gt"),
            left=FeatureOutputReference(
                feature_alias=sid("ema_fast"),
                output_id=sid("value"),
            ),
            right=LiteralOperand(
                value=RateParam(
                    value=RateValue(value=Decimal("1"), unit=RateUnitEnum.PERCENT)
                )
            ),
        )
        broken = _rule_strategy(trigger)
    elif code is ValidationIssueCode.OPERATOR_RESULT_NOT_BOOLEAN:
        non_bool = OperatorDescriptorV2(
            operator_id=StableIdentifier(value="test_non_bool"),
            contract_version=ContractVersion(value="catalog/v2"),
            description="test",
            signatures=(
                OperatorSignatureV2(
                    operands=(
                        OperatorOperandSpecV2(
                            operand_index=0,
                            origin=OperandOriginV2.FEATURE_OUTPUT,
                            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
                        ),
                        OperatorOperandSpecV2(
                            operand_index=1,
                            origin=OperandOriginV2.FEATURE_OUTPUT,
                            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
                        ),
                    ),
                    result_type=OperandTypeConstraintV2.DECIMAL_SERIES,
                    evaluation_semantics=EvaluationSemanticsV2.CURRENT_CLOSED_OBSERVATION,
                    null_policy=NullPolicyV2.STRICT_REJECT,
                    observation=None,
                    description="test non-boolean result",
                ),
            ),
        )
        op_registry = CatalogRegistryV2(
            name="operator",
            entries=tuple(catalogs.operators) + (non_bool,),
            id_getter=lambda d: d.operator_id.value,
        )
        bundle = CatalogBundleV2(
            features=catalogs.features,
            operators=op_registry,
            plugins=catalogs.plugins,
        )
        trigger = ComparisonExpression(
            operator_id=sid("test_non_bool"),
            left=FeatureOutputReference(
                feature_alias=sid("ema_fast"),
                output_id=sid("value"),
            ),
            right=FeatureOutputReference(
                feature_alias=sid("ema_slow"),
                output_id=sid("value"),
            ),
        )
        report = validate_strategy_v2_p4a(_rule_strategy(trigger), bundle)
        assert code in {issue.code for issue in report.issues}
        return
    elif code is ValidationIssueCode.PLUGIN_UNKNOWN:
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(
                edc_plugin_signal(),
                plugin=PluginRefV2(
                    plugin_id=sid("missing"),
                    contract_version=ContractVersion(value="catalog/v2"),
                    config=(),
                ),
            ),
        )
    elif code is ValidationIssueCode.PLUGIN_CONTRACT_VERSION:
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(
                edc_plugin_signal(),
                plugin=PluginRefV2(
                    plugin_id=sid("edc_m0_strict_sync"),
                    contract_version=ContractVersion(value="catalog/v1"),
                    config=(),
                ),
            ),
        )
    elif code is ValidationIssueCode.PLUGIN_KIND:
        from orderbook_analyse.strategy_lab.catalogs.v2.registry import PLUGIN_CATALOG_V2
        from orderbook_analyse.strategy_lab.models.enums import PluginKind

        edc = PLUGIN_CATALOG_V2.get("edc_m0_strict_sync")
        wrong_kind = replace(edc, kind=PluginKind.ENTRY)
        plugin_registry = CatalogRegistryV2(
            name="plugin",
            entries=tuple(
                wrong_kind if entry.plugin_id.value == "edc_m0_strict_sync" else entry
                for entry in PLUGIN_CATALOG_V2
            ),
            id_getter=lambda d: d.plugin_id.value,
        )
        bundle = CatalogBundleV2(
            features=catalogs.features,
            operators=catalogs.operators,
            plugins=plugin_registry,
        )
        report = validate_strategy_v2_p4a(valid_edc_strategy(), bundle)
        assert code in {issue.code for issue in report.issues}
        return
    elif code is ValidationIssueCode.PLUGIN_RESERVED_CONFIG_KEY:
        broken = valid_cluster_strategy()
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=cluster_plugin_signal().plugin.config + (
                    ConfigEntry(key="mode_id", value=IntParam(value=1)),
                ),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_DUPLICATE_PARAMETER:
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=cluster_plugin_signal().plugin.config + (
                    ConfigEntry(key="expire_bars", value=IntParam(value=10)),
                ),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_MISSING_PARAMETER:
        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=(),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_UNKNOWN_PARAMETER:
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=cluster_plugin_signal().plugin.config + (
                    ConfigEntry(key="unknown_key", value=IntParam(value=1)),
                ),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_PARAMETER_TYPE:
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        config = list(cluster_plugin_signal().plugin.config)
        config[0] = ConfigEntry(
            key="minimum_cluster_pools",
            value=RateParam(
                value=RateValue(value=Decimal("3"), unit=RateUnitEnum.PERCENT)
            ),
        )
        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=tuple(config),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_PARAMETER_BOUNDS:
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        config = list(cluster_plugin_signal().plugin.config)
        config[0] = ConfigEntry(key="minimum_cluster_pools", value=IntParam(value=0))
        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=tuple(config),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_RATE_UNIT:
        from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

        config = list(cluster_plugin_signal().plugin.config)
        config[1] = ConfigEntry(
            key="approach_bps",
            value=RateParam(
                value=RateValue(value=Decimal("25"), unit=RateUnitEnum.PERCENT)
            ),
        )
        signal = dataclasses.replace(
            cluster_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=cluster_plugin_signal().plugin.plugin_id,
                contract_version=ContractVersion(value="catalog/v2"),
                config=tuple(config),
            ),
        )
        broken = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    elif code is ValidationIssueCode.PLUGIN_POLICY_MISMATCH:
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(edc_plugin_signal(), confirmation_policy=None),
        )
    elif code is ValidationIssueCode.PLUGIN_MODE_MISMATCH:
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(edc_plugin_signal(), mode_id=None),
        )
    elif code is ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISSING:
        broken = dataclasses.replace(spec, features=edc_features()[:1])
    elif code is ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISMATCH:
        features = list(edc_features())
        features[0] = dataclasses.replace(features[0], bindings=features[1].bindings)
        broken = dataclasses.replace(spec, features=tuple(features))
    elif code is ValidationIssueCode.PLUGIN_DIRECTION_UNSUPPORTED:
        from orderbook_analyse.strategy_lab.catalogs.v2.registry import PLUGIN_CATALOG_V2

        edc = PLUGIN_CATALOG_V2.get("edc_m0_strict_sync")
        long_only = replace(edc, supported_directions=Directionality.LONG)
        plugin_registry = CatalogRegistryV2(
            name="plugin",
            entries=tuple(
                long_only if entry.plugin_id.value == "edc_m0_strict_sync" else entry
                for entry in PLUGIN_CATALOG_V2
            ),
            id_getter=lambda d: d.plugin_id.value,
        )
        bundle = CatalogBundleV2(
            features=catalogs.features,
            operators=catalogs.operators,
            plugins=plugin_registry,
        )
        broken = dataclasses.replace(
            spec,
            signal=dataclasses.replace(
                edc_plugin_signal(),
                directionality=Directionality.SHORT,
            ),
        )
        report = validate_strategy_v2_p4a(broken, bundle)
        assert code in {issue.code for issue in report.issues}
        return
    else:
        raise AssertionError(f"no emitter fixture for {code}")

    report = validate_strategy_v2_p4a(broken, catalogs)
    assert code in {issue.code for issue in report.issues}, report.issues


def _identifier_feature_bundle(catalogs: CatalogBundleV2) -> CatalogBundleV2:
    feature = FeatureDescriptorV2(
        feature_id=StableIdentifier(value="test_identifier_param"),
        contract_version=ContractVersion(value="catalog/v2"),
        description="Synthetic identifier-parameter feature for validation tests.",
        outputs=(
            FeatureOutputDescriptorV2(
                output_id=StableIdentifier(value="value"),
                value_type=FeatureOutputValueType.DECIMAL,
                temporal_shape=TemporalShape.SERIES,
                collection_shape=CollectionShape.SINGLE,
                nullable=False,
                availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
                missing_value_policy=MissingValuePolicyV2.REJECT,
                warmup=FeatureWarmupFormulaV2(
                    formula_kind=FeatureWarmupFormulaKindV2.NO_SEPARATE_BAR_GATE,
                    parameter_name=None,
                    minimum_bars=1,
                    notes="test fixture",
                ),
                description="decimal series",
            ),
        ),
        parameters=(
            ParameterDefinitionV2(
                name=StableIdentifier(value="label"),
                value_type=ParameterValueType.IDENTIFIER,
                required=True,
                description="closed identifier set",
                allowed_identifiers=(
                    StableIdentifier(value="alpha"),
                    StableIdentifier(value="beta"),
                ),
                int_bounds=None,
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value=None,
                must_be_explicit=True,
                research_space_varies=False,
                baseline_defining=False,
            ),
        ),
        data_requirements=(),
        provenance=(),
    )
    feature_registry = CatalogRegistryV2(
        name="feature",
        entries=tuple(catalogs.features) + (feature,),
        id_getter=lambda descriptor: descriptor.feature_id.value,
    )
    return CatalogBundleV2(
        features=feature_registry,
        operators=catalogs.operators,
        plugins=catalogs.plugins,
    )


def _comparison() -> ComparisonExpression:
    return ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("ema_fast"),
            output_id=sid("value"),
        ),
        right=FeatureOutputReference(
            feature_alias=sid("ema_slow"),
            output_id=sid("value"),
        ),
    )


def _rule_signal(trigger: ComparisonExpression) -> object:
    return RuleBasedSignalSpec(
        operator_contract_version=ContractVersion(value="catalog/v2"),
        directionality=Directionality.LONG,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        ),
        short=None,
    )


def _rule_strategy(trigger: ComparisonExpression):
    return dataclasses.replace(
        valid_edc_strategy(),
        signal=_rule_signal(trigger),
        features=edc_features(),
    )


@pytest.mark.parametrize("code", sorted(P4A_ACTIVE_CODES, key=lambda c: c.value))
def test_p4a_active_issue_code_is_emitted(code: ValidationIssueCode, catalogs) -> None:
    _emit(code, catalogs)
