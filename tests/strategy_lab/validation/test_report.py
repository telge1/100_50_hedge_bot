"""ValidationReport and require_valid tests."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.models import ContractVersion, FeatureBindingSpec
from orderbook_analyse.strategy_lab.validation import (
    ValidationFailedError,
    ValidationIssue,
    ValidationIssueCode,
    ValidationReport,
    ValidationSeverity,
    require_valid_strategy_v2_p4a,
    validate_strategy_v2_p4a,
)
from tests.strategy_lab.validation.conftest import catalogs, valid_edc_strategy


def test_empty_report_is_valid() -> None:
    report = ValidationReport(issues=())
    assert report.is_valid
    assert report.errors == ()
    assert report.warnings == ()


def test_error_makes_invalid() -> None:
    issue = ValidationIssue(
        code=ValidationIssueCode.FEATURE_UNKNOWN_ID,
        severity=ValidationSeverity.ERROR,
        path="features[0].catalog_feature_id",
        message="unknown feature",
        context=None,
    )
    report = ValidationReport(issues=(issue,))
    assert not report.is_valid
    assert len(report.errors) == 1


def test_warning_alone_stays_valid() -> None:
    issue = ValidationIssue(
        code=ValidationIssueCode.FEATURE_UNKNOWN_ID,
        severity=ValidationSeverity.WARNING,
        path="features[0]",
        message="warning only",
        context=None,
    )
    report = ValidationReport(issues=(issue,))
    assert report.is_valid
    assert report.warnings == (issue,)


def test_deterministic_sorting() -> None:
    a = ValidationIssue(
        code=ValidationIssueCode.FEATURE_UNKNOWN_ID,
        severity=ValidationSeverity.ERROR,
        path="features[1]",
        message="b",
        context=None,
    )
    b = ValidationIssue(
        code=ValidationIssueCode.FEATURE_DUPLICATE_ALIAS,
        severity=ValidationSeverity.ERROR,
        path="features[0]",
        message="a",
        context=None,
    )
    report = ValidationReport(issues=(a, b))
    assert report.issues[0].path == "features[0]"
    assert report.issues[1].path == "features[1]"


def test_require_valid_raises_full_report(catalogs) -> None:
    spec = valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        features=(
            FeatureBindingSpec(
                alias=spec.features[0].alias,
                catalog_feature_id=spec.features[0].catalog_feature_id,
                catalog_contract_version=ContractVersion(value="catalog/v1"),
                bindings=spec.features[0].bindings,
            ),
        ),
    )
    with pytest.raises(ValidationFailedError) as exc_info:
        require_valid_strategy_v2_p4a(broken, catalogs)
    assert exc_info.value.report is not None
    assert not exc_info.value.report.is_valid


def test_valid_edc_strategy_passes(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_edc_strategy(), catalogs)
    assert report.is_valid
