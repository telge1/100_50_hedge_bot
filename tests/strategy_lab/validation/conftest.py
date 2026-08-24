"""Shared fixtures for P4A validation tests."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    ContractVersion,
    FeatureBindingSpec,
    FeatureParameterBinding,
    IntParam,
    PluginRefV2,
    PluginSignalSpec,
    RateParam,
    RateValue,
    StableIdentifier,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import Directionality, RateUnit
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    ConfigEntry,
    ConfirmationSpec,
    InvalidationSpec,
    SetupSpec,
    TriggerSpec,
)
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2
from tests.strategy_lab.v2_fixtures import minimal_strategy_spec_v2, sid
import pytest


@pytest.fixture
def catalogs():
    return production_catalog_bundle_v2()


def ema_binding(alias: str, period: int) -> FeatureBindingSpec:
    return FeatureBindingSpec(
        alias=sid(alias),
        catalog_feature_id=sid("ema"),
        catalog_contract_version=ContractVersion(value="catalog/v2"),
        bindings=(
            FeatureParameterBinding(
                name=sid("period"),
                value=IntParam(value=period),
            ),
        ),
    )


def atr_binding(alias: str = "atr", period: int = 14) -> FeatureBindingSpec:
    return FeatureBindingSpec(
        alias=sid(alias),
        catalog_feature_id=sid("atr_wilder"),
        catalog_contract_version=ContractVersion(value="catalog/v2"),
        bindings=(
            FeatureParameterBinding(
                name=sid("period"),
                value=IntParam(value=period),
            ),
        ),
    )


def cluster_binding() -> FeatureBindingSpec:
    return FeatureBindingSpec(
        alias=sid("clusters"),
        catalog_feature_id=sid("lld_liquidity_clusters"),
        catalog_contract_version=ContractVersion(value="catalog/v2"),
        bindings=(
            FeatureParameterBinding(
                name=sid("gap_pct"),
                value=RateParam(
                    value=RateValue(value=Decimal("0.10"), unit=RateUnit.PERCENT)
                ),
            ),
            FeatureParameterBinding(
                name=sid("minimum_pools"),
                value=IntParam(value=3),
            ),
        ),
    )


def edc_features() -> tuple[FeatureBindingSpec, ...]:
    return (
        ema_binding("ema_fast", 9),
        ema_binding("ema_slow", 20),
        atr_binding(),
    )


def cluster_features() -> tuple[FeatureBindingSpec, ...]:
    return (
        ema_binding("ema_fast", 9),
        ema_binding("ema_medium", 20),
        ema_binding("ema_slow", 59),
        atr_binding(),
        cluster_binding(),
    )


def edc_plugin_signal() -> PluginSignalSpec:
    return PluginSignalSpec(
        plugin=PluginRefV2(
            plugin_id=sid("edc_m0_strict_sync"),
            contract_version=ContractVersion(value="catalog/v2"),
            config=(),
        ),
        mode_id=sid("m0_strict_sync"),
        directionality=Directionality.BOTH,
        rules_embedded_in_yaml=False,
        confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
        setup=SetupSpec(description="setup", decision_at="signal_bar_close"),
        trigger=TriggerSpec(description="trigger"),
        confirmation=ConfirmationSpec(description="confirmation"),
        invalidation=InvalidationSpec(description="invalidation"),
    )


def cluster_plugin_signal() -> PluginSignalSpec:
    return PluginSignalSpec(
        plugin=PluginRefV2(
            plugin_id=sid("cluster_sweep"),
            contract_version=ContractVersion(value="catalog/v2"),
            config=(
                ConfigEntry(key="minimum_cluster_pools", value=IntParam(value=3)),
                ConfigEntry(key="expire_bars", value=IntParam(value=24)),
                ConfigEntry(
                    key="approach_bps",
                    value=RateParam(
                        value=RateValue(value=Decimal("25"), unit=RateUnit.BASIS_POINTS)
                    ),
                ),
                ConfigEntry(key="require_cluster_entry", value=BoolParam(value=True)),
                ConfigEntry(
                    key="gap_pct",
                    value=RateParam(
                        value=RateValue(value=Decimal("0.10"), unit=RateUnit.PERCENT)
                    ),
                ),
            ),
        ),
        mode_id=None,
        directionality=Directionality.BOTH,
        rules_embedded_in_yaml=False,
        confirmation_policy=None,
        setup=SetupSpec(description="setup", decision_at="signal_bar_close"),
        trigger=TriggerSpec(description="trigger"),
        confirmation=ConfirmationSpec(description="confirmation"),
        invalidation=InvalidationSpec(description="invalidation"),
    )


def valid_edc_strategy():
    return minimal_strategy_spec_v2(
        signal=edc_plugin_signal(),
        features=edc_features(),
    )


def valid_cluster_strategy():
    return minimal_strategy_spec_v2(
        signal=cluster_plugin_signal(),
        features=cluster_features(),
    )
