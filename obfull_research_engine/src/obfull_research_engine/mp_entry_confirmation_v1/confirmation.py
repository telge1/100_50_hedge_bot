"""Causal price-structure confirmation after alert_ts (FAILED_BREAK / ABSORB / TRUE_BREAK)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .candles5m import Candle5m, first_allowed_5m_open_after_alert, post_alert_5m
from .params import (
    CONFIRMATION_TIMEOUT_MINUTES,
    NS,
    REQUIRED_RECLAIM_CLOSES,
    RETEST_TOLERANCE_PCT,
    STRUCTURE_LOOKBACK_BARS,
)


def _timeout_ns(alert_ts_ns: int, timeout_minutes: int = CONFIRMATION_TIMEOUT_MINUTES) -> int:
    return int(alert_ts_ns) + int(timeout_minutes) * 60 * NS


def _within_timeout(close_time_ns: int, alert_ts_ns: int, timeout_minutes: int) -> bool:
    return close_time_ns <= _timeout_ns(alert_ts_ns, timeout_minutes)


def structure_break_long(
    bars: Sequence[Candle5m],
    idx: int,
    *,
    lookback: int = STRUCTURE_LOOKBACK_BARS,
) -> bool:
    """Close of bars[idx] above max high of previous `lookback` completed bars."""
    if idx < lookback:
        return False
    prev = bars[idx - lookback : idx]
    level = max(b.high for b in prev)
    return bars[idx].close > level + 1e-15


def structure_break_short(
    bars: Sequence[Candle5m],
    idx: int,
    *,
    lookback: int = STRUCTURE_LOOKBACK_BARS,
) -> bool:
    if idx < lookback:
        return False
    prev = bars[idx - lookback : idx]
    level = min(b.low for b in prev)
    return bars[idx].close < level - 1e-15


@dataclass
class ConfirmationResult:
    confirmed: bool
    reason: str
    confirmation_ts: int | None = None
    confirmation_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    sequence_events: list[dict[str, Any]] = field(default_factory=list)


def confirm_failed_break(
    candles_5m: Sequence[Candle5m],
    *,
    alert_ts_ns: int,
    trade_side: str,
    confluence_low: float,
    confluence_high: float,
    timeout_minutes: int = CONFIRMATION_TIMEOUT_MINUTES,
    lookback: int = STRUCTURE_LOOKBACK_BARS,
) -> ConfirmationResult:
    """FAILED_BREAK: prior penetration assumed by label; wait for 2 reclaim closes + structure."""
    bars = post_alert_5m(candles_5m, alert_ts_ns=alert_ts_ns)
    side = trade_side.upper()
    events: list[dict[str, Any]] = []
    first_reclaim = None
    second_reclaim = None
    invalidation = False
    reset_count = 0
    streak = 0

    for i, b in enumerate(bars):
        if not _within_timeout(b.close_time_ns, alert_ts_ns, timeout_minutes):
            break
        if side == "LONG":
            reclaim = b.close > confluence_high + 1e-15
            invalidate = b.close < confluence_low - 1e-15
        else:
            reclaim = b.close < confluence_low - 1e-15
            invalidate = b.close > confluence_high + 1e-15

        if first_reclaim is not None and invalidate:
            events.append({"event": "invalidation_reset", "ts_ns": b.close_time_ns, "bar_open_ns": b.open_time_ns})
            first_reclaim = None
            second_reclaim = None
            streak = 0
            invalidation = True
            reset_count += 1
            continue

        if reclaim:
            streak += 1
            if first_reclaim is None:
                first_reclaim = b.close_time_ns
                events.append({"event": "first_reclaim_close", "ts_ns": b.close_time_ns})
            elif second_reclaim is None and streak >= REQUIRED_RECLAIM_CLOSES:
                # require consecutive
                second_reclaim = b.close_time_ns
                events.append({"event": "second_reclaim_close", "ts_ns": b.close_time_ns})
        else:
            # broke consecutive reclaim streak before second
            if second_reclaim is None:
                if streak > 0:
                    streak = 0
                    first_reclaim = None
                    reset_count += 1
                    events.append({"event": "reclaim_streak_reset", "ts_ns": b.close_time_ns})

        if second_reclaim is not None:
            if side == "LONG":
                ok = structure_break_long(bars, i, lookback=lookback)
            else:
                ok = structure_break_short(bars, i, lookback=lookback)
            if ok:
                delay_min = (b.close_time_ns - alert_ts_ns) / (60 * NS)
                return ConfirmationResult(
                    confirmed=True,
                    reason="CONFIRMED",
                    confirmation_ts=b.close_time_ns,
                    confirmation_type="FAILED_BREAK_STRUCTURE",
                    details={
                        "first_reclaim_close_ts": first_reclaim,
                        "second_reclaim_close_ts": second_reclaim,
                        "structure_confirmation_ts": b.close_time_ns,
                        "invalidation_before_confirmation": invalidation,
                        "confirmation_delay_minutes": delay_min,
                        "confirmation_reset_count": reset_count,
                    },
                    sequence_events=events
                    + [{"event": "structure_confirmation", "ts_ns": b.close_time_ns}],
                )

    return ConfirmationResult(
        confirmed=False,
        reason="NO_CONFIRMED_ENTRY",
        details={
            "first_reclaim_close_ts": first_reclaim,
            "second_reclaim_close_ts": second_reclaim,
            "structure_confirmation_ts": None,
            "invalidation_before_confirmation": invalidation,
            "confirmation_delay_minutes": None,
            "confirmation_reset_count": reset_count,
        },
        sequence_events=events,
    )


def confirm_absorb(
    candles_5m: Sequence[Candle5m],
    *,
    alert_ts_ns: int,
    trade_side: str,
    confluence_low: float,
    confluence_high: float,
    timeout_minutes: int = CONFIRMATION_TIMEOUT_MINUTES,
    lookback: int = STRUCTURE_LOOKBACK_BARS,
) -> ConfirmationResult:
    """ABSORB is never a direct entry: zone holds + structure, with reset on adverse close."""
    bars = post_alert_5m(candles_5m, alert_ts_ns=alert_ts_ns)
    side = trade_side.upper()
    events: list[dict[str, Any]] = []
    first_hold = None
    second_hold = None
    reset_count = 0
    streak = 0
    price_sweep = False
    seq_started = False

    for i, b in enumerate(bars):
        if not _within_timeout(b.close_time_ns, alert_ts_ns, timeout_minutes):
            break
        if side == "LONG":
            hold = b.close > confluence_high + 1e-15
            reset = b.close < confluence_low - 1e-15
            sweep = b.low < confluence_low - 1e-15
        else:
            hold = b.close < confluence_low - 1e-15
            reset = b.close > confluence_high + 1e-15
            sweep = b.high > confluence_high + 1e-15

        if sweep:
            price_sweep = True

        if seq_started and reset:
            events.append({"event": "sequence_reset", "ts_ns": b.close_time_ns})
            first_hold = None
            second_hold = None
            streak = 0
            seq_started = False
            reset_count += 1
            continue

        if hold:
            streak += 1
            seq_started = True
            if first_hold is None:
                first_hold = b.close_time_ns
                events.append({"event": "first_zone_hold", "ts_ns": b.close_time_ns})
            elif second_hold is None and streak >= REQUIRED_RECLAIM_CLOSES:
                second_hold = b.close_time_ns
                events.append({"event": "second_zone_hold", "ts_ns": b.close_time_ns})
        else:
            if second_hold is None and streak > 0:
                streak = 0
                first_hold = None
                seq_started = False
                reset_count += 1
                events.append({"event": "hold_streak_reset", "ts_ns": b.close_time_ns})

        if second_hold is not None:
            ok = (
                structure_break_long(bars, i, lookback=lookback)
                if side == "LONG"
                else structure_break_short(bars, i, lookback=lookback)
            )
            if ok:
                delay_min = (b.close_time_ns - alert_ts_ns) / (60 * NS)
                return ConfirmationResult(
                    confirmed=True,
                    reason="CONFIRMED",
                    confirmation_ts=b.close_time_ns,
                    confirmation_type="ABSORB_STRUCTURE",
                    details={
                        "sequence_reset_count": reset_count,
                        "first_zone_hold_ts": first_hold,
                        "second_zone_hold_ts": second_hold,
                        "structure_confirmation_ts": b.close_time_ns,
                        "price_sweep_after_alert": price_sweep,
                        "confirmation_delay_minutes": delay_min,
                        "confirmation_reset_count": reset_count,
                    },
                    sequence_events=events
                    + [{"event": "structure_confirmation", "ts_ns": b.close_time_ns}],
                )

    return ConfirmationResult(
        confirmed=False,
        reason="NO_CONFIRMED_ENTRY",
        details={
            "sequence_reset_count": reset_count,
            "first_zone_hold_ts": first_hold,
            "second_zone_hold_ts": second_hold,
            "structure_confirmation_ts": None,
            "price_sweep_after_alert": price_sweep,
            "confirmation_delay_minutes": None,
            "confirmation_reset_count": reset_count,
        },
        sequence_events=events,
    )


def confirm_true_break(
    candles_5m: Sequence[Candle5m],
    *,
    alert_ts_ns: int,
    trade_side: str,
    confluence_low: float,
    confluence_high: float,
    timeout_minutes: int = CONFIRMATION_TIMEOUT_MINUTES,
    lookback: int = STRUCTURE_LOOKBACK_BARS,
    retest_tolerance_pct: float = RETEST_TOLERANCE_PCT,
) -> ConfirmationResult:
    """TRUE_BREAK: acceptance → retest → hold → continuation structure."""
    bars = post_alert_5m(candles_5m, alert_ts_ns=alert_ts_ns)
    side = trade_side.upper()
    events: list[dict[str, Any]] = []
    acceptance_ts = None
    retest_ts = None
    retest_price = None
    retest_depth_pct = None
    retest_hold_ts = None
    reset_count = 0
    accept_streak = 0
    state = "WAIT_ACCEPTANCE"
    saw_retest_invalidation = False
    last_acceptance_ts = None
    last_retest_ts = None
    last_retest_price = None
    last_retest_depth = None
    last_retest_hold = None

    def _reset_sequence(*, reason: str, ts_ns: int) -> None:
        nonlocal acceptance_ts, retest_ts, retest_price, retest_depth_pct
        nonlocal retest_hold_ts, accept_streak, state, reset_count, saw_retest_invalidation
        events.append({"event": reason, "ts_ns": ts_ns})
        if reason == "retest_invalidated":
            saw_retest_invalidation = True
        reset_count += 1
        acceptance_ts = None
        retest_ts = None
        retest_price = None
        retest_depth_pct = None
        retest_hold_ts = None
        accept_streak = 0
        state = "WAIT_ACCEPTANCE"

    # TRUE_BREAK LONG is over UPPER (zone above); SHORT under LOWER.
    # Invalidated sequence yields no entry from that sequence; a new full
    # acceptance (two closes) may start afterward within the timeout window.
    for i, b in enumerate(bars):
        if not _within_timeout(b.close_time_ns, alert_ts_ns, timeout_minutes):
            break

        if side == "LONG":
            accept_close = b.close > confluence_high + 1e-15
            tol = confluence_high * (retest_tolerance_pct / 100.0)
            retest_touch = b.low <= confluence_high + tol + 1e-15
            retest_invalid = b.close < confluence_low - 1e-15
            hold_close = b.close > confluence_high + 1e-15
            depth_ref = confluence_high
        else:
            accept_close = b.close < confluence_low - 1e-15
            tol = confluence_low * (retest_tolerance_pct / 100.0)
            retest_touch = b.high >= confluence_low - tol - 1e-15
            retest_invalid = b.close > confluence_high + 1e-15
            hold_close = b.close < confluence_low - 1e-15
            depth_ref = confluence_low

        if state == "WAIT_ACCEPTANCE":
            if accept_close:
                accept_streak += 1
                if accept_streak == 1:
                    events.append({"event": "first_acceptance_close", "ts_ns": b.close_time_ns})
                if accept_streak >= REQUIRED_RECLAIM_CLOSES:
                    acceptance_ts = b.close_time_ns
                    last_acceptance_ts = acceptance_ts
                    state = "WAIT_RETEST"
                    events.append({"event": "acceptance", "ts_ns": b.close_time_ns})
            else:
                if accept_streak:
                    accept_streak = 0
                    reset_count += 1
            continue

        if state == "WAIT_RETEST":
            if retest_invalid:
                _reset_sequence(reason="retest_invalidated", ts_ns=b.close_time_ns)
                continue
            if retest_touch:
                retest_ts = b.close_time_ns
                last_retest_ts = retest_ts
                if side == "LONG":
                    retest_price = float(b.low)
                    retest_depth_pct = 100.0 * (depth_ref - b.low) / depth_ref if depth_ref else 0.0
                else:
                    retest_price = float(b.high)
                    retest_depth_pct = 100.0 * (b.high - depth_ref) / depth_ref if depth_ref else 0.0
                last_retest_price = retest_price
                last_retest_depth = retest_depth_pct
                state = "WAIT_HOLD"
                events.append({"event": "retest", "ts_ns": b.close_time_ns, "retest_price": retest_price})
            continue

        if state == "WAIT_HOLD":
            if retest_invalid:
                _reset_sequence(reason="retest_invalidated", ts_ns=b.close_time_ns)
                continue
            if hold_close:
                retest_hold_ts = b.close_time_ns
                last_retest_hold = retest_hold_ts
                state = "WAIT_CONTINUATION"
                events.append({"event": "retest_hold", "ts_ns": b.close_time_ns})
            continue

        if state == "WAIT_CONTINUATION":
            if retest_invalid:
                _reset_sequence(reason="retest_invalidated", ts_ns=b.close_time_ns)
                continue
            ok = (
                structure_break_long(bars, i, lookback=lookback)
                if side == "LONG"
                else structure_break_short(bars, i, lookback=lookback)
            )
            if ok:
                delay_min = (b.close_time_ns - alert_ts_ns) / (60 * NS)
                return ConfirmationResult(
                    confirmed=True,
                    reason="CONFIRMED",
                    confirmation_ts=b.close_time_ns,
                    confirmation_type="TRUE_BREAK_CONTINUATION",
                    details={
                        "acceptance_ts": acceptance_ts,
                        "retest_ts": retest_ts,
                        "retest_price": retest_price,
                        "retest_depth_pct": retest_depth_pct,
                        "retest_hold_ts": retest_hold_ts,
                        "continuation_confirmation_ts": b.close_time_ns,
                        "confirmation_delay_minutes": delay_min,
                        "confirmation_reset_count": reset_count,
                    },
                    sequence_events=events
                    + [{"event": "continuation_confirmation", "ts_ns": b.close_time_ns}],
                )

    # classify timeout reason
    if last_acceptance_ts is None:
        reason = "NO_CONFIRMED_ENTRY"
    elif last_retest_ts is None and saw_retest_invalidation:
        reason = "RETEST_INVALIDATED"
    elif last_retest_ts is None:
        reason = "NO_RETEST"
    elif saw_retest_invalidation and acceptance_ts is None:
        reason = "RETEST_INVALIDATED"
    else:
        reason = "NO_CONFIRMED_ENTRY"
    return ConfirmationResult(
        confirmed=False,
        reason=reason,
        details={
            "acceptance_ts": last_acceptance_ts,
            "retest_ts": last_retest_ts,
            "retest_price": last_retest_price,
            "retest_depth_pct": last_retest_depth,
            "retest_hold_ts": last_retest_hold,
            "continuation_confirmation_ts": None,
            "confirmation_delay_minutes": None,
            "confirmation_reset_count": reset_count,
        },
        sequence_events=events,
    )


def confirm_event(
    candles_5m: Sequence[Candle5m],
    *,
    alert_ts_ns: int,
    label: str,
    trade_side: str,
    confluence_low: float,
    confluence_high: float,
) -> ConfirmationResult:
    lab = str(label).upper()
    if lab == "FAILED_BREAK":
        return confirm_failed_break(
            candles_5m,
            alert_ts_ns=alert_ts_ns,
            trade_side=trade_side,
            confluence_low=confluence_low,
            confluence_high=confluence_high,
        )
    if lab == "ABSORB":
        return confirm_absorb(
            candles_5m,
            alert_ts_ns=alert_ts_ns,
            trade_side=trade_side,
            confluence_low=confluence_low,
            confluence_high=confluence_high,
        )
    if lab == "TRUE_BREAK":
        return confirm_true_break(
            candles_5m,
            alert_ts_ns=alert_ts_ns,
            trade_side=trade_side,
            confluence_low=confluence_low,
            confluence_high=confluence_high,
        )
    return ConfirmationResult(confirmed=False, reason="UNSUPPORTED_LABEL", details={})


def resolve_entry_1m(
    candles_1m: Sequence[Any],
    *,
    confirmation_ts_ns: int,
) -> tuple[int | None, float | None, str | None]:
    """Return (entry_ts_ns, entry_price, error_reason). Entry = first 1m open >= confirmation_ts."""
    for c in candles_1m:
        if int(c.open_time_ns) >= int(confirmation_ts_ns):
            return int(c.open_time_ns), float(c.open), None
    return None, None, "MISSING_ENTRY_CANDLE"
