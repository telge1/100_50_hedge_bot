"""Minimal Phase-1 StrategySpec V2 contracts required before P4C."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    EntryPriceReferenceV2,
    NotionalCurrencyV2,
    PortfolioEvaluationModeV2,
)
from orderbook_analyse.strategy_lab.models.enums import CausalityStatus
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    DurationValue,
    ModelingStatus,
    ParamValue,
    RateValue,
    TimeframeValue,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitSpecV2:
    take_profit: RateValue
    stop_loss: RateValue
    horizon: DurationValue


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionAssumptionsV2:
    execution_timeframe: TimeframeValue
    fixed_notional: Decimal
    notional_currency: NotionalCurrencyV2
    fill_price_reference: EntryPriceReferenceV2
    rounding_status: ModelingStatus

    def __post_init__(self) -> None:
        if type(self.fixed_notional) is not Decimal:
            raise TypeError("fixed_notional must be exact Decimal")
        if self.fixed_notional <= 0:
            raise ValueError("fixed_notional must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class CostsSpecV2:
    roundtrip_cost: RateValue
    slippage: ModelingStatus
    funding: ModelingStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioAssumptionsV2:
    evaluation_mode: PortfolioEvaluationModeV2
    compounding: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalTimeframeTargetV2:
    _schema_kind: ClassVar[str] = "signal_timeframe"


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureParameterTargetV2:
    _schema_kind: ClassVar[str] = "feature_parameter"
    feature_alias: StableIdentifier
    parameter_name: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginConfigParameterTargetV2:
    _schema_kind: ClassVar[str] = "plugin_config_parameter"
    parameter_name: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitParameterTargetV2:
    _schema_kind: ClassVar[str] = "exit_parameter"
    parameter_name: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class RoundtripCostTargetV2:
    _schema_kind: ClassVar[str] = "roundtrip_cost"


ParameterTargetV2 = (
    SignalTimeframeTargetV2
    | FeatureParameterTargetV2
    | PluginConfigParameterTargetV2
    | ExitParameterTargetV2
    | RoundtripCostTargetV2
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchDimensionV2:
    dimension_id: StableIdentifier
    target: ParameterTargetV2
    candidates: tuple[ParamValue, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchParameterSpaceV2:
    dimensions: tuple[ResearchDimensionV2, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionedUniverseRefV2:
    universe_id: StableIdentifier
    version: str
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version.strip():
            raise TypeError("version must be a non-empty str")
        if type(self.content_hash) is not str or not self.content_hash.strip():
            raise TypeError("content_hash must be a non-empty str")


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginProvenanceRefV2:
    plugin_id: StableIdentifier
    contract_version: ContractVersion


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceSpecV2:
    git_commit: str
    source_repository: str
    source_paths: tuple[str, ...]
    catalog_contract_version: ContractVersion
    plugin_refs: tuple[PluginProvenanceRefV2, ...]
    causality_status: CausalityStatus

    def __post_init__(self) -> None:
        if type(self.git_commit) is not str or not self.git_commit.strip():
            raise TypeError("git_commit must be a non-empty str")
        if type(self.source_repository) is not str or not self.source_repository.strip():
            raise TypeError("source_repository must be a non-empty str")
        if type(self.source_paths) is not tuple:
            raise TypeError("source_paths must be a tuple")
