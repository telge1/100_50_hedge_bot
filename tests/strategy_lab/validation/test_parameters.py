"""Central parameter binding validation tests."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.models import (
    BoolParam,
    IntParam,
    RateParam,
    RateValue,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.validation import ValidationIssueCode, validate_strategy_v2_p4a
from orderbook_analyse.strategy_lab.validation.parameters import validate_parameter_bindings
from orderbook_analyse.strategy_lab.catalogs.v2.registry import get_feature_v2
from tests.strategy_lab.validation.conftest import catalogs, sid, valid_edc_strategy


def test_feature_duplicate_parameter_name(catalogs) -> None:
    ema = get_feature_v2("ema")
    bindings = (
        (sid("period"), IntParam(value=9)),
        (sid("period"), IntParam(value=20)),
    )
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=ema.parameters,
        bindings=bindings,
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_DUPLICATE_PARAMETER for issue in issues)


def test_feature_missing_required_parameter(catalogs) -> None:
    ema = get_feature_v2("ema")
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=ema.parameters,
        bindings=(),
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_MISSING_PARAMETER for issue in issues)


def test_feature_unknown_parameter(catalogs) -> None:
    ema = get_feature_v2("ema")
    bindings = ((sid("unknown"), IntParam(value=1)),)
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=ema.parameters,
        bindings=bindings,
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_UNKNOWN_PARAMETER for issue in issues)


def test_feature_bool_not_accepted_as_int(catalogs) -> None:
    ema = get_feature_v2("ema")
    bindings = ((sid("period"), BoolParam(value=True)),)
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=ema.parameters,
        bindings=bindings,
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_PARAMETER_TYPE for issue in issues)


def test_feature_period_bounds(catalogs) -> None:
    ema = get_feature_v2("ema")
    bindings = ((sid("period"), IntParam(value=0)),)
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=ema.parameters,
        bindings=bindings,
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_PARAMETER_BOUNDS for issue in issues)


def test_feature_rate_unit_mismatch(catalogs) -> None:
    cluster = get_feature_v2("lld_liquidity_clusters")
    bindings = (
        (
            sid("gap_pct"),
            RateParam(
                value=RateValue(value=Decimal("0.10"), unit=RateUnit.BASIS_POINTS)
            ),
        ),
        (sid("minimum_pools"), IntParam(value=3)),
    )
    issues = validate_parameter_bindings(
        path_prefix="features[0].bindings",
        definitions=cluster.parameters,
        bindings=bindings,
        code_prefix="FEATURE",
    )
    assert any(issue.code is ValidationIssueCode.FEATURE_RATE_UNIT for issue in issues)


def test_end_to_end_parameter_errors_collected(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_edc_strategy(), catalogs)
    assert report.is_valid
