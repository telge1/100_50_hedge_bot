"""Distance, priority, confluence tests."""

from __future__ import annotations

from research.regime_scanner.orderflow_absorption_level.config import distance_bucket
from research.regime_scanner.orderflow_absorption_level.level_assign import (
    distance_atr,
    pick_level_for_anchor,
)


def test_distance_formula():
    d = distance_atr(close_t=100.0, level_price=99.0, atr_ref=2.0)
    assert abs(d - 0.5) < 1e-12


def test_atr_nan_yields_nan_distance():
    d = distance_atr(close_t=100.0, level_price=99.0, atr_ref=float("nan"))
    assert d != d


def test_distance_buckets():
    assert distance_bucket(0.05) == "touch"
    assert distance_bucket(0.15) == "very_near"
    assert distance_bucket(0.40) == "near"
    assert distance_bucket(0.60) == "far"
    assert distance_bucket(None) == "no_level"


def test_support_resistance_side_filter():
    candidates = [
        {
            "level_id": "s1",
            "level_type": "external_swing",
            "side": "support",
            "level_price": 101.0,  # above close → invalid support
        },
        {
            "level_id": "s2",
            "level_type": "external_swing",
            "side": "support",
            "level_price": 99.5,
        },
    ]
    picked = pick_level_for_anchor(
        candidates,
        close_t=100.0,
        atr_ref=2.0,
        wanted_side="support",
        max_distance_atr=0.50,
        confluence_atr=0.25,
    )
    assert picked["level_id"] == "s2"


def test_priority_protected_over_swing():
    candidates = [
        {
            "level_id": "sw",
            "level_type": "external_swing",
            "side": "support",
            "level_price": 99.8,
        },
        {
            "level_id": "pr",
            "level_type": "protected",
            "side": "support",
            "level_price": 99.0,  # farther but protected
        },
    ]
    picked = pick_level_for_anchor(
        candidates,
        close_t=100.0,
        atr_ref=2.0,
        wanted_side="support",
        max_distance_atr=0.50,
        confluence_atr=0.25,
    )
    assert picked["level_id"] == "pr"
    assert picked["confluent"] is True


def test_nearest_within_same_type():
    candidates = [
        {"level_id": "a", "level_type": "protected", "side": "support", "level_price": 99.0},
        {"level_id": "b", "level_type": "protected", "side": "support", "level_price": 99.8},
    ]
    picked = pick_level_for_anchor(
        candidates,
        close_t=100.0,
        atr_ref=2.0,
        wanted_side="support",
        max_distance_atr=0.50,
        confluence_atr=0.25,
    )
    assert picked["level_id"] == "b"


def test_far_from_level_flag():
    candidates = [
        {"level_id": "a", "level_type": "protected", "side": "support", "level_price": 90.0},
    ]
    picked = pick_level_for_anchor(
        candidates,
        close_t=100.0,
        atr_ref=2.0,
        wanted_side="support",
        max_distance_atr=0.50,
        confluence_atr=0.25,
    )
    assert picked["far_from_level"] is True
    assert picked["distance_bucket"] == "far"
