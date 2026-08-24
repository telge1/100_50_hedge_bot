"""Structural tests for minimal P3.7 phase-1 V2 contracts."""

from __future__ import annotations

from dataclasses import fields

from orderbook_analyse.strategy_lab.models import (
    CostsSpecV2,
    EntrySpecV2,
    ExecutionAssumptionsV2,
    ExitSpecV2,
    PortfolioAssumptionsV2,
    ProvenanceSpecV2,
    ResearchDimensionV2,
    StrategySpecV2,
)


def test_entry_spec_v2_has_exact_phase1_fields() -> None:
    assert {f.name for f in fields(EntrySpecV2)} == {
        "signal_decision_timing",
        "entry_timing_anchor",
        "entry_reference_rule",
        "entry_price_reference",
    }


def test_execution_timeframe_exists_only_in_execution_assumptions_v2() -> None:
    assert "execution_timeframe" not in {f.name for f in fields(EntrySpecV2)}
    assert "execution_timeframe" in {f.name for f in fields(ExecutionAssumptionsV2)}


def test_exit_spec_v2_has_only_tp_sl_horizon() -> None:
    assert {f.name for f in fields(ExitSpecV2)} == {
        "take_profit",
        "stop_loss",
        "horizon",
    }


def test_same_bar_truth_remains_only_intrabar_policy() -> None:
    assert "same_bar_priority" not in {f.name for f in fields(ExitSpecV2)}


def test_costs_spec_v2_has_only_roundtrip_slippage_funding() -> None:
    assert {f.name for f in fields(CostsSpecV2)} == {
        "roundtrip_cost",
        "slippage",
        "funding",
    }


def test_portfolio_assumptions_v2_has_only_mode_and_compounding() -> None:
    assert {f.name for f in fields(PortfolioAssumptionsV2)} == {
        "evaluation_mode",
        "compounding",
    }


def test_execution_notional_currency_is_closed_not_free_string() -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
        ExecutionAssumptionsV2,
    )

    field = next(f for f in fields(ExecutionAssumptionsV2) if f.name == "notional_currency")
    assert "NotionalCurrencyV2" in str(field.type)


def test_universe_exists_only_at_strategy_v2_root() -> None:
    assert "universe" in {f.name for f in fields(StrategySpecV2)}
    assert "universe" not in {f.name for f in fields(ProvenanceSpecV2)}


def test_provenance_has_no_schema_version_or_universe_duplication() -> None:
    assert {f.name for f in fields(ProvenanceSpecV2)} == {
        "git_commit",
        "source_repository",
        "source_paths",
        "catalog_contract_version",
        "plugin_refs",
        "causality_status",
    }


def test_feature_data_requirements_are_typed_on_v2_catalog_model() -> None:
    from orderbook_analyse.strategy_lab.catalogs.v2.models import FeatureDescriptorV2

    field = next(f for f in fields(FeatureDescriptorV2) if f.name == "data_requirements")
    assert "DataRequirementSpecV2" in str(field.type)


def test_research_target_union_is_closed_via_schema_kinds() -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
        ExitParameterTargetV2,
        FeatureParameterTargetV2,
        PluginConfigParameterTargetV2,
        RoundtripCostTargetV2,
        SignalTimeframeTargetV2,
    )

    assert {cls._schema_kind for cls in (
        SignalTimeframeTargetV2,
        FeatureParameterTargetV2,
        PluginConfigParameterTargetV2,
        ExitParameterTargetV2,
        RoundtripCostTargetV2,
    )} == {
        "signal_timeframe",
        "feature_parameter",
        "plugin_config_parameter",
        "exit_parameter",
        "roundtrip_cost",
    }


def test_strategy_v2_root_uses_minimal_phase1_replacements() -> None:
    names = {f.name for f in fields(StrategySpecV2)}
    assert "baseline" not in names
    assert "fees" not in names
    assert "slippage" not in names
    assert "funding" not in names
    assert {"universe", "exit", "execution_assumptions", "costs", "portfolio_assumptions", "research_parameter_space", "provenance"} <= names
