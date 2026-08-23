"""Closed feature catalog for Strategy Lab catalog/v2."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CATALOG_CONTRACT_VERSION,
    FeatureDescriptorV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    AvailabilityTimingV2,
    CollectionShape,
    DataSourceKindV2,
    FeatureOutputDescriptorV2,
    FeatureOutputValueType,
    FeatureWarmupFormulaKindV2,
    FeatureWarmupFormulaV2,
    IntBoundsV2,
    LegacyProvenanceRefV2,
    MissingValuePolicyV2,
    ParameterDefinitionV2,
    ParameterValueType,
    TemporalShape,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier

_SID = StableIdentifier

_VALUE_OUTPUT = FeatureOutputDescriptorV2(
    output_id=_SID(value="value"),
    value_type=FeatureOutputValueType.DECIMAL,
    temporal_shape=TemporalShape.SERIES,
    collection_shape=CollectionShape.SINGLE,
    nullable=False,
    availability=AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
    missing_value_policy=MissingValuePolicyV2.REJECT,
    warmup=FeatureWarmupFormulaV2(
        formula_kind=FeatureWarmupFormulaKindV2.BARS_FROM_PARAMETER,
        parameter_name=_SID(value="period"),
        minimum_bars=None,
        notes="First valid value after SMA seed at bar index period-1.",
    ),
    description="Decimal series value at signal bar close.",
)

_SNAPSHOTS_OUTPUT = FeatureOutputDescriptorV2(
    output_id=_SID(value="snapshots"),
    value_type=FeatureOutputValueType.CLUSTER_SNAPSHOT,
    temporal_shape=TemporalShape.INSTANT,
    collection_shape=CollectionShape.SEQUENCE,
    nullable=False,
    availability=AvailabilityTimingV2.PRIOR_BAR_OPEN,
    missing_value_policy=MissingValuePolicyV2.RETURN_UNAVAILABLE,
    warmup=FeatureWarmupFormulaV2(
        formula_kind=FeatureWarmupFormulaKindV2.NO_SEPARATE_BAR_GATE,
        parameter_name=None,
        minimum_bars=None,
        notes=(
            "Clusters are queried causally from prior-bar open; legacy "
            "active_clusters_as_of returns a Python list preserving "
            "filter_clusters iteration order (sequence, not set)."
        ),
    ),
    description=(
        "Ordered cluster snapshots at causal as_of; legacy returns list[ClusterSnapshot]."
    ),
)

EMA = FeatureDescriptorV2(
    feature_id=_SID(value="ema"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description=(
        "SMA-seed recursive EMA on closed signal-timeframe candles. "
        "Concrete period is bound per plugin usage."
    ),
    outputs=(_VALUE_OUTPUT,),
    parameters=(
        ParameterDefinitionV2(
            name=_SID(value="period"),
            value_type=ParameterValueType.INTEGER,
            required=True,
            description="EMA lookback period in signal-timeframe bars.",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value=None,
            must_be_explicit=True,
            research_space_varies=False,
            baseline_defining=False,
        ),
    ),
    data_requirements=(DataSourceKindV2.CANDLES_SIGNAL_TF.value,),
    provenance=(
        LegacyProvenanceRefV2(
            module="orderbook_analyse.cluster_sweep_research.ema_features",
            path="src/orderbook_analyse/cluster_sweep_research/ema_features.py",
            symbol="attach_emas",
            notes="Shared by EDC and cluster sweep via attach_emas().",
        ),
    ),
)

ATR_WILDER = FeatureDescriptorV2(
    feature_id=_SID(value="atr_wilder"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description=(
        "Wilder ATR on closed signal-timeframe candles. "
        "Concrete period is bound per plugin usage."
    ),
    outputs=(_VALUE_OUTPUT,),
    parameters=(
        ParameterDefinitionV2(
            name=_SID(value="period"),
            value_type=ParameterValueType.INTEGER,
            required=True,
            description="ATR lookback period in signal-timeframe bars.",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value=None,
            must_be_explicit=True,
            research_space_varies=False,
            baseline_defining=False,
        ),
    ),
    data_requirements=(DataSourceKindV2.CANDLES_SIGNAL_TF.value,),
    provenance=(
        LegacyProvenanceRefV2(
            module="orderbook_analyse.ema_dual_cross_multisource.ema_candidate",
            path="src/orderbook_analyse/ema_dual_cross_multisource/ema_candidate.py",
            symbol="attach_atr",
            notes="EDC detection uses ATR for compression and flat-slope guards.",
        ),
        LegacyProvenanceRefV2(
            module="orderbook_analyse.cluster_sweep_research.event_detector",
            path="src/orderbook_analyse/cluster_sweep_research/event_detector.py",
            symbol="_atr",
            notes="Cluster sweep computes atr_14 on the signal frame.",
        ),
    ),
)

LLD_LIQUIDITY_CLUSTERS = FeatureDescriptorV2(
    feature_id=_SID(value="lld_liquidity_clusters"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description=(
        "Causal liquidity-location cluster snapshots derived from OHLCV via the "
        "TRP Liquidity Location engine."
    ),
    outputs=(_SNAPSHOTS_OUTPUT,),
    parameters=(
        ParameterDefinitionV2(
            name=_SID(value="gap_pct"),
            value_type=ParameterValueType.RATE,
            required=True,
            description=(
                "Maximum relative gap between pools merged into a cluster, "
                "expressed as percent of price (TRP cluster_gap_pct convention)."
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
        ParameterDefinitionV2(
            name=_SID(value="minimum_pools"),
            value_type=ParameterValueType.INTEGER,
            required=True,
            description="Minimum pool_count required for a cluster to be active.",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value="3",
            must_be_explicit=True,
            research_space_varies=False,
            baseline_defining=True,
        ),
        ParameterDefinitionV2(
            name=_SID(value="amount"),
            value_type=ParameterValueType.INTEGER,
            required=False,
            description="TRP display prune amount (does not affect causal snapshots).",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value="300",
            must_be_explicit=False,
            research_space_varies=False,
            baseline_defining=False,
        ),
        ParameterDefinitionV2(
            name=_SID(value="highest_len"),
            value_type=ParameterValueType.INTEGER,
            required=False,
            description="TRP swing-high confirmation length.",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value="2",
            must_be_explicit=False,
            research_space_varies=False,
            baseline_defining=False,
        ),
        ParameterDefinitionV2(
            name=_SID(value="lowest_len"),
            value_type=ParameterValueType.INTEGER,
            required=False,
            description="TRP swing-low confirmation length.",
            allowed_identifiers=(),
            int_bounds=IntBoundsV2(min_value=1, max_value=None),
            decimal_bounds=None,
            required_rate_unit=None,
            legacy_reference_value="2",
            must_be_explicit=False,
            research_space_varies=False,
            baseline_defining=False,
        ),
    ),
    data_requirements=(
        DataSourceKindV2.CANDLES_SIGNAL_TF.value,
        DataSourceKindV2.LIQUIDITY_LOCATIONS.value,
    ),
    provenance=(
        LegacyProvenanceRefV2(
            module="orderbook_analyse.cluster_sweep_research.cluster_adapter",
            path="src/orderbook_analyse/cluster_sweep_research/cluster_adapter.py",
            symbol="active_clusters_as_of",
            notes="Returns list[ClusterSnapshot] preserving filter_clusters order.",
        ),
    ),
)

FEATURE_DESCRIPTORS_V2: tuple[FeatureDescriptorV2, ...] = (
    ATR_WILDER,
    EMA,
    LLD_LIQUIDITY_CLUSTERS,
)
