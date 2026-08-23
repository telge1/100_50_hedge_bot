"""StrategySpec V2 root model."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.data_requirement import (
    DataRequirementSpecV2,
    EntrySpecV2,
)
from orderbook_analyse.strategy_lab.models.features import FeatureBindingSpec
from orderbook_analyse.strategy_lab.models.provenance import ProvenanceSpec
from orderbook_analyse.strategy_lab.models.signals import SignalDefinition
from orderbook_analyse.strategy_lab.models.strategy import (
    AnalysisRequirements,
    BaselineSpec,
    ExecutionAssumptions,
    ExitSpec,
    FeesSpec,
    IntrabarPolicy,
    Metadata,
    ModelingStatusBlock,
    PortfolioAssumptions,
    ResearchParameterSpace,
    Timeframes,
    UniverseSpec,
    ValidationRequirements,
)
from orderbook_analyse.strategy_lab.models.warmup_v2 import WarmupSpecV2

STRATEGY_SPEC_V2_SCHEMA_VERSION = "strategy_spec/v2"


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategySpecV2:
    """Root StrategySpec V2 — signal variants own setup/trigger semantics."""

    metadata: Metadata
    universe: UniverseSpec
    timeframes: Timeframes
    data_requirements: tuple[DataRequirementSpecV2, ...]
    warmup: WarmupSpecV2
    features: tuple[FeatureBindingSpec, ...]
    signal: SignalDefinition
    entry: EntrySpecV2
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
        if self.metadata.schema_version != STRATEGY_SPEC_V2_SCHEMA_VERSION:
            raise ValueError(
                f"metadata.schema_version must be {STRATEGY_SPEC_V2_SCHEMA_VERSION!r}, "
                f"got {self.metadata.schema_version!r}"
            )
