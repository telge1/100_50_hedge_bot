"""P4A scope boundary tests."""

from __future__ import annotations

import dataclasses
import inspect

from orderbook_analyse.strategy_lab.models import Directionality
from orderbook_analyse.strategy_lab.validation import validate_strategy_v2_p4a
from orderbook_analyse.strategy_lab.validation import models as validation_models
from orderbook_analyse.strategy_lab.validation import diagnostics as validation_diagnostics
from tests.strategy_lab.validation.conftest import catalogs, valid_edc_strategy


def test_p4a_does_not_emit_p4b_component_errors(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_edc_strategy(), catalogs)
    codes = {issue.code.value for issue in report.issues}
    assert "UNKNOWN_COMPONENT" not in codes
    assert "COMPONENT_CYCLE" not in codes
    assert "STATE_REFERENCE" not in codes


def test_p4a_does_not_emit_p4c_strategy_errors(catalogs) -> None:
    report = validate_strategy_v2_p4a(valid_edc_strategy(), catalogs)
    codes = {issue.code.value for issue in report.issues}
    for prefix in ("DATA_", "WARMUP_", "ENTRY_", "EXIT_", "TIMEFRAME_"):
        assert not any(code.startswith(prefix) for code in codes)


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


def test_strategy_not_mutated(catalogs) -> None:
    spec = valid_edc_strategy()
    before = dataclasses.asdict(spec)
    validate_strategy_v2_p4a(spec, catalogs)
    assert dataclasses.asdict(spec) == before


def test_validation_package_has_no_legacy_imports() -> None:
    import orderbook_analyse.strategy_lab.validation as validation_pkg

    source = inspect.getsource(validation_pkg)
    assert "strategy_lab.catalogs.plugins" not in source
    assert "strategy_lab.models.strategy_v1" not in source
