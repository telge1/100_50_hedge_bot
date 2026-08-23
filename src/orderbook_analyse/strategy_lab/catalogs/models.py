"""Shared catalog value objects, enums, and exceptions for Strategy Lab P3."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from orderbook_analyse.strategy_lab.models.enums import (
    CausalityStatus,
    Directionality,
    PluginKind,
    RateUnit,
)
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    DurationParam,
    IdentifierParam,
    IntParam,
    RateParam,
    StringParam,
    TimeframeParam,
)

CATALOG_CONTRACT_VERSION = "catalog/v1"
CATALOG_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

ParamBindingValue = (
    BoolParam
    | IntParam
    | DecimalParam
    | RateParam
    | DurationParam
    | TimeframeParam
    | StringParam
    | IdentifierParam
)


class CatalogError(Exception):
    """Base error for catalog operations."""


class UnknownCatalogEntryError(CatalogError):
    """Raised when a catalog lookup misses a closed registry entry."""


class DuplicateCatalogEntryError(CatalogError):
    """Raised when a catalog would contain duplicate IDs."""


class InvalidCatalogDefinitionError(CatalogError):
    """Raised when a catalog definition violates structural rules."""


class ValueType(str, Enum):
    """Closed value types for catalog parameters and feature outputs."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    RATE = "rate"
    DURATION = "duration"
    TIMEFRAME = "timeframe"
    STRING = "string"
    IDENTIFIER = "identifier"
    PRICE_SERIES = "price_series"
    CLUSTER_SNAPSHOT = "cluster_snapshot"
    SIGNAL_EVENT = "signal_event"


class Arity(str, Enum):
    UNARY = "unary"
    BINARY = "binary"
    NARY = "nary"


class NullPolicy(str, Enum):
    """How an operator behaves when operands are missing."""

    STRICT_REJECT = "strict_reject"
    PROPAGATE_NULL = "propagate_null"


class AvailabilityTiming(str, Enum):
    """When a feature value becomes causally available."""

    SIGNAL_BAR_CLOSE = "signal_bar_close"
    PRIOR_BAR_OPEN = "prior_bar_open"
    CONFIRMATION_BAR_CLOSE = "confirmation_bar_close"
    ENTRY_BAR_OPEN = "entry_bar_open"
    WINDOW_EDGE = "window_edge"


class DataSourceKind(str, Enum):
    """Closed raw data source keys referenced by catalog descriptors."""

    CANDLES_SIGNAL_TF = "candles_signal_tf"
    CANDLES_EXECUTION_1M = "candles_execution_1m"
    PUBLIC_TRADES_1M = "public_trades_1m"
    ORDERBOOK_OB200_V3_1M = "orderbook_ob200_v3_1m"
    OPEN_INTEREST_1M = "open_interest_1m"
    LIQUIDATIONS = "liquidations"
    LIQUIDITY_LOCATIONS = "liquidity_locations"


class ResearchConfirmationPolicy(str, Enum):
    """Closed research confirmation policy for signal plugins."""

    CORE_RESEARCH_SUPPORTIVE = "core_research_supportive"


class DataRequirementRole(str, Enum):
    """Role of a raw data source relative to plugin signal and execution."""

    SIGNAL_REQUIRED = "signal_required"
    EXECUTION_REQUIRED = "execution_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ANALYSIS_OPTIONAL = "analysis_optional"
    VALIDATION_OPTIONAL = "validation_optional"


class SignalTimeframeMode(str, Enum):
    """How a plugin declares its signal-timeframe semantics."""

    FIXED = "fixed"
    ALLOWED_SET = "allowed_set"
    CALLER_CONFIGURED = "caller_configured"


class AdapterBindingStatus(str, Enum):
    CATALOG_ONLY = "catalog_only"
    ADAPTER_PENDING = "adapter_pending"
    ADAPTER_AVAILABLE = "adapter_available"


class MissingValuePolicy(str, Enum):
    """How a feature behaves when inputs are missing."""

    REJECT = "reject"
    RETURN_UNAVAILABLE = "return_unavailable"
    OPTIONAL_ENRICHMENT_ONLY = "optional_enrichment_only"


