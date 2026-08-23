"""StableIdentifier and ContractVersion tests for StrategySpec V2."""

from __future__ import annotations

import pytest

from orderbook_analyse.strategy_lab.models import ContractVersion, StableIdentifier


@pytest.mark.parametrize(
    "value",
    ("a", "ema_fast", "state_1", "x9"),
)
def test_stable_identifier_accepts_valid_values(value: str) -> None:
    assert StableIdentifier(value=value).value == value


@pytest.mark.parametrize(
    "value",
    ("", " ", " Ema", "Ema", "ema-fast", "ema.fast", "9ema", "ema fast"),
)
def test_stable_identifier_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        StableIdentifier(value=value)


def test_stable_identifier_no_normalization() -> None:
    with pytest.raises(ValueError):
        StableIdentifier(value="EMA")


@pytest.mark.parametrize(
    "value",
    ("catalog/v1", "feature/v2", "strategy_spec/v2"),
)
def test_contract_version_accepts_valid_values(value: str) -> None:
    assert ContractVersion(value=value).value == value


@pytest.mark.parametrize(
    "value",
    ("", " catalog/v1", "catalog/v1 ", "Catalog/V1", "catalog-v1", "v1"),
)
def test_contract_version_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        ContractVersion(value=value)
