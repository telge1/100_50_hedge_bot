"""Smoke tests for a-priori quality classification."""

from __future__ import annotations

from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import assign_quality_class


def test_short_a_plus() -> None:
    q = assign_quality_class(
        "SHORT",
        zones=["HIGH", "HIGH", "MID"],
        phases=["HIGH_UP", "HIGH_DOWN", "MID_DOWN"],
    )
    assert q["quality_class"] == "A_PLUS_TIMING"
    assert q["exhausted_count"] == 0
    assert q["favorable_count"] >= 2


def test_short_a_minus() -> None:
    q = assign_quality_class(
        "SHORT",
        zones=["LOW", "LOW", "MID"],
        phases=["LOW_DOWN", "LOW_UP", "MID_UP"],
    )
    assert q["quality_class"] == "A_MINUS_TIMING"
    assert q["exhausted_count"] >= 2


def test_long_mirror() -> None:
    q = assign_quality_class(
        "LONG",
        zones=["LOW", "LOW", "MID"],
        phases=["LOW_UP", "LOW_DOWN", "MID_UP"],
    )
    assert q["quality_class"] == "A_PLUS_TIMING"
    q2 = assign_quality_class(
        "LONG",
        zones=["HIGH", "HIGH", "MID"],
        phases=["HIGH_UP", "HIGH_DOWN", "MID_DOWN"],
    )
    assert q2["quality_class"] == "A_MINUS_TIMING"
