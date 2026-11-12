"""Manual Long Box V2 — entry via original V3AN/V3AO watcher_fsm (no duplicated FSM)."""

from __future__ import annotations

from typing import Any

from .hline_adapter import long_box_to_hline
from .l1_imports import default_watcher_config, watcher_replay_entrypoint
from .models import LongBoxReject, LongBoxSetup, ReplayResult, SimEvent, SimTrade
from .recognition import recognize_long_boxes
from .validation import (
    MIN_REMAINING_TP_PCT_AT_FILL,
    allow_long_at_fill,
    pct_dist,
    validate_long_geometry_at_fill,
)

# Re-export for parity tests: proves v2 uses original entrypoint
REPLAY_WATCHER_FROM_CONFIG = "watcher_fsm.replay_watcher_from_config"
DEFAULT_CONFIG_SOURCE = "watcher_integration_v3ao.v3ao_context.DEFAULT_WATCHER_CONFIG"


def _candle_ts(c: dict[str, Any]) -> int:
    return int(c["time"])


def window_indices(candles: list[dict], start_ts: int, end_ts: int) -> tuple[int, int]:
    if not candles:
        return 0, -1
    i_start = 0
    for i, c in enumerate(candles):
        if _candle_ts(c) >= start_ts:
            i_start = i
            break
    i_end = len(candles) - 1
    for i in range(len(candles) - 1, -1, -1):
        if _candle_ts(candles[i]) <= end_ts:
            i_end = i
            break
    if i_end < i_start:
        return 0, -1
    return i_start, i_end


def fsm_transitions_to_events(
    candles: list[dict],
    drawing_id: str,
    transitions,
) -> list[SimEvent]:
    events: list[SimEvent] = []
    touch_emitted = False
    for tr in transitions:
        ts = _candle_ts(candles[tr.bar_idx])
        if tr.from_state == "IDLE" and tr.to_state in (
            "WATCHING_ABOVE",
            "WATCHING_BELOW",
        ):
            if not touch_emitted:
                events.append(
                    SimEvent(tr.bar_idx, ts, drawing_id, "TOUCH", tr.reason, tr.line_px)
                )
                touch_emitted = True
            events.append(
                SimEvent(tr.bar_idx, ts, drawing_id, "WATCH", tr.to_state, tr.line_px)
            )
        if tr.to_state == "ENTRY_SCHEDULED":
            events.append(
                SimEvent(
                    tr.bar_idx,
                    ts,
                    drawing_id,
                    "CONFIRMED",
                    tr.reason,
                    float(tr.c),
                )
            )
        if tr.to_state == "EXPIRED":
            events.append(SimEvent(tr.bar_idx, ts, drawing_id, "EXPIRED", tr.reason, tr.line_px))
    return events


def _simulate_box_exit(
    candles: list[dict],
    setup: LongBoxSetup,
    entry_idx: int,
    fill_price: float,
) -> tuple[list[SimEvent], SimTrade | None]:
    stop, tgt = setup.stop_price, setup.target_price
    ok, reason = validate_long_geometry_at_fill(stop, fill_price, tgt)
    if not ok:
        return [
            SimEvent(entry_idx, _candle_ts(candles[entry_idx]), setup.drawing_id, "INVALIDATED", reason)
        ], None

    trade = SimTrade(
        drawing_id=setup.drawing_id,
        planned_entry=setup.entry_price,
        fill_price=fill_price,
        stop_price=stop,
        target_price=tgt,
        fill_utc=_candle_ts(candles[entry_idx]),
        exit_utc=None,
        exit_reason="",
        risk_pct_at_fill=round(pct_dist(fill_price, stop), 6),
        reward_pct_at_fill=round(pct_dist(fill_price, tgt), 6),
    )
    events = [
        SimEvent(
            entry_idx,
            trade.fill_utc,
            setup.drawing_id,
            "FILLED",
            "v3ao_fsm_entry_at_open",
            fill_price,
        )
    ]
    for i in range(entry_idx + 1, len(candles)):
        c = candles[i]
        ts = _candle_ts(c)
        lo, hi = float(c["low"]), float(c["high"])
        if lo <= stop:
            trade.exit_utc = ts
            trade.exit_reason = "SL"
            events.append(SimEvent(i, ts, setup.drawing_id, "CLOSED_SL", "stop_hit", stop))
            break
        if hi >= tgt:
            trade.exit_utc = ts
            trade.exit_reason = "TP"
            events.append(SimEvent(i, ts, setup.drawing_id, "CLOSED_TP", "target_hit", tgt))
            break
    return events, trade


