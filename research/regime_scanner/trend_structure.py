"""Global causal market-structure layer (research-only).

Setup-independent. Reuses ``swings`` + ``structure.classify_swing_structure``.
Does not mutate price-action / momentum / pipeline state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

import pandas as pd

from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.swings import ConfirmedPivot, filter_pivots_as_of, latest_pivots

StructureBias = Literal["bullish", "bearish", "neutral", "unknown"]
SwingLabel = Literal[
    "higher_high",
    "lower_high",
    "equal_high",
    "higher_low",
    "lower_low",
    "equal_low",
]


@dataclass(frozen=True)
class TrendStructureConfig:
    """Research start values — not fitted to any calendar window."""

    epsilon_pct: float = 0.01
    bos_close_buffer_atr: float = 0.0
    breakout_tolerance_pct: float = 0.0
    failed_return_max_bars: int = 3
    valid_break_hold_bars: int = 2
    retest_max_bars: int = 8
    retest_atr_tol_mult: float = 0.05
    history_limit: int = 64

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_trend_structure_config() -> TrendStructureConfig:
    return TrendStructureConfig()


@dataclass(frozen=True)
class StructureEvent:
    event_type: str
    timeframe: str
    event_time: pd.Timestamp
    level: float | None
    reference_pivot_time: pd.Timestamp | None
    reference_pivot_price: float | None
    direction: str | None
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timeframe": self.timeframe,
            "event_time": _iso(self.event_time),
            "level": self.level,
            "reference_pivot_time": (
                None if self.reference_pivot_time is None else _iso(self.reference_pivot_time)
            ),
            "reference_pivot_price": self.reference_pivot_price,
            "direction": self.direction,
            "reason_codes": list(self.reason_codes),
        }


@dataclass
class MarketStructureState:
    """Mutable per-timeframe structure context (reproducible from candles)."""

    timeframe: str = "5m"
    last_confirmed_swing_high: ConfirmedPivot | None = None
    last_confirmed_swing_low: ConfirmedPivot | None = None
    previous_confirmed_swing_high: ConfirmedPivot | None = None
    previous_confirmed_swing_low: ConfirmedPivot | None = None
    last_higher_high: ConfirmedPivot | None = None
    last_higher_low: ConfirmedPivot | None = None
    last_lower_high: ConfirmedPivot | None = None
    last_lower_low: ConfirmedPivot | None = None
    last_high_label: SwingLabel | None = None
    last_low_label: SwingLabel | None = None
    current_structure_bias: StructureBias = "unknown"
    last_bos: StructureEvent | None = None
    last_choch: StructureEvent | None = None
    last_failed_breakout: StructureEvent | None = None
    last_failed_breakdown: StructureEvent | None = None
    active_break_level: float | None = None
    active_retest_level: float | None = None
    active_retest_direction: str | None = None
    retest_bars_remaining: int = 0
    structure_confidence: float = 0.0
    last_updated_at: pd.Timestamp | None = None
    # Failed-break windows
    pending_breakout_level: float | None = None
    pending_breakout_bars_left: int = 0
    pending_breakout_beyond_closes: int = 0
    pending_breakdown_level: float | None = None
    pending_breakdown_bars_left: int = 0
    pending_breakdown_beyond_closes: int = 0
    last_broken_low_level: float | None = None
    last_broken_high_level: float | None = None
    prior_close: float | None = None
    recent_events: list[StructureEvent] = field(default_factory=list)
    known_high_confirm_keys: set[str] = field(default_factory=set)
    known_low_confirm_keys: set[str] = field(default_factory=set)
    # V6+V2 hybrid protective levels (sticky continued HL/LH only)
    protective_low_level: float | None = None
    protective_low_pivot: ConfirmedPivot | None = None
    protective_low_set_at: pd.Timestamp | None = None
    pending_protective_low_pivot: ConfirmedPivot | None = None
    last_continued_low_pivot: ConfirmedPivot | None = None
    protective_high_level: float | None = None
    protective_high_pivot: ConfirmedPivot | None = None
    protective_high_set_at: pd.Timestamp | None = None
    pending_protective_high_pivot: ConfirmedPivot | None = None
    last_continued_high_pivot: ConfirmedPivot | None = None

    def to_dict(self) -> dict[str, Any]:
        def _p(p: ConfirmedPivot | None) -> dict[str, Any] | None:
            return None if p is None else p.to_dict()

        return {
            "timeframe": self.timeframe,
            "last_confirmed_swing_high": _p(self.last_confirmed_swing_high),
            "last_confirmed_swing_low": _p(self.last_confirmed_swing_low),
            "previous_confirmed_swing_high": _p(self.previous_confirmed_swing_high),
            "previous_confirmed_swing_low": _p(self.previous_confirmed_swing_low),
            "last_higher_high": _p(self.last_higher_high),
            "last_higher_low": _p(self.last_higher_low),
            "last_lower_high": _p(self.last_lower_high),
            "last_lower_low": _p(self.last_lower_low),
            "last_high_label": self.last_high_label,
            "last_low_label": self.last_low_label,
            "current_structure_bias": self.current_structure_bias,
            "last_bos": None if self.last_bos is None else self.last_bos.to_dict(),
            "last_choch": None if self.last_choch is None else self.last_choch.to_dict(),
            "last_failed_breakout": (
                None
                if self.last_failed_breakout is None
                else self.last_failed_breakout.to_dict()
            ),
            "last_failed_breakdown": (
                None
                if self.last_failed_breakdown is None
                else self.last_failed_breakdown.to_dict()
            ),
            "active_break_level": self.active_break_level,
            "active_retest_level": self.active_retest_level,
            "active_retest_direction": self.active_retest_direction,
            "retest_bars_remaining": self.retest_bars_remaining,
            "structure_confidence": self.structure_confidence,
            "last_updated_at": None if self.last_updated_at is None else _iso(self.last_updated_at),
            "recent_event_types": [e.event_type for e in self.recent_events[-8:]],
            "protective_low_level": self.protective_low_level,
            "protective_low_pivot": _p(self.protective_low_pivot),
            "protective_low_set_at": (
                None if self.protective_low_set_at is None else _iso(self.protective_low_set_at)
            ),
            "protective_high_level": self.protective_high_level,
            "protective_high_pivot": _p(self.protective_high_pivot),
            "protective_high_set_at": (
                None if self.protective_high_set_at is None else _iso(self.protective_high_set_at)
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bias": self.current_structure_bias,
            "last_high_label": self.last_high_label,
            "last_low_label": self.last_low_label,
            "confidence": self.structure_confidence,
            "active_break_level": self.active_break_level,
            "active_retest_direction": self.active_retest_direction,
            "last_bos": None if self.last_bos is None else self.last_bos.event_type,
            "last_choch": None if self.last_choch is None else self.last_choch.event_type,
        }


def _iso(value: object) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _finite(value: object) -> float | None:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def _pivot_key(pivot: ConfirmedPivot) -> str:
    return f"{pivot.pivot_type}:{pivot.confirmation_index}:{pivot.pivot_index}:{pivot.price}"


def _tol(level: float, tolerance_pct: float) -> float:
    return abs(float(level)) * float(tolerance_pct) / 100.0


def _event(
    *,
    event_type: str,
    timeframe: str,
    event_time: pd.Timestamp,
    level: float | None = None,
    pivot: ConfirmedPivot | None = None,
    direction: str | None = None,
    reason_codes: tuple[str, ...] = (),
) -> StructureEvent:
    return StructureEvent(
        event_type=event_type,
        timeframe=timeframe,
        event_time=_ts(event_time),
        level=level,
        reference_pivot_time=None if pivot is None else _ts(pivot.pivot_timestamp),
        reference_pivot_price=None if pivot is None else float(pivot.price),
        direction=direction,
        reason_codes=reason_codes,
    )


def derive_structure_bias(
    last_high_label: SwingLabel | None,
    last_low_label: SwingLabel | None,
) -> StructureBias:
    if last_high_label is None and last_low_label is None:
        return "unknown"
    bullish = 0
    bearish = 0
    if last_high_label == "higher_high":
        bullish += 1
    elif last_high_label == "lower_high":
        bearish += 1
    if last_low_label == "higher_low":
        bullish += 1
    elif last_low_label == "lower_low":
        bearish += 1
    if bullish >= 2:
        return "bullish"
    if bearish >= 2:
        return "bearish"
    if bullish == 0 and bearish == 0:
        return "neutral"
    return "neutral"


def structure_confidence_from_state(state: MarketStructureState) -> float:
    score = 0.0
    if state.last_high_label in {"higher_high", "lower_high"}:
        score += 0.25
    if state.last_low_label in {"higher_low", "lower_low"}:
        score += 0.25
    if state.current_structure_bias in {"bullish", "bearish"}:
        score += 0.25
    if state.last_bos is not None or state.last_choch is not None:
        score += 0.15
    if state.active_retest_direction is not None:
        score += 0.10
    return min(1.0, score)


def _remember_event(
    state: MarketStructureState,
    event: StructureEvent,
    *,
    cfg: TrendStructureConfig,
) -> None:
    state.recent_events.append(event)
    if len(state.recent_events) > int(cfg.history_limit):
        state.recent_events = state.recent_events[-int(cfg.history_limit) :]


def _arm_retest(
    state: MarketStructureState,
    *,
    level: float,
    direction: str,
    cfg: TrendStructureConfig,
) -> None:
    state.active_break_level = float(level)
    state.active_retest_level = float(level)
    state.active_retest_direction = direction
    state.retest_bars_remaining = int(cfg.retest_max_bars)


def _same_pivot(a: ConfirmedPivot | None, b: ConfirmedPivot | None) -> bool:
    if a is None or b is None:
        return False
    return (
        a.pivot_type == b.pivot_type
        and int(a.confirmation_index) == int(b.confirmation_index)
        and int(a.pivot_index) == int(b.pivot_index)
        and float(a.price) == float(b.price)
    )


def _level_matches(broken: float | None, level: float | None) -> bool:
    """Same equality semantics as existing last_broken_* checks (exact float)."""
    if broken is None or level is None:
        return False
    return float(broken) == float(level)


def _set_protective_low(
    state: MarketStructureState,
    pivot: ConfirmedPivot,
    *,
    event_time: pd.Timestamp,
) -> None:
    state.protective_low_level = float(pivot.price)
    state.protective_low_pivot = pivot
    state.protective_low_set_at = _ts(event_time)


def _set_protective_high(
    state: MarketStructureState,
    pivot: ConfirmedPivot,
    *,
    event_time: pd.Timestamp,
) -> None:
    state.protective_high_level = float(pivot.price)
    state.protective_high_pivot = pivot
    state.protective_high_set_at = _ts(event_time)


def _clear_protective_low(state: MarketStructureState) -> None:
    state.protective_low_level = None
    state.protective_low_pivot = None
    state.protective_low_set_at = None


def _clear_protective_high(state: MarketStructureState) -> None:
    state.protective_high_level = None
    state.protective_high_pivot = None
    state.protective_high_set_at = None


def _refresh_protective_levels(
    state: MarketStructureState,
    *,
    event_time: pd.Timestamp,
) -> None:
    """V6+V2: sticky continued HL/LH only; clear on prior-bar break; no unconfirmed fallback.

    Call after swing labels (pending/continued updated) and before BOS/CHoCH.
    Invalidation uses last_broken_* from a *previous* candle's break.
    """
    # --- Low side ---
    if state.protective_low_level is not None and _level_matches(
        state.last_broken_low_level, state.protective_low_level
    ):
        broken = state.protective_low_pivot
        _clear_protective_low(state)
        if _same_pivot(broken, state.last_continued_low_pivot):
            state.last_continued_low_pivot = None
        if _same_pivot(broken, state.pending_protective_low_pivot):
            state.pending_protective_low_pivot = None

    cont_low = state.last_continued_low_pivot
    if cont_low is not None:
        active = state.protective_low_pivot
        if active is None:
            _set_protective_low(state, cont_low, event_time=event_time)
        elif int(cont_low.confirmation_index) > int(active.confirmation_index):
            _set_protective_low(state, cont_low, event_time=event_time)

    # --- High side (mirror) ---
    if state.protective_high_level is not None and _level_matches(
        state.last_broken_high_level, state.protective_high_level
    ):
        broken_h = state.protective_high_pivot
        _clear_protective_high(state)
        if _same_pivot(broken_h, state.last_continued_high_pivot):
            state.last_continued_high_pivot = None
        if _same_pivot(broken_h, state.pending_protective_high_pivot):
            state.pending_protective_high_pivot = None

    cont_high = state.last_continued_high_pivot
    if cont_high is not None:
        active_h = state.protective_high_pivot
        if active_h is None:
            _set_protective_high(state, cont_high, event_time=event_time)
        elif int(cont_high.confirmation_index) > int(active_h.confirmation_index):
            _set_protective_high(state, cont_high, event_time=event_time)


def _protective_low(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
    """Active sticky continued HL only (V6+V2). No last_higher_low / swing-low fallback."""
    if state.protective_low_level is not None and state.protective_low_pivot is not None:
        return float(state.protective_low_level), state.protective_low_pivot
    return None, None


def _protective_high(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
    """Active sticky continued LH only (V6+V2). No last_lower_high / swing-high fallback."""
    if state.protective_high_level is not None and state.protective_high_pivot is not None:
        return float(state.protective_high_level), state.protective_high_pivot
    return None, None


def _apply_new_swing_labels(
    state: MarketStructureState,
    pivots: list[ConfirmedPivot],
    *,
    event_time: pd.Timestamp,
    cfg: TrendStructureConfig,
) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    highs = [p for p in pivots if p.pivot_type == "high"]
    lows = [p for p in pivots if p.pivot_type == "low"]

    for pivot in highs:
        key = _pivot_key(pivot)
        if key in state.known_high_confirm_keys:
            continue
        state.known_high_confirm_keys.add(key)
        prev = state.last_confirmed_swing_high
        state.previous_confirmed_swing_high = prev
        state.last_confirmed_swing_high = pivot
        if prev is None:
            continue
        pack = classify_swing_structure(
            {"price": prev.price},
            {"price": pivot.price},
            side="high",
            epsilon_pct=cfg.epsilon_pct,
        )
        label = str(pack["structure_type"])  # type: ignore[assignment]
        state.last_high_label = label  # type: ignore[assignment]
        if label == "higher_high":
            state.last_higher_high = pivot
        elif label == "lower_high":
            state.last_lower_high = pivot
            state.pending_protective_high_pivot = pivot
        ev = _event(
            event_type=label,
            timeframe=state.timeframe,
            event_time=event_time,
            level=float(pivot.price),
            pivot=pivot,
            direction="bullish" if label == "higher_high" else "bearish",
            reason_codes=("confirmed_swing_pair",),
        )
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)

    for pivot in lows:
        key = _pivot_key(pivot)
        if key in state.known_low_confirm_keys:
            continue
        state.known_low_confirm_keys.add(key)
        prev = state.last_confirmed_swing_low
        state.previous_confirmed_swing_low = prev
        state.last_confirmed_swing_low = pivot
        if prev is None:
            continue
        pack = classify_swing_structure(
            {"price": prev.price},
            {"price": pivot.price},
            side="low",
            epsilon_pct=cfg.epsilon_pct,
        )
        label = str(pack["structure_type"])  # type: ignore[assignment]
        state.last_low_label = label  # type: ignore[assignment]
        if label == "higher_low":
            state.last_higher_low = pivot
            state.pending_protective_low_pivot = pivot
        elif label == "lower_low":
            state.last_lower_low = pivot
        ev = _event(
            event_type=label,
            timeframe=state.timeframe,
            event_time=event_time,
            level=float(pivot.price),
            pivot=pivot,
            direction="bullish" if label == "higher_low" else "bearish",
            reason_codes=("confirmed_swing_pair",),
        )
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)

    state.current_structure_bias = derive_structure_bias(
        state.last_high_label, state.last_low_label
    )
    _resolve_protective_continuations(state)
    _refresh_protective_levels(state, event_time=event_time)
    return events


def _resolve_protective_continuations(state: MarketStructureState) -> None:
    """Mark pending HL/LH as continued when a later HH/LL is already confirmed.

    Runs after both high and low label passes so same-decision-candle HL+HH works
    even though highs are processed before lows.
    """
    hh = state.last_higher_high
    if hh is not None:
        cand = state.pending_protective_low_pivot
        if cand is None:
            cand = state.last_higher_low
        if cand is not None and int(cand.confirmation_index) < int(hh.confirmation_index):
            state.last_continued_low_pivot = cand
            if state.pending_protective_low_pivot is None:
                state.pending_protective_low_pivot = cand

    ll = state.last_lower_low
    if ll is not None:
        cand_h = state.pending_protective_high_pivot
        if cand_h is None:
            cand_h = state.last_lower_high
        if cand_h is not None and int(cand_h.confirmation_index) < int(ll.confirmation_index):
            state.last_continued_high_pivot = cand_h
            if state.pending_protective_high_pivot is None:
                state.pending_protective_high_pivot = cand_h


def _close_breaks_below(close: float, level: float, *, atr: float | None, cfg: TrendStructureConfig) -> bool:
    buf = 0.0
    if atr is not None and float(cfg.bos_close_buffer_atr) > 0:
        buf = float(atr) * float(cfg.bos_close_buffer_atr)
    return close < (level - buf)


def _close_breaks_above(close: float, level: float, *, atr: float | None, cfg: TrendStructureConfig) -> bool:
    buf = 0.0
    if atr is not None and float(cfg.bos_close_buffer_atr) > 0:
        buf = float(atr) * float(cfg.bos_close_buffer_atr)
    return close > (level + buf)


def _detect_bos_choch(
    state: MarketStructureState,
    *,
    close: float,
    high: float,
    low: float,
    prior_close: float | None,
    event_time: pd.Timestamp,
    atr: float | None,
    cfg: TrendStructureConfig,
) -> list[StructureEvent]:
    """Emit BOS/CHoCH only on a close *cross* of a confirmed level (not while already beyond)."""
    events: list[StructureEvent] = []
    bias = state.current_structure_bias

    prot_low, prot_low_pivot = _protective_low(state)
    prot_high, prot_high_pivot = _protective_high(state)

    # Wick-only tests (never BOS)
    if prot_low is not None and low < prot_low <= close:
        ev = _event(
            event_type="structure_test_low",
            timeframe=state.timeframe,
            event_time=event_time,
            level=prot_low,
            pivot=prot_low_pivot,
            direction="bearish",
            reason_codes=("wick_only",),
        )
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)
    if prot_high is not None and high > prot_high >= close:
        ev = _event(
            event_type="structure_test_high",
            timeframe=state.timeframe,
            event_time=event_time,
            level=prot_high,
            pivot=prot_high_pivot,
            direction="bullish",
            reason_codes=("wick_only",),
        )
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)

    crossed_below = (
        prot_low is not None
        and prior_close is not None
        and prior_close >= prot_low
        and _close_breaks_below(close, prot_low, atr=atr, cfg=cfg)
        and state.last_broken_low_level != prot_low
    )
    if crossed_below and prot_low is not None:
        if bias in {"bullish", "neutral", "unknown"}:
            ev = _event(
                event_type="bearish_choch",
                timeframe=state.timeframe,
                event_time=event_time,
                level=prot_low,
                pivot=prot_low_pivot,
                direction="bearish",
                reason_codes=("close_break", "first_counter_break"),
            )
            state.last_choch = ev
            _arm_retest(state, level=prot_low, direction="bearish", cfg=cfg)
        else:
            ev = _event(
                event_type="bearish_bos",
                timeframe=state.timeframe,
                event_time=event_time,
                level=prot_low,
                pivot=prot_low_pivot,
                direction="bearish",
                reason_codes=("close_break", "trend_continuation"),
            )
            state.last_bos = ev
            _arm_retest(state, level=prot_low, direction="bearish", cfg=cfg)
        state.last_broken_low_level = float(prot_low)
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)

    crossed_above = (
        prot_high is not None
        and prior_close is not None
        and prior_close <= prot_high
        and _close_breaks_above(close, prot_high, atr=atr, cfg=cfg)
        and state.last_broken_high_level != prot_high
    )
    if crossed_above and prot_high is not None:
        if bias in {"bearish", "neutral", "unknown"}:
            ev = _event(
                event_type="bullish_choch",
                timeframe=state.timeframe,
                event_time=event_time,
                level=prot_high,
                pivot=prot_high_pivot,
                direction="bullish",
                reason_codes=("close_break", "first_counter_break"),
            )
            state.last_choch = ev
            _arm_retest(state, level=prot_high, direction="bullish", cfg=cfg)
        else:
            ev = _event(
                event_type="bullish_bos",
                timeframe=state.timeframe,
                event_time=event_time,
                level=prot_high,
                pivot=prot_high_pivot,
                direction="bullish",
                reason_codes=("close_break", "trend_continuation"),
            )
            state.last_bos = ev
            _arm_retest(state, level=prot_high, direction="bullish", cfg=cfg)
        state.last_broken_high_level = float(prot_high)
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)

    return events


def _detect_failed_breaks(
    state: MarketStructureState,
    *,
    close: float,
    high: float,
    low: float,
    event_time: pd.Timestamp,
    atr: float | None,
    cfg: TrendStructureConfig,
) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    ref_high = state.last_confirmed_swing_high
    ref_low = state.last_confirmed_swing_low

    # Arm / track breakout failure vs last swing high
    if ref_high is not None:
        level = float(ref_high.price)
        tol = _tol(level, cfg.breakout_tolerance_pct)
        if state.pending_breakout_level is None:
            if high > level or close > level:
                if close <= level:  # wick-only liquidity sweep candidate
                    ev = _event(
                        event_type="liquidity_sweep_high",
                        timeframe=state.timeframe,
                        event_time=event_time,
                        level=level,
                        pivot=ref_high,
                        direction="bearish",
                        reason_codes=("wick_beyond_close_inside",),
                    )
                    events.append(ev)
                    _remember_event(state, ev, cfg=cfg)
                state.pending_breakout_level = level
                state.pending_breakout_bars_left = int(cfg.failed_return_max_bars)
                state.pending_breakout_beyond_closes = 1 if close > level else 0
        else:
            state.pending_breakout_bars_left -= 1
            pend = float(state.pending_breakout_level)
            if close > pend:
                state.pending_breakout_beyond_closes += 1
            if close < pend - tol:
                if state.pending_breakout_beyond_closes < int(cfg.valid_break_hold_bars):
                    ev = _event(
                        event_type="failed_breakout",
                        timeframe=state.timeframe,
                        event_time=event_time,
                        level=pend,
                        pivot=ref_high,
                        direction="bearish",
                        reason_codes=("close_return_inside",),
                    )
                    state.last_failed_breakout = ev
                    events.append(ev)
                    _remember_event(state, ev, cfg=cfg)
                state.pending_breakout_level = None
                state.pending_breakout_bars_left = 0
                state.pending_breakout_beyond_closes = 0
            elif state.pending_breakout_bars_left <= 0:
                state.pending_breakout_level = None
                state.pending_breakout_bars_left = 0
                state.pending_breakout_beyond_closes = 0

    if ref_low is not None:
        level = float(ref_low.price)
        tol = _tol(level, cfg.breakout_tolerance_pct)
        if state.pending_breakdown_level is None:
            if low < level or close < level:
                if close >= level:
                    ev = _event(
                        event_type="liquidity_sweep_low",
                        timeframe=state.timeframe,
                        event_time=event_time,
                        level=level,
                        pivot=ref_low,
                        direction="bullish",
                        reason_codes=("wick_beyond_close_inside",),
                    )
                    events.append(ev)
                    _remember_event(state, ev, cfg=cfg)
                state.pending_breakdown_level = level
                state.pending_breakdown_bars_left = int(cfg.failed_return_max_bars)
                state.pending_breakdown_beyond_closes = 1 if close < level else 0
        else:
            state.pending_breakdown_bars_left -= 1
            pend = float(state.pending_breakdown_level)
            if close < pend:
                state.pending_breakdown_beyond_closes += 1
            if close > pend + tol:
                if state.pending_breakdown_beyond_closes < int(cfg.valid_break_hold_bars):
                    ev = _event(
                        event_type="failed_breakdown",
                        timeframe=state.timeframe,
                        event_time=event_time,
                        level=pend,
                        pivot=ref_low,
                        direction="bullish",
                        reason_codes=("close_return_inside",),
                    )
                    state.last_failed_breakdown = ev
                    events.append(ev)
                    _remember_event(state, ev, cfg=cfg)
                state.pending_breakdown_level = None
                state.pending_breakdown_bars_left = 0
                state.pending_breakdown_beyond_closes = 0
            elif state.pending_breakdown_bars_left <= 0:
                state.pending_breakdown_level = None
                state.pending_breakdown_bars_left = 0
                state.pending_breakdown_beyond_closes = 0

    return events


def _detect_retest(
    state: MarketStructureState,
    *,
    close: float,
    high: float,
    low: float,
    event_time: pd.Timestamp,
    atr: float | None,
    cfg: TrendStructureConfig,
) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    if state.active_retest_level is None or state.active_retest_direction is None:
        return events
    if state.retest_bars_remaining <= 0:
        ev = _event(
            event_type="retest_expired",
            timeframe=state.timeframe,
            event_time=event_time,
            level=state.active_retest_level,
            direction=state.active_retest_direction,
            reason_codes=("window_elapsed",),
        )
        events.append(ev)
        _remember_event(state, ev, cfg=cfg)
        state.active_retest_level = None
        state.active_retest_direction = None
        state.retest_bars_remaining = 0
        return events

    state.retest_bars_remaining -= 1
    level = float(state.active_retest_level)
    atr_tol = 0.0 if atr is None else float(atr) * float(cfg.retest_atr_tol_mult)
    eps = abs(level) * float(cfg.epsilon_pct) / 100.0
    tol = max(eps, atr_tol)
    direction = state.active_retest_direction

    touched = (low <= level + tol) and (high >= level - tol)
    if not touched:
        if state.retest_bars_remaining <= 0:
            ev = _event(
                event_type="retest_expired",
                timeframe=state.timeframe,
                event_time=event_time,
                level=level,
                direction=direction,
                reason_codes=("no_touch",),
            )
            events.append(ev)
            _remember_event(state, ev, cfg=cfg)
            state.active_retest_level = None
            state.active_retest_direction = None
        return events

    if direction == "bearish":
        if close <= level + tol:
            ev = _event(
                event_type="bearish_retest_holds",
                timeframe=state.timeframe,
                event_time=event_time,
                level=level,
                direction="bearish",
                reason_codes=("close_stays_below",),
            )
        else:
            ev = _event(
                event_type="bearish_retest_fails",
                timeframe=state.timeframe,
                event_time=event_time,
                level=level,
                direction="bullish",
                reason_codes=("close_back_above",),
            )
    else:
        if close >= level - tol:
            ev = _event(
                event_type="bullish_retest_holds",
                timeframe=state.timeframe,
                event_time=event_time,
                level=level,
                direction="bullish",
                reason_codes=("close_stays_above",),
            )
        else:
            ev = _event(
                event_type="bullish_retest_fails",
                timeframe=state.timeframe,
                event_time=event_time,
                level=level,
                direction="bearish",
                reason_codes=("close_back_below",),
            )
    events.append(ev)
    _remember_event(state, ev, cfg=cfg)
    state.active_retest_level = None
    state.active_retest_direction = None
    state.retest_bars_remaining = 0
    return events


def update_market_structure(
    state: MarketStructureState,
    *,
    candle: dict[str, Any] | pd.Series,
    pivots: list[ConfirmedPivot],
    decision_time: object,
    atr: float | None = None,
    cfg: TrendStructureConfig | None = None,
) -> tuple[MarketStructureState, list[StructureEvent]]:
    """Advance structure one closed candle. Pivots must already be causal as-of decision_time."""
    config = cfg or default_trend_structure_config()
    decision_ts = _ts(decision_time)
    as_of = filter_pivots_as_of(pivots, decision_ts)
    row = candle if isinstance(candle, dict) else candle.to_dict()
    close = _finite(row.get("close"))
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    if close is None or high is None or low is None:
        state.last_updated_at = decision_ts
        return state, []

    # Prefer candle close time (= decision_time for closed 5m bar)
    event_time = decision_ts
    events: list[StructureEvent] = []
    events.extend(
        _apply_new_swing_labels(state, as_of, event_time=event_time, cfg=config)
    )
    events.extend(
        _detect_bos_choch(
            state,
            close=close,
            high=high,
            low=low,
            prior_close=state.prior_close,
            event_time=event_time,
            atr=atr,
            cfg=config,
        )
    )
    events.extend(
        _detect_failed_breaks(
            state,
            close=close,
            high=high,
            low=low,
            event_time=event_time,
            atr=atr,
            cfg=config,
        )
    )
    events.extend(
        _detect_retest(
            state,
            close=close,
            high=high,
            low=low,
            event_time=event_time,
            atr=atr,
            cfg=config,
        )
    )
    state.structure_confidence = structure_confidence_from_state(state)
    state.last_updated_at = decision_ts
    state.prior_close = float(close)
    return state, events


def events_of_types(
    events: list[StructureEvent],
    *types: str,
) -> list[StructureEvent]:
    wanted = set(types)
    return [e for e in events if e.event_type in wanted]


def has_lh_ll(state: MarketStructureState) -> bool:
    return state.last_high_label == "lower_high" and state.last_low_label == "lower_low"


def has_hh_hl(state: MarketStructureState) -> bool:
    return state.last_high_label == "higher_high" and state.last_low_label == "higher_low"


def copy_structure_state(state: MarketStructureState) -> MarketStructureState:
    """Shallow-safe copy for replay isolation."""
    return replace(
        state,
        recent_events=list(state.recent_events),
        known_high_confirm_keys=set(state.known_high_confirm_keys),
        known_low_confirm_keys=set(state.known_low_confirm_keys),
    )


__all__ = [
    "TrendStructureConfig",
    "default_trend_structure_config",
    "StructureEvent",
    "MarketStructureState",
    "derive_structure_bias",
    "update_market_structure",
    "events_of_types",
    "has_lh_ll",
    "has_hh_hl",
    "copy_structure_state",
    "filter_pivots_as_of",
    "latest_pivots",
]
