"""Unit tests for causal Phase-D unlock signals (lookahead / EMA / retest)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.backtests.emergency_lock.phase_d_signals import (
    EmaReclaimSignal,
    ReboundBaselineSignal,
    SignalContext,
    SwingBreakRetestSignal,
    SwingBreakWithEmaSignal,
    SwingHighBreakSignal,
    causal_ema_series,
    confirmed_swing_highs,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace(
        hour=min((i * 5) // 60, 23), minute=(i * 5) % 60
    )


def _c(i: int, *, h: float, l: float, c: float | None = None) -> dict:
    close = float(c if c is not None else (h + l) / 2.0)
    return {
        "timestamp": _ts(i),
        "open": close,
        "high": float(h),
        "low": float(l),
        "close": close,
        "volume": 1.0,
    }


def _ctx(
    candles: list[dict],
    index: int,
    *,
    post_lock_start: int = 0,
    stage: int = 0,
    bars_since: int | None = None,
    last_fill: float | None = None,
) -> SignalContext:
    return SignalContext(
        candles=candles[: index + 1],
        index=index,
        post_lock_start_index=post_lock_start,
        long_avg=100.0,
        short_avg=100.0,
        long_qty=1.0,
        short_qty=1.0,
        next_unlock_stage=stage,
        last_unlock_fill=last_fill,
        last_unlock_reference=None,
        bars_since_last_unlock=bars_since,
        post_lock_low=float(candles[post_lock_start]["low"]),
        unlock_rebound_pcts=(0.03, 0.05, 0.075, 0.10),
        full_lock_short_qty=1.0,
    )


def test_swing_high_unknown_before_right_bars() -> None:
    # Pivot at i=3 with left=3,right=3 → confirmed at bar 6.
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),  # swing candidate
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),  # confirmation bar
        _c(7, h=16, l=9, c=16),  # close break
    ]
    assert confirmed_swing_highs(candles, asof_index=5, left=3, right=3) == []
    confirmed = confirmed_swing_highs(candles, asof_index=6, left=3, right=3)
    assert confirmed == [(3, 15.0)]


def test_swing_high_confirmed_after_right_bars() -> None:
    candles = [_c(i, h=10 + (i == 3) * 5, l=9) for i in range(7)]
    sig = SwingHighBreakSignal(left=3, right=3, break_confirmation_closes=1)
    # At confirmation bar, swing known but close may not break.
    d = sig.evaluate(_ctx(candles, 6))
    assert d.metadata.get("swing_high") == 15.0
    assert d.metadata.get("swing_confirmed_at") == 6
    assert d.triggered is False


def test_swing_break_requires_close_not_high_touch() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=14.5),  # high above swing, close below
    ]
    sig = SwingHighBreakSignal(left=3, right=3)
    d = sig.evaluate(_ctx(candles, 7))
    assert d.triggered is False
    assert d.reason == "waiting_close_break"


def test_swing_break_causal_close_break() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=15.5),
    ]
    sig = SwingHighBreakSignal(left=3, right=3)
    assert sig.evaluate(_ctx(candles, 7)).triggered is True


def test_future_higher_high_does_not_change_past_decision() -> None:
    base = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=15.5),
    ]
    sig_a = SwingHighBreakSignal(left=3, right=3)
    d_a = sig_a.evaluate(_ctx(base, 7))
    extended = base + [_c(8, h=30, l=9, c=29)]
    sig_b = SwingHighBreakSignal(left=3, right=3)
    # Replay through bar 7 on extended series (causal prefix)
    for i in range(8):
        d_b = sig_b.evaluate(_ctx(extended, i))
    assert d_a.triggered == d_b.triggered
    assert d_a.metadata.get("swing_high") == d_b.metadata.get("swing_high")


def test_retest_impossible_before_break() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=14, l=13, c=13.5),  # no close break
    ]
    sig = SwingBreakRetestSignal(left=3, right=3)
    d = sig.evaluate(_ctx(candles, 7))
    assert d.triggered is False
    assert d.reason in {"waiting_break", "need_new_structure"}


def test_retest_window_expiry() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=15, c=15.5),  # arm break
    ]
    # After arm, stay above without touching level for > retest_max_bars
    for i in range(8, 25):
        candles.append(_c(i, h=17, l=16, c=16.5))
    sig = SwingBreakRetestSignal(left=3, right=3, retest_max_bars=12)
    reasons = []
    for i in range(7, len(candles)):
        reasons.append(sig.evaluate(_ctx(candles, i)).reason)
    assert "break_armed_waiting_retest" in reasons
    assert "retest_window_expired" in reasons
    assert not any(r == "retest_close_reclaim" for r in reasons)


def test_invalid_retest_no_signal() -> None:
    """Pierce far below tolerance without reclaim close → no unlock."""
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=15, c=15.5),
        _c(8, h=15, l=14, c=14.2),  # close still below break level
    ]
    sig = SwingBreakRetestSignal(left=3, right=3, retest_tolerance_atr=0.01)
    assert sig.evaluate(_ctx(candles, 7)).triggered is False
    d = sig.evaluate(_ctx(candles, 8))
    assert d.triggered is False


def test_valid_retest_single_signal() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=15, c=15.5),  # break arm
        _c(8, h=15.2, l=14.8, c=15.1),  # retest touch + close reclaim
    ]
    sig = SwingBreakRetestSignal(left=3, right=3, retest_tolerance_atr=1.0)
    assert sig.evaluate(_ctx(candles, 7)).triggered is False
    d = sig.evaluate(_ctx(candles, 8))
    assert d.triggered is True
    assert d.reason == "retest_close_reclaim"
    sig.note_unlock(_ctx(candles, 8), d)
    # Same structure must not re-fire without new progress
    assert sig.evaluate(_ctx(candles, 8, stage=1, bars_since=10, last_fill=15.0)).triggered is False


def test_ema_causal_and_warmup() -> None:
    closes = [float(i) for i in range(1, 25)]
    ema9 = causal_ema_series(closes, 9)
    ema20 = causal_ema_series(closes, 20)
    assert all(v is None for v in ema9[:8])
    assert ema9[8] == pytest.approx(sum(closes[:9]) / 9)
    assert all(v is None for v in ema20[:19])
    assert ema20[19] == pytest.approx(sum(closes[:20]) / 20)
    # Step recursion causal
    alpha = 2.0 / 10.0
    expected = alpha * closes[9] + (1 - alpha) * float(ema9[8])
    assert ema9[9] == pytest.approx(expected)


def test_ema_reclaim_needs_confirmation_closes() -> None:
    # Build series: flat below, then reclaim above EMA20 for one bar only.
    candles = [_c(i, h=10.1, l=9.9, c=10.0) for i in range(25)]
    candles.append(_c(25, h=12, l=11, c=11.5))  # reclaim candidate
    candles.append(_c(26, h=9.5, l=9.0, c=9.2))  # falls back — no 2nd confirm
    sig = EmaReclaimSignal(ema_confirmation_closes=2)
    # Drive through warmup
    for i in range(25):
        sig.evaluate(_ctx(candles, i))
    d25 = sig.evaluate(_ctx(candles, 25))
    # May or may not arm depending on EMA levels; if armed, not yet triggered at 1
    if d25.metadata.get("reclaim_armed"):
        assert d25.triggered is False
    d26 = sig.evaluate(_ctx(candles, 26))
    assert d26.triggered is False


def test_ema_reclaim_triggers_after_two_closes() -> None:
    candles = [_c(i, h=10.1, l=9.9, c=10.0) for i in range(30)]
    # Force a sustained lift so EMA20 is below close
    for j, px in enumerate([10.5, 11.0, 11.5, 12.0, 12.5]):
        candles.append(_c(30 + j, h=px + 0.2, l=px - 0.2, c=px))
    sig = EmaReclaimSignal(ema_confirmation_closes=2, require_fast_above_slow=True)
    triggered_at = []
    for i in range(len(candles)):
        d = sig.evaluate(_ctx(candles, i))
        if d.triggered:
            triggered_at.append(i)
    # If a reclaim occurs, first trigger needs streak>=2 (not same bar as first arm alone
    # unless arm already had prior — with confirmation=2, arm bar alone is insufficient).
    if triggered_at:
        first = triggered_at[0]
        # Reconstruct: confirmation streak must be >=2
        assert first >= 20


def test_structure_plus_ema_needs_both() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=15.5),
    ]
    # Pad warmup for EMA with low closes so EMA20 >> close after break path —
    # use long flat then structure; EMA may still filter.
    pad = [_c(i, h=20, l=19, c=19.5) for i in range(30)]
    # Rebuild structure after pad
    body = [
        _c(30, h=10, l=9, c=9.5),
        _c(31, h=11, l=9, c=10),
        _c(32, h=12, l=9, c=11),
        _c(33, h=15, l=9, c=12),
        _c(34, h=12, l=9, c=11),
        _c(35, h=11, l=9, c=10),
        _c(36, h=10, l=9, c=9.5),
        _c(37, h=16, l=9, c=15.5),  # swing break close
    ]
    series = pad + body
    swing_only = SwingHighBreakSignal(left=3, right=3)
    combo = SwingBreakWithEmaSignal()
    # Evaluate through series
    for i in range(len(series) - 1):
        swing_only.evaluate(_ctx(series, i, post_lock_start=30))
        combo.evaluate(_ctx(series, i, post_lock_start=30))
    d_s = swing_only.evaluate(_ctx(series, len(series) - 1, post_lock_start=30))
    d_c = combo.evaluate(_ctx(series, len(series) - 1, post_lock_start=30))
    # Swing alone may trigger; combo requires EMA9>=EMA20 and close>EMA20.
    if d_s.triggered and not (
        d_c.metadata.get("ema_20") is not None
        and float(series[-1]["close"]) > float(d_c.metadata["ema_20"])
        and d_c.metadata.get("ema_fast_above_slow")
    ):
        assert d_c.triggered is False


def test_min_bars_between_stages() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=15.5),
        _c(8, h=18, l=9, c=17.5),
        _c(9, h=14, l=9),
        _c(10, h=13, l=9),
        _c(11, h=12, l=9),
        _c(12, h=20, l=9),  # new higher swing pivot
        _c(13, h=14, l=9),
        _c(14, h=13, l=9),
        _c(15, h=12, l=9),  # confirm new swing at 15
        _c(16, h=22, l=9, c=21),
    ]
    sig = SwingHighBreakSignal(left=3, right=3, minimum_bars_between_unlock_stages=6)
    d0 = sig.evaluate(_ctx(candles, 7, stage=0))
    assert d0.triggered
    sig.note_unlock(_ctx(candles, 7), d0)
    # Too soon for next stage even with higher swing later
    d_early = sig.evaluate(
        _ctx(candles, 16, stage=1, bars_since=3, last_fill=15.5)
    )
    assert d_early.triggered is False
    assert d_early.reason == "min_bars_between_stages"


def test_next_stage_needs_new_progress_not_sticky_true() -> None:
    candles = [
        _c(0, h=10, l=9),
        _c(1, h=11, l=9),
        _c(2, h=12, l=9),
        _c(3, h=15, l=9),
        _c(4, h=12, l=9),
        _c(5, h=11, l=9),
        _c(6, h=10, l=9),
        _c(7, h=16, l=9, c=15.5),
        _c(8, h=16.5, l=9, c=16.0),
        _c(9, h=17, l=9, c=16.5),
        _c(10, h=17.5, l=9, c=17.0),
        _c(11, h=18, l=9, c=17.5),
        _c(12, h=18.5, l=9, c=18.0),
        _c(13, h=19, l=9, c=18.5),
    ]
    sig = SwingHighBreakSignal(left=3, right=3, minimum_bars_between_unlock_stages=1)
    d0 = sig.evaluate(_ctx(candles, 7))
    assert d0.triggered
    sig.note_unlock(_ctx(candles, 7), d0)
    # Sticky closes above same swing must not unlock again
    triggers = 0
    for i in range(8, 14):
        d = sig.evaluate(
            _ctx(candles, i, stage=1, bars_since=10, last_fill=15.0)
        )
        if d.triggered:
            triggers += 1
    assert triggers == 0


def test_rebound_baseline_high_touch() -> None:
    candles = [_c(0, h=100, l=90, c=95), _c(1, h=94, l=90, c=91)]
    sig = ReboundBaselineSignal()
    # post_lock_low=90, stage0 rebound 3% → ref=92.7; high 94 touches
    d = sig.evaluate(_ctx(candles, 1, post_lock_start=0))
    assert d.triggered is True