@dataclass(frozen=True, slots=True, kw_only=True)
class IntBounds:
    min_value: int | None = None
    max_value: int | None = None

    def __post_init__(self) -> None:
        if self.min_value is not None and type(self.min_value) is not int:
            raise TypeError("IntBounds.min_value must be exact int")
        if self.max_value is not None and type(self.max_value) is not int:
            raise TypeError("IntBounds.max_value must be exact int")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise InvalidCatalogDefinitionError("IntBounds min_value > max_value")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecimalBounds:
    min_value: Decimal | None = None
    max_value: Decimal | None = None

    def __post_init__(self) -> None:
        if self.min_value is not None and type(self.min_value) is not Decimal:
            raise TypeError("DecimalBounds.min_value must be exact Decimal")
        if self.max_value is not None and type(self.max_value) is not Decimal:
            raise TypeError("DecimalBounds.max_value must be exact Decimal")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise InvalidCatalogDefinitionError(
                "DecimalBounds min_value > max_value"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterDefinition:
    """Typed parameter contract for features and plugins."""

    name: str
    value_type: ValueType
    required: bool
    description: str
    allowed_identifiers: tuple[str, ...] = ()
    int_bounds: IntBounds | None = None
    decimal_bounds: DecimalBounds | None = None
    required_rate_unit: RateUnit | None = None
    legacy_reference_value: str | None = None
    must_be_explicit: bool = True
    research_space_varies: bool = False
    baseline_defining: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyProvenanceRef:
    """Non-executable legacy source reference (metadata only)."""

    module: str
    path: str
    symbol: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureWarmupContract:
    """Feature-level minimum warmup; may depend on a bound period parameter."""

    minimum_bars_parameter: str | None = None
    legacy_reference_minimum_bars: int | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginSignalWarmup:
    """Engine-side bar-index gate before a plugin may emit signals."""

    minimum_bar_index: int
    slow_ema_period: int
    legacy_extra_bars: int
    notes: str | None = None
    provenance: tuple[LegacyProvenanceRef, ...] = ()

    @property
    def total_signal_tf_bars(self) -> int:
        return self.slow_ema_period + self.legacy_extra_bars


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceLoadingPadding:
    """Calendar padding applied when loading market data around a window."""

    candle_pad_days: int = 0
    auxiliary_source_pad_hours: int = 0
    notes: str | None = None
    provenance: tuple[LegacyProvenanceRef, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeEvaluationPadding:
    """Calendar padding after the evaluation window for outcome simulation."""

    outcome_pad_hours: int = 0
    notes: str | None = None
    provenance: tuple[LegacyProvenanceRef, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundParameterBinding:
    """One explicitly bound feature parameter value."""

    name: str
    value: ParamBindingValue


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundFeatureRequirement:
    """Plugin-bound feature usage with explicit parameters and stable alias."""

    alias: str
    feature_id: str
    bindings: tuple[BoundParameterBinding, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DataRequirementDescriptor:
    """Typed raw data requirement with role and causal availability."""

    requirement_id: str
    source_kind: DataSourceKind
    role: DataRequirementRole
    required: bool
    granularity_minutes: int
    availability: AvailabilityTiming
    rationale: str
    required_for_policy: ResearchConfirmationPolicy | None = None
    provenance: tuple[LegacyProvenanceRef, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalTimeframeContract:
    """Signal-timeframe semantics for a plugin."""

    mode: SignalTimeframeMode
    reference_minutes: int
    allowed_minutes: tuple[int, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureDescriptor:
    feature_id: str
    contract_version: str
    description: str
    output_type: ValueType
    parameters: tuple[ParameterDefinition, ...]
    data_requirements: tuple[DataSourceKind, ...]
    warmup: FeatureWarmupContract
    availability: AvailabilityTiming
    missing_value_policy: MissingValuePolicy
    provenance: tuple[LegacyProvenanceRef, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class OperandTypeSpec:
    operand_index: int
    value_type: ValueType


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorDescriptor:
    operator_id: str
    contract_version: str
    description: str
    arity: Arity
    operand_types: tuple[OperandTypeSpec, ...]
    result_type: ValueType
    null_policy: NullPolicy
    requires_previous_observation: bool
    causal_semantics: str
    contract_note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginDescriptor:
    plugin_id: str
    contract_version: str
    kind: PluginKind
    description: str
    parameters: tuple[ParameterDefinition, ...]
    required_features: tuple[BoundFeatureRequirement, ...]
    data_requirements: tuple[DataRequirementDescriptor, ...]
    confirmation_policy: ResearchConfirmationPolicy | None = None
    supported_directions: Directionality
    signal_timeframe: SignalTimeframeContract
    execution_timeframe_minutes: int
    decision_timing: AvailabilityTiming
    entry_timing: AvailabilityTiming
    entry_rule_id: str
    signal_warmup: PluginSignalWarmup
    source_loading_padding: SourceLoadingPadding | None = None
    outcome_evaluation_padding: OutcomeEvaluationPadding | None = None
    causality_status: CausalityStatus
    causality_claim: str
    adapter_status: AdapterBindingStatus
    provenance: tuple[LegacyProvenanceRef, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogIntegrityIssue:
    code: str
    message: str
    entry_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogIntegrityReport:
    issues: tuple[CatalogIntegrityIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues
