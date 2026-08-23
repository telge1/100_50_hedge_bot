"""Rule tree tests for StrategySpec V2."""

from __future__ import annotations

import pytest

from orderbook_analyse.strategy_lab.models import (
    BooleanAndExpression,
    BooleanNotExpression,
    BooleanOrExpression,
    BoolParam,
    ComparisonExpression,
    ComponentReference,
    FeatureOutputReference,
    LiteralOperand,
    RuleComponentSpec,
    StableIdentifier,
)
from tests.strategy_lab.v2_fixtures import _comparison, sid


def _literal_bool(value: bool = True) -> LiteralOperand:
    return LiteralOperand(value=BoolParam(value=value))


def test_comparison_expression() -> None:
    expr = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("ema_fast"),
            output_id=sid("value"),
        ),
        right=_literal_bool(),
    )
    assert expr.operator_id.value == "gt"
    assert expr.left.output_id.value == "value"


def test_and_requires_at_least_two_operands() -> None:
    with pytest.raises(ValueError, match="at least two"):
        BooleanAndExpression(operands=(_comparison(),))
    with pytest.raises(TypeError, match="tuple"):
        BooleanAndExpression(operands=[_comparison(), _comparison()])  # type: ignore[arg-type]


def test_or_requires_at_least_two_operands() -> None:
    with pytest.raises(ValueError, match="at least two"):
        BooleanOrExpression(operands=(_comparison(),))


def test_not_exactly_one_operand() -> None:
    expr = BooleanNotExpression(operand=_comparison())
    assert expr.operand is not None


def test_nested_recursion_four_levels() -> None:
    leaf = _comparison()
    level3 = BooleanNotExpression(operand=leaf)
    level2 = BooleanAndExpression(operands=(level3, leaf))
    level1 = BooleanOrExpression(operands=(level2, leaf))
    root = BooleanNotExpression(operand=level1)
    assert isinstance(root.operand, BooleanOrExpression)


def test_component_reference_structurally_allowed() -> None:
    ref = ComponentReference(component_id=sid("setup_filter"))
    comp = RuleComponentSpec(
        component_id=sid("setup_filter"),
        description="local component",
        root=_comparison(),
    )
    assert ref.component_id == comp.component_id


def test_unknown_component_reference_structurally_allowed() -> None:
    ref = ComponentReference(component_id=sid("missing"))
    assert ref.component_id.value == "missing"


def test_component_cycle_structurally_possible() -> None:
    a = sid("a")
    b = sid("b")
    comp_a = RuleComponentSpec(
        component_id=a,
        description="refs b",
        root=ComponentReference(component_id=b),
    )
    comp_b = RuleComponentSpec(
        component_id=b,
        description="refs a",
        root=ComponentReference(component_id=a),
    )
    assert comp_a.root.component_id == b
    assert comp_b.root.component_id == a
