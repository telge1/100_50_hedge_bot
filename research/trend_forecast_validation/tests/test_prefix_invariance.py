"""Prefix-invariance focused tests (alias module per audit checklist)."""

from __future__ import annotations

from research.trend_forecast_validation.tests.test_causal_replay import (
    test_prefix_invariance_on_synthetic,
)


def test_prefix_invariance() -> None:
    test_prefix_invariance_on_synthetic()
