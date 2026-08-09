"""Smoke tests for relative context classification."""

from __future__ import annotations

from orderbook_analyse.fractal_parent_signal_lower_tf_context.context import (
    phase_from_dir_zone,
    relative_context,
)


def test_phase_mapping() -> None:
    assert phase_from_dir_zone("UP", "HIGH") == "HIGH_UP"
    assert phase_from_dir_zone("DOWN", "LOW") == "LOW_DOWN"


def test_relative_priority_short() -> None:
    # LOW_UP is LATE by zone priority, not COUNTER
    assert relative_context("SHORT", "UP", "LOW", "LOW_UP") == "LATE"
    assert relative_context("SHORT", "DOWN", "HIGH", "HIGH_DOWN") == "FAVORABLE_EARLY"
    assert relative_context("SHORT", "DOWN", "MID", "MID_DOWN") == "FAVORABLE_MID"
    assert relative_context("SHORT", "UP", "MID", "MID_UP") == "COUNTER"


def test_relative_priority_long() -> None:
    assert relative_context("LONG", "DOWN", "HIGH", "HIGH_DOWN") == "LATE"
    assert relative_context("LONG", "UP", "LOW", "LOW_UP") == "FAVORABLE_EARLY"
