"""StrategySpec V2 root model."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.data_requirement import (
    DataRequirementSpecV2,
    EntrySpecV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    CostsSpecV2,
    ExecutionAssumptionsV2,
    ExitSpecV2,
    PortfolioAssumptionsV2,
    ProvenanceSpecV2,
    ResearchParameterSpaceV2,
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.features import FeatureBindingSpec
from orderbook_analyse.strategy_lab.models.signals import SignalDefinition
from orderbook_analyse.strategy_lab.models.strategy import (
    AnalysisRequirements,
    IntrabarPolicy,
    Metadata,
    Timeframes,
    ValidationRequirements,
)
from orderbook_analyse.strategy_lab.models.warmup_v2 import WarmupSpecV2

STRATEGY_SPEC_V2_SCHEMA_VERSION = "strategy_spec/v2"


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategySpecV2:
    """Root StrategySpec V2 — signal variants own setup/trigger semantics."""

    metadata: Metadata
    universe: VersionedUniverseRefV2
    timeframes: Timeframes
    data_requirements: tuple[DataRequirementSpecV2, ...]
    warmup: WarmupSpecV2
    features: tuple[FeatureBindingSpec, ...]
    signal: SignalDefinition
    entry: EntrySpecV2
    exit: ExitSpecV2
    intrabar_policy: IntrabarPolicy
    execution_assumptions: ExecutionAssumptionsV2
    costs: CostsSpecV2
    portfolio_assumptions: PortfolioAssumptionsV2
    research_parameter_space: ResearchParameterSpaceV2
    analysis_requirements: AnalysisRequirements
    validation_requirements: ValidationRequirements
    provenance: ProvenanceSpecV2

    def __post_init__(self) -> None:
        if self.metadata.schema_version != STRATEGY_SPEC_V2_SCHEMA_VERSION:
            raise ValueError(
                f"metadata.schema_version must be {STRATEGY_SPEC_V2_SCHEMA_VERSION!r}, "
                f"got {self.metadata.schema_version!r}"
            )
