"""Closed signal plugin catalog for Strategy Lab catalog/v2."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CATALOG_CONTRACT_VERSION,
    CandidatePluginDescriptorV2,
    PluginDescriptorV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    AdapterBindingStatusV2,
    AvailabilityTimingV2,
    BoundFeatureRequirementV2,
    BoundParameterBindingV2,
    DataRequirementRoleV2,
    DataRequirementSpecV2,
    DataSourceKindV2,
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    EventStreamGranularityV2,
    IntBoundsV2,
    LegacyProvenanceRefV2,
    OutcomeEvaluationPaddingV2,
    PaddingNotApplicable,
    ParameterDefinitionV2,
    ParameterValueType,
    PluginContractStatusV2,
    PluginModeContractV2,
    PluginModeRequirementV2,
    PluginParameterBindingTargetV2,
    PluginParameterDefinitionV2,
    ResearchConfirmationPolicyV2,
    SelectedSignalTimeframeGranularityV2,
    SignalEngineWarmupRequirementV2,
    SignalTimeframeContractV2,
    SignalTimeframeModeV2,
    SnapshotGranularityV2,
    SourceLoadingPaddingV2,
    StrategyRunIntentV2,
    TimeframeGranularityV2,
    WarmupTimeframeBasisV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    CausalityStatus,
    Directionality,
    DurationUnit,
    PluginKind,
    RateUnit,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    DurationValue,
    IntParam,
    RateParam,
    RateValue,
    TimeframeValue,
)

_SID = StableIdentifier

_SELECTED_SIGNAL_TF = SelectedSignalTimeframeGranularityV2(
    binds_to_selected_signal_timeframe=True,
)


def _tf(minutes: int) -> TimeframeValue:
    return TimeframeValue(value=minutes, unit=TimeframeUnit.MINUTES)


def _dur_hours(value: str) -> DurationValue:
    return DurationValue(value=Decimal(value), unit=DurationUnit.HOURS)


def _dur_days(value: str) -> DurationValue:
    hours = Decimal(value) * Decimal("24")
    return DurationValue(value=hours, unit=DurationUnit.HOURS)


_SHARED_SIGNAL_WARMUP = SignalEngineWarmupRequirementV2(
    minimum_bars=79,
    timeframe_basis=WarmupTimeframeBasisV2.SELECTED_SIGNAL_TIMEFRAME,
    fixed_timeframe=None,
)

_EDC_SOURCE_PADDING = SourceLoadingPaddingV2(
    candle_history=_dur_days("5"),
    auxiliary_source_history=_dur_hours("2"),
)

_EDC_OUTCOME_PADDING = OutcomeEvaluationPaddingV2(
    post_window_duration=_dur_hours("12"),
)

EDC_M0_STRICT_SYNC = PluginDescriptorV2(
    plugin_id=_SID(value="edc_m0_strict_sync"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    kind=PluginKind.SIGNAL,
    description=(
        "EDC M0 strict-sync baseline: dual EMA cross on signal-TF candles with "
        "CORE_RESEARCH_SUPPORTIVE confirmation policy."
    ),
    parameters=(),
    mode_contract=PluginModeContractV2(
        requirement=PluginModeRequirementV2.REQUIRED,
        allowed_modes=(_SID(value="m0_strict_sync"),),
    ),
    required_features=(
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_fast"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=9),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_medium"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=20),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_slow"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=59),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="atr"),
            feature_id=_SID(value="atr_wilder"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=14),
                ),
            ),
        ),
    ),
    data_requirements=(
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_candles_signal_tf"),
            source_kind=DataSourceKindV2.CANDLES_SIGNAL_TF,
            role=DataRequirementRoleV2.SIGNAL_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(5)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale="M0 cross detection uses only aggregated signal-TF OHLCV+EMA+ATR.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.detect_bar_gap"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/detect_bar_gap.py"
                    ),
                    symbol="detect_strict_sync_baseline",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_candles_execution_1m"),
            source_kind=DataSourceKindV2.CANDLES_EXECUTION_1M,
            role=DataRequirementRoleV2.EXECUTION_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.ENTRY_BAR_OPEN,
            rationale="Entry and outcome simulation walk 1m execution candles.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.shared_strategy.market_data"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/shared_strategy/market_data.py"
                    ),
                    symbol="fetch_candles_1m",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_public_trades_1m"),
            source_kind=DataSourceKindV2.PUBLIC_TRADES_1M,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale=(
                "Aggregated 1m trade features for CORE_RESEARCH_SUPPORTIVE; "
                "does not change M0 cross detection."
            ),
            required_for_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                    notes="Listed in core_required.",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_orderbook_ob200_v3_1m"),
            source_kind=DataSourceKindV2.ORDERBOOK_OB200_V3_1M,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale=(
                "Aggregated 1m orderbook features for CORE_RESEARCH_SUPPORTIVE."
            ),
            required_for_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_liquidity_locations"),
            source_kind=DataSourceKindV2.LIQUIDITY_LOCATIONS,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=SnapshotGranularityV2(aligned_timeframe=_tf(5)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale=(
                "Signal-TF-aligned liquidity snapshot for CORE_RESEARCH_SUPPORTIVE."
            ),
            required_for_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                    notes="core_required includes liquidity_locations.",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_open_interest_1m"),
            source_kind=DataSourceKindV2.OPEN_INTEREST_1M,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Aggregated 1m OI evaluated when present at window edge."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                    notes="evaluated_when_present.",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="edc_liquidations"),
            source_kind=DataSourceKindV2.LIQUIDATIONS,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=EventStreamGranularityV2(native_event_stream=True),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Native liquidation event stream evaluated when present; "
                "no candle timeframe."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                    notes="evaluated_when_present.",
                ),
            ),
        ),
    ),
    confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
    supported_directions=Directionality.BOTH,
    signal_timeframe=SignalTimeframeContractV2(
        mode=SignalTimeframeModeV2.FIXED,
        reference_minutes=5,
        allowed_minutes=(5,),
        notes="Frozen reference cell uses 5m signal timeframe.",
    ),
    execution_timeframe=_tf(1),
    signal_decision_timing=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
    entry_reference_rule=EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR,
    entry_timing_anchor=EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN,
    entry_price_reference=EntryPriceReferenceV2.BAR_OPEN,
    signal_warmup=_SHARED_SIGNAL_WARMUP,
    source_loading_padding=_EDC_SOURCE_PADDING,
    outcome_evaluation_padding=_EDC_OUTCOME_PADDING,
    causality_status=CausalityStatus.CAUSAL_PROVEN,
    causality_claim=(
        "M0 candidate decision_at is signal bar close; entry is next signal-TF "
        "open. CORE_RESEARCH_SUPPORTIVE is a post-detection research filter."
    ),
    adapter_status=AdapterBindingStatusV2.ADAPTER_PENDING,
    provenance=(
        LegacyProvenanceRefV2(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".detect_bar_gap"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/detect_bar_gap.py"
            ),
            symbol="detect_strict_sync_baseline",
            notes=None,
        ),
        LegacyProvenanceRefV2(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".shared_strategy.semantics"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/shared_strategy/semantics.py"
            ),
            symbol="REF_GROUP",
            notes="EDC_FROZEN_XRP_REFERENCE_V1 / M0_TP075_SL050_H8 reference cell.",
        ),
    ),
)

CLUSTER_SWEEP = PluginDescriptorV2(
    plugin_id=_SID(value="cluster_sweep"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    kind=PluginKind.SIGNAL,
    description=(
        "Cluster sweep research signal: EMA 9/20/59 structure plus causal LLD "
        "cluster approach/entry and forward confirmation variants."
    ),
    parameters=(
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="minimum_cluster_pools"),
                value_type=ParameterValueType.INTEGER,
                required=True,
                description="Minimum pool_count for an active cluster.",
                allowed_identifiers=(),
                int_bounds=IntBoundsV2(min_value=1, max_value=None),
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value="3",
                must_be_explicit=True,
                research_space_varies=True,
                baseline_defining=True,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="expire_bars"),
                value_type=ParameterValueType.INTEGER,
                required=True,
                description="Forward bars scanned for confirmation after candidate.",
                allowed_identifiers=(),
                int_bounds=IntBoundsV2(min_value=1, max_value=None),
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value="24",
                must_be_explicit=True,
                research_space_varies=True,
                baseline_defining=False,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="approach_bps"),
                value_type=ParameterValueType.RATE,
                required=True,
                description="Maximum approach distance to cluster mid in basis points.",
                allowed_identifiers=(),
                int_bounds=None,
                decimal_bounds=None,
                required_rate_unit=RateUnit.BASIS_POINTS,
                legacy_reference_value="25",
                must_be_explicit=True,
                research_space_varies=True,
                baseline_defining=True,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="require_cluster_entry"),
                value_type=ParameterValueType.BOOLEAN,
                required=True,
                description="Require actual cluster box entry, not approach-only.",
                allowed_identifiers=(),
                int_bounds=None,
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value="true",
                must_be_explicit=True,
                research_space_varies=False,
                baseline_defining=True,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="gap_pct"),
                value_type=ParameterValueType.RATE,
                required=True,
                description=(
                    "Cluster aggregation gap percent passed to LLD engine "
                    "(TRP cluster_gap_pct convention)."
                ),
                allowed_identifiers=(),
                int_bounds=None,
                decimal_bounds=None,
                required_rate_unit=RateUnit.PERCENT,
                legacy_reference_value="0.10",
                must_be_explicit=True,
                research_space_varies=False,
                baseline_defining=True,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
    ),
    mode_contract=PluginModeContractV2(
        requirement=PluginModeRequirementV2.NOT_APPLICABLE,
        allowed_modes=(),
    ),
    required_features=(
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_fast"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=9),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_medium"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=20),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_slow"),
            feature_id=_SID(value="ema"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=59),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="atr"),
            feature_id=_SID(value="atr_wilder"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="period"),
                    value=IntParam(value=14),
                ),
            ),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="clusters"),
            feature_id=_SID(value="lld_liquidity_clusters"),
            bindings=(
                BoundParameterBindingV2(
                    name=_SID(value="gap_pct"),
                    value=RateParam(
                        value=RateValue(
                            value=Decimal("0.10"),
                            unit=RateUnit.PERCENT,
                        )
                    ),
                ),
                BoundParameterBindingV2(
                    name=_SID(value="minimum_pools"),
                    value=IntParam(value=3),
                ),
            ),
        ),
    ),
    data_requirements=(
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_candles_signal_tf"),
            source_kind=DataSourceKindV2.CANDLES_SIGNAL_TF,
            role=DataRequirementRoleV2.SIGNAL_REQUIRED,
            required=True,
            granularity=_SELECTED_SIGNAL_TF,
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale=(
                "EMA, ATR, sweep detection, confirmation, and primary outcomes; "
                "granularity follows the strategy's chosen signal timeframe (5m or 15m)."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.event_detector",
                    path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
                    symbol="detect_candidates",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_liquidity_locations"),
            source_kind=DataSourceKindV2.LIQUIDITY_LOCATIONS,
            role=DataRequirementRoleV2.SIGNAL_REQUIRED,
            required=True,
            granularity=_SELECTED_SIGNAL_TF,
            availability=AvailabilityTimingV2.PRIOR_BAR_OPEN,
            rationale=(
                "TRP LLD clusters derived from signal-TF OHLCV; causal as_of prior bar; "
                "aligned to the strategy's chosen signal timeframe."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.cluster_adapter",
                    path="src/orderbook_analyse/cluster_sweep_research/cluster_adapter.py",
                    symbol="active_clusters_as_of",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_candles_execution_1m"),
            source_kind=DataSourceKindV2.CANDLES_EXECUTION_1M,
            role=DataRequirementRoleV2.EXECUTION_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.ENTRY_BAR_OPEN,
            rationale="Base 1m feed aggregated to signal TF and used for extended analysis.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.clickhouse_source",
                    path="src/orderbook_analyse/cluster_sweep_research/clickhouse_source.py",
                    symbol="fetch_candles_1m",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_public_trades_1m"),
            source_kind=DataSourceKindV2.PUBLIC_TRADES_1M,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Optional 1m-aggregated trade features for orderflow enrichment; "
                "legacy fetch_trades_1m groups toStartOfMinute."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_orderbook_ob200_v3_1m"),
            source_kind=DataSourceKindV2.ORDERBOOK_OB200_V3_1M,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Optional 1m-aggregated orderbook features; legacy fetch_ob_1m "
                "groups ob200_v3 1s buckets toStartOfMinute."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_open_interest_1m"),
            source_kind=DataSourceKindV2.OPEN_INTEREST_1M,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Optional 1m-aggregated open-interest series; legacy fetch_oi_1m "
                "groups open_interest_5s toStartOfMinute."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                    notes=None,
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="cluster_liquidations"),
            source_kind=DataSourceKindV2.LIQUIDATIONS,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=EventStreamGranularityV2(native_event_stream=True),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Optional native liquidation events; legacy fetch_liquidations "
                "returns per-event rows keyed by event_time."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                    notes=None,
                ),
            ),
        ),
    ),
    confirmation_policy=None,
    supported_directions=Directionality.BOTH,
    signal_timeframe=SignalTimeframeContractV2(
        mode=SignalTimeframeModeV2.ALLOWED_SET,
        reference_minutes=15,
        allowed_minutes=(5, 15),
        notes=(
            "Legacy runners pass timeframe as a caller parameter; 15m is canonical "
            "default, 5m is the visual-audit workflow."
        ),
    ),
    execution_timeframe=_tf(1),
    signal_decision_timing=AvailabilityTimingV2.CONFIRMATION_BAR_CLOSE,
    entry_reference_rule=EntryReferenceRuleV2.NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR,
    entry_timing_anchor=EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN,
    entry_price_reference=EntryPriceReferenceV2.BAR_OPEN,
    signal_warmup=_SHARED_SIGNAL_WARMUP,
    source_loading_padding=SourceLoadingPaddingV2(
        candle_history=PaddingNotApplicable(not_applicable=True),
        auxiliary_source_history=PaddingNotApplicable(not_applicable=True),
    ),
    outcome_evaluation_padding=OutcomeEvaluationPaddingV2(
        post_window_duration=PaddingNotApplicable(not_applicable=True),
    ),
    causality_status=CausalityStatus.CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE,
    causality_claim=(
        "Clusters are causal when TRP Liquidity Location is available; "
        "confirmation uses closed signal-TF bars and entry is the next "
        "signal-TF bar open (not the first 1m tick)."
    ),
    adapter_status=AdapterBindingStatusV2.ADAPTER_PENDING,
    provenance=(
        LegacyProvenanceRefV2(
            module="orderbook_analyse.cluster_sweep_research.pipeline",
            path="src/orderbook_analyse/cluster_sweep_research/pipeline.py",
            symbol="run_cluster_sweep_on_candles",
            notes=None,
        ),
        LegacyProvenanceRefV2(
            module="orderbook_analyse.cluster_sweep_research.event_detector",
            path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
            symbol="detect_candidates",
            notes=None,
        ),
    ),
)

EMA_ZONE_MICROSTRUCTURE_CONFIRMATION = CandidatePluginDescriptorV2(
    plugin_id=_SID(value="ema_zone_microstructure_confirmation"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    kind=PluginKind.SIGNAL,
    description=(
        "RESEARCH_CONTRACT_ONLY: EMA9/20/59 regime + EMA20/59/200 zones with "
        "microstructure candidate classification. No detector/runner; no entry/exit."
    ),
    parameters=(
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="regime_slope_lookback_short"),
                value_type=ParameterValueType.INTEGER,
                required=True,
                description="Closed 5m bars for short EMA slope (research default).",
                allowed_identifiers=(),
                int_bounds=IntBoundsV2(min_value=1, max_value=None),
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value="3",
                must_be_explicit=True,
                research_space_varies=True,
                baseline_defining=False,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
        PluginParameterDefinitionV2(
            definition=ParameterDefinitionV2(
                name=_SID(value="regime_slope_lookback_long"),
                value_type=ParameterValueType.INTEGER,
                required=True,
                description="Closed 5m bars for long EMA slope (research default).",
                allowed_identifiers=(),
                int_bounds=IntBoundsV2(min_value=1, max_value=None),
                decimal_bounds=None,
                required_rate_unit=None,
                legacy_reference_value="6",
                must_be_explicit=True,
                research_space_varies=True,
                baseline_defining=False,
            ),
            binding_target=PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG,
        ),
    ),
    mode_contract=PluginModeContractV2(
        requirement=PluginModeRequirementV2.NOT_APPLICABLE,
        allowed_modes=(),
    ),
    required_features=(
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_fast"),
            feature_id=_SID(value="ema"),
            bindings=(BoundParameterBindingV2(name=_SID(value="period"), value=IntParam(value=9)),),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_medium"),
            feature_id=_SID(value="ema"),
            bindings=(BoundParameterBindingV2(name=_SID(value="period"), value=IntParam(value=20)),),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_slow"),
            feature_id=_SID(value="ema"),
            bindings=(BoundParameterBindingV2(name=_SID(value="period"), value=IntParam(value=59)),),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="ema_structure"),
            feature_id=_SID(value="ema"),
            bindings=(BoundParameterBindingV2(name=_SID(value="period"), value=IntParam(value=200)),),
        ),
        BoundFeatureRequirementV2(
            alias=_SID(value="atr"),
            feature_id=_SID(value="atr_wilder"),
            bindings=(BoundParameterBindingV2(name=_SID(value="period"), value=IntParam(value=14)),),
        ),
    ),
    data_requirements=(
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_candles_signal_tf"),
            source_kind=DataSourceKindV2.CANDLES_SIGNAL_TF,
            role=DataRequirementRoleV2.SIGNAL_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(5)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale="Closed 5m OHLCV for EMA regime and zone geometry.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
                    path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
                    symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
                    notes="research_contract_only",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_candles_execution_1m"),
            source_kind=DataSourceKindV2.CANDLES_EXECUTION_1M,
            role=DataRequirementRoleV2.SIGNAL_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
            rationale="1m detail candles for zone approach clocks (not trade entry).",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
                    path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
                    symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
                    notes="research_contract_only",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_orderbook_ob200_v3_raw"),
            source_kind=DataSourceKindV2.ORDERBOOK_OB200_V3_RAW,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=EventStreamGranularityV2(native_event_stream=True),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Per-level raw OB200 closed archive segments (ob200_v3.zst). "
                "Distinct from orderbook_ob200_v3_1m. Open *.tmp must not be read. "
                "Missing data → candidate_state data_incomplete."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.ob200_v3_raw_discovery.files",
                    path="src/orderbook_analyse/ob200_v3_raw_discovery/files.py",
                    symbol="list_closed_segments",
                    notes="metadata registration only; no loader wired in this contract",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_public_trades_native"),
            source_kind=DataSourceKindV2.PUBLIC_TRADES_NATIVE,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=EventStreamGranularityV2(native_event_stream=True),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale=(
                "Native/tick public trades (public_trades_canonical). "
                "Distinct from public_trades_1m aggregate."
            ),
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.l2_wall_attack_discovery.trades",
                    path="src/orderbook_analyse/l2_wall_attack_discovery/trades.py",
                    symbol="load_public_trades",
                    notes="metadata registration only; no loader wired in this contract",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_open_interest_1m"),
            source_kind=DataSourceKindV2.OPEN_INTEREST_1M,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=TimeframeGranularityV2(timeframe=_tf(1)),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale="1m OI context at zones.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
                    path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
                    symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
                    notes="research_contract_only",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_liquidations"),
            source_kind=DataSourceKindV2.LIQUIDATIONS,
            role=DataRequirementRoleV2.CONFIRMATION_REQUIRED,
            required=True,
            granularity=EventStreamGranularityV2(native_event_stream=True),
            availability=AvailabilityTimingV2.WINDOW_EDGE,
            rationale="Native liquidation events for flush/squeeze/exhaustion context.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
                    path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
                    symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
                    notes="research_contract_only",
                ),
            ),
        ),
        DataRequirementSpecV2(
            requirement_id=_SID(value="ezm_liquidity_locations"),
            source_kind=DataSourceKindV2.LIQUIDITY_LOCATIONS,
            role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
            required=False,
            granularity=SnapshotGranularityV2(aligned_timeframe=_tf(5)),
            availability=AvailabilityTimingV2.PRIOR_BAR_OPEN,
            rationale="Optional LLD locations for next-zone clearance confluence.",
            required_for_policy=None,
            provenance=(
                LegacyProvenanceRefV2(
                    module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
                    path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
                    symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
                    notes="research_contract_only",
                ),
            ),
        ),
    ),
    confirmation_policy=None,
    supported_directions=Directionality.BOTH,
    signal_timeframe=SignalTimeframeContractV2(
        mode=SignalTimeframeModeV2.FIXED,
        reference_minutes=5,
        allowed_minutes=(5,),
        notes="Regime/EMA decisions use closed 5m bars only.",
    ),
    detail_timeframe=_tf(1),
    signal_decision_timing=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
    candidate_states=(
        _SID(value="watch_zone"),
        _SID(value="block_flat_compression"),
        _SID(value="wait_microstructure_confirmation"),
        _SID(value="defense_rejection_confirmed"),
        _SID(value="breakout_confirmed"),
        _SID(value="false_breakout_confirmed"),
        _SID(value="wait_next_zone_confirmation"),
        _SID(value="possible_regime_flip"),
        _SID(value="full_regime_flip_confirmed"),
        _SID(value="no_trade"),
        _SID(value="data_incomplete"),
    ),
    signal_warmup=SignalEngineWarmupRequirementV2(
        minimum_bars=200,
        timeframe_basis=WarmupTimeframeBasisV2.SELECTED_SIGNAL_TIMEFRAME,
        fixed_timeframe=None,
    ),
    source_loading_padding=SourceLoadingPaddingV2(
        candle_history=_dur_days("10"),
        auxiliary_source_history=_dur_hours("2"),
    ),
    outcome_evaluation_padding=OutcomeEvaluationPaddingV2(
        post_window_duration=PaddingNotApplicable(not_applicable=True),
    ),
    causality_status=CausalityStatus.CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE,
    causality_claim=(
        "Regime/zone decisions use closed 5m bars only. Event_time, decision_time, "
        "and possible later entry_time remain separate. No outcome MFE/MAE lookahead. "
        "Missing raw OB/trades → data_incomplete."
    ),
    adapter_status=AdapterBindingStatusV2.CATALOG_ONLY,
    provenance=(
        LegacyProvenanceRefV2(
            module="orderbook_analyse.strategy_lab.catalogs.v2.plugins",
            path="src/orderbook_analyse/strategy_lab/catalogs/v2/plugins.py",
            symbol="EMA_ZONE_MICROSTRUCTURE_CONFIRMATION",
            notes="RESEARCH_CONTRACT_ONLY; no detector implemented",
        ),
    ),
    contract_status=PluginContractStatusV2.RESEARCH_CONTRACT_ONLY,
    run_intent=StrategyRunIntentV2.CANDIDATE_DISCOVERY,
)

PLUGIN_DESCRIPTORS_V2: tuple[PluginDescriptorV2 | CandidatePluginDescriptorV2, ...] = (
    EDC_M0_STRICT_SYNC,
    CLUSTER_SWEEP,
    EMA_ZONE_MICROSTRUCTURE_CONFIRMATION,
)
