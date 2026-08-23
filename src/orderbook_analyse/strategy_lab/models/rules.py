"""Rule tree models for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    ParamValue,
    _PARAM_VALUE_TYPES,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureOutputReference:
    _schema_kind: ClassVar[str] = "feature_output"
    feature_alias: StableIdentifier
    output_id: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class LiteralOperand:
    _schema_kind: ClassVar[str] = "literal"
    value: ParamValue

    def __post_init__(self) -> None:
        if not isinstance(self.value, _PARAM_VALUE_TYPES):
            raise TypeError("LiteralOperand.value must be a typed ParamValue")


Operand = FeatureOutputReference | LiteralOperand


@dataclass(frozen=True, slots=True, kw_only=True)
class ComparisonExpression:
    _schema_kind: ClassVar[str] = "comparison"
    operator_id: StableIdentifier
    left: Operand
    right: Operand


@dataclass(frozen=True, slots=True, kw_only=True)
class BooleanAndExpression:
    _schema_kind: ClassVar[str] = "boolean_and"
    operands: tuple[BooleanExpression, ...]

    def __post_init__(self) -> None:
        if type(self.operands) is not tuple:
            raise TypeError("BooleanAndExpression.operands must be a tuple")
        if len(self.operands) < 2:
            raise ValueError("BooleanAndExpression requires at least two operands")


@dataclass(frozen=True, slots=True, kw_only=True)
class BooleanOrExpression:
    _schema_kind: ClassVar[str] = "boolean_or"
    operands: tuple[BooleanExpression, ...]

    def __post_init__(self) -> None:
        if type(self.operands) is not tuple:
            raise TypeError("BooleanOrExpression.operands must be a tuple")
        if len(self.operands) < 2:
            raise ValueError("BooleanOrExpression requires at least two operands")


@dataclass(frozen=True, slots=True, kw_only=True)
class BooleanNotExpression:
    _schema_kind: ClassVar[str] = "boolean_not"
    operand: BooleanExpression


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentReference:
    _schema_kind: ClassVar[str] = "component_ref"
    component_id: StableIdentifier


BooleanExpression = (
    ComparisonExpression
    | BooleanAndExpression
    | BooleanOrExpression
    | BooleanNotExpression
    | ComponentReference
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleComponentSpec:
    component_id: StableIdentifier
    description: str
    root: BooleanExpression
