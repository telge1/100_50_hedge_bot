"""P4C determinism tests."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.validation import validate_strategy_v2_p4c
from tests.strategy_lab.validation.conftest import catalogs, p4c_valid_edc_strategy


def test_repeated_p4c_validation_identical_report(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    first = validate_strategy_v2_p4c(spec, catalogs)
    second = validate_strategy_v2_p4c(spec, catalogs)
    assert first == second
