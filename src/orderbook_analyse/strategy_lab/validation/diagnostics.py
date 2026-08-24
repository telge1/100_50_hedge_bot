"""Diagnostic helpers and typed issue contexts for P4A validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    OperandTypeConstraintV2,
    ParameterValueType,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownIdentifierContext:
    _schema_kind: ClassVar[str] = "unknown_identifier"
    identifier: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedActualTypeContext:
    _schema_kind: ClassVar[str] = "expected_actual_type"
    expected: ParameterValueType
    actual: ParameterValueType


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedActualVersionContext:
    _schema_kind: ClassVar[str] = "expected_actual_version"
    expected: str
    actual: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterNameContext:
    _schema_kind: ClassVar[str] = "parameter_name"
    parameter_name: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundsContext:
    _schema_kind: ClassVar[str] = "bounds"
    parameter_name: StableIdentifier
    bound_value: Decimal | int
    minimum: Decimal | int | None
    maximum: Decimal | int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorSignatureContext:
    _schema_kind: ClassVar[str] = "operator_signature"
    operator_id: StableIdentifier
    left_constraint: OperandTypeConstraintV2 | None
    right_constraint: OperandTypeConstraintV2 | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureAliasContext:
    _schema_kind: ClassVar[str] = "feature_alias"
    feature_alias: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentCycleContext:
    _schema_kind: ClassVar[str] = "component_cycle"
    cycle_path: tuple[StableIdentifier, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class StatePriorityContext:
    _schema_kind: ClassVar[str] = "state_priority"
    state_id: StableIdentifier
    priority: int
    event_ids: tuple[StableIdentifier, ...]


IssueContext = (
    UnknownIdentifierContext
    | ExpectedActualTypeContext
    | ExpectedActualVersionContext
    | ParameterNameContext
    | BoundsContext
    | OperatorSignatureContext
    | FeatureAliasContext
    | ComponentCycleContext
    | StatePriorityContext
)
