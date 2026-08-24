"""Validation invariant and catalog-integrity guard tests."""

from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from orderbook_analyse.strategy_lab.catalogs.v2.registry import (
    CatalogRegistryV2,
    _validate_operator_signatures,
)
from orderbook_analyse.strategy_lab.models import (
    ComparisonExpression,
    ContractVersion,
    FeatureOutputReference,
)
from orderbook_analyse.strategy_lab.models.enums import Directionality, EvaluationTiming
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.invariants import ValidationInvariantError
from orderbook_analyse.strategy_lab.validation.p4a import validate_strategy_v2_p4a
from tests.strategy_lab.validation.conftest import catalogs, edc_features, sid, valid_edc_strategy


def test_duplicate_operator_signatures_fail_catalog_integrity(catalogs) -> None:
    gt = catalogs.operators.get("gt")
    duplicate = gt.signatures[0]
    bad = replace(gt, signatures=gt.signatures + (duplicate,))
    issues = _validate_operator_signatures(bad)
    assert any(issue.code == "AMBIGUOUS_OPERATOR_SIGNATURE" for issue in issues)


def test_ambiguous_operator_raises_validation_invariant(catalogs) -> None:
    gt = catalogs.operators.get("gt")
    duplicate = gt.signatures[0]
    bad = replace(gt, signatures=gt.signatures + (duplicate,))
    operator_registry = CatalogRegistryV2(
        name="operator",
        entries=tuple(
            bad if entry.operator_id.value == "gt" else entry
            for entry in catalogs.operators
        ),
        id_getter=lambda descriptor: descriptor.operator_id.value,
    )
    bundle = CatalogBundleV2(
        features=catalogs.features,
        operators=operator_registry,
        plugins=catalogs.plugins,
    )
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("ema_fast"),
            output_id=sid("value"),
        ),
        right=FeatureOutputReference(
            feature_alias=sid("ema_slow"),
            output_id=sid("value"),
        ),
    )
    from orderbook_analyse.strategy_lab.models import (
        RuleBasedSignalSpec,
        SideName,
        SideRuleBundle,
    )

    signal = RuleBasedSignalSpec(
        operator_contract_version=ContractVersion(value="catalog/v2"),
        directionality=Directionality.LONG,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        ),
        short=None,
    )
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=signal,
        features=edc_features(),
    )
    with pytest.raises(ValidationInvariantError, match="ambiguous operator signature"):
        validate_strategy_v2_p4a(spec, bundle)
