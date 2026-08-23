"""Plugin catalog tests."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.catalogs import (
    FEATURE_CATALOG,
    PLUGIN_CATALOG,
    BoundFeatureRequirement,
    BoundParameterBinding,
    DataRequirementRole,
    ResearchConfirmationPolicy,
    SignalTimeframeMode,
    UnknownCatalogEntryError,
    get_plugin,
    validate_catalog_integrity,
)
from orderbook_analyse.strategy_lab.catalogs.models import (
    AdapterBindingStatus,
    AvailabilityTiming,
    DataSourceKind,
)
from orderbook_analyse.strategy_lab.models.enums import CausalityStatus, PluginKind, RateUnit
from orderbook_analyse.strategy_lab.models.strategy import IntParam


def test_reference_plugins_present() -> None:
    assert set(PLUGIN_CATALOG.ids) == {"cluster_sweep", "edc_m0_strict_sync"}


def test_edc_policy_explicitly_bound() -> None:
    plugin = get_plugin("edc_m0_strict_sync")
    assert plugin.confirmation_policy is ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE
    group = next(param for param in plugin.parameters if param.name == "group_id")
    assert "CORE_RESEARCH_SUPPORTIVE" in group.allowed_identifiers


def test_edc_missing_policy_fails_integrity() -> None:
    import dataclasses

    from orderbook_analyse.strategy_lab.catalogs import CatalogRegistry

    bad = dataclasses.replace(
        get_plugin("edc_m0_strict_sync"),
        confirmation_policy=None,
    )
    report = validate_catalog_integrity(
        plugins=CatalogRegistry(
            name="plugin",
            entries=(bad,),
            id_getter=lambda d: d.plugin_id,
        )
    )
    assert not report.ok
    assert any(issue.code == "MISSING_CONFIRMATION_POLICY" for issue in report.issues)


def test_edc_confirmation_sources_match_bound_policy() -> None:
    plugin = get_plugin("edc_m0_strict_sync")
    confirmation = [
        req
        for req in plugin.data_requirements
        if req.role is DataRequirementRole.CONFIRMATION_REQUIRED
    ]
    assert {req.source_kind for req in confirmation} == {
        DataSourceKind.PUBLIC_TRADES_1M,
        DataSourceKind.ORDERBOOK_OB200_V3_1M,
        DataSourceKind.LIQUIDITY_LOCATIONS,
    }
    for req in confirmation:
        assert req.required_for_policy is ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE


def test_cluster_has_no_confirmation_policy_default() -> None:
    plugin = get_plugin("cluster_sweep")
    assert plugin.confirmation_policy is None


def test_p3_bindings_do_not_use_float() -> None:
    from dataclasses import fields, is_dataclass

    from orderbook_analyse.strategy_lab.catalogs import PLUGIN_CATALOG

    def walk(obj: object) -> None:
        if isinstance(obj, float):
            raise AssertionError(f"float found in catalog binding: {obj!r}")
        if is_dataclass(obj):
            for field in fields(obj):
                walk(getattr(obj, field.name))
        elif isinstance(obj, tuple):
            for item in obj:
                walk(item)

    for plugin in PLUGIN_CATALOG:
        for requirement in plugin.required_features:
            walk(requirement)


def test_approach_bps_requires_basis_points_unit() -> None:
    plugin = get_plugin("cluster_sweep")
    approach = next(param for param in plugin.parameters if param.name == "approach_bps")
    assert approach.required_rate_unit is RateUnit.BASIS_POINTS


def test_edc_plugin_contract() -> None:
    plugin = get_plugin("edc_m0_strict_sync")
    assert plugin.kind is PluginKind.SIGNAL
    assert plugin.signal_timeframe.reference_minutes == 5
    assert plugin.signal_timeframe.mode is SignalTimeframeMode.FIXED
    assert plugin.execution_timeframe_minutes == 1
    assert plugin.decision_timing is AvailabilityTiming.SIGNAL_BAR_CLOSE
    assert plugin.entry_timing is AvailabilityTiming.ENTRY_BAR_OPEN
    assert plugin.entry_rule_id == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
    assert plugin.adapter_status is AdapterBindingStatus.ADAPTER_PENDING
    assert plugin.causality_status is CausalityStatus.CAUSAL_PROVEN

    aliases = {req.alias: req for req in plugin.required_features}
    assert aliases["ema_fast"].feature_id == "ema"
    assert aliases["ema_fast"].bindings[0].value.value == 9
    assert aliases["atr"].feature_id == "atr_wilder"
    assert aliases["atr"].bindings[0].value.value == 14


def test_cluster_plugin_contract() -> None:
    plugin = get_plugin("cluster_sweep")
    assert plugin.kind is PluginKind.SIGNAL
    assert plugin.signal_timeframe.reference_minutes == 15
    assert plugin.signal_timeframe.mode is SignalTimeframeMode.ALLOWED_SET
    assert plugin.signal_timeframe.allowed_minutes == (5, 15)
    assert plugin.decision_timing is AvailabilityTiming.CONFIRMATION_BAR_CLOSE
    assert plugin.entry_rule_id == "NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR"
    assert plugin.adapter_status is AdapterBindingStatus.ADAPTER_PENDING
    assert (
        plugin.causality_status
        is CausalityStatus.CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE
    )
    cluster_req = next(
        req for req in plugin.required_features if req.alias == "clusters"
    )
    assert cluster_req.feature_id == "lld_liquidity_clusters"


def test_plugin_feature_references_exist() -> None:
    feature_ids = set(FEATURE_CATALOG.ids)
    for plugin in PLUGIN_CATALOG:
        for requirement in plugin.required_features:
            assert requirement.feature_id in feature_ids


def test_data_roles_separate_signal_execution_and_optional() -> None:
    edc = get_plugin("edc_m0_strict_sync")
    roles = {req.source_kind: req for req in edc.data_requirements}
    assert roles[DataSourceKind.CANDLES_SIGNAL_TF].role is DataRequirementRole.SIGNAL_REQUIRED
    assert roles[DataSourceKind.CANDLES_SIGNAL_TF].required is True
    assert roles[DataSourceKind.CANDLES_EXECUTION_1M].role is DataRequirementRole.EXECUTION_REQUIRED
    assert roles[DataSourceKind.PUBLIC_TRADES_1M].role is DataRequirementRole.CONFIRMATION_REQUIRED
    assert roles[DataSourceKind.OPEN_INTEREST_1M].role is DataRequirementRole.ANALYSIS_OPTIONAL
    assert roles[DataSourceKind.OPEN_INTEREST_1M].required is False
    assert roles[DataSourceKind.LIQUIDATIONS].required is False

    cluster = get_plugin("cluster_sweep")
    cluster_roles = {req.source_kind: req for req in cluster.data_requirements}
    assert (
        cluster_roles[DataSourceKind.CANDLES_SIGNAL_TF].role
        is DataRequirementRole.SIGNAL_REQUIRED
    )
    assert (
        cluster_roles[DataSourceKind.LIQUIDITY_LOCATIONS].role
        is DataRequirementRole.SIGNAL_REQUIRED
    )
    assert (
        cluster_roles[DataSourceKind.PUBLIC_TRADES_1M].role
        is DataRequirementRole.ANALYSIS_OPTIONAL
    )
    assert cluster_roles["public_trades_1m"].required is False


def test_required_data_sources_have_availability_timing() -> None:
    for plugin in PLUGIN_CATALOG:
        for requirement in plugin.data_requirements:
            if requirement.required:
                assert requirement.availability is not None


def test_edc_warmup_vs_padding_separated() -> None:
    plugin = get_plugin("edc_m0_strict_sync")
    assert plugin.signal_warmup.total_signal_tf_bars == 79
    assert plugin.source_loading_padding is not None
    assert plugin.source_loading_padding.candle_pad_days == 5
    assert plugin.outcome_evaluation_padding is not None
    assert plugin.outcome_evaluation_padding.outcome_pad_hours == 12
    assert plugin.signal_warmup.minimum_bar_index == 79


def test_cluster_warmup_has_no_loading_or_outcome_padding() -> None:
    plugin = get_plugin("cluster_sweep")
    assert plugin.signal_warmup.total_signal_tf_bars == 79
    assert plugin.source_loading_padding is None
    assert plugin.outcome_evaluation_padding is None


def test_ema10_50_plugin_binding_without_new_feature() -> None:
    custom = BoundFeatureRequirement(
        alias="ema_fast",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=10)),),
    )
    assert custom.feature_id == "ema"
    assert custom.bindings[0].value.value == 10


def test_plugin_unknown_id_raises() -> None:
    with pytest.raises(UnknownCatalogEntryError):
        PLUGIN_CATALOG.get("edc.detect_cross_events")


def test_plugin_catalog_has_no_legacy_imports() -> None:
    root = Path(__file__).resolve().parents[2] / "src/orderbook_analyse/strategy_lab/catalogs"
    forbidden = (
        "orderbook_analyse.ema_dual_cross_multisource",
        "orderbook_analyse.cluster_sweep_research",
    )
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in forbidden:
                        assert not alias.name.startswith(bad), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                for bad in forbidden:
                    assert not node.module.startswith(bad), path
