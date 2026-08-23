"""Closed signal plugin catalog for Strategy Lab V1 reference strategies."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.catalogs.models import (
    AdapterBindingStatus,
    AvailabilityTiming,
    BoundFeatureRequirement,
    BoundParameterBinding,
    CATALOG_CONTRACT_VERSION,
    DataRequirementDescriptor,
    DataRequirementRole,
    DataSourceKind,
    DecimalBounds,
    IntBounds,
    LegacyProvenanceRef,
    OutcomeEvaluationPadding,
    ParameterDefinition,
    PluginDescriptor,
    PluginSignalWarmup,
    ResearchConfirmationPolicy,
    SignalTimeframeContract,
    SignalTimeframeMode,
    SourceLoadingPadding,
    ValueType,
)
from orderbook_analyse.strategy_lab.models.enums import (
    CausalityStatus,
    Directionality,
    PluginKind,
    RateUnit,
)
from orderbook_analyse.strategy_lab.models.strategy import (
    IntParam,
    RateParam,
    RateValue,
)

_SHARED_SIGNAL_WARMUP = PluginSignalWarmup(
    minimum_bar_index=79,
    slow_ema_period=59,
    legacy_extra_bars=20,
    notes="required_warmup_bars(slow=59, extra=20) from ema_features.py",
    provenance=(
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.ema_features",
            path="src/orderbook_analyse/cluster_sweep_research/ema_features.py",
            symbol="required_warmup_bars",
        ),
        LegacyProvenanceRef(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".multicoin_frozen_validation.constants"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/multicoin_frozen_validation/constants.py"
            ),
            symbol="WARMUP_BARS_EXTRA",
            notes="Frozen convention: ema_slow + 20 signal-TF bars.",
        ),
    ),
)

_EDC_SOURCE_PADDING = SourceLoadingPadding(
    candle_pad_days=5,
    auxiliary_source_pad_hours=2,
    notes="Canonical market load pads from shared_strategy/semantics.py.",
    provenance=(
        LegacyProvenanceRef(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".shared_strategy.semantics"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/shared_strategy/semantics.py"
            ),
            symbol="WARMUP_PAD_DAYS",
        ),
        LegacyProvenanceRef(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".shared_strategy.market_data"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/shared_strategy/market_data.py"
            ),
            symbol="load_market_data_canonical",
            notes="Applies candle pad_days and source_pad_hours to auxiliary feeds.",
        ),
    ),
)

_EDC_OUTCOME_PADDING = OutcomeEvaluationPadding(
    outcome_pad_hours=12,
    notes="Post-window 1m candle load for TP/SL/horizon outcome simulation.",
    provenance=(
        LegacyProvenanceRef(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".shared_strategy.semantics"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/shared_strategy/semantics.py"
            ),
            symbol="OUTCOME_PAD_HOURS",
        ),
    ),
)

EDC_M0_STRICT_SYNC = PluginDescriptor(
    plugin_id="edc_m0_strict_sync",
    contract_version=CATALOG_CONTRACT_VERSION,
    kind=PluginKind.SIGNAL,
    description=(
        "Frozen EDC M0 strict synchronous dual EMA cross on signal-timeframe "
        "bars. The reference cell additionally filters to "
        "CORE_RESEARCH_SUPPORTIVE via multisource confirmation."
    ),
    parameters=(
        ParameterDefinition(
            name="mode_id",
            value_type=ValueType.IDENTIFIER,
            required=True,
            description="Frozen tolerance-research mode identifier.",
            allowed_identifiers=("M0_STRICT_SYNC",),
            legacy_reference_value="M0_STRICT_SYNC",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="enable_sync_cross",
            value_type=ValueType.BOOLEAN,
            required=True,
            description="Enable synchronous dual EMA cross detection.",
            legacy_reference_value="true",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="ema_fast",
            value_type=ValueType.INTEGER,
            required=True,
            description="Fast EMA period.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="9",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="ema_medium",
            value_type=ValueType.INTEGER,
            required=True,
            description="Medium EMA period.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="20",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="ema_slow",
            value_type=ValueType.INTEGER,
            required=True,
            description="Slow EMA period.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="59",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="band_compression_pct",
            value_type=ValueType.RATE,
            required=True,
            description=(
                "Maximum |ema9-ema20|/close before cross, expressed as percent "
                "of price."
            ),
            required_rate_unit=RateUnit.PERCENT,
            legacy_reference_value="0.15",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="band_compression_atr",
            value_type=ValueType.DECIMAL,
            required=True,
            description="Maximum |ema9-ema20|/ATR before cross.",
            decimal_bounds=DecimalBounds(min_value=Decimal("0")),
            legacy_reference_value="0.35",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="flat_slope_atr",
            value_type=ValueType.DECIMAL,
            required=True,
            description="Minimum |EMA slope|/ATR to avoid flat-slope rejection.",
            decimal_bounds=DecimalBounds(min_value=Decimal("0")),
            legacy_reference_value="0.02",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="atr_period",
            value_type=ValueType.INTEGER,
            required=True,
            description="ATR period used by detection guards.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="14",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="group_id",
            value_type=ValueType.IDENTIFIER,
            required=True,
            description="Frozen research group filter for the reference cell.",
            allowed_identifiers=("CORE_RESEARCH_SUPPORTIVE",),
            legacy_reference_value="CORE_RESEARCH_SUPPORTIVE",
            baseline_defining=True,
        ),
    ),
    required_features=(
        BoundFeatureRequirement(
            alias="ema_fast",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=9)),),
        ),
        BoundFeatureRequirement(
            alias="ema_medium",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=20)),),
        ),
        BoundFeatureRequirement(
            alias="ema_slow",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=59)),),
        ),
        BoundFeatureRequirement(
            alias="atr",
            feature_id="atr_wilder",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=14)),),
        ),
    ),
    data_requirements=(
        DataRequirementDescriptor(
            requirement_id="edc_candles_signal_tf",
            source_kind=DataSourceKind.CANDLES_SIGNAL_TF,
            role=DataRequirementRole.SIGNAL_REQUIRED,
            required=True,
            granularity_minutes=5,
            availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
            rationale="M0 cross detection uses only aggregated signal-TF OHLCV+EMA+ATR.",
            provenance=(
                LegacyProvenanceRef(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.detect_bar_gap"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/detect_bar_gap.py"
                    ),
                    symbol="detect_strict_sync_baseline",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="edc_candles_execution_1m",
            source_kind=DataSourceKind.CANDLES_EXECUTION_1M,
            role=DataRequirementRole.EXECUTION_REQUIRED,
            required=True,
            granularity_minutes=1,
            availability=AvailabilityTiming.ENTRY_BAR_OPEN,
            rationale="Entry and outcome simulation walk 1m execution candles.",
            provenance=(
                LegacyProvenanceRef(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.shared_strategy.market_data"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/shared_strategy/market_data.py"
                    ),
                    symbol="fetch_candles_1m",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="edc_public_trades_1m",
            source_kind=DataSourceKind.PUBLIC_TRADES_1M,
            role=DataRequirementRole.CONFIRMATION_REQUIRED,
            required=True,
            granularity_minutes=1,
            availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
            rationale=(
                "Required for CORE_RESEARCH_SUPPORTIVE membership; does not change "
                "M0 cross detection."
            ),
            required_for_policy=ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRef(
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
        DataRequirementDescriptor(
            requirement_id="edc_orderbook_ob200_v3_1m",
            source_kind=DataSourceKind.ORDERBOOK_OB200_V3_1M,
            role=DataRequirementRole.CONFIRMATION_REQUIRED,
            required=True,
            granularity_minutes=1,
            availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
            rationale=(
                "Required for CORE_RESEARCH_SUPPORTIVE membership; missing/stale "
                "coverage yields CORE_RESEARCH_INSUFFICIENT."
            ),
            required_for_policy=ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRef(
                    module=(
                        "orderbook_analyse.ema_dual_cross_multisource"
                        ".tolerance_research.core_sources_research_policy"
                    ),
                    path=(
                        "src/orderbook_analyse/ema_dual_cross_multisource/"
                        "tolerance_research/core_sources_research_policy.py"
                    ),
                    symbol="core_research_policy_document",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="edc_liquidity_locations",
            source_kind=DataSourceKind.LIQUIDITY_LOCATIONS,
            role=DataRequirementRole.CONFIRMATION_REQUIRED,
            required=True,
            granularity_minutes=5,
            availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
            rationale=(
                "Liquidity confluence is evaluated for CORE_RESEARCH_SUPPORTIVE; "
                "not used in M0 cross detection."
            ),
            required_for_policy=ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE,
            provenance=(
                LegacyProvenanceRef(
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
        DataRequirementDescriptor(
            requirement_id="edc_open_interest_1m",
            source_kind=DataSourceKind.OPEN_INTEREST_1M,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale=(
                "Evaluated when present; oi_liq_missing_does_not_block_core_research."
            ),
            provenance=(
                LegacyProvenanceRef(
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
        DataRequirementDescriptor(
            requirement_id="edc_liquidations",
            source_kind=DataSourceKind.LIQUIDATIONS,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale=(
                "Evaluated when present; missing liquidations do not block "
                "CORE_RESEARCH_SUPPORTIVE."
            ),
            provenance=(
                LegacyProvenanceRef(
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
    confirmation_policy=ResearchConfirmationPolicy.CORE_RESEARCH_SUPPORTIVE,
    supported_directions=Directionality.BOTH,
    signal_timeframe=SignalTimeframeContract(
        mode=SignalTimeframeMode.FIXED,
        reference_minutes=5,
        allowed_minutes=(5,),
        notes="Frozen reference cell uses 5m signal timeframe.",
    ),
    execution_timeframe_minutes=1,
    decision_timing=AvailabilityTiming.SIGNAL_BAR_CLOSE,
    entry_timing=AvailabilityTiming.ENTRY_BAR_OPEN,
    entry_rule_id="SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR",
    signal_warmup=_SHARED_SIGNAL_WARMUP,
    source_loading_padding=_EDC_SOURCE_PADDING,
    outcome_evaluation_padding=_EDC_OUTCOME_PADDING,
    causality_status=CausalityStatus.CAUSAL_PROVEN,
    causality_claim=(
        "M0 candidate decision_at is signal bar close; entry is next signal-TF "
        "open. CORE_RESEARCH_SUPPORTIVE is a post-detection research filter."
    ),
    adapter_status=AdapterBindingStatus.ADAPTER_PENDING,
    provenance=(
        LegacyProvenanceRef(
            module=(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
                ".detect_bar_gap"
            ),
            path=(
                "src/orderbook_analyse/ema_dual_cross_multisource/"
                "tolerance_research/detect_bar_gap.py"
            ),
            symbol="detect_strict_sync_baseline",
        ),
        LegacyProvenanceRef(
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

CLUSTER_SWEEP = PluginDescriptor(
    plugin_id="cluster_sweep",
    contract_version=CATALOG_CONTRACT_VERSION,
    kind=PluginKind.SIGNAL,
    description=(
        "Cluster sweep research signal: EMA 9/20/59 structure plus causal LLD "
        "cluster approach/entry and forward confirmation variants."
    ),
    parameters=(
        ParameterDefinition(
            name="minimum_cluster_pools",
            value_type=ValueType.INTEGER,
            required=True,
            description="Minimum pool_count for an active cluster.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="3",
            baseline_defining=True,
            research_space_varies=True,
        ),
        ParameterDefinition(
            name="expire_bars",
            value_type=ValueType.INTEGER,
            required=True,
            description="Forward bars scanned for confirmation after candidate.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="24",
            research_space_varies=True,
        ),
        ParameterDefinition(
            name="approach_bps",
            value_type=ValueType.RATE,
            required=True,
            description="Maximum approach distance to cluster mid in basis points.",
            required_rate_unit=RateUnit.BASIS_POINTS,
            legacy_reference_value="25",
            baseline_defining=True,
            research_space_varies=True,
        ),
        ParameterDefinition(
            name="require_cluster_entry",
            value_type=ValueType.BOOLEAN,
            required=True,
            description="Require actual cluster box entry, not approach-only.",
            legacy_reference_value="true",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="gap_pct",
            value_type=ValueType.RATE,
            required=True,
            description=(
                "Cluster aggregation gap percent passed to LLD engine "
                "(TRP cluster_gap_pct convention)."
            ),
            required_rate_unit=RateUnit.PERCENT,
            legacy_reference_value="0.10",
            baseline_defining=True,
        ),
    ),
    required_features=(
        BoundFeatureRequirement(
            alias="ema_fast",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=9)),),
        ),
        BoundFeatureRequirement(
            alias="ema_medium",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=20)),),
        ),
        BoundFeatureRequirement(
            alias="ema_slow",
            feature_id="ema",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=59)),),
        ),
        BoundFeatureRequirement(
            alias="atr",
            feature_id="atr_wilder",
            bindings=(BoundParameterBinding(name="period", value=IntParam(value=14)),),
        ),
        BoundFeatureRequirement(
            alias="clusters",
            feature_id="lld_liquidity_clusters",
            bindings=(
                BoundParameterBinding(
                    name="gap_pct",
                    value=RateParam(
                        value=RateValue(
                            value=Decimal("0.10"),
                            unit=RateUnit.PERCENT,
                        )
                    ),
                ),
                BoundParameterBinding(
                    name="minimum_pools",
                    value=IntParam(value=3),
                ),
            ),
        ),
    ),
    data_requirements=(
        DataRequirementDescriptor(
            requirement_id="cluster_candles_signal_tf",
            source_kind=DataSourceKind.CANDLES_SIGNAL_TF,
            role=DataRequirementRole.SIGNAL_REQUIRED,
            required=True,
            granularity_minutes=15,
            availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
            rationale="EMA, ATR, sweep detection, confirmation, and primary outcomes.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.event_detector",
                    path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
                    symbol="detect_candidates",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_liquidity_locations",
            source_kind=DataSourceKind.LIQUIDITY_LOCATIONS,
            role=DataRequirementRole.SIGNAL_REQUIRED,
            required=True,
            granularity_minutes=15,
            availability=AvailabilityTiming.PRIOR_BAR_OPEN,
            rationale="TRP LLD clusters derived from signal-TF OHLCV; causal as_of prior bar.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.cluster_adapter",
                    path="src/orderbook_analyse/cluster_sweep_research/cluster_adapter.py",
                    symbol="active_clusters_as_of",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_candles_execution_1m",
            source_kind=DataSourceKind.CANDLES_EXECUTION_1M,
            role=DataRequirementRole.EXECUTION_REQUIRED,
            required=True,
            granularity_minutes=1,
            availability=AvailabilityTiming.ENTRY_BAR_OPEN,
            rationale="Base 1m feed aggregated to signal TF and used for extended analysis.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.clickhouse_source",
                    path="src/orderbook_analyse/cluster_sweep_research/clickhouse_source.py",
                    symbol="fetch_candles_1m",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_public_trades_1m",
            source_kind=DataSourceKind.PUBLIC_TRADES_1M,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale="Optional orderflow enrichment only; missing does not block detection.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_orderbook_ob200_v3_1m",
            source_kind=DataSourceKind.ORDERBOOK_OB200_V3_1M,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale="Optional orderflow enrichment only.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_open_interest_1m",
            source_kind=DataSourceKind.OPEN_INTEREST_1M,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale="Optional orderflow enrichment only.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                ),
            ),
        ),
        DataRequirementDescriptor(
            requirement_id="cluster_liquidations",
            source_kind=DataSourceKind.LIQUIDATIONS,
            role=DataRequirementRole.ANALYSIS_OPTIONAL,
            required=False,
            granularity_minutes=1,
            availability=AvailabilityTiming.WINDOW_EDGE,
            rationale="Optional orderflow enrichment only.",
            provenance=(
                LegacyProvenanceRef(
                    module="orderbook_analyse.cluster_sweep_research.feature_enrichment",
                    path="src/orderbook_analyse/cluster_sweep_research/feature_enrichment.py",
                    symbol="enrich_event_orderflow",
                ),
            ),
        ),
    ),
    supported_directions=Directionality.BOTH,
    signal_timeframe=SignalTimeframeContract(
        mode=SignalTimeframeMode.ALLOWED_SET,
        reference_minutes=15,
        allowed_minutes=(5, 15),
        notes=(
            "Legacy runners pass timeframe as a caller parameter; 15m is canonical "
            "default, 5m is the visual-audit workflow."
        ),
    ),
    execution_timeframe_minutes=1,
    decision_timing=AvailabilityTiming.CONFIRMATION_BAR_CLOSE,
    entry_timing=AvailabilityTiming.ENTRY_BAR_OPEN,
    entry_rule_id="NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR",
    signal_warmup=_SHARED_SIGNAL_WARMUP,
    causality_status=(
        CausalityStatus.CAUSAL_REUSABLE_WHEN_DEPENDENCY_AVAILABLE
    ),
    causality_claim=(
        "Clusters are causal when TRP Liquidity Location is available; "
        "confirmation uses closed signal-TF bars and entry is the next "
        "signal-TF bar open (not the first 1m tick)."
    ),
    adapter_status=AdapterBindingStatus.ADAPTER_PENDING,
    provenance=(
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.pipeline",
            path="src/orderbook_analyse/cluster_sweep_research/pipeline.py",
            symbol="run_cluster_sweep_on_candles",
        ),
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.event_detector",
            path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
            symbol="detect_candidates",
        ),
    ),
)

PLUGIN_DESCRIPTORS: tuple[PluginDescriptor, ...] = (
    EDC_M0_STRICT_SYNC,
    CLUSTER_SWEEP,
)
