"""Closed operator catalog for Strategy Lab catalog/v2."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CATALOG_CONTRACT_VERSION,
    OperatorDescriptorV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    EvaluationSemanticsV2,
    NullPolicyV2,
    ObservationContractV2,
    OperandOriginV2,
    OperandTypeConstraintV2,
    OperatorOperandSpecV2,
    OperatorSignatureV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier

_SID = StableIdentifier

_CLOSED_OBS = ObservationContractV2(
    requires_previous_observation=False,
    requires_current_observation=True,
    causal_semantics="Evaluated on the current closed observation only.",
)

_CROSS_OBS = ObservationContractV2(
    requires_previous_observation=True,
    requires_current_observation=True,
    causal_semantics=(
        "Requires previous and current closed observations; no lookahead."
    ),
)

_SERIES_VS_SERIES = OperatorSignatureV2(
    operands=(
        OperatorOperandSpecV2(
            operand_index=0,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
        OperatorOperandSpecV2(
            operand_index=1,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
    ),
    result_type=OperandTypeConstraintV2.BOOLEAN_LITERAL,
    null_policy=NullPolicyV2.STRICT_REJECT,
    evaluation_semantics=EvaluationSemanticsV2.CURRENT_CLOSED_OBSERVATION,
    observation=_CLOSED_OBS,
    description="Decimal series compared to decimal series on closed bar.",
)

_SERIES_VS_DECIMAL = OperatorSignatureV2(
    operands=(
        OperatorOperandSpecV2(
            operand_index=0,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
        OperatorOperandSpecV2(
            operand_index=1,
            origin=OperandOriginV2.LITERAL_PARAM,
            type_constraint=OperandTypeConstraintV2.DECIMAL_LITERAL,
        ),
    ),
    result_type=OperandTypeConstraintV2.BOOLEAN_LITERAL,
    null_policy=NullPolicyV2.STRICT_REJECT,
    evaluation_semantics=EvaluationSemanticsV2.CURRENT_CLOSED_OBSERVATION,
    observation=_CLOSED_OBS,
    description="Decimal series compared to decimal literal on closed bar.",
)

_SERIES_VS_INT = OperatorSignatureV2(
    operands=(
        OperatorOperandSpecV2(
            operand_index=0,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
        OperatorOperandSpecV2(
            operand_index=1,
            origin=OperandOriginV2.LITERAL_PARAM,
            type_constraint=OperandTypeConstraintV2.INTEGER_LITERAL,
        ),
    ),
    result_type=OperandTypeConstraintV2.BOOLEAN_LITERAL,
    null_policy=NullPolicyV2.STRICT_REJECT,
    evaluation_semantics=EvaluationSemanticsV2.CURRENT_CLOSED_OBSERVATION,
    observation=_CLOSED_OBS,
    description="Decimal series compared to integer literal on closed bar.",
)

_LITERAL_VS_LITERAL = OperatorSignatureV2(
    operands=(
        OperatorOperandSpecV2(
            operand_index=0,
            origin=OperandOriginV2.LITERAL_PARAM,
            type_constraint=OperandTypeConstraintV2.DECIMAL_LITERAL,
        ),
        OperatorOperandSpecV2(
            operand_index=1,
            origin=OperandOriginV2.LITERAL_PARAM,
            type_constraint=OperandTypeConstraintV2.DECIMAL_LITERAL,
        ),
    ),
    result_type=OperandTypeConstraintV2.BOOLEAN_LITERAL,
    null_policy=NullPolicyV2.STRICT_REJECT,
    evaluation_semantics=EvaluationSemanticsV2.CURRENT_CLOSED_OBSERVATION,
    observation=_CLOSED_OBS,
    description="Decimal literal compared to decimal literal.",
)

_CROSS_SIGNATURE = OperatorSignatureV2(
    operands=(
        OperatorOperandSpecV2(
            operand_index=0,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
        OperatorOperandSpecV2(
            operand_index=1,
            origin=OperandOriginV2.FEATURE_OUTPUT,
            type_constraint=OperandTypeConstraintV2.DECIMAL_SERIES,
        ),
    ),
    result_type=OperandTypeConstraintV2.BOOLEAN_LITERAL,
    null_policy=NullPolicyV2.STRICT_REJECT,
    evaluation_semantics=EvaluationSemanticsV2.CROSS_REQUIRES_PRIOR_AND_CURRENT,
    observation=_CROSS_OBS,
    description="Cross detected between two decimal series on closed bars.",
)

_COMPARE_SIGNATURES = (
    _SERIES_VS_SERIES,
    _SERIES_VS_DECIMAL,
    _SERIES_VS_INT,
    _LITERAL_VS_LITERAL,
)

GT = OperatorDescriptorV2(
    operator_id=_SID(value="gt"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Greater-than comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

GTE = OperatorDescriptorV2(
    operator_id=_SID(value="gte"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Greater-than-or-equal comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

LT = OperatorDescriptorV2(
    operator_id=_SID(value="lt"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Less-than comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

LTE = OperatorDescriptorV2(
    operator_id=_SID(value="lte"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Less-than-or-equal comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

EQ = OperatorDescriptorV2(
    operator_id=_SID(value="eq"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Equality comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

NE = OperatorDescriptorV2(
    operator_id=_SID(value="ne"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Inequality comparison on closed observations.",
    signatures=_COMPARE_SIGNATURES,
)

CROSSES_ABOVE = OperatorDescriptorV2(
    operator_id=_SID(value="crosses_above"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Detects an upward cross between two series on closed bars.",
    signatures=(_CROSS_SIGNATURE,),
)

CROSSES_BELOW = OperatorDescriptorV2(
    operator_id=_SID(value="crosses_below"),
    contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
    description="Detects a downward cross between two series on closed bars.",
    signatures=(_CROSS_SIGNATURE,),
)

OPERATOR_DESCRIPTORS_V2: tuple[OperatorDescriptorV2, ...] = (
    EQ,
    NE,
    GT,
    GTE,
    LT,
    LTE,
    CROSSES_ABOVE,
    CROSSES_BELOW,
)