def _apply_fill_and_tp_gate(
    candles: list[dict],
    setup: LongBoxSetup,
    ent,
    events: list[SimEvent],
    trades: list[SimTrade],
    open_decisions: list[str],
) -> None:
    """Post-FSM only: next 5m open fill + minimum remaining TP distance."""
    idx = int(ent.entry_idx)
    if idx < 0 or idx >= len(candles):
        open_decisions.append("fill_bar_missing:no_tp_check_without_open")
        return

    fill_price = float(ent.entry_price)
    fill_ts = _candle_ts(candles[idx])
    events.append(
        SimEvent(idx, fill_ts, setup.drawing_id, "FILL_CANDIDATE", "next_5m_open", fill_price)
    )

    ok, rem, block_reason = allow_long_at_fill(fill_price, setup.target_price)
    events.append(
        SimEvent(
            idx,
            fill_ts,
            setup.drawing_id,
            "TP_DISTANCE_CHECK",
            f"remaining_tp_pct={rem} min={MIN_REMAINING_TP_PCT_AT_FILL}",
            rem,
        )
    )

    if not ok:
        touch_idx = int(getattr(ent, "touch_idx", -1))
        confirm_idx = int(getattr(ent, "confirm_idx", -1))
        touch_ts = _candle_ts(candles[touch_idx]) if 0 <= touch_idx < len(candles) else None
        confirm_ts = _candle_ts(candles[confirm_idx]) if 0 <= confirm_idx < len(candles) else None
        events.append(
            SimEvent(
                idx,
                fill_ts,
                setup.drawing_id,
                "BLOCKED_TP_DISTANCE",
                f"{block_reason} remaining={rem} threshold={MIN_REMAINING_TP_PCT_AT_FILL} "
                f"tp={setup.target_price} fill={fill_price}",
                fill_price,
            )
        )
        open_decisions.extend(
            [
                "entry_status=BLOCKED_TP_DISTANCE",
                f"blocked_drawing_id={setup.drawing_id}",
                f"blocked_touch_ts={touch_ts}",
                f"blocked_confirm_ts={confirm_ts}",
                f"blocked_planned_fill_ts={fill_ts}",
                f"blocked_fill_open={fill_price}",
                f"blocked_manual_tp={setup.target_price}",
                f"blocked_remaining_tp_pct={rem}",
                f"blocked_min_remaining_tp_pct={MIN_REMAINING_TP_PCT_AT_FILL}",
            ]
        )
        return

    exit_events, trade = _simulate_box_exit(candles, setup, idx, fill_price)
    events.extend(exit_events)
    if trade:
        trades.append(trade)
        open_decisions.append("entry_status=ENTRY_FILLED")


def replay_long_box_v2(
    candles: list[dict[str, Any]],
    setup: LongBoxSetup,
    *,
    watcher_cfg=None,
    one_entry_only: bool = True,
) -> tuple[Any, ReplayResult]:
    """
    Returns (v3an_replay_result, research_replay_result).
    Entry path: replay_watcher_from_config only.
    """
    replay_watcher_from_config, fmt_utc = watcher_replay_entrypoint()
    cfg = watcher_cfg or default_watcher_config()
    line = long_box_to_hline(setup)
    i_start, i_end = window_indices(candles, setup.start_ts, setup.end_ts)
    if i_end < i_start:
        empty = ReplayResult(boxes=[setup], rejects=[], events=[], trades=[])
        return None, empty

    fsm_result = replay_watcher_from_config(
        candles,
        line,
        cfg,
        i_start,
        i_end,
        fmt_utc,
        one_entry_only=one_entry_only,
    )

    events = fsm_transitions_to_events(candles, setup.drawing_id, fsm_result.transitions)
    trades: list[SimTrade] = []
    open_decisions = [
        f"entry_fsm={REPLAY_WATCHER_FROM_CONFIG}",
        f"config={DEFAULT_CONFIG_SOURCE}",
        f"config_id={fsm_result.config_id}",
        f"terminal={fsm_result.terminal_reason}",
        f"min_remaining_tp_pct_at_fill={MIN_REMAINING_TP_PCT_AT_FILL}",
    ]
    for ent in fsm_result.entries:
        _apply_fill_and_tp_gate(candles, setup, ent, events, trades, open_decisions)

    research = ReplayResult(
        boxes=[setup],
        rejects=[],
        events=events,
        trades=trades,
        open_decisions=open_decisions,
    )
    return fsm_result, research


def replay_long_boxes_v2(
    candles: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    symbol: str | None = "DOGEUSDT",
    manual_only: bool = True,
) -> ReplayResult:
    setups, rejects = recognize_long_boxes(payload, symbol=symbol, manual_only=manual_only)
    if not setups:
        return ReplayResult(boxes=[], rejects=rejects, events=[], trades=[])
    if len(setups) > 1:
        rejects.append(
            LongBoxReject("", "multiple_boxes_v2", f"ids={[s.drawing_id for s in setups]}")
        )
        return ReplayResult(boxes=setups, rejects=rejects, events=[], trades=[])
    _, research = replay_long_box_v2(candles, setups[0])
    research = ReplayResult(
        boxes=research.boxes,
        rejects=rejects + research.rejects,
        events=research.events,
        trades=research.trades,
        open_decisions=research.open_decisions,
    )
    return research
