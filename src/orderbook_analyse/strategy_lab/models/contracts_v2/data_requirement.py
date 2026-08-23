"""Data requirement and entry contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AvailabilityTimingV2,
    DataRequirementRoleV2,
    DataSourceKindV2,
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.granularity import DataGranularityV2
from orderbook_analyse.strategy_lab.models.contracts_v2.provenance import (
    LegacyProvenanceRefV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import PluginRef, TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class DataRequirementSpecV2:
    """Typed raw data requirement with role, granularity, and availability."""

    requirement_id: StableIdentifier
    source_kind: DataSourceKindV2
    role: DataRequirementRoleV2
    required: bool
    granularity: DataGranularityV2
    availability: AvailabilityTimingV2
    rationale: str
    required_for_policy: ResearchConfirmationPolicyV2 | None
    provenance: tuple[LegacyProvenanceRefV2, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class EntrySpecV2:
    """Entry timing contract with closed enums (no causal delay field)."""

    signal_decision_timing: AvailabilityTimingV2
    entry_reference_rule: EntryReferenceRuleV2
    entry_timing_anchor: EntryTimingAnchorV2
    entry_price_reference: EntryPriceReferenceV2
    execution_timeframe: TimeframeValue
    entry_plugin: PluginRef
    description: str | None
