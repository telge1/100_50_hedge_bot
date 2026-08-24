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
        ema_binding("ema_medium", 20),
        ema_binding("ema_slow", 59),
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


def valid_rule_based_long_strategy():
    from tests.strategy_lab.v2_fixtures import rule_based_signal_v2

    return minimal_strategy_spec_v2(
        signal=rule_based_signal_v2(Directionality.LONG),
        features=edc_features(),
    )


def valid_state_machine_long_strategy():
    from tests.strategy_lab.v2_fixtures import state_machine_signal_v2

    return minimal_strategy_spec_v2(
        signal=state_machine_signal_v2(),
        features=edc_features(),
    )


def _p4c_root_overrides(*, plugin_id: str, signal_minutes: int = 5):
    """Build StrategySpecV2 root overrides that satisfy P4C for production plugins."""
    import dataclasses
    from orderbook_analyse.strategy_lab.catalogs.v2.registry import PLUGIN_CATALOG_V2
    from orderbook_analyse.strategy_lab.models import (
        CausalityStatus,
        PluginProvenanceRefV2,
        ProvenanceSpecV2,
        ResearchParameterSpaceV2,
        ValidationRequirements,
        WarmupSpecV2,
        SignalEngineWarmupV2,
        ContractVersion,
    )
    from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
        PaddingNotApplicable,
        SourceLoadingPaddingV2,
        OutcomeEvaluationPaddingV2,
    )
    from tests.strategy_lab.conftest import _tf

    plugin = PLUGIN_CATALOG_V2.get(plugin_id)
    source = plugin.source_loading_padding
    outcome = plugin.outcome_evaluation_padding
    if source is None:
        source_loading = SourceLoadingPaddingV2(
            candle_history=PaddingNotApplicable(not_applicable=True),
            auxiliary_source_history=PaddingNotApplicable(not_applicable=True),
        )
    else:
        source_loading = source
    if outcome is None:
        outcome_evaluation = OutcomeEvaluationPaddingV2(
            post_window_duration=PaddingNotApplicable(not_applicable=True),
        )
    else:
        outcome_evaluation = outcome

    return {
        "data_requirements": plugin.data_requirements,
        "warmup": WarmupSpecV2(
            signal_engine=SignalEngineWarmupV2(
                minimum_bars=max(plugin.signal_warmup.minimum_bars, 79),
                bar_timeframe=_tf(signal_minutes),
            ),
            source_loading=source_loading,
            outcome_evaluation=outcome_evaluation,
        ),
        "timeframes": dataclasses.replace(
            minimal_strategy_spec_v2().timeframes,
            signal=_tf(signal_minutes),
        ),
        "research_parameter_space": ResearchParameterSpaceV2(dimensions=()),
        "validation_requirements": ValidationRequirements(
            require_causality_audit=True,
            require_strategy_parity_check=True,
            allowed_causality_statuses=(),
        ),
        "provenance": ProvenanceSpecV2(
            git_commit="0000000000000000000000000000000000000000",
            source_repository="orderbook_analyse",
            source_paths=("tests/strategy_lab/",),
            catalog_contract_version=ContractVersion(value="catalog/v2"),
            plugin_refs=(
                PluginProvenanceRefV2(
                    plugin_id=sid(plugin_id),
                    contract_version=ContractVersion(value="catalog/v2"),
                ),
            ),
            causality_status=plugin.causality_status,
        ),
    }


def p4c_valid_edc_strategy():
    import dataclasses
    from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
        AvailabilityTimingV2,
        EntryReferenceRuleV2,
    )

    base = valid_edc_strategy()
    overrides = _p4c_root_overrides(plugin_id="edc_m0_strict_sync", signal_minutes=5)
    return dataclasses.replace(
        base,
        **overrides,
        entry=dataclasses.replace(
            base.entry,
            signal_decision_timing=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            entry_reference_rule=EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR,
        ),
    )


def p4c_valid_cluster_strategy(*, signal_minutes: int = 15):
    import dataclasses
    from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
        AvailabilityTimingV2,
        EntryReferenceRuleV2,
    )

    base = valid_cluster_strategy()
    overrides = _p4c_root_overrides(
        plugin_id="cluster_sweep",
        signal_minutes=signal_minutes,
    )
    return dataclasses.replace(
        base,
        **overrides,
        entry=dataclasses.replace(
            base.entry,
            signal_decision_timing=AvailabilityTimingV2.CONFIRMATION_BAR_CLOSE,
            entry_reference_rule=EntryReferenceRuleV2.NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR,
        ),
    )


def p4c_valid_rule_based_long_strategy():
    import dataclasses
    from orderbook_analyse.strategy_lab.models import (
        ProvenanceSpecV2,
        ResearchParameterSpaceV2,
        ValidationRequirements,
        WarmupSpecV2,
        SignalEngineWarmupV2,
        ContractVersion,
        CausalityStatus,
    )
    from orderbook_analyse.strategy_lab.catalogs.v2.registry import FEATURE_CATALOG_V2
    from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
        SourceLoadingPaddingV2,
        OutcomeEvaluationPaddingV2,
        PaddingNotApplicable,
    )
    from tests.strategy_lab.conftest import _tf, _dur
    from orderbook_analyse.strategy_lab.models.enums import DurationUnit
    from decimal import Decimal
    from orderbook_analyse.strategy_lab.models.strategy import DurationValue

    base = valid_rule_based_long_strategy()
    reqs = []
    for binding in base.features:
        feature = FEATURE_CATALOG_V2.get(binding.catalog_feature_id.value)
        reqs.extend(feature.data_requirements)
    # Deduplicate by requirement_id while keeping first
    seen = set()
    unique = []
    for req in reqs:
        if req.requirement_id.value in seen:
            continue
        seen.add(req.requirement_id.value)
        unique.append(req)
    return dataclasses.replace(
        base,
        data_requirements=tuple(unique),
        warmup=WarmupSpecV2(
            signal_engine=SignalEngineWarmupV2(
                minimum_bars=79,
                bar_timeframe=_tf(5),
            ),
            source_loading=SourceLoadingPaddingV2(
                candle_history=DurationValue(value=Decimal("120"), unit=DurationUnit.HOURS),
                auxiliary_source_history=DurationValue(
                    value=Decimal("2"), unit=DurationUnit.HOURS
                ),
            ),
            outcome_evaluation=OutcomeEvaluationPaddingV2(
                post_window_duration=DurationValue(
                    value=Decimal("12"), unit=DurationUnit.HOURS
                ),
            ),
        ),
        research_parameter_space=ResearchParameterSpaceV2(dimensions=()),
        validation_requirements=ValidationRequirements(
            require_causality_audit=True,
            require_strategy_parity_check=True,
            allowed_causality_statuses=(),
        ),
        provenance=ProvenanceSpecV2(
            git_commit="0000000000000000000000000000000000000000",
            source_repository="orderbook_analyse",
            source_paths=("tests/strategy_lab/",),
            catalog_contract_version=ContractVersion(value="catalog/v2"),
            plugin_refs=(),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
        ),
    )
