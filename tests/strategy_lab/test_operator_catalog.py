"""Operator catalog tests."""

from __future__ import annotations

import pytest

from orderbook_analyse.strategy_lab.catalogs import OPERATOR_CATALOG, get_operator
from orderbook_analyse.strategy_lab.catalogs.models import UnknownCatalogEntryError


def test_operator_ids_unique_and_expected() -> None:
    assert OPERATOR_CATALOG.ids == (
        "and",
        "crosses_above",
        "crosses_below",
        "eq",
        "gt",
        "gte",
        "lt",
        "lte",
        "ne",
        "not",
        "or",
    )


def test_cross_operators_require_previous_observation() -> None:
    above = get_operator("crosses_above")
    below = get_operator("crosses_below")
    assert above.requires_previous_observation is True
    assert below.requires_previous_observation is True
    assert "previous(a)" in (above.contract_note or "")
    assert "previous(a)" in (below.contract_note or "")


def test_operator_lookup_unknown_raises() -> None:
    with pytest.raises(UnknownCatalogEntryError):
        OPERATOR_CATALOG.get("near")
