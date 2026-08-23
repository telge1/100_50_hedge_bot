"""Feature output and parameter definition contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AvailabilityTimingV2,
    CollectionShape,
    FeatureOutputValueType,
    MissingValuePolicyV2,
    ParameterValueType,
    TemporalShape,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.warmup import FeatureWarmupFormulaV2
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class IntBoundsV2:
    min_value: int | None
    max_value: int | None

    def __post_init__(self) -> None:
        if self.min_value is not None and type(self.min_value) is not int:
            raise TypeError("IntBoundsV2.min_value must be exact int")
        if self.max_value is not None and type(self.max_value) is not int:
            raise TypeError("IntBoundsV2.max_value must be exact int")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("IntBoundsV2 min_value > max_value")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecimalBoundsV2:
    min_value: Decimal | None
    max_value: Decimal | None

    def __post_init__(self) -> None:
        if self.min_value is not None and type(self.min_value) is not Decimal:
            raise TypeError("DecimalBoundsV2.min_value must be exact Decimal")
        if self.max_value is not None and type(self.max_value) is not Decimal:
            raise TypeError("DecimalBoundsV2.max_value must be exact Decimal")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("DecimalBoundsV2 min_value > max_value")


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterDefinitionV2:
    """Typed parameter contract for features and plugins."""

    name: StableIdentifier
    value_type: ParameterValueType
    required: bool
    description: str
    allowed_identifiers: tuple[StableIdentifier, ...]
    int_bounds: IntBoundsV2 | None
    decimal_bounds: DecimalBoundsV2 | None
    required_rate_unit: RateUnit | None
    legacy_reference_value: str | None
    must_be_explicit: bool
    research_space_varies: bool
    baseline_defining: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureOutputDescriptorV2:
    """Explicit feature output contract."""

    output_id: StableIdentifier
    value_type: FeatureOutputValueType
    temporal_shape: TemporalShape
    collection_shape: CollectionShape
    nullable: bool
    availability: AvailabilityTimingV2
    missing_value_policy: MissingValuePolicyV2
    warmup: FeatureWarmupFormulaV2
    description: str
