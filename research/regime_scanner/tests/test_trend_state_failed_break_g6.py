"""Unit tests for G6 failed-break weakening qualification."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _events_are_independent,
    _failed_breakdown_is_trenddefining,
    _failed_breakout_is_trenddefining,
    _propose_transition,
    _qualified_failed_breakdown_for_weakening,
    _qualified_failed_breakout_for_weakening,
    _structure_level_equal,
    default_trend_state_config,
)
from research.regime_scanner.trend_structure import MarketStructureState, StructureEvent


def _ts(s: str = "2026-01-15T12:00:00+00:00") -> pd.Timestamp:
    return pd.Timestamp(s)


def _ev(
    event_type: str,
    *,
    level: float | None,
    pivot_time: str | None = None,
    pivot_price: float | None = None,
    event_time: str = "2026-01-15T12:00:00+00:00",
) -> StructureEvent:
    return StructureEvent(
        event_type=event_type,
        timeframe="5m",
        event_time=_ts(event_time),
        level=level,
        reference_pivot_time=None if pivot_time is None else _ts(pivot_time),
        reference_pivot_price=pivot_price,
        direction=None,
        reason_codes=(),
    )


def _rt(state: str, *, age: int = 10) -> TrendRuntime:
    rt = TrendRuntime()
    rt.state = state  # type: ignore[assignment]
    rt.age_5m_bars = age
    rt.unavailable_reason = None
    rt.entered_at = _ts()
    return rt


def _row() -> dict:
    return {
        "open": 1.0,
        "high": 1.01,
        "low": 0.99,
        "close": 1.0,
        "ema_9": 1.0,
        "ema_20": 1.0,
        "ema_9_slope_3_pct": 0.0,
        "ema_20_slope_3_pct": 0.0,
        "di_spread": 0.0,
        "adx": 20.0,
    }


def _propose(rt: TrendRuntime, events: list[StructureEvent]):
    return _propose_transition(rt, events=events, row=_row(), cfg=default_trend_state_config())


# ---------------------------------------------------------------------------
# Failed break alone
# ---------------------------------------------------------------------------


def test_failed_breakdown_alone_does_not_weaken_early_bearish() -> None:
    rt = _rt("early_bearish")
    rt.structure_5m.protective_low_level = 1.0
    events = [_ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T10:00:00+00:00")]
    # trenddefining but no choch and no hh_hl
    proposed, _ = _propose(rt, events)
    assert proposed != "bearish_weakening"


def test_failed_breakdown_alone_does_not_weaken_strong_bearish() -> None:
    rt = _rt("strong_bearish")
    rt.structure_5m.protective_low_level = 1.0
    events = [_ev("failed_breakdown", level=1.0)]
    proposed, _ = _propose(rt, events)
    assert proposed != "bearish_weakening"


def test_failed_breakout_alone_does_not_weaken_early_bullish() -> None:
    rt = _rt("early_bullish")
    rt.structure_5m.protective_high_level = 2.0
    events = [_ev("failed_breakout", level=2.0)]
    proposed, _ = _propose(rt, events)
    assert proposed != "bullish_weakening"


def test_failed_breakout_alone_does_not_weaken_strong_bullish() -> None:
    rt = _rt("strong_bullish")
    rt.structure_5m.protective_high_level = 2.0
    events = [_ev("failed_breakout", level=2.0)]
    proposed, _ = _propose(rt, events)
    assert proposed != "bullish_weakening"


# ---------------------------------------------------------------------------
# Trenddefining
# ---------------------------------------------------------------------------


def test_failed_breakdown_on_active_protective_low_is_trenddefining() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_low_level = 1.25
    ev = _ev("failed_breakdown", level=1.25)
    assert _failed_breakdown_is_trenddefining(ev, s5)


def test_failed_breakdown_on_last_broken_low_is_trenddefining() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.last_broken_low_level = 1.25
    ev = _ev("failed_breakdown", level=1.25)
    assert _failed_breakdown_is_trenddefining(ev, s5)


def test_failed_breakout_on_active_protective_high_is_trenddefining() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_high_level = 2.5
    ev = _ev("failed_breakout", level=2.5)
    assert _failed_breakout_is_trenddefining(ev, s5)


def test_failed_breakout_on_last_broken_high_is_trenddefining() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.last_broken_high_level = 2.5
    ev = _ev("failed_breakout", level=2.5)
    assert _failed_breakout_is_trenddefining(ev, s5)


def test_failed_break_on_arbitrary_swing_level_is_not_trenddefining() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_low_level = 1.27
    s5.last_broken_low_level = 1.2697
    ev = _ev("failed_breakdown", level=1.265)
    assert not _failed_breakdown_is_trenddefining(ev, s5)


def test_none_levels_never_match() -> None:
    assert not _structure_level_equal(None, None)
    assert not _structure_level_equal(1.0, None)
    assert not _structure_level_equal(None, 1.0)
    s5 = MarketStructureState(timeframe="5m")
    assert not _failed_breakdown_is_trenddefining(_ev("failed_breakdown", level=None), s5)


# ---------------------------------------------------------------------------
# Early rules
# ---------------------------------------------------------------------------


def test_early_bearish_weakens_on_td_failed_breakdown_plus_hh_hl() -> None:
    rt = _rt("early_bearish")
    rt.structure_5m.protective_low_level = 1.0
    rt.structure_5m.last_high_label = "higher_high"
    rt.structure_5m.last_low_label = "higher_low"
    events = [_ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00")]
    proposed, reasons = _propose(rt, events)
    assert proposed == "bearish_weakening"
    assert "trenddefining_failed_breakdown_with_counterstructure" in reasons


def test_early_bearish_weakens_on_td_failed_breakdown_plus_independent_bullish_choch() -> None:
    rt = _rt("early_bearish")
    rt.structure_5m.last_broken_low_level = 1.0
    events = [
        _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00", pivot_price=1.0),
        _ev("bullish_choch", level=1.5, pivot_time="2026-01-15T08:00:00+00:00", pivot_price=1.5),
    ]
    proposed, reasons = _propose(rt, events)
    assert proposed == "bearish_weakening"
    assert "trenddefining_failed_breakdown_with_counterstructure" in reasons


def test_early_bullish_weakens_on_td_failed_breakout_plus_lh_ll() -> None:
    rt = _rt("early_bullish")
    rt.structure_5m.protective_high_level = 2.0
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_5m.last_low_label = "lower_low"
    events = [_ev("failed_breakout", level=2.0)]
    proposed, reasons = _propose(rt, events)
    assert proposed == "bullish_weakening"
    assert "trenddefining_failed_breakout_with_counterstructure" in reasons


def test_early_bullish_weakens_on_td_failed_breakout_plus_independent_bearish_choch() -> None:
    rt = _rt("early_bullish")
    rt.structure_5m.last_broken_high_level = 2.0
    events = [
        _ev("failed_breakout", level=2.0, pivot_time="2026-01-15T09:00:00+00:00", pivot_price=2.0),
        _ev("bearish_choch", level=1.5, pivot_time="2026-01-15T08:00:00+00:00", pivot_price=1.5),
    ]
    proposed, _ = _propose(rt, events)
    assert proposed == "bullish_weakening"


# ---------------------------------------------------------------------------
# Strong rules
# ---------------------------------------------------------------------------


def test_strong_bearish_rejects_td_failed_breakdown_plus_hh_hl_without_choch() -> None:
    rt = _rt("strong_bearish")
    rt.structure_5m.protective_low_level = 1.0
    rt.structure_5m.last_high_label = "higher_high"
    rt.structure_5m.last_low_label = "higher_low"
    events = [_ev("failed_breakdown", level=1.0)]
    proposed, _ = _propose(rt, events)
    assert proposed != "bearish_weakening"


def test_strong_bearish_weakens_on_td_failed_breakdown_plus_independent_bullish_choch() -> None:
    rt = _rt("strong_bearish")
    rt.structure_5m.last_broken_low_level = 1.0
    events = [
        _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00", pivot_price=1.0),
        _ev("bullish_choch", level=1.4, pivot_time="2026-01-15T07:00:00+00:00", pivot_price=1.4),
    ]
    proposed, reasons = _propose(rt, events)
    assert proposed == "bearish_weakening"
    assert "trenddefining_failed_breakdown_with_counterstructure" in reasons


def test_strong_bullish_rejects_td_failed_breakout_plus_lh_ll_without_choch() -> None:
    rt = _rt("strong_bullish")
    rt.structure_5m.protective_high_level = 2.0
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_5m.last_low_label = "lower_low"
    events = [_ev("failed_breakout", level=2.0)]
    proposed, _ = _propose(rt, events)
    assert proposed != "bullish_weakening"


def test_strong_bullish_weakens_on_td_failed_breakout_plus_independent_bearish_choch() -> None:
    rt = _rt("strong_bullish")
    rt.structure_5m.protective_high_level = 2.0
    events = [
        _ev("failed_breakout", level=2.0, pivot_time="2026-01-15T09:00:00+00:00", pivot_price=2.0),
        _ev("bearish_choch", level=1.6, pivot_time="2026-01-15T07:00:00+00:00", pivot_price=1.6),
    ]
    proposed, _ = _propose(rt, events)
    assert proposed == "bullish_weakening"


# ---------------------------------------------------------------------------
# Double counting / independence
# ---------------------------------------------------------------------------


def test_same_level_failed_break_and_choch_are_not_independent() -> None:
    fb = _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00")
    # artificially same level (should not happen for bullish_choch vs FB, but rule must hold)
    ch = _ev("bullish_choch", level=1.0, pivot_time="2026-01-15T08:00:00+00:00")
    assert not _events_are_independent(fb, ch)


def test_same_source_pivot_failed_break_and_choch_are_not_independent() -> None:
    fb = _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00")
    ch = _ev("bullish_choch", level=1.5, pivot_time="2026-01-15T09:00:00+00:00")
    assert not _events_are_independent(fb, ch)


def test_different_level_and_pivot_events_are_independent() -> None:
    fb = _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00")
    ch = _ev("bullish_choch", level=1.5, pivot_time="2026-01-15T08:00:00+00:00")
    assert _events_are_independent(fb, ch)


def test_missing_pivot_metadata_is_handled_conservatively() -> None:
    fb = _ev("failed_breakdown", level=1.0)
    ch_same = _ev("bullish_choch", level=1.0)
    ch_diff = _ev("bullish_choch", level=1.5)
    assert not _events_are_independent(fb, ch_same)
    assert _events_are_independent(fb, ch_diff)
    fb_none = _ev("failed_breakdown", level=None)
    assert not _events_are_independent(fb_none, ch_diff)


# ---------------------------------------------------------------------------
# Same-bar and sticky
# ---------------------------------------------------------------------------


def test_same_bar_independent_events_can_qualify() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_low_level = 1.0
    events = [
        _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00"),
        _ev("bullish_choch", level=1.4, pivot_time="2026-01-15T08:00:00+00:00"),
    ]
    assert _qualified_failed_breakdown_for_weakening(events, s5, strong=True)


def test_old_sticky_failed_break_is_not_combined_with_later_choch() -> None:
    rt = _rt("early_bearish")
    rt.structure_5m.protective_low_level = 1.0
    rt.structure_5m.last_failed_breakdown = _ev(
        "failed_breakdown",
        level=1.0,
        event_time="2026-01-15T10:00:00+00:00",
        pivot_time="2026-01-15T09:00:00+00:00",
    )
    # Current candle only has bullish_choch — sticky FB must not qualify G6
    events = [_ev("bullish_choch", level=1.4, pivot_time="2026-01-15T08:00:00+00:00")]
    assert not _qualified_failed_breakdown_for_weakening(events, rt.structure_5m, strong=False)
    proposed, _ = _propose(rt, events)
    # choch alone without higher_low does not early-weaken
    assert proposed != "bearish_weakening"


def test_current_event_age_zero_is_required_for_g6() -> None:
    """G6 uses only the same-bar events list; sticky slot is ignored."""
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_low_level = 1.0
    s5.last_failed_breakdown = _ev("failed_breakdown", level=1.0)
    # empty current events → not qualified even with sticky + hh_hl
    s5.last_high_label = "higher_high"
    s5.last_low_label = "higher_low"
    assert not _qualified_failed_breakdown_for_weakening([], s5, strong=False)


# ---------------------------------------------------------------------------
# Existing paths unchanged
# ---------------------------------------------------------------------------


def test_retest_failure_weakening_path_is_unchanged() -> None:
    rt = _rt("early_bearish")
    events = [_ev("bearish_retest_fails", level=1.0)]
    proposed, reasons = _propose(rt, events)
    assert proposed == "bearish_weakening"
    assert "early_invalidation_toward_weakening" in reasons


def test_existing_choch_hl_lh_weakening_path_is_unchanged() -> None:
    rt = _rt("early_bearish")
    events = [
        _ev("bullish_choch", level=1.5),
        _ev("higher_low", level=1.1),
    ]
    proposed, _ = _propose(rt, events)
    assert proposed == "bearish_weakening"


def test_strong_bars_since_extreme_path_is_unchanged() -> None:
    rt = _rt("strong_bearish")
    rt.bars_since_ll = 99
    proposed, _ = _propose(rt, [])
    assert proposed == "bearish_weakening"


def test_bearish_continuation_guard_is_unchanged() -> None:
    rt = _rt("strong_bearish")
    rt.structure_5m.last_broken_low_level = 1.0
    # Would qualify FB+choch, but continuation guard blocks
    events = [
        _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00"),
        _ev("bullish_choch", level=1.4, pivot_time="2026-01-15T08:00:00+00:00"),
        _ev("bearish_bos", level=0.9),
        _ev("lower_low", level=0.85),
    ]
    proposed, _ = _propose(rt, events)
    assert proposed is None


def test_bullish_continuation_guard_is_unchanged() -> None:
    rt = _rt("strong_bullish")
    rt.structure_5m.protective_high_level = 2.0
    events = [
        _ev("failed_breakout", level=2.0, pivot_time="2026-01-15T09:00:00+00:00"),
        _ev("bearish_choch", level=1.5, pivot_time="2026-01-15T08:00:00+00:00"),
        _ev("bullish_bos", level=2.1),
        _ev("higher_high", level=2.2),
    ]
    proposed, _ = _propose(rt, events)
    assert proposed is None


# ---------------------------------------------------------------------------
# Historical regressions (structure inputs, no hardcoded production clocks)
# ---------------------------------------------------------------------------


def test_feb01_style_non_td_failed_breakdown_does_not_weaken() -> None:
    """Mirrors audit: FB@1.265 with last_broken=1.2697, no protective."""
    rt = _rt("early_bearish", age=18)
    rt.structure_5m.last_broken_low_level = 1.2697
    rt.structure_5m.protective_low_level = None
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_5m.last_low_label = "lower_low"
    events = [_ev("failed_breakdown", level=1.265)]
    assert not _failed_breakdown_is_trenddefining(events[0], rt.structure_5m)
    proposed, _ = _propose(rt, events)
    assert proposed != "bearish_weakening"


def test_feb12_style_non_td_failed_breakout_does_not_weaken() -> None:
    rt = _rt("early_bullish")
    rt.structure_5m.protective_high_level = None
    rt.structure_5m.last_broken_high_level = 0.96
    events = [_ev("failed_breakout", level=0.9511)]
    assert not _failed_breakout_is_trenddefining(events[0], rt.structure_5m)
    proposed, _ = _propose(rt, events)
    assert proposed != "bullish_weakening"


def test_march_style_non_td_counterfactual_early_and_strong() -> None:
    """FB@0.9926 not trenddefining — neither early nor strong FB path weakens."""
    events = [_ev("failed_breakdown", level=0.9926)]
    for state in ("early_bearish", "strong_bearish"):
        rt = _rt(state)
        rt.structure_5m.protective_low_level = None
        rt.structure_5m.last_broken_low_level = 0.995
        rt.structure_5m.last_high_label = "higher_high"
        rt.structure_5m.last_low_label = "higher_low"
        assert not _failed_breakdown_is_trenddefining(events[0], rt.structure_5m)
        assert not _qualified_failed_breakdown_for_weakening(
            events, rt.structure_5m, strong=(state == "strong_bearish")
        )
        proposed, _ = _propose(rt, events)
        assert proposed != "bearish_weakening"


def test_multiple_failed_breakdowns_any_qualifying_pair_counts() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.protective_low_level = 1.0
    events = [
        _ev("failed_breakdown", level=0.9),  # not td
        _ev("failed_breakdown", level=1.0, pivot_time="2026-01-15T09:00:00+00:00"),
        _ev("bullish_choch", level=1.4, pivot_time="2026-01-15T08:00:00+00:00"),
    ]
    assert _qualified_failed_breakdown_for_weakening(events, s5, strong=True)


def test_qualified_breakout_mirror_helper() -> None:
    s5 = MarketStructureState(timeframe="5m")
    s5.last_broken_high_level = 2.0
    s5.last_high_label = "lower_high"
    s5.last_low_label = "lower_low"
    events = [_ev("failed_breakout", level=2.0)]
    assert _qualified_failed_breakout_for_weakening(events, s5, strong=False)
    assert not _qualified_failed_breakout_for_weakening(events, s5, strong=True)
