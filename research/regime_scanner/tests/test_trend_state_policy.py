"""Unit tests for trend direction policy."""

from __future__ import annotations

from research.regime_scanner.trend_state_policy import (
    all_policies,
    policy_for_state,
    would_block_long,
    would_block_short,
)


def test_bottoming_policy_b() -> None:
    p = policy_for_state("bottoming")
    assert p.allow_long is True
    assert p.allow_short is False
    assert p.require_stricter_long_confirmation is True
    assert p.block_new_short_setup is True
    assert p.abort_running_short_pa is True
    assert p.abort_running_short_momentum is True
    assert p.abort_running_long_pa is False


def test_early_strong_bearish_block_longs() -> None:
    for state in ("early_bearish", "strong_bearish"):
        p = policy_for_state(state)
        assert p.allow_long is False
        assert p.allow_short is True
        assert p.block_new_long_setup is True
        assert p.abort_running_long_pa is True
        assert would_block_long(state) is True
        assert would_block_short(state) is False


def test_early_strong_bullish_block_shorts() -> None:
    for state in ("early_bullish", "strong_bullish"):
        p = policy_for_state(state)
        assert p.allow_long is True
        assert p.allow_short is False
        assert would_block_short(state) is True


def test_bearish_warning_does_not_hard_block_longs() -> None:
    p = policy_for_state("bearish_warning")
    assert p.allow_long is True
    assert p.block_new_long_setup is False
    assert p.require_stricter_long_confirmation is True


def test_topping_mirrors_bottoming() -> None:
    t = policy_for_state("topping")
    assert t.allow_long is False
    assert t.allow_short is True
    assert t.require_stricter_short_confirmation is True


def test_no_direct_trade_open_fields() -> None:
    for p in all_policies().values():
        d = p.to_dict()
        assert "open_trade" not in d
        assert "entry" not in d
