"""P1 smoke tests: package exports and StrategySpec construction guards."""

from __future__ import annotations

from dataclasses import replace

import pytest

from orderbook_analyse.strategy_lab import STRATEGY_SPEC_SCHEMA_VERSION, StrategySpec
from orderbook_analyse.strategy_lab.models import ModelingStatus
from tests.strategy_lab.conftest import minimal_strategy_spec


def test_package_exports_schema_version_and_root() -> None:
    assert STRATEGY_SPEC_SCHEMA_VERSION == "strategy_spec/v1"
    assert StrategySpec is not None


def test_construct_minimal_strategy_spec_smoke() -> None:
    spec = minimal_strategy_spec()
    assert spec.metadata.strategy_id == "test.minimal"
    assert spec.fees.roundtrip_cost.unit.name == "BASIS_POINTS"
    assert spec.funding.status is ModelingStatus.UNAVAILABLE
    assert spec.slippage.status is ModelingStatus.NOT_MODELED
    assert spec.signal.rules_embedded_in_yaml is False


def test_rejects_wrong_schema_version() -> None:
    base = minimal_strategy_spec()
    bad_meta = replace(base.metadata, schema_version="strategy_spec/v0")
    with pytest.raises(ValueError, match="schema_version"):
        replace(base, metadata=bad_meta)


def test_rejects_embedded_yaml_signal_rules() -> None:
    base = minimal_strategy_spec()
    bad_signal = replace(base.signal, rules_embedded_in_yaml=True)
    with pytest.raises(ValueError, match="rules_embedded_in_yaml"):
        replace(base, signal=bad_signal)


def test_modeling_status_enum_is_closed() -> None:
    assert {s.value for s in ModelingStatus} == {
        "modeled",
        "not_modeled",
        "not_applicable",
        "unavailable",
    }
