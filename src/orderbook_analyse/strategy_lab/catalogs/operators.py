"""Closed operator catalog for Strategy Lab V1."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.catalogs.models import (
    Arity,
    CATALOG_CONTRACT_VERSION,
    NullPolicy,
    OperandTypeSpec,
    OperatorDescriptor,
    ValueType,
)

_CROSS_ABOVE_NOTE = (
    "crosses_above(a, b) is true only on closed observations when "
    "previous(a) <= previous(b) and current(a) > current(b)."
)
_CROSS_BELOW_NOTE = (
    "crosses_below(a, b) is true only on closed observations when "
    "previous(a) >= previous(b) and current(a) < current(b)."
)

EQ = OperatorDescriptor(
    operator_id="eq",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Equality comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

NE = OperatorDescriptor(
    operator_id="ne",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Inequality comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

GT = OperatorDescriptor(
    operator_id="gt",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Greater-than comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

GTE = OperatorDescriptor(
    operator_id="gte",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Greater-than-or-equal comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

LT = OperatorDescriptor(
    operator_id="lt",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Less-than comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

LTE = OperatorDescriptor(
    operator_id="lte",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Less-than-or-equal comparison on closed observations.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.DECIMAL),
        OperandTypeSpec(operand_index=1, value_type=ValueType.DECIMAL),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

AND = OperatorDescriptor(
    operator_id="and",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Logical conjunction of boolean operands.",
    arity=Arity.NARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.BOOLEAN),
        OperandTypeSpec(operand_index=1, value_type=ValueType.BOOLEAN),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

OR = OperatorDescriptor(
    operator_id="or",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Logical disjunction of boolean operands.",
    arity=Arity.NARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.BOOLEAN),
        OperandTypeSpec(operand_index=1, value_type=ValueType.BOOLEAN),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

NOT = OperatorDescriptor(
    operator_id="not",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Logical negation of a boolean operand.",
    arity=Arity.UNARY,
    operand_types=(OperandTypeSpec(operand_index=0, value_type=ValueType.BOOLEAN),),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=False,
    causal_semantics="Evaluated on the current closed observation only.",
)

CROSSES_ABOVE = OperatorDescriptor(
    operator_id="crosses_above",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Detects an upward cross between two series on closed bars.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.PRICE_SERIES),
        OperandTypeSpec(operand_index=1, value_type=ValueType.PRICE_SERIES),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=True,
    causal_semantics=(
        "Requires previous and current closed observations; no lookahead."
    ),
    contract_note=_CROSS_ABOVE_NOTE,
)

CROSSES_BELOW = OperatorDescriptor(
    operator_id="crosses_below",
    contract_version=CATALOG_CONTRACT_VERSION,
    description="Detects a downward cross between two series on closed bars.",
    arity=Arity.BINARY,
    operand_types=(
        OperandTypeSpec(operand_index=0, value_type=ValueType.PRICE_SERIES),
        OperandTypeSpec(operand_index=1, value_type=ValueType.PRICE_SERIES),
    ),
    result_type=ValueType.BOOLEAN,
    null_policy=NullPolicy.STRICT_REJECT,
    requires_previous_observation=True,
    causal_semantics=(
        "Requires previous and current closed observations; no lookahead."
    ),
    contract_note=_CROSS_BELOW_NOTE,
)

OPERATOR_DESCRIPTORS: tuple[OperatorDescriptor, ...] = (
    EQ,
    NE,
    GT,
    GTE,
    LT,
    LTE,
    AND,
    OR,
    NOT,
    CROSSES_ABOVE,
    CROSSES_BELOW,
)
