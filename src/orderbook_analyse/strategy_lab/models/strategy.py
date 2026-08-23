"""StrategySpec V1 root and nested section models.

Stdlib-only dataclasses. No YAML/JSON loading, no validator pipeline, no
silent trading-parameter defaults, no legacy imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.enums import (
    CausalityStatus,
    DataRequirementStatus,
    Directionality,
    DurationUnit,
    ExitMode,
    MirrorMode,
    ModelingStatus,
    PluginKind,
    RateUnit,
    SameBarPriority,
    SideName,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.provenance import ProvenanceSpec

STRATEGY_SPEC_SCHEMA_VERSION = "strategy_spec/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class RateValue:
    """Financial rate with explicit unit (Decimal — never binary float)."""

    value: Decimal
    unit: RateUnit

    def __post_init__(self) -> None:
        if type(self.value) is not Decimal:
            raise TypeError(
                "RateValue.value must be exact Decimal (float not accepted)"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DurationValue:
    """Holding / pad duration with explicit unit (not a bar timeframe)."""

    value: Decimal
    unit: DurationUnit

    def __post_init__(self) -> None:
        if type(self.value) is not Decimal:
            raise TypeError(
                "DurationValue.value must be exact Decimal (float not accepted)"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeframeValue:
    """Bar timeframe (signal/execution). Distinct from holding duration."""

    value: int
    unit: TimeframeUnit

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise TypeError(
                "TimeframeValue.value must be exact int (bool not accepted)"
            )


# ---------------------------------------------------------------------------
# Typed plugin / modeling parameter values (no str-only, no Any, no dicts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StringParam:
    """Free-form string parameter. ``_schema_kind`` discriminates ParamValue."""

    _schema_kind: ClassVar[str] = "string"
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("StringParam.value must be exact str")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoolParam:
    _schema_kind: ClassVar[str] = "boolean"
    value: bool

    def __post_init__(self) -> None:
        if type(self.value) is not bool:
            raise TypeError("BoolParam.value must be exact bool")


@dataclass(frozen=True, slots=True, kw_only=True)
class IntParam:
    _schema_kind: ClassVar[str] = "integer"
    value: int

    def __post_init__(self) -> None:
        # bool is a subclass of int — reject explicitly
        if type(self.value) is not int:
            raise TypeError(
                "IntParam.value must be exact int (bool not accepted)"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DecimalParam:
    _schema_kind: ClassVar[str] = "decimal"
    value: Decimal

    def __post_init__(self) -> None:
        if type(self.value) is not Decimal:
            raise TypeError(
                "DecimalParam.value must be exact Decimal (float not accepted)"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class RateParam:
    _schema_kind: ClassVar[str] = "rate"
    value: RateValue


@dataclass(frozen=True, slots=True, kw_only=True)
class DurationParam:
    _schema_kind: ClassVar[str] = "duration"
    value: DurationValue


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeframeParam:
    _schema_kind: ClassVar[str] = "timeframe"
    value: TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentifierParam:
    """Enum / reference identifier (semantically distinct from free-form string)."""

    _schema_kind: ClassVar[str] = "identifier"
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("IdentifierParam.value must be exact str")


ParamValue = (
    StringParam
    | BoolParam
    | IntParam
    | DecimalParam
    | RateParam
    | DurationParam
    | TimeframeParam
    | IdentifierParam
)

_PARAM_VALUE_TYPES: tuple[type, ...] = (
    StringParam,
    BoolParam,
    IntParam,
    DecimalParam,
    RateParam,
    DurationParam,
    TimeframeParam,
    IdentifierParam,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigEntry:
    """Typed key/value pair for plugin config (immutable; no free-form dicts)."""

    key: str
    value: ParamValue

    def __post_init__(self) -> None:
        if type(self.key) is not str:
            raise TypeError("ConfigEntry.key must be exact str")
        if not isinstance(self.value, _PARAM_VALUE_TYPES):
            raise TypeError(
                "ConfigEntry.value must be a typed ParamValue "
                f"(got {type(self.value).__name__})"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginRef:
    id: str
    version: str
    kind: PluginKind
    config: tuple[ConfigEntry, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureRef:
    id: str
    version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelingStatusBlock:
    """Explicit status for a modeling domain (slippage, funding, …)."""

    status: ModelingStatus
    detail: str | None = None
    parameters: tuple[ConfigEntry, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Metadata:
    schema_version: str
    strategy_id: str
    strategy_version: str
    family: str
    variant: str
    title: str
    status: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSpec:
    role: str
    ref: str | None = None
    symbols: tuple[str, ...] = ()
    exclude_symbols: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeframes:
    signal: TimeframeValue
    execution: TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class DataRequirement:
    id: str
    status: DataRequirementStatus
    source: str | None = None
    timeframe: TimeframeValue | None = None
    group: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WarmupSpec:
    ema_slow_bars: int
    extra_bars: int
    pad_days: int
    outcome_pad_hours: int
    source_pad_hours: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalSpec:
    plugin: PluginRef
    mode_id: str | None = None
    rules_embedded_in_yaml: bool = False
    directionality: Directionality = Directionality.BOTH


@dataclass(frozen=True, slots=True, kw_only=True)
class SetupSpec:
    """Market/structure setup (distinct from trigger and confirmation)."""

    description: str
    decision_at: str
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TriggerSpec:
    """Event that fires after setup (distinct from setup/confirmation)."""

    description: str
    plugin: PluginRef | None = None
    plugin_ref: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfirmationSpec:
    """Confirmation / gate layer (distinct from setup/trigger)."""

    description: str
    gates_policy_id: str | None = None
    gates_policy_version: str | None = None
    plugin: PluginRef | None = None
    rules_embedded_in_yaml: bool = False
    status: ModelingStatus | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SideSpec:
    name: SideName
    mirror_mode: MirrorMode
    mirror_of: SideName | None = None
    sign_flip_fields: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EntrySpec:
    """Entry timing: decision point vs first tradable point are separate."""

    decision_point: str
    tradable_point: str
    rule_id: str
    plugin: PluginRef
    description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidationSpec:
    description: str
    rules_embedded_in_yaml: bool = False
    plugin: PluginRef | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitSpec:
    """Exit: parametric TP/SL/horizon **or** fully plugin-described."""

    mode: ExitMode
    plugin: PluginRef | None = None
    take_profit: RateValue | None = None
    stop_loss: RateValue | None = None
    horizon: DurationValue | None = None
    same_bar_priority: SameBarPriority | None = None
    require_full_horizon: bool | None = None
    incomplete_outcome_reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode is ExitMode.PARAMETRIC:
            missing = [
                name
                for name, val in (
                    ("take_profit", self.take_profit),
                    ("stop_loss", self.stop_loss),
                    ("horizon", self.horizon),
                    ("same_bar_priority", self.same_bar_priority),
                    ("require_full_horizon", self.require_full_horizon),
                    ("incomplete_outcome_reason", self.incomplete_outcome_reason),
                )
                if val is None
            ]
            if missing:
                raise ValueError(
                    "ExitMode.PARAMETRIC requires fields: " + ", ".join(missing)
                )
        elif self.mode is ExitMode.PLUGIN:
            if self.plugin is None:
                raise ValueError("ExitMode.PLUGIN requires plugin")


@dataclass(frozen=True, slots=True, kw_only=True)
class IntrabarPolicy:
    same_bar_priority: SameBarPriority
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionAssumptions:
    notional: Decimal
    notional_currency: str
    fill_model: str
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeesSpec:
    """Fee schedule only — slippage and funding are separate top-level fields."""

    roundtrip_cost: RateValue
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioAssumptions:
    evaluation_mode: str
    one_trade_per_candidate: bool
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineCell:
    timeframe: TimeframeValue
    mode_id: str
    group: str
    take_profit: RateValue
    stop_loss: RateValue
    horizon: DurationValue
    cost: RateValue
    cell_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineSpec:
    cell: BaselineCell
    is_reference: bool
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchParameterSpace:
    cells: tuple[BaselineCell, ...]
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisRequirements:
    required_label_fields: tuple[str, ...]
    min_trades: int | None = None
    min_symbols: int | None = None
    forbidden_leakage_features: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationRequirements:
    require_causality_audit: bool
    require_strategy_parity_check: bool
    allowed_causality_statuses: tuple[CausalityStatus, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategySpec:
    """Root StrategySpec V1 — all major sections are required fields."""

    metadata: Metadata
    universe: UniverseSpec
    timeframes: Timeframes
    data_requirements: tuple[DataRequirement, ...]
    warmup: WarmupSpec
    features: tuple[FeatureRef, ...]
    signal: SignalSpec
    setup: SetupSpec
    trigger: TriggerSpec
    confirmation: ConfirmationSpec
    long: SideSpec
    short: SideSpec
    entry: EntrySpec
    invalidation: InvalidationSpec
    exit: ExitSpec
    intrabar_policy: IntrabarPolicy
    execution_assumptions: ExecutionAssumptions
    fees: FeesSpec
    slippage: ModelingStatusBlock
    funding: ModelingStatusBlock
    portfolio_assumptions: PortfolioAssumptions
    baseline: BaselineSpec
    research_parameter_space: ResearchParameterSpace
    analysis_requirements: AnalysisRequirements
    validation_requirements: ValidationRequirements
    provenance: ProvenanceSpec

    def __post_init__(self) -> None:
        if self.metadata.schema_version != STRATEGY_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"metadata.schema_version must be {STRATEGY_SPEC_SCHEMA_VERSION!r}, "
                f"got {self.metadata.schema_version!r}"
            )
        if self.signal.rules_embedded_in_yaml:
            raise ValueError(
                "signal.rules_embedded_in_yaml must be False in StrategySpec V1 "
                "(complex logic belongs in plugins)"
            )
        if self.long.name is not SideName.LONG:
            raise ValueError("long.name must be SideName.LONG")
        if self.short.name is not SideName.SHORT:
            raise ValueError("short.name must be SideName.SHORT")
