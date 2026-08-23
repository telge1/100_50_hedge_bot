"""Central ParamValue to ParameterValueType mapping."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import ParameterValueType
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    DurationParam,
    IdentifierParam,
    IntParam,
    ParamValue,
    RateParam,
    StringParam,
    TimeframeParam,
)


def param_value_to_parameter_type(value: ParamValue) -> ParameterValueType:
    """Map a typed ParamValue instance to its closed ParameterValueType."""
    if isinstance(value, BoolParam):
        return ParameterValueType.BOOLEAN
    if isinstance(value, IntParam):
        return ParameterValueType.INTEGER
    if isinstance(value, DecimalParam):
        return ParameterValueType.DECIMAL
    if isinstance(value, RateParam):
        return ParameterValueType.RATE
    if isinstance(value, DurationParam):
        return ParameterValueType.DURATION
    if isinstance(value, TimeframeParam):
        return ParameterValueType.TIMEFRAME
    if isinstance(value, StringParam):
        return ParameterValueType.STRING
    if isinstance(value, IdentifierParam):
        return ParameterValueType.IDENTIFIER
    raise TypeError(f"unsupported ParamValue type: {type(value).__name__}")
