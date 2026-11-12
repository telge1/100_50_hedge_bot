"""LEGACY V1 — simplified parallel FSM (not V3AO-parity).

**Do not use for V2 entry path.** Use `simulator_v2.replay_long_box_v2` instead.
"""

from __future__ import annotations

from typing import Any

from .entry_touch import box_active_at, entry_level_touch
from .l1_confirm import evaluate_confirmation
from .models import LongBoxReject, LongBoxSetup, ReplayResult, SimEvent, SimTrade
from .recognition import recognize_long_boxes
from .validation import validate_long_geometry_at_fill
from .watcher_pool import WatcherPool


def _candle_ts(c: dict[str, Any]) -> int:
    t = c.get("time")
    if isinstance(t, (int, float)):
        return int(t)
    raise ValueError("candle missing time")


def replay_long_boxes(
    candles: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    symbol: str | None = "DOGEUSDT",
    manual_only: bool = True,
    confirmation_mode: str = "A2",
    watch_window_bars: int = 12,
    touch_tolerance_pct: float = 0.0,
) -> ReplayResult:
    setups, rejects = recognize_long_boxes(payload, symbol=symbol, manual_only=manual_only)
    pool = WatcherPool(
        watch_window_bars=watch_window_bars,
        confirmation_mode=confirmation_mode,  # type: ignore[arg-type]
        touch_tolerance_pct=touch_tolerance_pct,
    )
    for s in setups:
        pool.register(s)

    events: list[SimEvent] = []
    trades: list[SimTrade] = []
    open_decisions = [
        "Confirm-Modus produktiv: Forschung nutzt A2 (L1-Default); A3 optional.",
        "Konkurrenz mehrerer WATCHING: nur erste bestätigte Box erhält Entry solange global offen.",
    ]

    active_trade: SimTrade | None = None
    active_trade_fill_bar: int | None = None
    pending_entry: tuple[str, int, float] | None = None  # drawing_id, fill_bar_idx, planned entry

    for i, candle in enumerate(candles):
        ts = _candle_ts(candle)
        prev_close = float(candles[i - 1]["close"]) if i > 0 else float(candle["open"])

        if pending_entry and i == pending_entry[1]:
            did, _, planned = pending_entry
            fill = float(candle["open"])
            w = pool.watchers[did]
            stop, tgt = w.setup.stop_price, w.setup.target_price
            ok, reason = validate_long_geometry_at_fill(stop, fill, tgt)
            if not ok:
                events.append(SimEvent(i, ts, did, "INVALIDATED", reason))
                w.phase = "INVALIDATED"
            elif pool.can_schedule_entry(did):
                pool.mark_filled(did)
                from .validation import pct_dist

                active_trade = SimTrade(
                    drawing_id=did,
                    planned_entry=planned,
                    fill_price=fill,
                    stop_price=stop,
                    target_price=tgt,
                    fill_utc=ts,
                    exit_utc=None,
                    exit_reason="",
                    risk_pct_at_fill=round(pct_dist(fill, stop), 6),
                    reward_pct_at_fill=round(pct_dist(fill, tgt), 6),
                )
                trades.append(active_trade)
                active_trade_fill_bar = i
                events.append(SimEvent(i, ts, did, "FILLED", "entry_at_open", fill))
            pending_entry = None

        if active_trade is not None and active_trade_fill_bar is not None and i > active_trade_fill_bar:
            lo, hi = float(candle["low"]), float(candle["high"])
            if lo <= active_trade.stop_price:
                active_trade.exit_utc = ts
                active_trade.exit_reason = "SL"
                pool.watchers[active_trade.drawing_id].phase = "CLOSED_SL"
                events.append(
                    SimEvent(i, ts, active_trade.drawing_id, "CLOSED_SL", "stop_hit", active_trade.stop_price)
                )
                pool.mark_closed(active_trade.drawing_id)
                active_trade = None
                active_trade_fill_bar = None
            elif hi >= active_trade.target_price:
                active_trade.exit_utc = ts
                active_trade.exit_reason = "TP"
                pool.watchers[active_trade.drawing_id].phase = "CLOSED_TP"
                events.append(
                    SimEvent(i, ts, active_trade.drawing_id, "CLOSED_TP", "target_hit", active_trade.target_price)
                )
                pool.mark_closed(active_trade.drawing_id)
                active_trade = None
                active_trade_fill_bar = None

        if pool.global_position_open or pending_entry:
            continue

        for did, w in sorted(pool.watchers.items(), key=lambda x: x[1].setup.start_ts):
            if w.phase in ("FILLED", "CLOSED_SL", "CLOSED_TP", "INVALIDATED", "EXPIRED"):
                continue
            if not box_active_at(w.setup.start_ts, w.setup.end_ts, ts):
                continue
            touched, tmsg = entry_level_touch(
                candle, w.setup.entry_price, pool.touch_tolerance_pct
            )

            if w.phase == "IDLE" and touched:
                w.touch_bar_idx = i
                w.touch_utc = ts
                w.watch_bars_left = pool.watch_window_bars
                events.append(SimEvent(i, ts, did, "TOUCH", tmsg, w.setup.entry_price))
                w.phase = "WATCHING"
                events.append(SimEvent(i, ts, did, "WATCH", "watcher_active", w.setup.entry_price))

            if w.phase == "WATCHING":
                w.watch_bars_left -= 1
                if evaluate_confirmation(
                    pool.confirmation_mode,
                    candle,
                    w.setup.entry_price,
                    prev_close,
                    min_gain_pct=pool.confirmation_min_gain_pct,
                ):
                    w.phase = "CONFIRMED"
                    w.confirmed_bar_idx = i
                    events.append(SimEvent(i, ts, did, "CONFIRMED", "A2_close", float(candle["close"])))
                    if i + 1 < len(candles) and pool.can_schedule_entry(did):
                        pending_entry = (did, i + 1, w.setup.entry_price)
                        w.phase = "CONFIRMED"
                    break
                if w.watch_bars_left <= 0:
                    w.phase = "EXPIRED"
                    events.append(SimEvent(i, ts, did, "EXPIRED", "watch_window"))

    return ReplayResult(
        boxes=setups,
        rejects=rejects,
        events=events,
        trades=trades,
        open_decisions=open_decisions,
    )
