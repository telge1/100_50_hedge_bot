"""Operator signature contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    EvaluationSemanticsV2,
    NullPolicyV2,
    OperandOriginV2,
    OperandTypeConstraintV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationContractV2:
    """Observation requirements for cross operators."""

    requires_previous_observation: bool
    requires_current_observation: bool
    causal_semantics: str

    def __post_init__(self) -> None:
        if type(self.requires_previous_observation) is not bool:
            raise TypeError("requires_previous_observation must be exact bool")
        if type(self.requires_current_observation) is not bool:
            raise TypeError("requires_current_observation must be exact bool")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorOperandSpecV2:
    """One operand slot in an operator signature overload."""

    operand_index: int
    origin: OperandOriginV2
    type_constraint: OperandTypeConstraintV2

    def __post_init__(self) -> None:
        if type(self.operand_index) is not int:
            raise TypeError("operand_index must be exact int")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorSignatureV2:
    """One explicit operator overload (no implicit numeric coercion)."""

    operands: tuple[OperatorOperandSpecV2, ...]
    result_type: OperandTypeConstraintV2
    null_policy: NullPolicyV2
    evaluation_semantics: EvaluationSemanticsV2
    observation: ObservationContractV2 | None
    description: str
