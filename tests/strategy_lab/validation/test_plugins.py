"""Plugin signal validation tests."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    ConfigEntry,
    ContractVersion,
    Directionality,
    IntParam,
    PluginRefV2,
    RateParam,
    RateValue,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from tests.strategy_lab.validation.conftest import (
    catalogs,
    cluster_features,
    cluster_plugin_signal,
    edc_features,
    edc_plugin_signal,
    sid,
    valid_cluster_strategy,
    valid_edc_strategy,
)


def test_valid_edc_plugin(catalogs) -> None:
    assert validate_strategy_v2_p4a(valid_edc_strategy(), catalogs).is_valid


def test_valid_cluster_plugin(catalogs) -> None:
    assert validate_strategy_v2_p4a(valid_cluster_strategy(), catalogs).is_valid


def test_unknown_plugin(catalogs) -> None:
    signal = dataclasses.replace(
        edc_plugin_signal(),
        plugin=PluginRefV2(
            plugin_id=sid("missing"),
            contract_version=ContractVersion(value="catalog/v2"),
            config=(),
        ),
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(issue.code is ValidationIssueCode.PLUGIN_UNKNOWN for issue in report.issues)


def test_wrong_contract_version(catalogs) -> None:
    signal = dataclasses.replace(
        edc_plugin_signal(),
        plugin=PluginRefV2(
            plugin_id=sid("edc_m0_strict_sync"),
            contract_version=ContractVersion(value="catalog/v1"),
            config=(),
        ),
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_CONTRACT_VERSION
        for issue in report.issues
    )


def test_reserved_config_key(catalogs) -> None:
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
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_RESERVED_CONFIG_KEY
        for issue in report.issues
    )


def test_duplicate_config_key(catalogs) -> None:
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
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_DUPLICATE_PARAMETER
        for issue in report.issues
    )


def test_missing_plugin_parameter(catalogs) -> None:
    signal = dataclasses.replace(
        cluster_plugin_signal(),
        plugin=PluginRefV2(
            plugin_id=cluster_plugin_signal().plugin.plugin_id,
            contract_version=ContractVersion(value="catalog/v2"),
            config=(),
        ),
    )
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_MISSING_PARAMETER
        for issue in report.issues
    )


def test_wrong_plugin_parameter_type(catalogs) -> None:
    config = list(cluster_plugin_signal().plugin.config)
    config[0] = ConfigEntry(key="minimum_cluster_pools", value=RateParam(
        value=RateValue(value=Decimal("3"), unit=RateUnit.PERCENT)
    ))
    signal = dataclasses.replace(
        cluster_plugin_signal(),
        plugin=PluginRefV2(
            plugin_id=cluster_plugin_signal().plugin.plugin_id,
            contract_version=ContractVersion(value="catalog/v2"),
            config=tuple(config),
        ),
    )
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_PARAMETER_TYPE
        for issue in report.issues
    )


def test_wrong_confirmation_policy(catalogs) -> None:
    signal = dataclasses.replace(
        edc_plugin_signal(),
        confirmation_policy=None,
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_POLICY_MISMATCH
        for issue in report.issues
    )


def test_missing_mode_id(catalogs) -> None:
    signal = dataclasses.replace(edc_plugin_signal(), mode_id=None)
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_MODE_MISMATCH
        for issue in report.issues
    )


def test_invalid_mode_id(catalogs) -> None:
    signal = dataclasses.replace(edc_plugin_signal(), mode_id=sid("wrong_mode"))
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_MODE_MISMATCH
        for issue in report.issues
    )


def test_cluster_mode_id_not_applicable(catalogs) -> None:
    signal = dataclasses.replace(cluster_plugin_signal(), mode_id=sid("any"))
    spec = dataclasses.replace(valid_cluster_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_MODE_MISMATCH
        for issue in report.issues
    )


def test_unsupported_directionality(catalogs) -> None:
    from dataclasses import replace

    from orderbook_analyse.strategy_lab.catalogs.v2.registry import (
        CatalogRegistryV2,
        PLUGIN_CATALOG_V2,
    )
    from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2

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
    signal = dataclasses.replace(
        edc_plugin_signal(),
        directionality=Directionality.SHORT,
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, bundle)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_DIRECTION_UNSUPPORTED
        for issue in report.issues
    )


def test_missing_required_feature(catalogs) -> None:
    spec = dataclasses.replace(valid_edc_strategy(), features=edc_features()[:1])
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISSING
        for issue in report.issues
    )


def test_required_feature_binding_mismatch(catalogs) -> None:
    features = list(edc_features())
    features[0] = dataclasses.replace(features[0], bindings=features[1].bindings)
    spec = dataclasses.replace(valid_edc_strategy(), features=tuple(features))
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.PLUGIN_REQUIRED_FEATURE_MISMATCH
        for issue in report.issues
    )
