"""Repo-wide pytest hooks (test tiers only — no production logic)."""

from __future__ import annotations

import pytest

from backtester.short.dashboard.research_charts.tests.oracle_test_tiers import (
    ORACLE_MEDIUM_KEYS,
    ORACLE_PARAMETRIZED_TEST_NAMES,
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        base_name = item.originalname or item.name.split("[", 1)[0]
        if base_name not in ORACLE_PARAMETRIZED_TEST_NAMES:
            continue
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        symbol = callspec.params.get("symbol")
        decision_iso = callspec.params.get("decision_iso")
        if not symbol or not decision_iso:
            continue
        key = (str(symbol), str(decision_iso))
        if key in ORACLE_MEDIUM_KEYS:
            item.add_marker(pytest.mark.medium)
        else:
            item.add_marker(pytest.mark.slow)
