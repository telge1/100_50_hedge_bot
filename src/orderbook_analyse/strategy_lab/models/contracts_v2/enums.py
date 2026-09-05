"""Closed enumerations for neutral Strategy Lab V2 contracts."""

from __future__ import annotations

from enum import Enum


class ParameterValueType(str, Enum):
    """Closed parameter value types (distinct from feature output types)."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    RATE = "rate"
    DURATION = "duration"
    TIMEFRAME = "timeframe"
    STRING = "string"
    IDENTIFIER = "identifier"


class FeatureOutputValueType(str, Enum):
    """Closed feature output value types (distinct from parameter types)."""

    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    CLUSTER_SNAPSHOT = "cluster_snapshot"


class TemporalShape(str, Enum):
    """How a feature output evolves across evaluation time."""

    SERIES = "series"
    INSTANT = "instant"


class CollectionShape(str, Enum):
    """How many elements an output carries at one evaluation instant."""

    SINGLE = "single"
    SEQUENCE = "sequence"
    SET = "set"


class AvailabilityTimingV2(str, Enum):
    """When a value becomes causally available."""

    SIGNAL_BAR_CLOSE = "signal_bar_close"
    PRIOR_BAR_OPEN = "prior_bar_open"
    CONFIRMATION_BAR_CLOSE = "confirmation_bar_close"
    ENTRY_BAR_OPEN = "entry_bar_open"
    WINDOW_EDGE = "window_edge"


class DataSourceKindV2(str, Enum):
    """Closed raw data source keys."""

    CANDLES_SIGNAL_TF = "candles_signal_tf"
    CANDLES_EXECUTION_1M = "candles_execution_1m"
    PUBLIC_TRADES_1M = "public_trades_1m"
    # Native/tick public trades (e.g. public_trades_canonical); not the 1m aggregate.
    PUBLIC_TRADES_NATIVE = "public_trades_native"
    ORDERBOOK_OB200_V3_1M = "orderbook_ob200_v3_1m"
    # Closed per-level raw OB200 archive (ob200_v3.zst segments); not the 1m aggregate.
    # Open *.tmp / *_open_* segments must not be read.
    ORDERBOOK_OB200_V3_RAW = "orderbook_ob200_v3_raw"
    OPEN_INTEREST_1M = "open_interest_1m"
    LIQUIDATIONS = "liquidations"
    LIQUIDITY_LOCATIONS = "liquidity_locations"


class StrategyRunIntentV2(str, Enum):
    """Discriminates trade-backtest vs candidate-discovery StrategySpec V2 roots."""

    TRADE_BACKTEST = "trade_backtest"
    CANDIDATE_DISCOVERY = "candidate_discovery"


class PluginContractStatusV2(str, Enum):
    """Whether a catalog plugin is production-ready or research-contract-only."""

    PRODUCTION = "production"
    RESEARCH_CONTRACT_ONLY = "research_contract_only"


class DataRequirementRoleV2(str, Enum):
    """Role of a raw data source relative to plugin signal and execution."""

    SIGNAL_REQUIRED = "signal_required"
    EXECUTION_REQUIRED = "execution_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ANALYSIS_OPTIONAL = "analysis_optional"
    VALIDATION_OPTIONAL = "validation_optional"


class ResearchConfirmationPolicyV2(str, Enum):
    """Closed research confirmation policy for signal plugins."""

    CORE_RESEARCH_SUPPORTIVE = "core_research_supportive"


class MissingValuePolicyV2(str, Enum):
    """How a feature behaves when inputs are missing."""

    REJECT = "reject"
    RETURN_UNAVAILABLE = "return_unavailable"
    OPTIONAL_ENRICHMENT_ONLY = "optional_enrichment_only"


class WarmupTimeframeBasisV2(str, Enum):
    """How plugin minimum warmup bars relate to signal timeframe."""

    SELECTED_SIGNAL_TIMEFRAME = "selected_signal_timeframe"
    FIXED_TIMEFRAME = "fixed_timeframe"


class FeatureWarmupFormulaKindV2(str, Enum):
    """Closed warmup formula kinds for feature outputs."""

    BARS_FROM_PARAMETER = "bars_from_parameter"
    PLUGIN_SIGNAL_GATE = "plugin_signal_gate"
    NO_SEPARATE_BAR_GATE = "no_separate_bar_gate"


class EvaluationSemanticsV2(str, Enum):
    """Closed evaluation semantics for operators."""

    CURRENT_CLOSED_OBSERVATION = "current_closed_observation"
    CROSS_REQUIRES_PRIOR_AND_CURRENT = "cross_requires_prior_and_current"


class OperandOriginV2(str, Enum):
    """Where an operator operand may originate."""

    FEATURE_OUTPUT = "feature_output"
    LITERAL_PARAM = "literal_param"


class OperandTypeConstraintV2(str, Enum):
    """Closed operand type constraints for operator signatures."""

    DECIMAL_SERIES = "decimal_series"
    BOOLEAN_SERIES = "boolean_series"
    DECIMAL_LITERAL = "decimal_literal"
    INTEGER_LITERAL = "integer_literal"
    BOOLEAN_LITERAL = "boolean_literal"


class NullPolicyV2(str, Enum):
    """How an operator behaves when operands are missing."""

    STRICT_REJECT = "strict_reject"
    PROPAGATE_NULL = "propagate_null"


class EntryReferenceRuleV2(str, Enum):
    """Closed entry reference rules for signal plugins."""

    SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR = "signal_tf_next_open_after_signal_bar"
    NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR = "next_bar_open_after_confirmation_bar"


class EntryTimingAnchorV2(str, Enum):
    """Evaluation anchor that precedes tradable entry."""

    SIGNAL_TIMEFRAME_BAR_OPEN = "signal_timeframe_bar_open"


class EntryPriceReferenceV2(str, Enum):
    """Price reference for tradable entry."""

    BAR_OPEN = "bar_open"


class PortfolioEvaluationModeV2(str, Enum):
    """Minimal portfolio evaluation mode for phase-1 research."""

    PER_TRADE_INDEPENDENT = "per_trade_independent"


class NotionalCurrencyV2(str, Enum):
    """Closed phase-1 trade notional currency."""

    USDT = "USDT"


class SignalTimeframeModeV2(str, Enum):
    """How a plugin declares its signal-timeframe semantics."""

    FIXED = "fixed"
    ALLOWED_SET = "allowed_set"
    CALLER_CONFIGURED = "caller_configured"


class AdapterBindingStatusV2(str, Enum):
    """Adapter binding status for catalog-only plugins."""

    CATALOG_ONLY = "catalog_only"
    ADAPTER_PENDING = "adapter_pending"
    ADAPTER_AVAILABLE = "adapter_available"


class PluginParameterBindingTargetV2(str, Enum):
    """Where a plugin parameter value must be bound in StrategySpecV2."""

    PLUGIN_REF_CONFIG = "plugin_ref_config"
    PLUGIN_SIGNAL_SPEC = "plugin_signal_spec"


class PluginModeRequirementV2(str, Enum):
    """Whether and how a plugin uses mode_id on PluginSignalSpec."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"
