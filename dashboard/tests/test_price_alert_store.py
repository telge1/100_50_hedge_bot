"""Unit tests for price alert trigger math."""

from __future__ import annotations

from price_alert_store import alert_key, condition_met, crossed_level


def test_alert_key_format():
    assert alert_key("solusdt", 142.5, "above") == "SOLUSDT_142.500000_above"


def test_condition_met():
    assert condition_met(150, 140, "above")
    assert not condition_met(130, 140, "above")
    assert condition_met(130, 140, "below")


def test_crossed_above():
    assert crossed_level(138, 141, 140, "above")
    assert not crossed_level(141, 142, 140, "above")
    assert crossed_level(145, 150, 140, "above") is False


def test_crossed_below():
    assert crossed_level(142, 138, 140, "below")
    assert not crossed_level(138, 137, 140, "below")