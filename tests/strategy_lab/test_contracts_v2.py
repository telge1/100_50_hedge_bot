"""Neutral V2 contract type tests."""

from __future__ import annotations

import pytest

from orderbook_analyse.strategy_lab.models import (
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
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    CollectionShape,
    FeatureOutputValueType,
    ParameterValueType,
    TemporalShape,
    param_value_to_parameter_type,
)
from orderbook_analyse.strategy_lab.models.enums import DurationUnit, RateUnit, TimeframeUnit
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import DurationValue, RateValue, TimeframeValue
from decimal import Decimal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (BoolParam(value=True), ParameterValueType.BOOLEAN),
        (IntParam(value=3), ParameterValueType.INTEGER),
        (DecimalParam(value=Decimal("1.5")), ParameterValueType.DECIMAL),
        (
            RateParam(value=RateValue(value=Decimal("1"), unit=RateUnit.PERCENT)),
            ParameterValueType.RATE,
        ),
        (
            DurationParam(value=DurationValue(value=Decimal("1"), unit=DurationUnit.HOURS)),
            ParameterValueType.DURATION,
        ),
        (
            TimeframeParam(value=TimeframeValue(value=5, unit=TimeframeUnit.MINUTES)),
            ParameterValueType.TIMEFRAME,
        ),
        (StringParam(value="x"), ParameterValueType.STRING),
        (IdentifierParam(value="mode_a"), ParameterValueType.IDENTIFIER),
    ],
)
def test_param_value_to_parameter_type_complete(
    value: ParamValue,
    expected: ParameterValueType,
) -> None:
    assert param_value_to_parameter_type(value) is expected


def test_bool_not_mapped_as_int() -> None:
    with pytest.raises(TypeError):
        param_value_to_parameter_type(True)  # type: ignore[arg-type]


def test_feature_output_types_distinct_from_parameter_types() -> None:
    assert FeatureOutputValueType is not ParameterValueType
    assert TemporalShape is not CollectionShape


def test_parameter_definition_allowed_identifiers_typed() -> None:
    from orderbook_analyse.strategy_lab.catalogs.v2.plugins import CLUSTER_SWEEP

    for plugin_param in CLUSTER_SWEEP.parameters:
        allowed = plugin_param.definition.allowed_identifiers
        assert isinstance(allowed, tuple)
        for item in allowed:
            assert isinstance(item, StableIdentifier)
