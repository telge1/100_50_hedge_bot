"""Closed feature catalog for Strategy Lab V1 reference strategies."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.catalogs.models import (
    AvailabilityTiming,
    CATALOG_CONTRACT_VERSION,
    DataSourceKind,
    FeatureDescriptor,
    FeatureWarmupContract,
    IntBounds,
    LegacyProvenanceRef,
    MissingValuePolicy,
    ParameterDefinition,
    ValueType,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit

EMA = FeatureDescriptor(
    feature_id="ema",
    contract_version=CATALOG_CONTRACT_VERSION,
    description=(
        "SMA-seed recursive EMA on closed signal-timeframe candles. "
        "Concrete period is bound per plugin usage."
    ),
    output_type=ValueType.PRICE_SERIES,
    parameters=(
        ParameterDefinition(
            name="period",
            value_type=ValueType.INTEGER,
            required=True,
            description="EMA lookback period in signal-timeframe bars.",
            int_bounds=IntBounds(min_value=1),
            must_be_explicit=True,
        ),
    ),
    data_requirements=(DataSourceKind.CANDLES_SIGNAL_TF,),
    warmup=FeatureWarmupContract(
        minimum_bars_parameter="period",
        legacy_reference_minimum_bars=59,
        notes="First valid value after SMA seed at bar index period-1.",
    ),
    availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
    missing_value_policy=MissingValuePolicy.REJECT,
    provenance=(
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.ema_features",
            path="src/orderbook_analyse/cluster_sweep_research/ema_features.py",
            symbol="attach_emas",
            notes="Shared by EDC and cluster sweep via attach_emas().",
        ),
    ),
)

ATR_WILDER = FeatureDescriptor(
    feature_id="atr_wilder",
    contract_version=CATALOG_CONTRACT_VERSION,
    description=(
        "Wilder ATR on closed signal-timeframe candles. "
        "Concrete period is bound per plugin usage."
    ),
    output_type=ValueType.PRICE_SERIES,
    parameters=(
        ParameterDefinition(
            name="period",
            value_type=ValueType.INTEGER,
            required=True,
            description="ATR lookback period in signal-timeframe bars.",
            int_bounds=IntBounds(min_value=1),
            must_be_explicit=True,
        ),
    ),
    data_requirements=(DataSourceKind.CANDLES_SIGNAL_TF,),
    warmup=FeatureWarmupContract(
        minimum_bars_parameter="period",
        legacy_reference_minimum_bars=14,
        notes="Reference strategies gate ATR via plugin signal warmup, not ATR alone.",
    ),
    availability=AvailabilityTiming.SIGNAL_BAR_CLOSE,
    missing_value_policy=MissingValuePolicy.REJECT,
    provenance=(
        LegacyProvenanceRef(
            module="orderbook_analyse.ema_dual_cross_multisource.ema_candidate",
            path="src/orderbook_analyse/ema_dual_cross_multisource/ema_candidate.py",
            symbol="attach_atr",
            notes="EDC detection uses ATR for compression and flat-slope guards.",
        ),
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.event_detector",
            path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
            symbol="_atr",
            notes="Cluster sweep computes atr_14 on the signal frame.",
        ),
    ),
)

LLD_LIQUIDITY_CLUSTERS = FeatureDescriptor(
    feature_id="lld_liquidity_clusters",
    contract_version=CATALOG_CONTRACT_VERSION,
    description=(
        "Causal liquidity-location cluster snapshots derived from OHLCV via the "
        "TRP Liquidity Location engine."
    ),
    output_type=ValueType.CLUSTER_SNAPSHOT,
    parameters=(
        ParameterDefinition(
            name="gap_pct",
            value_type=ValueType.RATE,
            required=True,
            description=(
                "Maximum relative gap between pools merged into a cluster, "
                "expressed as percent of price (TRP cluster_gap_pct convention)."
            ),
            required_rate_unit=RateUnit.PERCENT,
            legacy_reference_value="0.10",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="minimum_pools",
            value_type=ValueType.INTEGER,
            required=True,
            description="Minimum pool_count required for a cluster to be active.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="3",
            baseline_defining=True,
        ),
        ParameterDefinition(
            name="amount",
            value_type=ValueType.INTEGER,
            required=False,
            description="TRP display prune amount (does not affect causal snapshots).",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="300",
            must_be_explicit=False,
        ),
        ParameterDefinition(
            name="highest_len",
            value_type=ValueType.INTEGER,
            required=False,
            description="TRP swing-high confirmation length.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="2",
            must_be_explicit=False,
        ),
        ParameterDefinition(
            name="lowest_len",
            value_type=ValueType.INTEGER,
            required=False,
            description="TRP swing-low confirmation length.",
            int_bounds=IntBounds(min_value=1),
            legacy_reference_value="2",
            must_be_explicit=False,
        ),
    ),
    data_requirements=(
        DataSourceKind.CANDLES_SIGNAL_TF,
        DataSourceKind.LIQUIDITY_LOCATIONS,
    ),
    warmup=FeatureWarmupContract(
        notes=(
            "Clusters are queried causally from prior-bar open within the loaded "
            "signal-timeframe history; no separate bar-count gate beyond plugin warmup."
        ),
    ),
    availability=AvailabilityTiming.PRIOR_BAR_OPEN,
    missing_value_policy=MissingValuePolicy.RETURN_UNAVAILABLE,
    provenance=(
        LegacyProvenanceRef(
            module="orderbook_analyse.cluster_sweep_research.cluster_adapter",
            path="src/orderbook_analyse/cluster_sweep_research/cluster_adapter.py",
            symbol="active_clusters_as_of",
            notes="Clusters are queried with as_of = prior bar open_time.",
        ),
    ),
)

FEATURE_DESCRIPTORS: tuple[FeatureDescriptor, ...] = (
    ATR_WILDER,
    EMA,
    LLD_LIQUIDITY_CLUSTERS,
)
