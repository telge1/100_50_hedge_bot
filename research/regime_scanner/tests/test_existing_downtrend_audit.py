"""Minimal tests for existing-downtrend read-only audit helpers."""

from research.regime_scanner.existing_downtrend_audit import (
    classify_long_row,
    _is_bearish,
    _is_strong_bearish,
)


def test_bearish_helpers() -> None:
    assert _is_bearish("bearish_trend_with_trend_weakness")
    assert not _is_bearish("bullish_trend")
    assert _is_strong_bearish("strong_bearish_trend")
    assert not _is_strong_bearish("bearish_trend")


def test_classify_long_verdicts() -> None:
    assert (
        classify_long_row(
            regime_15m="bullish_trend_with_trend_weakness",
            regime_30m="neutral",
            blockers="[]",
        )
        == "regime_not_bearish_recognized_at_setup"
    )
    assert (
        classify_long_row(
            regime_15m="bearish_trend_with_trend_weakness",
            regime_30m="bearish_trend_with_trend_weakness",
            blockers="[]",
        )
        == "regime_bearish_recognized_but_no_direction_blocker"
    )
    assert (
        classify_long_row(
            regime_15m="bullish_trend",
            regime_30m="bearish_trend",
            blockers='["HTF_OPPOSING_TREND"]',
        )
        == "blocked_by_existing_htf_policy"
    )
