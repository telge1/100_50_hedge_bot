"""Unit tests for audit-only multilevel policy comparison helpers."""

from __future__ import annotations

from research.regime_scanner.multilevel_market_structure import BEARISH, BULLISH
from research.regime_scanner.multilevel_structure_policy_comparison_audit import policy_decision


def test_possible_bullish_is_wait_even_if_swing_already_bullish() -> None:
    d, reason = policy_decision(
        direction="long",
        internal_bias=BULLISH,
        swing_bias=BULLISH,
        primary_label="possible_bullish_swing_reversal",
    )
    assert d == "WAIT"
    assert reason == "possible_bullish_swing_reversal"


def test_recovery_blocks_long_while_swing_bearish() -> None:
    d, reason = policy_decision(
        direction="long",
        internal_bias=BULLISH,
        swing_bias=BEARISH,
        primary_label="bullish_recovery_inside_bearish_swing",
    )
    assert d == "BLOCK"
    assert reason == "recovery_long_blocked"


def test_confirmed_bull_rev_allows_long_only_with_internal_bull() -> None:
    allow, _ = policy_decision(
        direction="long",
        internal_bias=BULLISH,
        swing_bias=BULLISH,
        primary_label="confirmed_bullish_swing_reversal",
    )
    wait, _ = policy_decision(
        direction="long",
        internal_bias=BEARISH,
        swing_bias=BULLISH,
        primary_label="confirmed_bullish_swing_reversal",
    )
    assert allow == "ALLOW"
    assert wait == "WAIT"


def test_confirmed_bearish_blocks_long_when_swing_already_bearish() -> None:
    d, _ = policy_decision(
        direction="long",
        internal_bias=BEARISH,
        swing_bias=BEARISH,
        primary_label="confirmed_bearish_swing_reversal",
    )
    assert d == "BLOCK"
