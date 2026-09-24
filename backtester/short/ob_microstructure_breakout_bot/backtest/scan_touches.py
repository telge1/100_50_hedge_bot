"""Discover EMA59 touches and classify them without hardcoded labels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from ob_microstructure_breakout_bot.data.bars import Bar5m, load_5m_bars
from ob_microstructure_breakout_bot.data.ema_candles import _ema_series
from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands
from ob_microstructure_breakout_bot.models import (
    ClassificationResult,
    CoinThresholds,
    EmaSnapshot,
    TouchDirection,
    TradeWindowStats,
)
from ob_microstructure_breakout_bot.rule_engine import RuleEngine


@dataclass(frozen=True)
class TouchEvent:
    """One bar-close EMA59 touch candidate.

    ``bar_ts`` is the **start** of the completed 5m bucket that contains the
    touch (e.g. a touch somewhere inside 10:05–10:10 is stored as 10:05).
    It is *not* the exact intrabar second of the touch. Classification becomes
    available only at ``ScannedTouch.decision_ts`` after confirm + follow-through.
    """

    bar_ts: datetime
    direction: TouchDirection
    is_first_in_cluster: bool
    ema: EmaSnapshot
    bar: Bar5m
    previous_ema: EmaSnapshot | None = None


@dataclass
class ScannedTouch:
    """Classified touch with availability timestamp.

    ``decision_ts`` is the earliest time all confirm/follow-through bars are
    closed. Callers must treat this — not ``touch.bar_ts`` — as the signal time.
    """

    touch: TouchEvent
    context: TradeWindowStats
    confirm: TradeWindowStats
    followthrough: TradeWindowStats
    decision_ts: datetime
    result: ClassificationResult
    ob_ok: bool
    ob_error: str | None = None


def _sum_bars(bars: list[Bar5m]) -> TradeWindowStats:
    if not bars:
        return TradeWindowStats(0.0, 0.0)
    return TradeWindowStats(
        buy_notional=sum(b.buy_notional for b in bars),
        sell_notional=sum(b.sell_notional for b in bars),
        trade_count=sum(b.trade_count for b in bars),
        open_price=bars[0].open,
        close_price=bars[-1].close,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
    )


def _snapshot_at(
    bars: list[Bar5m],
    idx: int,
    e9: list[float | None],
    e20: list[float | None],
    e59: list[float | None],
    e200: list[float | None],
) -> EmaSnapshot | None:
    if e9[idx] is None or e20[idx] is None or e59[idx] is None:
        return None
    return EmaSnapshot(
        ema9=float(e9[idx]),
        ema20=float(e20[idx]),
        ema59=float(e59[idx]),
        price=bars[idx].close,
        ema200=None if e200[idx] is None else float(e200[idx]),
    )


def detect_ema59_touches(
    bars: list[Bar5m],
    *,
    cluster_gap_bars: int = 3,
) -> list[TouchEvent]:
    """Detect completed 5m bars where price touches/crosses EMA59 (bar-close).

    Requires fully closed OHLC bars. The stored ``TouchEvent.bar_ts`` is the
    bucket start of that completed candle, not an intrabar timestamp.
    """
    closes = [b.close for b in bars]
    e9 = _ema_series(closes, 9)
    e20 = _ema_series(closes, 20)
    e59 = _ema_series(closes, 59)
    e200 = _ema_series(closes, 200)

    touches: list[TouchEvent] = []
    last_touch_idx: Optional[int] = None

    for i, bar in enumerate(bars):
        snap = _snapshot_at(bars, i, e9, e20, e59, e200)
        if snap is None:
            continue
        ema59 = snap.ema59
        prev_close = bars[i - 1].close if i > 0 else bar.open

        touched_from_above = prev_close >= ema59 and bar.low <= ema59
        touched_from_below = prev_close <= ema59 and bar.high >= ema59
        if not (touched_from_above or touched_from_below):
            continue

        if touched_from_above and not touched_from_below:
            direction = TouchDirection.FROM_ABOVE
        elif touched_from_below and not touched_from_above:
            direction = TouchDirection.FROM_BELOW
        else:
            # both sides in one bar — use close relative to EMA
            direction = (
                TouchDirection.FROM_ABOVE
                if bar.close < ema59
                else TouchDirection.FROM_BELOW
            )

        is_first = last_touch_idx is None or (i - last_touch_idx) > cluster_gap_bars
        last_touch_idx = i
        previous_ema = (
            _snapshot_at(bars, i - 1, e9, e20, e59, e200) if i > 0 else None
        )
        touches.append(
            TouchEvent(
                bar_ts=bar.ts,
                direction=direction,
                is_first_in_cluster=is_first,
                ema=snap,
                bar=bar,
                previous_ema=previous_ema,
            )
        )
    return touches


def scan_ema59_touches(
    symbol: str,
    start: datetime,
    end: datetime,
    thresholds: CoinThresholds,
    *,
    client: Any | None = None,
    fetch_ob: bool = True,
    only_first_in_cluster: bool = True,
    context_bars: int = 6,  # 30m before touch
    confirm_bars: int = 2,  # touch bar + next 5m (= 10m confirm)
    followthrough_bars: int = 2,  # following 10m
) -> list[ScannedTouch]:
    """Scan a range for EMA59 touches and classify each one (bar-close model).

    ``load_5m_bars`` only returns fully closed 5m buckets relative to ``end``.
    A classification is emitted only when confirm + follow-through are complete;
    ``decision_ts`` is the close of the last follow-through bar and is the
    earliest valid signal time (not ``touch.bar_ts``).
    """
    # Warmup for EMA200 (needs >= 200 closed 5m bars)
    warmup = start - timedelta(minutes=5 * 220)
    bars = load_5m_bars(symbol, warmup, end, client=client)
    if len(bars) < 200:
        raise RuntimeError(f"Not enough 5m bars for EMA200 scan ({len(bars)})")

    touches = detect_ema59_touches(bars)
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    engine = RuleEngine(thresholds)
    results: list[ScannedTouch] = []

    for touch in touches:
        # bar_ts is bucket start of a completed candle; keep it inside the scan window.
        if touch.bar_ts < start or touch.bar_ts >= end:
            continue
        if only_first_in_cluster and not touch.is_first_in_cluster:
            continue

        idx = by_ts[touch.bar_ts]
        ctx_slice = bars[max(0, idx - context_bars) : idx]
        confirm_slice = bars[idx : idx + confirm_bars]
        ft_slice = bars[idx + confirm_bars : idx + confirm_bars + followthrough_bars]

        # Do not classify until confirm + follow-through bars are fully present.
        # Otherwise the scanner would emit a decision before decision_ts.
        if len(confirm_slice) < confirm_bars or len(ft_slice) < followthrough_bars:
            continue

        context = _sum_bars(ctx_slice)
        confirm = _sum_bars(confirm_slice)
        followthrough = _sum_bars(ft_slice)
        decision_bar = bars[idx + confirm_bars + followthrough_bars - 1]
        # Close of the last follow-through bar (= earliest known-complete decision).
        decision_ts = decision_bar.ts + timedelta(minutes=5)
        if decision_ts <= touch.bar_ts:
            continue

        ob = None
        ob_ok = True
        ob_error = None
        if fetch_ob:
            try:
                # Conservative: OB as-of bucket start, not post-close of the touch bar.
                ob = sample_ob_bands(symbol, touch.bar_ts)
            except Exception as exc:  # noqa: BLE001
                ob_ok = False
                ob_error = str(exc)

        result = engine.classify_ema59_touch(
            direction=touch.direction,
            context=context,
            at_touch=confirm,
            followthrough=followthrough,
            ob_at_touch=ob,
            ema=touch.ema,
            previous_ema=touch.previous_ema,
            is_first_touch=touch.is_first_in_cluster,
        )
        results.append(
            ScannedTouch(
                touch=touch,
                context=context,
                confirm=confirm,
                followthrough=followthrough,
                decision_ts=decision_ts,
                result=result,
                ob_ok=ob_ok,
                ob_error=ob_error,
            )
        )
    return results
