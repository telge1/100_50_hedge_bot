"""Shared builders for StrategySpec V1 unit tests."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_SCHEMA_VERSION,
    AnalysisRequirements,
    BaselineCell,
    BaselineSpec,
    CausalityStatus,
    ConfirmationSpec,
    DataRequirement,
    DataRequirementStatus,
    Directionality,
    DurationUnit,
    DurationValue,
    EntrySpec,
    ExecutionAssumptions,
    ExitMode,
    ExitSpec,
    FeatureRef,
    FeesSpec,
    IntrabarPolicy,
    InvalidationSpec,
    Metadata,
    MirrorMode,
    ModelingStatus,
    ModelingStatusBlock,
    PluginKind,
    PluginProvenanceRef,
    PluginRef,
    PortfolioAssumptions,
    ProvenanceSpec,
    RateUnit,
    RateValue,
    ResearchParameterSpace,
    SameBarPriority,
    SetupSpec,
    SideName,
    SideSpec,
    SignalSpec,
    StrategySpec,
    TimeframeUnit,
    TimeframeValue,
    Timeframes,
    TriggerSpec,
    UniverseSpec,
    ValidationRequirements,
    WarmupSpec,
)


def _tf(minutes: int) -> TimeframeValue:
    return TimeframeValue(value=minutes, unit=TimeframeUnit.MINUTES)


def _rate(value: str, unit: RateUnit) -> RateValue:
    return RateValue(value=Decimal(value), unit=unit)


def _dur(value: str, unit: DurationUnit) -> DurationValue:
    return DurationValue(value=Decimal(value), unit=unit)


def _plugin(pid: str = "test.plugin", kind: PluginKind = PluginKind.SIGNAL) -> PluginRef:
    return PluginRef(id=pid, version="1.0.0", kind=kind)


def minimal_strategy_spec(**overrides: object) -> StrategySpec:
    """Fully populated minimal StrategySpec (all required sections)."""
    cell = BaselineCell(
        timeframe=_tf(5),
        mode_id="mode_a",
        group="core",
        take_profit=_rate("0.75", RateUnit.PERCENT),
        stop_loss=_rate("0.5", RateUnit.PERCENT),
        horizon=_dur("8", DurationUnit.HOURS),
        cost=_rate("15", RateUnit.BASIS_POINTS),
        cell_id="cell_a",
    )
    kwargs: dict[str, object] = {
        "metadata": Metadata(
            schema_version=STRATEGY_SPEC_SCHEMA_VERSION,
            strategy_id="test.minimal",
            strategy_version="0.1.0",
            family="test",
            variant="minimal",
            title="Minimal StrategySpec",
        ),
        "universe": UniverseSpec(role="fixed", symbols=("XRPUSDT",)),
        "timeframes": Timeframes(signal=_tf(5), execution=_tf(1)),
        "data_requirements": (
            DataRequirement(
                id="klines_1m",
                status=DataRequirementStatus.REQUIRED,
                source="clickhouse",
                timeframe=_tf(1),
            ),
        ),
        "warmup": WarmupSpec(
            ema_slow_bars=200,
            extra_bars=50,
            pad_days=1,
            outcome_pad_hours=8,
            source_pad_hours=2,
        ),
        "features": (FeatureRef(id="ema.cross", version="1.0.0"),),
        "signal": SignalSpec(
            plugin=_plugin("signal.ema_dual_cross", PluginKind.SIGNAL),
            mode_id="mode_a",
            directionality=Directionality.BOTH,
        ),
        "setup": SetupSpec(
            description="EMA structure aligned",
            decision_at="signal_bar_close",
        ),
        "trigger": TriggerSpec(
            description="dual cross event",
            plugin=_plugin("trigger.dual_cross", PluginKind.SIGNAL),
        ),
        "confirmation": ConfirmationSpec(
            description="optional gate layer",
            gates_policy_id="none",
            gates_policy_version="1.0.0",
        ),
        "long": SideSpec(
            name=SideName.LONG,
            mirror_mode=MirrorMode.NONE,
        ),
        "short": SideSpec(
            name=SideName.SHORT,
            mirror_mode=MirrorMode.FULL_MIRROR,
            mirror_of=SideName.LONG,
        ),
        "entry": EntrySpec(
            decision_point="signal_bar_close",
            tradable_point="next_execution_bar_open",
            rule_id="entry.next_open",
            plugin=_plugin("entry.next_open", PluginKind.ENTRY),
        ),
        "invalidation": InvalidationSpec(description="none"),
        "exit": ExitSpec(
            mode=ExitMode.PARAMETRIC,
            take_profit=_rate("0.75", RateUnit.PERCENT),
            stop_loss=_rate("0.5", RateUnit.PERCENT),
            horizon=_dur("8", DurationUnit.HOURS),
            same_bar_priority=SameBarPriority.SL_FIRST,
            require_full_horizon=True,
            incomplete_outcome_reason="horizon_truncated",
        ),
        "intrabar_policy": IntrabarPolicy(same_bar_priority=SameBarPriority.SL_FIRST),
        "execution_assumptions": ExecutionAssumptions(
            notional=Decimal("100"),
            notional_currency="USDT",
            fill_model="next_open",
        ),
        "fees": FeesSpec(roundtrip_cost=_rate("15", RateUnit.BASIS_POINTS)),
        "slippage": ModelingStatusBlock(status=ModelingStatus.NOT_MODELED),
        "funding": ModelingStatusBlock(status=ModelingStatus.UNAVAILABLE),
        "portfolio_assumptions": PortfolioAssumptions(
            evaluation_mode="per_trade",
            one_trade_per_candidate=True,
        ),
        "baseline": BaselineSpec(cell=cell, is_reference=True),
        "research_parameter_space": ResearchParameterSpace(cells=(cell,)),
        "analysis_requirements": AnalysisRequirements(
            required_label_fields=("mfe", "mae", "realized_pnl"),
        ),
        "validation_requirements": ValidationRequirements(
            require_causality_audit=True,
            require_strategy_parity_check=True,
            allowed_causality_statuses=(CausalityStatus.CAUSALITY_UNPROVEN,),
        ),
        "provenance": ProvenanceSpec(
            source_of_truth_module="tests.strategy_lab",
            source_of_truth_path="tests/strategy_lab/",
            git_commit="0000000000000000000000000000000000000000",
            strategy_ref="test.minimal@0.1.0",
            policy_ref="none@1.0.0",
            plugin_refs=(
                PluginProvenanceRef(plugin_id="signal.ema_dual_cross", version="1.0.0"),
            ),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
            causality_claim="unit-test fixture only",
            external_runtime_dependencies=("TRP",),
            known_limitations=("fixture",),
            notes=("P1 construct test",),
        ),
    }
    kwargs.update(overrides)
    return StrategySpec(**kwargs)  # type: ignore[arg-type]


def edc_shaped_strategy_spec() -> StrategySpec:
    """EDC-like structure without importing legacy EDC packages."""
    cell = BaselineCell(
        timeframe=_tf(5),
        mode_id="edc_core",
        group="core_sources",
        take_profit=_rate("0.75", RateUnit.PERCENT),
        stop_loss=_rate("0.50", RateUnit.PERCENT),
        horizon=_dur("8", DurationUnit.HOURS),
        cost=_rate("0.0015", RateUnit.FRACTION),
        cell_id="edc_5m_0.75pct_8h",
    )
    return minimal_strategy_spec(
        metadata=Metadata(
            schema_version=STRATEGY_SPEC_SCHEMA_VERSION,
            strategy_id="edc.multisource.shaped",
            strategy_version="1.0.0",
            family="ema_dual_cross",
            variant="multisource_shaped",
            title="EDC-shaped StrategySpec (no legacy import)",
        ),
        timeframes=Timeframes(signal=_tf(5), execution=_tf(1)),
        long=SideSpec(name=SideName.LONG, mirror_mode=MirrorMode.NONE),
        short=SideSpec(
            name=SideName.SHORT,
            mirror_mode=MirrorMode.SIGN_FLIP,
            mirror_of=SideName.LONG,
            sign_flip_fields=("direction",),
        ),
        exit=ExitSpec(
            mode=ExitMode.PARAMETRIC,
            take_profit=_rate("0.75", RateUnit.PERCENT),
            stop_loss=_rate("0.50", RateUnit.PERCENT),
            horizon=_dur("8", DurationUnit.HOURS),
            same_bar_priority=SameBarPriority.SL_FIRST,
            require_full_horizon=True,
            incomplete_outcome_reason="incomplete_bars",
        ),
        fees=FeesSpec(roundtrip_cost=_rate("0.0015", RateUnit.FRACTION)),
        baseline=BaselineSpec(cell=cell, is_reference=True),
        research_parameter_space=ResearchParameterSpace(
            cells=(
                cell,
                BaselineCell(
                    timeframe=_tf(5),
                    mode_id="edc_core",
                    group="core_sources",
                    take_profit=_rate("0.0075", RateUnit.FRACTION),
                    stop_loss=_rate("0.0050", RateUnit.FRACTION),
                    horizon=_dur("480", DurationUnit.MINUTES),
                    cost=_rate("0.0015", RateUnit.FRACTION),
                    cell_id="edc_fraction_alt_cell",
                ),
            ),
            notes="baseline vs research space are separate sections",
        ),
        provenance=ProvenanceSpec(
            source_of_truth_module=(
                "orderbook_analyse.ema_dual_cross_multisource"
                ".tolerance_research.multicoin_frozen_validation"
            ),
            source_of_truth_path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/multicoin_frozen_validation/"
            ),
            git_commit="5cb698a047d8d002243dba3bf7d0bda18218a397",
            strategy_ref="edc.multisource@frozen",
            policy_ref="gates.none@1",
            plugin_refs=(
                PluginProvenanceRef(plugin_id="signal.ema_dual_cross", version="1.0.0"),
            ),
            causality_status=CausalityStatus.CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE,
            causality_claim="reusable when TRP runtime available",
            external_runtime_dependencies=("TRP",),
            known_limitations=("shaped fixture; not a compiled adapter",),
        ),
    )


def cluster_shaped_strategy_spec() -> StrategySpec:
    """Cluster-sweep-like structure without importing legacy packages."""
    cell = BaselineCell(
        timeframe=_tf(15),
        mode_id="cluster_sweep",
        group="sweep",
        take_profit=_rate("0.005", RateUnit.FRACTION),
        stop_loss=_rate("0.003", RateUnit.FRACTION),
        horizon=_dur("4", DurationUnit.HOURS),
        cost=_rate("4", RateUnit.BASIS_POINTS),
        cell_id="cluster_15m_0.005frac",
    )
    return minimal_strategy_spec(
        metadata=Metadata(
            schema_version=STRATEGY_SPEC_SCHEMA_VERSION,
            strategy_id="cluster.sweep.shaped",
            strategy_version="1.0.0",
            family="cluster_sweep",
            variant="research_shaped",
            title="Cluster-sweep-shaped StrategySpec (no legacy import)",
        ),
        timeframes=Timeframes(signal=_tf(15), execution=_tf(1)),
        setup=SetupSpec(
            description="liquidity cluster present",
            decision_at="signal_bar_close",
        ),
        trigger=TriggerSpec(description="sweep through cluster"),
        confirmation=ConfirmationSpec(
            description="orderbook confirmation gate",
            status=ModelingStatus.MODELED,
        ),
        exit=ExitSpec(
            mode=ExitMode.PARAMETRIC,
            take_profit=_rate("0.005", RateUnit.FRACTION),
            stop_loss=_rate("0.003", RateUnit.FRACTION),
            horizon=_dur("4", DurationUnit.HOURS),
            same_bar_priority=SameBarPriority.TP_FIRST,
            require_full_horizon=False,
            incomplete_outcome_reason="open_at_horizon",
        ),
        fees=FeesSpec(roundtrip_cost=_rate("4", RateUnit.BASIS_POINTS)),
        baseline=BaselineSpec(cell=cell, is_reference=True),
        research_parameter_space=ResearchParameterSpace(cells=(cell,)),
        provenance=ProvenanceSpec(
            source_of_truth_module="orderbook_analyse.cluster_sweep_research",
            source_of_truth_path="src/orderbook_analyse/cluster_sweep_research/",
            git_commit="5cb698a047d8d002243dba3bf7d0bda18218a397",
            strategy_ref="cluster.sweep@research",
            policy_ref="none@1",
            plugin_refs=(
                PluginProvenanceRef(plugin_id="signal.cluster_sweep", version="1.0.0"),
            ),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
            causality_claim="research fixture",
            external_runtime_dependencies=(),
            known_limitations=("shaped fixture",),
        ),
    )
