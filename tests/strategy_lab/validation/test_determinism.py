"""Determinism and multi-error collection tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import ContractVersion, FeatureBindingSpec
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from tests.strategy_lab.validation.conftest import catalogs, sid, valid_edc_strategy


def test_collects_multiple_independent_errors(catalogs) -> None:
    spec = valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("missing_feature"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=spec.features[0].bindings,
            ),
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v1"),
                bindings=spec.features[0].bindings,
            ),
            FeatureBindingSpec(
                alias=sid("ema_slow"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(),
            ),
            FeatureBindingSpec(
                alias=sid("atr"),
                catalog_feature_id=sid("atr_wilder"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=spec.features[2].bindings,
            ),
            FeatureBindingSpec(
                alias=sid("extra"),
                catalog_feature_id=sid("ema"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    spec.features[0].bindings[0],
                    spec.features[0].bindings[0],
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4a(broken, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.FEATURE_UNKNOWN_ID in codes
    assert ValidationIssueCode.FEATURE_DUPLICATE_ALIAS in codes
    assert ValidationIssueCode.FEATURE_CONTRACT_VERSION in codes
    assert ValidationIssueCode.FEATURE_MISSING_PARAMETER in codes
    assert ValidationIssueCode.FEATURE_DUPLICATE_PARAMETER in codes
    assert len(report.issues) >= 5


def test_repeated_runs_produce_identical_reports(catalogs) -> None:
    spec = valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        features=spec.features[:1],
    )
    first = validate_strategy_v2_p4a(broken, catalogs)
    second = validate_strategy_v2_p4a(broken, catalogs)
    assert first.issues == second.issues


def test_strategy_spec_not_mutated(catalogs) -> None:
    spec = valid_edc_strategy()
    before = dataclasses.asdict(spec)
    validate_strategy_v2_p4a(spec, catalogs)
    after = dataclasses.asdict(spec)
    assert before == after
