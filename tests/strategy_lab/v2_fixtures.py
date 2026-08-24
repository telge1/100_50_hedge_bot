"""Shared builders for StrategySpec V2 unit tests."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_V2_SCHEMA_VERSION,
    AnalysisRequirements,
    AvailabilityTimingV2,
    CausalityStatus,
    ComparisonExpression,
    ConfirmationSpec,
    ContractVersion,
    CostsSpecV2,
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
    ExecutionAssumptionsV2,
    ExitSpecV2,
    FeatureBindingSpec,
    FeatureOutputReference,
    FeatureParameterBinding,
    FeatureParameterTargetV2,
    IdentifierParam,
    IntrabarPolicy,
    InvalidationSpec,
    IntParam,
    LiteralOperand,
    Metadata,
    ModelingStatus,
    NotionalCurrencyV2,
    OutcomeEvaluationPaddingV2,
    PluginRefV2,
    PluginConfigParameterTargetV2,
    PluginSignalSpec,
    PortfolioAssumptionsV2,
    PortfolioEvaluationModeV2,
    ProvenanceSpecV2,
    RateUnit,
    ResearchDimensionV2,
    ResearchParameterSpaceV2,
    ResetEvent,
    ResetRule,
    RoundtripCostTargetV2,
    RuleBasedSignalSpec,
    SameBarPriority,
    SignalTimeframeTargetV2,
    SetupSpec,
    SideName,
    SideRuleBundle,
    SignalEmissionSpec,
    SignalEngineWarmupV2,
    SourceLoadingPaddingV2,
    PluginProvenanceRefV2,
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
    VersionedUniverseRefV2,
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
        entry_timing_anchor=EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN,
        entry_price_reference=EntryPriceReferenceV2.BAR_OPEN,
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
    return {
        "metadata": Metadata(
            schema_version=STRATEGY_SPEC_V2_SCHEMA_VERSION,
            strategy_id="test.minimal.v2",
            strategy_version="0.1.0",
            family="test",
            variant="minimal_v2",
            title="Minimal StrategySpec V2",
        ),
        "universe": VersionedUniverseRefV2(
            universe_id=sid("tradeable_51"),
            version="v1",
            content_hash="sha256:fixture_tradeable_51",
        ),
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
        "exit": ExitSpecV2(
            take_profit=_rate("0.75", RateUnit.PERCENT),
            stop_loss=_rate("0.5", RateUnit.PERCENT),
            horizon=_dur("8", DurationUnit.HOURS),
        ),
        "intrabar_policy": IntrabarPolicy(same_bar_priority=SameBarPriority.SL_FIRST),
        "execution_assumptions": ExecutionAssumptionsV2(
            execution_timeframe=_tf(1),
            fixed_notional=Decimal("1000"),
            notional_currency=NotionalCurrencyV2.USDT,
            fill_price_reference=EntryPriceReferenceV2.BAR_OPEN,
            rounding_status=ModelingStatus.NOT_MODELED,
        ),
        "costs": CostsSpecV2(
            roundtrip_cost=_rate("0.15", RateUnit.PERCENT),
            slippage=ModelingStatus.NOT_MODELED,
            funding=ModelingStatus.NOT_MODELED,
        ),
        "portfolio_assumptions": PortfolioAssumptionsV2(
            evaluation_mode=PortfolioEvaluationModeV2.PER_TRADE_INDEPENDENT,
            compounding=False,
        ),
        "research_parameter_space": ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("signal_tf"),
                    target=SignalTimeframeTargetV2(),
                    candidates=(),
                ),
                ResearchDimensionV2(
                    dimension_id=sid("ema_period"),
                    target=FeatureParameterTargetV2(
                        feature_alias=sid("ema_fast"),
                        parameter_name=sid("period"),
                    ),
                    candidates=(IntParam(value=9),),
                ),
                ResearchDimensionV2(
                    dimension_id=sid("plugin_gap_pct"),
                    target=PluginConfigParameterTargetV2(parameter_name=sid("gap_pct")),
                    candidates=(IdentifierParam(value="gap_pct_baseline"),),
                ),
                ResearchDimensionV2(
                    dimension_id=sid("roundtrip_cost"),
                    target=RoundtripCostTargetV2(),
                    candidates=(),
                ),
            ),
        ),
        "analysis_requirements": AnalysisRequirements(
            required_label_fields=("mfe", "mae", "realized_pnl"),
        ),
        "validation_requirements": ValidationRequirements(
            require_causality_audit=True,
            require_strategy_parity_check=True,
            allowed_causality_statuses=(CausalityStatus.CAUSALITY_UNPROVEN,),
        ),
        "provenance": ProvenanceSpecV2(
            git_commit="0000000000000000000000000000000000000000",
            source_repository="orderbook_analyse",
            source_paths=("tests/strategy_lab/",),
            catalog_contract_version=ContractVersion(value="catalog/v2"),
            plugin_refs=(
                PluginProvenanceRefV2(
                    plugin_id=sid("edc_m0_strict_sync"),
                    contract_version=ContractVersion(value="catalog/v2"),
                ),
            ),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
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
