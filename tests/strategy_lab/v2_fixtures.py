"""Shared builders for StrategySpec V2 unit tests."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_V2_SCHEMA_VERSION,
    AnalysisRequirements,
    AvailabilityTimingV2,
    BaselineCell,
    BaselineSpec,
    CausalityStatus,
    ComparisonExpression,
    ConfirmationSpec,
    ContractVersion,
    DataRequirementRoleV2,
    DataRequirementSpecV2,
    DataSourceKindV2,
    Directionality,
    DurationUnit,
    DurationValue,
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntrySpecV2,
    EntryTimingAnchorV2,
    EvaluationTiming,
    ExecutionAssumptions,
    ExitMode,
    ExitSpec,
    FeatureBindingSpec,
    FeatureOutputReference,
    FeatureParameterBinding,
    FeesSpec,
    IdentifierParam,
    IntrabarPolicy,
    InvalidationSpec,
    IntParam,
    LiteralOperand,
    Metadata,
    ModelingStatus,
    ModelingStatusBlock,
    OutcomeEvaluationPaddingV2,
    PluginKind,
    PluginProvenanceRef,
    PluginRef,
    PluginRefV2,
    PluginSignalSpec,
    PortfolioAssumptions,
    ProvenanceSpec,
    RateUnit,
    ResearchParameterSpace,
    ResetEvent,
    ResetRule,
    RuleBasedSignalSpec,
    SameBarPriority,
    SetupSpec,
    SideName,
    SideRuleBundle,
    SignalEmissionSpec,
    SignalEngineWarmupV2,
    SourceLoadingPaddingV2,
    StableIdentifier,
    StateMachineSignalSpec,
    StateSpec,
    StrategySpecV2,
    TimeframeGranularityV2,
    Timeframes,
    TransitionConflictPolicy,
    TransitionExecutionPolicy,
    TransitionPurpose,
    TransitionSpec,
    TriggerSpec,
    UniverseSpec,
    ValidationRequirements,
    WarmupSpecV2,
)
from tests.strategy_lab.conftest import _dur, _rate, _tf


def sid(name: str) -> StableIdentifier:
    return StableIdentifier(value=name)


def _catalog_v2() -> ContractVersion:
    return ContractVersion(value="catalog/v2")


def _plugin_v2(
    pid: str = "edc_m0_strict_sync",
    *,
    contract_version: str = "catalog/v2",
) -> PluginRefV2:
    return PluginRefV2(
        plugin_id=sid(pid),
        contract_version=ContractVersion(value=contract_version),
        config=(),
    )


def _entry_v2() -> EntrySpecV2:
    return EntrySpecV2(
        signal_decision_timing=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
        entry_reference_rule=EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR,
        entry_timing_anchor=EntryTimingAnchorV2.SIGNAL_BAR_CLOSE,
        entry_price_reference=EntryPriceReferenceV2.NEXT_SIGNAL_TF_OPEN,
        execution_timeframe=_tf(1),
        entry_plugin=PluginRef(
            id="entry.next_open",
            version="1.0.0",
            kind=PluginKind.ENTRY,
        ),
        description="Next signal-TF open after signal bar close.",
    )


def _warmup_v2() -> WarmupSpecV2:
    return WarmupSpecV2(
        signal_engine=SignalEngineWarmupV2(minimum_bars=79, bar_timeframe=_tf(5)),
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
    )


def _data_requirement_v2() -> DataRequirementSpecV2:
    return DataRequirementSpecV2(
        requirement_id=sid("edc_candles_signal_tf"),
        source_kind=DataSourceKindV2.CANDLES_SIGNAL_TF,
        role=DataRequirementRoleV2.SIGNAL_REQUIRED,
        required=True,
        granularity=TimeframeGranularityV2(timeframe=_tf(5)),
        availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
        rationale="Signal-TF candles for unit-test fixture.",
        required_for_policy=None,
        provenance=(),
    )


def _comparison(
    operator: str = "gt",
    feature: str = "ema_fast",
    output: str = "value",
) -> ComparisonExpression:
    return ComparisonExpression(
        operator_id=sid(operator),
        left=FeatureOutputReference(
            feature_alias=sid(feature),
            output_id=sid(output),
        ),
        right=LiteralOperand(value=IntParam(value=0)),
    )


def _v2_base_kwargs() -> dict[str, object]:
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
    return {
        "metadata": Metadata(
            schema_version=STRATEGY_SPEC_V2_SCHEMA_VERSION,
            strategy_id="test.minimal.v2",
            strategy_version="0.1.0",
            family="test",
            variant="minimal_v2",
            title="Minimal StrategySpec V2",
        ),
        "universe": UniverseSpec(role="fixed", symbols=("XRPUSDT",)),
        "timeframes": Timeframes(signal=_tf(5), execution=_tf(1)),
        "data_requirements": (_data_requirement_v2(),),
        "warmup": _warmup_v2(),
        "features": (
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v1"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("period"),
                        value=IdentifierParam(value="period_9"),
                    ),
                ),
            ),
        ),
        "entry": _entry_v2(),
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
            strategy_ref="test.minimal.v2@0.1.0",
            policy_ref="none@1.0.0",
            plugin_refs=(
                PluginProvenanceRef(plugin_id="signal.ema_dual_cross", version="1.0.0"),
            ),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
            causality_claim="unit-test fixture only",
            external_runtime_dependencies=("TRP",),
            known_limitations=("fixture",),
            notes=("P3.5 construct test",),
        ),
    }


def plugin_signal_v2(**overrides: object) -> PluginSignalSpec:
    base: dict[str, object] = {
        "plugin": _plugin_v2(),
        "mode_id": sid("mode_a"),
        "directionality": Directionality.BOTH,
        "rules_embedded_in_yaml": False,
        "confirmation_policy": None,
        "setup": SetupSpec(description="setup", decision_at="signal_bar_close"),
        "trigger": TriggerSpec(description="trigger"),
        "confirmation": ConfirmationSpec(description="confirmation"),
        "invalidation": InvalidationSpec(description="invalidation"),
    }
    base.update(overrides)
    return PluginSignalSpec(**base)  # type: ignore[arg-type]


def rule_based_signal_v2(
    directionality: Directionality = Directionality.LONG,
    *,
    long: SideRuleBundle | None = None,
    short: SideRuleBundle | None = None,
) -> RuleBasedSignalSpec:
    trigger = _comparison()
    if directionality is Directionality.LONG:
        long = long or SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    elif directionality is Directionality.SHORT:
        short = short or SideRuleBundle(
            side=SideName.SHORT,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    else:
        long = long or SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
        short = short or SideRuleBundle(
            side=SideName.SHORT,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        )
    return RuleBasedSignalSpec(
        operator_contract_version=_catalog_v2(),
        directionality=directionality,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        long=long,
        short=short,
    )


def state_machine_signal_v2() -> StateMachineSignalSpec:
    idle = sid("idle")
    armed = sid("armed")
    condition = _comparison()
    return StateMachineSignalSpec(
        operator_contract_version=_catalog_v2(),
        directionality=Directionality.LONG,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        initial_state=idle,
        states=(
            StateSpec(state_id=idle, description="idle"),
            StateSpec(state_id=armed, description="armed"),
        ),
        transitions=(
            TransitionSpec(
                transition_id=sid("to_armed"),
                from_state=idle,
                to_state=armed,
                condition=condition,
                priority=1,
                purpose=TransitionPurpose.NORMAL,
                emission=SignalEmissionSpec(
                    side=SideName.LONG,
                    emission_id=sid("entry_long"),
                ),
            ),
            TransitionSpec(
                transition_id=sid("invalidate"),
                from_state=armed,
                to_state=idle,
                condition=condition,
                priority=2,
                purpose=TransitionPurpose.INVALIDATION,
                emission=None,
            ),
        ),
        transition_execution_policy=TransitionExecutionPolicy.ONE_PER_EVALUATION_BAR,
        transition_conflict_policy=TransitionConflictPolicy.PRIORITY_WINS,
        reset_rules=(
            ResetRule(event=ResetEvent.SIGNAL_EMITTED, target_state=idle),
            ResetRule(event=ResetEvent.INVALIDATED, target_state=idle),
        ),
    )


def minimal_strategy_spec_v2(signal: object | None = None, **overrides: object) -> StrategySpecV2:
    kwargs = _v2_base_kwargs()
    kwargs["signal"] = signal if signal is not None else plugin_signal_v2()
    kwargs.update(overrides)
    return StrategySpecV2(**kwargs)  # type: ignore[arg-type]
