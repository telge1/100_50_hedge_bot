"""P4C scope boundary tests."""

from __future__ import annotations

import dataclasses
import inspect

from orderbook_analyse.strategy_lab.validation import (
    validate_strategy_v2_p4a,
    validate_strategy_v2_p4b,
    validate_strategy_v2_p4c,
)
from orderbook_analyse.strategy_lab.validation import diagnostics as validation_diagnostics
from orderbook_analyse.strategy_lab.validation import models as validation_models
from tests.strategy_lab.validation.conftest import (
    catalogs,
    p4c_valid_edc_strategy,
    p4c_valid_rule_based_long_strategy,
)


def test_p4c_valid_edc_passes(catalogs) -> None:
    report = validate_strategy_v2_p4c(p4c_valid_edc_strategy(), catalogs)
    assert report.is_valid, report.issues


def test_p4c_valid_rule_based_passes(catalogs) -> None:
    report = validate_strategy_v2_p4c(p4c_valid_rule_based_long_strategy(), catalogs)
    assert report.is_valid, report.issues


def test_p4c_includes_p4b_errors(catalogs) -> None:
    from orderbook_analyse.strategy_lab.models import FeatureBindingSpec, ContractVersion
    from tests.strategy_lab.validation.conftest import sid, edc_features

    broken = dataclasses.replace(
        p4c_valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("missing"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=edc_features()[0].bindings,
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert any(issue.code.value.startswith("FEATURE_") for issue in report.issues)


def test_p4a_and_p4b_still_available(catalogs) -> None:
    assert validate_strategy_v2_p4a(p4c_valid_edc_strategy(), catalogs).is_valid
    assert validate_strategy_v2_p4b(p4c_valid_edc_strategy(), catalogs).is_valid


def test_strategy_not_mutated(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    before = dataclasses.asdict(spec)
    validate_strategy_v2_p4c(spec, catalogs)
    assert dataclasses.asdict(spec) == before


def test_validation_models_have_no_dict_or_any_fields() -> None:
    for module in (validation_models, validation_diagnostics):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            if not hasattr(cls, "__annotations__"):
                continue
            for annotation in cls.__annotations__.values():
                assert "dict" not in str(annotation).lower()
                assert "Any" not in str(annotation)


def test_data_optional_role_does_not_cover_signal_required(catalogs) -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
        DataRequirementRoleV2,
    )

    spec = p4c_valid_edc_strategy()
    weakened = []
    for req in spec.data_requirements:
        if req.role is DataRequirementRoleV2.SIGNAL_REQUIRED:
            weakened.append(
                dataclasses.replace(
                    req,
                    role=DataRequirementRoleV2.ANALYSIS_OPTIONAL,
                    required=False,
                )
            )
        else:
            weakened.append(req)
    broken = dataclasses.replace(spec, data_requirements=tuple(weakened))
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert any(issue.code.value == "DATA_REQUIREMENT_MISSING" for issue in report.issues)


def test_warmup_bars_below_required_from_feature_only_rule_based(catalogs) -> None:
    spec = p4c_valid_rule_based_long_strategy()
    broken = dataclasses.replace(
        spec,
        warmup=dataclasses.replace(
            spec.warmup,
            signal_engine=dataclasses.replace(
                spec.warmup.signal_engine,
                minimum_bars=5,
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert any(issue.code.value == "WARMUP_BARS_BELOW_REQUIRED" for issue in report.issues)


def test_warmup_bars_uses_max_of_feature_and_plugin(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    # plugin requires 79; features max 20 — lowering below 79 must fail
    broken = dataclasses.replace(
        spec,
        warmup=dataclasses.replace(
            spec.warmup,
            signal_engine=dataclasses.replace(
                spec.warmup.signal_engine,
                minimum_bars=50,
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert any(issue.code.value == "WARMUP_BARS_BELOW_REQUIRED" for issue in report.issues)


def test_cost_roundtrip_zero_allowed(catalogs) -> None:
    from decimal import Decimal
    from orderbook_analyse.strategy_lab.models.enums import RateUnit
    from orderbook_analyse.strategy_lab.models.strategy import RateValue

    spec = p4c_valid_edc_strategy()
    ok = dataclasses.replace(
        spec,
        costs=dataclasses.replace(
            spec.costs,
            roundtrip_cost=RateValue(value=Decimal("0"), unit=RateUnit.PERCENT),
        ),
    )
    report = validate_strategy_v2_p4c(ok, catalogs)
    assert not any(issue.code.value == "COST_ROUNDTRIP_NEGATIVE" for issue in report.issues)
    assert report.is_valid
