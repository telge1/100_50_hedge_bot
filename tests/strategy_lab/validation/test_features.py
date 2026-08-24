"""Feature binding validation tests."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.models import (
    ContractVersion,
    FeatureBindingSpec,
    FeatureParameterBinding,
    IntParam,
    RateParam,
    RateValue,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from orderbook_analyse.strategy_lab.validation.features import build_feature_resolution_index
from tests.strategy_lab.validation.conftest import (
    catalogs,
    cluster_features,
    edc_features,
    sid,
    valid_cluster_strategy,
    valid_edc_strategy,
)


def test_valid_edc_ema_bindings(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_edc_strategy(), catalogs)
    assert report.is_valid


def test_valid_cluster_bindings(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_cluster_strategy(), catalogs)
    assert report.is_valid


def test_duplicate_alias(catalogs) -> None:
    features = (edc_features()[0], edc_features()[0])
    spec = dataclasses.replace(valid_edc_strategy(), features=features)
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_DUPLICATE_ALIAS
        for issue in report.issues
    )


def test_unknown_feature(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("unknown"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=edc_features()[0].bindings,
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(issue.code is ValidationIssueCode.FEATURE_UNKNOWN_ID for issue in report.issues)


def test_wrong_contract_version(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v1"),
                bindings=edc_features()[0].bindings,
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_CONTRACT_VERSION
        for issue in report.issues
    )


def test_missing_period(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_MISSING_PARAMETER
        for issue in report.issues
    )


def test_unknown_parameter(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("period"),
                        value=IntParam(value=9),
                    ),
                    FeatureParameterBinding(
                        name=sid("extra"),
                        value=IntParam(value=1),
                    ),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_UNKNOWN_PARAMETER
        for issue in report.issues
    )


def test_bool_instead_of_int_rejected(catalogs) -> None:
    from orderbook_analyse.strategy_lab.models import BoolParam

    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("period"),
                        value=BoolParam(value=True),
                    ),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_PARAMETER_TYPE
        for issue in report.issues
    )


def test_period_zero_rejected(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("period"),
                        value=IntParam(value=0),
                    ),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(
        issue.code is ValidationIssueCode.FEATURE_PARAMETER_BOUNDS
        for issue in report.issues
    )


def test_wrong_rate_unit(catalogs) -> None:
    spec = dataclasses.replace(
        valid_cluster_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("clusters"),
                catalog_feature_id=sid("lld_liquidity_clusters"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("gap_pct"),
                        value=RateParam(
                            value=RateValue(
                                value=Decimal("0.10"),
                                unit=RateUnit.BASIS_POINTS,
                            )
                        ),
                    ),
                    FeatureParameterBinding(
                        name=sid("minimum_pools"),
                        value=IntParam(value=3),
                    ),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    assert any(issue.code is ValidationIssueCode.FEATURE_RATE_UNIT for issue in report.issues)


def test_unknown_output_id(catalogs) -> None:
    index = build_feature_resolution_index(edc_features(), catalogs)
    assert index.resolve_output(sid("ema_fast"), sid("missing")) is None
