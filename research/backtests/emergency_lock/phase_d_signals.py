"""Causal Phase-D unlock signals for Emergency-Lock research.

All signals are strictly causal: they may only use candles up to and including
the current bar. Swing highs become known only after ``right`` confirmation
bars have closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .signals import unlock_reference_price, unlock_signal_touched


@dataclass
class SignalDecision:
    triggered: bool
    signal_name: str
    reference_price: float | None
    invalidation_price: float | None
    reason: str
    metadata: dict[str, float | int | str | bool | None] = field(default_factory=dict)


@dataclass
class SignalContext:
    """Causal view available to unlock signals (no event-low / oracle)."""

    candles: list[dict[str, Any]]  # simulation window, indices 0..t
    index: int  # current bar index in candles
    post_lock_start_index: int  # index of lock bar within candles
    long_avg: float
    short_avg: float
    long_qty: float
    short_qty: float
    next_unlock_stage: int
    last_unlock_fill: float | None
    last_unlock_reference: float | None
    bars_since_last_unlock: int | None
    post_lock_low: float | None
    unlock_rebound_pcts: tuple[float, ...]
    full_lock_short_qty: float


class UnlockSignal(Protocol):
    name: str

    def reset(self) -> None: ...

    def evaluate(self, ctx: SignalContext) -> SignalDecision: ...

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None: ...

    def invalidation(
        self, ctx: SignalContext
    ) -> SignalDecision: ...


def _atr(candles: list[dict[str, Any]], end_i: int, period: int = 14) -> float:
    if end_i <= 0:
        return max(float(candles[end_i]["high"]) - float(candles[end_i]["low"]), 1e-12)
    start = max(1, end_i - period + 1)
    trs: list[float] = []
    for i in range(start, end_i + 1):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        prev_c = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return max(sum(trs) / len(trs), 1e-12)


def confirmed_swing_highs(
    candles: list[dict[str, Any]],
    *,
    asof_index: int,
    left: int,
    right: int,
    start_index: int = 0,
) -> list[tuple[int, float]]:
    """Return ``(pivot_index, high)`` confirmed by ``asof_index``.

    A pivot at ``i`` is confirmed on bar ``i + right`` when its high is the
    maximum of ``[i-left, i+right]``. It is therefore unknown before that bar.
    """
    out: list[tuple[int, float]] = []
    # Latest pivot that can be confirmed: i + right <= asof_index
    last_pivot = asof_index - int(right)
    first_pivot = max(int(start_index) + int(left), int(left))
    for i in range(first_pivot, last_pivot + 1):
        hi = float(candles[i]["high"])
        lo_i = i - int(left)
        hi_i = i + int(right)
        window = [float(candles[j]["high"]) for j in range(lo_i, hi_i + 1)]
        if hi >= max(window) - 1e-15:
            out.append((i, hi))
    return out


def confirmed_swing_lows(
    candles: list[dict[str, Any]],
    *,
    asof_index: int,
    left: int,
    right: int,
    start_index: int = 0,
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    last_pivot = asof_index - int(right)
    first_pivot = max(int(start_index) + int(left), int(left))
    for i in range(first_pivot, last_pivot + 1):
        lo = float(candles[i]["low"])
        lo_i = i - int(left)
        hi_i = i + int(right)
        window = [float(candles[j]["low"]) for j in range(lo_i, hi_i + 1)]
        if lo <= min(window) + 1e-15:
            out.append((i, lo))
    return out


def causal_ema_series(closes: list[float], period: int) -> list[float | None]:
    """Causal EMA; ``None`` until ``period`` samples are available."""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    # Seed with SMA of first ``period`` closes.
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, len(closes)):
        prev = alpha * closes[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _progress_ok(
    ctx: SignalContext,
    *,
    min_bars: int,
    require_above_last_fill: bool,
    mark: float,
) -> tuple[bool, str]:
    if ctx.next_unlock_stage == 0:
        return True, "first_stage"
    if ctx.bars_since_last_unlock is None:
        return True, "no_prior_unlock_bars"
    if int(ctx.bars_since_last_unlock) < int(min_bars):
        return False, "min_bars_between_stages"
    if require_above_last_fill and ctx.last_unlock_fill is not None:
        if float(mark) <= float(ctx.last_unlock_fill) + 1e-12:
            return False, "price_not_above_last_unlock_fill"
    return True, "progress_ok"


@dataclass
class ReboundBaselineSignal:
    """Phase-B rebound: high touch of post_lock_low × (1+pct[stage])."""

    name: str = "rebound_baseline"

    def reset(self) -> None:
        return None

    def evaluate(self, ctx: SignalContext) -> SignalDecision:
        if ctx.post_lock_low is None or ctx.next_unlock_stage >= len(ctx.unlock_rebound_pcts):
            return SignalDecision(False, self.name, None, None, "no_stage")
        pct = float(ctx.unlock_rebound_pcts[ctx.next_unlock_stage])
        ref = unlock_reference_price(post_lock_low=ctx.post_lock_low, rebound_pct=pct)
        candle = ctx.candles[ctx.index]
        touched = unlock_signal_touched(
            candle_high=float(candle["high"]), unlock_reference=ref
        )
        return SignalDecision(
            triggered=touched,
            signal_name=self.name,
            reference_price=ref,
            invalidation_price=None,
            reason="rebound_high_touch" if touched else "waiting_rebound",
            metadata={"post_lock_low": ctx.post_lock_low, "rebound_pct": pct},
        )

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None:
        return None

    def invalidation(self, ctx: SignalContext) -> SignalDecision:
        return SignalDecision(False, self.name, None, None, "no_signal_invalidation")


@dataclass
class SwingHighBreakSignal:
    name: str = "swing_high_break"
    left: int = 3
    right: int = 3
    break_confirmation_closes: int = 1
    break_buffer_atr: float = 0.0
    minimum_bars_between_unlock_stages: int = 6
    _break_streak: int = 0
    _last_used_swing_high: float | None = None
    _pending_swing: float | None = None

    def reset(self) -> None:
        self._break_streak = 0
        self._last_used_swing_high = None
        self._pending_swing = None

    def evaluate(self, ctx: SignalContext) -> SignalDecision:
        mark = float(ctx.candles[ctx.index]["close"])
        ok, why = _progress_ok(
            ctx,
            min_bars=self.minimum_bars_between_unlock_stages,
            require_above_last_fill=True,
            mark=mark,
        )
        swings = confirmed_swing_highs(
            ctx.candles,
            asof_index=ctx.index,
            left=self.left,
            right=self.right,
            start_index=ctx.post_lock_start_index,
        )
        if not swings:
            self._break_streak = 0
            return SignalDecision(
                False,
                self.name,
                None,
                None,
                "no_confirmed_swing_high",
                {"swing_high": None},
            )
        pivot_i, swing_high = swings[-1]
        confirmed_at = pivot_i + self.right
        atr = _atr(ctx.candles, ctx.index)
        level = float(swing_high) + float(self.break_buffer_atr) * atr

        # New progress: require a swing high strictly above the one last used.
        if self._last_used_swing_high is not None:
            if swing_high <= float(self._last_used_swing_high) + 1e-12:
                self._break_streak = 0
                return SignalDecision(
                    False,
                    self.name,
                    level,
                    None,
                    "need_higher_confirmed_swing",
                    {
                        "swing_high": swing_high,
                        "swing_confirmed_at": confirmed_at,
                        "break_level": level,
                    },
                )

        if not ok:
            self._break_streak = 0
            return SignalDecision(
                False,
                self.name,
                level,
                None,
                why,
                {
                    "swing_high": swing_high,
                    "swing_confirmed_at": confirmed_at,
                    "break_level": level,
                },
            )

        close = mark
        if close > level:
            self._break_streak += 1
        else:
            self._break_streak = 0

        triggered = self._break_streak >= int(self.break_confirmation_closes)
        if triggered:
            self._pending_swing = float(swing_high)
        # Invalidation diagnostic level: last confirmed swing low
        lows = confirmed_swing_lows(
            ctx.candles,
            asof_index=ctx.index,
            left=self.left,
            right=self.right,
            start_index=ctx.post_lock_start_index,
        )
        inv = float(lows[-1][1]) if lows else None
        return SignalDecision(
            triggered=triggered,
            signal_name=self.name,
            reference_price=level,
            invalidation_price=inv,
            reason="swing_high_close_break" if triggered else "waiting_close_break",
            metadata={
                "swing_high": swing_high,
                "swing_confirmed_at": confirmed_at,
                "break_level": level,
                "break_streak": self._break_streak,
            },
        )

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None:
        if self._pending_swing is not None:
            self._last_used_swing_high = float(self._pending_swing)
        self._break_streak = 0
        self._pending_swing = None

    def invalidation(self, ctx: SignalContext) -> SignalDecision:
        lows = confirmed_swing_lows(
            ctx.candles,
            asof_index=ctx.index,
            left=self.left,
            right=self.right,
            start_index=ctx.post_lock_start_index,
        )
        if not lows:
            return SignalDecision(False, self.name, None, None, "no_swing_low")
        level = float(lows[-1][1])
        close = float(ctx.candles[ctx.index]["close"])
        hit = close < level
        return SignalDecision(
            triggered=hit,
            signal_name=self.name,
            reference_price=level,
            invalidation_price=level,
            reason="close_below_swing_low" if hit else "hold",
            metadata={"swing_low": level},
        )


@dataclass
class SwingBreakRetestSignal:
    name: str = "swing_break_retest"
    left: int = 3
    right: int = 3
    retest_max_bars: int = 12
    retest_tolerance_atr: float = 0.25
    retest_confirmation_closes: int = 1
    minimum_bars_between_unlock_stages: int = 6
    _armed_level: float | None = None
    _armed_bar: int | None = None
    _retest_seen: bool = False
    _retest_low: float | None = None
    _confirm_streak: int = 0
    _last_used_level: float | None = None

    def reset(self) -> None:
        self._armed_level = None
        self._armed_bar = None
        self._retest_seen = False
        self._retest_low = None
        self._confirm_streak = 0
        self._last_used_level = None

    def evaluate(self, ctx: SignalContext) -> SignalDecision:
        mark = float(ctx.candles[ctx.index]["close"])
        candle = ctx.candles[ctx.index]
        atr = _atr(ctx.candles, ctx.index)
        swings = confirmed_swing_highs(
            ctx.candles,
            asof_index=ctx.index,
            left=self.left,
            right=self.right,
            start_index=ctx.post_lock_start_index,
        )
        meta: dict[str, float | int | str | bool | None] = {
            "swing_high": swings[-1][1] if swings else None,
            "break_level": self._armed_level,
            "retest_low": self._retest_low,
        }

        # Arm on close break of latest confirmed swing (if not already armed).
        if self._armed_level is None and swings:
            swing_high = float(swings[-1][1])
            if self._last_used_level is not None and swing_high <= float(self._last_used_level):
                return SignalDecision(False, self.name, swing_high, None, "need_new_structure", meta)
            if mark > swing_high:
                self._armed_level = swing_high
                self._armed_bar = ctx.index
                self._retest_seen = False
                self._retest_low = None
                self._confirm_streak = 0
                meta["break_level"] = swing_high
                return SignalDecision(
                    False, self.name, swing_high, None, "break_armed_waiting_retest", meta
                )

        if self._armed_level is None:
            return SignalDecision(False, self.name, None, None, "waiting_break", meta)

        level = float(self._armed_level)
        bars_since = ctx.index - int(self._armed_bar or ctx.index)
        if bars_since > int(self.retest_max_bars):
            kept = self._last_used_level
            self._armed_level = None
            self._armed_bar = None
            self._retest_seen = False
            self._retest_low = None
            self._confirm_streak = 0
            self._last_used_level = kept
            return SignalDecision(False, self.name, level, None, "retest_window_expired", meta)

        tol = float(self.retest_tolerance_atr) * atr
        # Retest: low comes back to/through level within tolerance (may pierce slightly).
        if float(candle["low"]) <= level + tol:
            self._retest_seen = True
            self._retest_low = (
                float(candle["low"])
                if self._retest_low is None
                else min(float(self._retest_low), float(candle["low"]))
            )
            meta["retest_low"] = self._retest_low

        if not self._retest_seen:
            return SignalDecision(False, self.name, level, None, "waiting_retest", meta)

        ok, why = _progress_ok(
            ctx,
            min_bars=self.minimum_bars_between_unlock_stages,
            require_above_last_fill=True,
            mark=mark,
        )
        if not ok:
            return SignalDecision(False, self.name, level, None, why, meta)

        if mark > level:
            self._confirm_streak += 1
        else:
            self._confirm_streak = 0
        triggered = self._confirm_streak >= int(self.retest_confirmation_closes)
        meta["retest_confirmed"] = triggered
        return SignalDecision(
            triggered=triggered,
            signal_name=self.name,
            reference_price=level,
            invalidation_price=self._retest_low,
            reason="retest_close_reclaim" if triggered else "waiting_retest_close",
            metadata=meta,
        )

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None:
        if self._armed_level is not None:
            self._last_used_level = float(self._armed_level)
        self._armed_level = None
        self._armed_bar = None
        self._retest_seen = False
        self._retest_low = None
        self._confirm_streak = 0

    def invalidation(self, ctx: SignalContext) -> SignalDecision:
        # After unlock, invalidate on close below last retest low / armed invalidation.
        level = self._retest_low
        if level is None:
            lows = confirmed_swing_lows(
                ctx.candles,
                asof_index=ctx.index,
                left=self.left,
                right=self.right,
                start_index=ctx.post_lock_start_index,
            )
            if not lows:
                return SignalDecision(False, self.name, None, None, "no_invalidation_level")
            level = float(lows[-1][1])
        close = float(ctx.candles[ctx.index]["close"])
        hit = close < float(level)
        return SignalDecision(
            hit, self.name, float(level), float(level),
            "close_below_retest_low" if hit else "hold",
        )


@dataclass
class EmaReclaimSignal:
    """EMA reclaim: cross above EMA20, then N consecutive closes above EMA20.

    ``ema_confirmation_closes`` counts closes *after arming* (including the
    reclaim bar). With the baseline value 2, unlock needs the reclaim close
    plus one further close still above EMA20 — a single-bar reclaim cross
    alone is not enough.
    """

    name: str = "ema_reclaim"
    ema_fast: int = 9
    ema_slow: int = 20
    ema_confirmation_closes: int = 2
    require_fast_above_slow: bool = True
    minimum_bars_between_unlock_stages: int = 6
    _confirm_streak: int = 0
    _reclaim_armed: bool = False
    _condition_lost_since_unlock: bool = True

    def reset(self) -> None:
        self._confirm_streak = 0
        self._reclaim_armed = False
        self._condition_lost_since_unlock = True

    def _series(self, ctx: SignalContext) -> tuple[list[float | None], list[float | None]]:
        closes = [float(c["close"]) for c in ctx.candles]
        return (
            causal_ema_series(closes, self.ema_fast),
            causal_ema_series(closes, self.ema_slow),
        )

    def evaluate(self, ctx: SignalContext) -> SignalDecision:
        fast_s, slow_s = self._series(ctx)
        i = ctx.index
        if i < 1 or fast_s[i] is None or slow_s[i] is None or slow_s[i - 1] is None:
            return SignalDecision(False, self.name, None, None, "ema_warmup")
        ema9 = float(fast_s[i])
        ema20 = float(slow_s[i])
        prev20 = float(slow_s[i - 1])
        close = float(ctx.candles[i]["close"])
        prev_close = float(ctx.candles[i - 1]["close"])
        fast_above = ema9 >= ema20
        slope_ok = ema20 >= prev20 - 1e-12  # not further strongly falling
        reclaim_cross = prev_close < prev20 and close > ema20
        above = close > ema20
        fast_ok = (not self.require_fast_above_slow) or fast_above

        meta: dict[str, float | int | str | bool | None] = {
            "ema_9": ema9,
            "ema_20": ema20,
            "ema_fast_above_slow": fast_above,
            "ema_slope": ema20 - prev20,
            "reclaim_armed": self._reclaim_armed,
            "confirm_streak": self._confirm_streak,
        }

        if not above:
            self._confirm_streak = 0
            self._reclaim_armed = False
            self._condition_lost_since_unlock = True
            return SignalDecision(
                False, self.name, ema20, ema20, "below_ema20", meta
            )

        ok, why = _progress_ok(
            ctx,
            min_bars=self.minimum_bars_between_unlock_stages,
            require_above_last_fill=True,
            mark=close,
        )
        if ctx.next_unlock_stage > 0 and not self._condition_lost_since_unlock:
            return SignalDecision(
                False, self.name, ema20, ema20, "need_new_reclaim_after_loss", meta
            )
        if not ok:
            return SignalDecision(False, self.name, ema20, ema20, why, meta)

        if reclaim_cross and slope_ok and fast_ok:
            self._reclaim_armed = True
            self._confirm_streak = 1
        elif self._reclaim_armed and above and fast_ok and slope_ok:
            self._confirm_streak += 1
        elif self._reclaim_armed and above and fast_ok:
            # Still above but slope failed — hold streak, do not advance.
            pass
        else:
            self._confirm_streak = 0
            self._reclaim_armed = False

        meta["reclaim_armed"] = self._reclaim_armed
        meta["confirm_streak"] = self._confirm_streak
        triggered = (
            self._reclaim_armed
            and self._confirm_streak >= int(self.ema_confirmation_closes)
        )
        return SignalDecision(
            triggered=triggered,
            signal_name=self.name,
            reference_price=ema20,
            invalidation_price=ema20,
            reason="ema_reclaim" if triggered else "waiting_ema_reclaim",
            metadata=meta,
        )

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None:
        self._confirm_streak = 0
        self._reclaim_armed = False
        self._condition_lost_since_unlock = False

    def invalidation(self, ctx: SignalContext) -> SignalDecision:
        fast_s, slow_s = self._series(ctx)
        i = ctx.index
        if i < 1 or slow_s[i] is None or slow_s[i - 1] is None:
            return SignalDecision(False, self.name, None, None, "ema_warmup")
        ema20 = float(slow_s[i])
        close = float(ctx.candles[i]["close"])
        prev_close = float(ctx.candles[i - 1]["close"])
        # Two closes below EMA20
        hit = prev_close < float(slow_s[i - 1]) and close < ema20
        return SignalDecision(
            hit, self.name, ema20, ema20,
            "two_closes_below_ema20" if hit else "hold",
            {"ema_20": ema20},
        )


@dataclass
class SwingBreakWithEmaSignal:
    """Swing close-break plus causal EMA filter (close > EMA20, EMA9 >= EMA20)."""

    name: str = "swing_break_with_ema"
    swing: SwingHighBreakSignal = field(default_factory=SwingHighBreakSignal)
    ema_fast: int = 9
    ema_slow: int = 20

    def __post_init__(self) -> None:
        self.swing.name = self.name

    def reset(self) -> None:
        self.swing.reset()

    def evaluate(self, ctx: SignalContext) -> SignalDecision:
        s = self.swing.evaluate(ctx)
        closes = [float(c["close"]) for c in ctx.candles]
        fast_s = causal_ema_series(closes, self.ema_fast)
        slow_s = causal_ema_series(closes, self.ema_slow)
        i = ctx.index
        close = float(ctx.candles[i]["close"])
        ema9 = fast_s[i]
        ema20 = slow_s[i]
        fast_above = (
            ema9 is not None and ema20 is not None and float(ema9) >= float(ema20)
        )
        ema_ok = ema20 is not None and close > float(ema20) and fast_above
        meta = {
            **s.metadata,
            "ema_9": ema9,
            "ema_20": ema20,
            "ema_fast_above_slow": fast_above,
        }
        triggered = bool(s.triggered) and ema_ok
        if s.triggered and not ema_ok:
            # Do not consume swing progress when EMA filter blocks.
            self.swing._break_streak = max(0, self.swing._break_streak - 1)
            self.swing._pending_swing = None
            triggered = False
        ref = s.reference_price
        if ref is None and ema20 is not None:
            ref = float(ema20)
        return SignalDecision(
            triggered=triggered,
            signal_name=self.name,
            reference_price=ref,
            invalidation_price=s.invalidation_price,
            reason="swing_break_with_ema" if triggered else "waiting_combo",
            metadata=meta,
        )

    def note_unlock(self, ctx: SignalContext, decision: SignalDecision) -> None:
        self.swing.note_unlock(ctx, decision)

    def invalidation(self, ctx: SignalContext) -> SignalDecision:
        return self.swing.invalidation(ctx)


def build_signal(name: str) -> Any:
    mapping = {
        "rebound_baseline": ReboundBaselineSignal,
        "swing_high_break": SwingHighBreakSignal,
        "swing_break_retest": SwingBreakRetestSignal,
        "ema_reclaim": EmaReclaimSignal,
        "swing_break_with_ema": SwingBreakWithEmaSignal,
    }
    if name not in mapping:
        raise ValueError(f"unknown Phase D signal: {name}")
    return mapping[name]()


PHASE_D_TRADABLE_SIGNALS = (
    "full_lock_control",
    "rebound_baseline",
    "swing_high_break",
    "swing_break_retest",
    "ema_reclaim",
    "swing_break_with_ema",
)

# C3.5 protected-structure adapter intentionally omitted: existing research modules
# are fill-audit pipelines without a clean causal read-only bool signal API.
PROTECTED_STRUCTURE_ADAPTER_AVAILABLE = False
PROTECTED_STRUCTURE_ADAPTER_SKIP_REASON = (
    "C3.5 modules under research/regime_scanner are path/fill audit pipelines "
    "without a stable causal unlock SignalDecision interface; skipping rather "
    "than copying unverifiable parity logic."
)
