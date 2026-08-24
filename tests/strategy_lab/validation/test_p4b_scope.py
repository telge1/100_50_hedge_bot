"""P4B scope boundary tests."""

from __future__ import annotations

import dataclasses
import inspect

from orderbook_analyse.strategy_lab.models import (
    FeatureBindingSpec,
    FeatureParameterBinding,
    IntParam,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion
from orderbook_analyse.strategy_lab.validation import (
    validate_strategy_v2_p4a,
    validate_strategy_v2_p4b,
)
from orderbook_analyse.strategy_lab.validation import diagnostics as validation_diagnostics
from orderbook_analyse.strategy_lab.validation import models as validation_models
from tests.strategy_lab.validation.conftest import (
    catalogs,
    sid,
    valid_edc_strategy,
    valid_rule_based_long_strategy,
    valid_state_machine_long_strategy,
)


def test_p4b_does_not_emit_p4c_strategy_errors(catalogs) -> None:
    report = validate_strategy_v2_p4b(valid_state_machine_long_strategy(), catalogs)
    codes = {issue.code.value for issue in report.issues}
    for prefix in ("DATA_", "WARMUP_", "ENTRY_", "EXIT_", "TIMEFRAME_"):
        assert not any(code.startswith(prefix) for code in codes)


def test_p4b_report_includes_p4a_errors(catalogs) -> None:
    broken = dataclasses.replace(
        valid_edc_strategy(),
        features=(
            FeatureBindingSpec(
                alias=sid("ema_fast"),
                catalog_feature_id=sid("missing"),
                catalog_contract_version=ContractVersion(value="catalog/v2"),
                bindings=(
                    FeatureParameterBinding(
                        name=sid("period"),
                        value=IntParam(value=9),
                    ),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4b(broken, catalogs)
    assert any(issue.code.value.startswith("FEATURE_") for issue in report.issues)


def test_p4a_still_available_without_p4b_graph_checks(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_rule_based_long_strategy(), catalogs)
    assert report.is_valid


def test_strategy_not_mutated(catalogs) -> None:
    spec = valid_state_machine_long_strategy()
    before = dataclasses.asdict(spec)
    validate_strategy_v2_p4b(spec, catalogs)
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


def test_validation_package_has_no_legacy_imports() -> None:
    import orderbook_analyse.strategy_lab.validation as validation_pkg

    source = inspect.getsource(validation_pkg)
    assert "strategy_lab.catalogs.plugins" not in source
    assert "strategy_lab.models.strategy_v1" not in source
