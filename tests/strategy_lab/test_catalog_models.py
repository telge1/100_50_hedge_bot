"""Catalog model contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.catalogs.models import (
    DecimalBounds,
    IntBounds,
    InvalidCatalogDefinitionError,
    ParameterDefinition,
    ValueType,
)


def test_parameter_definition_is_frozen_slots_kwonly() -> None:
    param = ParameterDefinition(
        name="ema_fast",
        value_type=ValueType.INTEGER,
        required=True,
        description="fast period",
        int_bounds=IntBounds(min_value=1),
    )
    params = getattr(ParameterDefinition, "__dataclass_params__")
    assert params.frozen is True
    assert getattr(ParameterDefinition, "__slots__", None) is not None
    for field in fields(ParameterDefinition):
        assert field.kw_only is True
    with pytest.raises(FrozenInstanceError):
        param.name = "x"  # type: ignore[misc]


def test_int_bounds_reject_bool_as_int() -> None:
    with pytest.raises(TypeError):
        IntBounds(min_value=True)  # type: ignore[arg-type]


def test_decimal_bounds_reject_float() -> None:
    with pytest.raises(TypeError):
        DecimalBounds(min_value=0.1)  # type: ignore[arg-type]


def test_decimal_bounds_reject_inverted_range() -> None:
    with pytest.raises(InvalidCatalogDefinitionError):
        DecimalBounds(min_value=Decimal("2"), max_value=Decimal("1"))
