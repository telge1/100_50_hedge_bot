"""Decisive-break state machine (v3). Does not mutate v2 monitor semantics."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.tem_structure_break.decisive_levels import (
    confirmed_swing_highs,
    confirmed_swing_lows,
    pick_decisive_level,
    prepare_h4_series,
)
from research.regime_scanner.tem_structure_break.decisive_models import (
    RANGE_LOOKBACK_BARS,
    RECLAIM_CONFIRM_BARS,
    STABILIZE_4H_BARS,
    DecisiveLevel,
    DecisiveRuntime,
    DecisiveState,
)


def _emit(rt: DecisiveRuntime, event: str, **payload: Any) -> None:
    rt.events.append({"event": event, "state": rt.state.value, **payload})


def _arm_h4_index(h4: pd.DataFrame, arm_ts: str) -> int:
    """First 4h bar whose close_decision is >= arm signal time."""
    target = pd.Timestamp(arm_ts)
    if target.tzinfo is None:
        target = target.tz_localize("UTC")
    else:
        target = target.tz_convert("UTC")
    closes = pd.to_datetime(h4["htf_close_decision"], utc=True)
    hits = [i for i, t in enumerate(closes) if t >= target]
    if not hits:
        return len(h4) - 1
    return int(hits[0])


def run_decisive_break(
    h4_raw: pd.DataFrame,
    *,
    v2_first_break_ts: str | None,
    v2_break_level: float | None = None,
    stabilize_bars: int = STABILIZE_4H_BARS,
    range_lookback: int = RANGE_LOOKBACK_BARS,
    end_close_decision: str | None = None,
) -> DecisiveRuntime:
    """Run decisive track from first v2 4h break pending timestamp.

    Processes completed 4h bars only. Causal pivot confirmation via next-bar close.
    """
    rt = DecisiveRuntime(stabilize_bars=int(stabilize_bars))
    if not v2_first_break_ts or h4_raw is None or h4_raw.empty:
        return rt

    h4 = prepare_h4_series(h4_raw)
    if end_close_decision is not None:
        end_t = pd.Timestamp(end_close_decision)
        if end_t.tzinfo is None:
            end_t = end_t.tz_localize("UTC")
        mask = pd.to_datetime(h4["htf_close_decision"], utc=True) <= end_t
        h4 = h4.loc[mask].reset_index(drop=True)
        if h4.empty:
            return rt

    swings_low = confirmed_swing_lows(h4)
    swings_high = confirmed_swing_highs(h4)

    arm_idx = _arm_h4_index(h4, v2_first_break_ts)
    rt.arm_ts = v2_first_break_ts
    rt.arm_bar_h4 = arm_idx
    rt.v2_first_break_ts = v2_first_break_ts
    rt.v2_break_level = v2_break_level
    rt.state = DecisiveState.DECISIVE_ARMING
    _emit(
        rt,
        "DECISIVE_ARMED",
        timestamp=v2_first_break_ts,
        arm_h4_index=arm_idx,
        v2_break_level=v2_break_level,
        stabilize_bars=stabilize_bars,
    )

    # Iterate from arm_idx onward; each step is a newly available completed 4h bar.
    for idx in range(arm_idx, len(h4)):
        if rt.state == DecisiveState.DECISIVE_BREAK_CONFIRMED:
            break

        row = h4.iloc[idx]
        close = float(row["close"])
        close_dec = str(row["htf_close_decision"])
        ts = str(row["timestamp"])
        rt.bars_since_arm = idx - arm_idx

        # Soft: reclaimed → arming
        if rt.state == DecisiveState.DECISIVE_BREAK_RECLAIMED:
            rt.state = DecisiveState.DECISIVE_ARMING
            rt.level = None
            _emit(rt, "DECISIVE_REARM_AFTER_RECLAIM", timestamp=close_dec, bar_h4=idx)

        # Resolve pending on bars after the pending bar
        if rt.state == DecisiveState.DECISIVE_BREAK_PENDING and rt.pending_close_decision is not None:
            pend = pd.Timestamp(rt.pending_close_decision)
            cur = pd.Timestamp(close_dec)
            if cur > pend:
                level = None if rt.level is None else float(rt.level.value)
                if level is not None and close >= level:
                    rt.state = DecisiveState.DECISIVE_BREAK_RECLAIMED
                    rt.reclaim_ts = close_dec
                    _emit(
                        rt,
                        "DECISIVE_BREAK_RECLAIMED",
                        timestamp=ts,
                        signal_available_ts=close_dec,
                        level=level,
                        bar_h4=idx,
                    )
                    # continue same bar into re-arm path next iteration semantics:
                    # already set RECLAIMED; next idx will re-arm
                    continue
                rt.state = DecisiveState.DECISIVE_BREAK_CONFIRMED
                rt.confirmed_ts = close_dec
                rt.reason = (
                    "D1_swing_low_reclaim_failure"
                    if rt.level and rt.level.level_type == "confirmed_swing_low_4h"
                    else "D2_range_support_reclaim_failure"
                )
                if rt.level and rt.level.lower_high_ts:
                    rt.reason = "D3_lower_high_plus_" + rt.reason
                _emit(
                    rt,
                    "DECISIVE_BREAK_CONFIRMED",
                    timestamp=ts,
                    signal_available_ts=close_dec,
                    level=level,
                    reason=rt.reason,
                    bar_h4=idx,
                )
                break

        # Arming / level ready: set level once; do not chase newer lows while waiting for break
        if rt.state in {
            DecisiveState.DECISIVE_ARMING,
            DecisiveState.DECISIVE_BREAK_RECLAIMED,
        } or (rt.state == DecisiveState.DECISIVE_LEVEL_READY and rt.level is None):
            lvl, lh = pick_decisive_level(
                h4,
                asof_idx=idx,
                arm_idx=arm_idx,
                stabilize_bars=stabilize_bars,
                range_lookback=range_lookback,
                swings_low=swings_low,
                swings_high=swings_high,
            )
            if lh is not None:
                rt.last_lower_high_ts = lh["confirmed_ts"]
                rt.last_lower_high_price = float(lh["price"])
            if lvl is not None:
                new_level = DecisiveLevel(
                    value=float(lvl["value"]),
                    level_type=str(lvl["level_type"]),
                    source=str(lvl["source"]),
                    formed_ts=str(lvl["formed_ts"]),
                    confirmed_ts=str(lvl["confirmed_ts"]),
                    lower_high_ts=lvl.get("lower_high_ts"),
                    stabilize_bars_used=stabilize_bars,
                )
                rt.level = new_level
                rt.state = DecisiveState.DECISIVE_LEVEL_READY
                hist = {
                    "timestamp": close_dec,
                    "level": new_level.value,
                    "level_type": new_level.level_type,
                    "source": new_level.source,
                    "formed_ts": new_level.formed_ts,
                    "confirmed_ts": new_level.confirmed_ts,
                    "lower_high_ts": new_level.lower_high_ts,
                }
                rt.level_history.append(hist)
                _emit(rt, "DECISIVE_LEVEL_READY", bar_h4=idx, **hist)
        elif rt.state == DecisiveState.DECISIVE_LEVEL_READY:
            # still track lower-high diagnostics without moving the armed level
            _, lh = pick_decisive_level(
                h4,
                asof_idx=idx,
                arm_idx=arm_idx,
                stabilize_bars=stabilize_bars,
                range_lookback=range_lookback,
                swings_low=swings_low,
                swings_high=swings_high,
            )
            if lh is not None:
                rt.last_lower_high_ts = lh["confirmed_ts"]
                rt.last_lower_high_price = float(lh["price"])
                if rt.level is not None and rt.level.lower_high_ts is None:
                    rt.level.lower_high_ts = lh["confirmed_ts"]

        # Break pending: close below ready level on a bar at/after level confirmation
        if (
            rt.state == DecisiveState.DECISIVE_LEVEL_READY
            and rt.level is not None
            and close < float(rt.level.value)
        ):
            lvl_conf = pd.Timestamp(rt.level.confirmed_ts)
            if pd.Timestamp(close_dec) > lvl_conf:
                rt.state = DecisiveState.DECISIVE_BREAK_PENDING
                rt.pending_ts = close_dec
                rt.pending_close_decision = close_dec
                _emit(
                    rt,
                    "DECISIVE_BREAK_PENDING",
                    timestamp=ts,
                    signal_available_ts=close_dec,
                    level=rt.level.value,
                    level_type=rt.level.level_type,
                    source=rt.level.source,
                    bar_h4=idx,
                )

    # Diagnostic: if pending never resolved because series ended, leave pending
    return rt


def extract_v2_arm_from_events(events: list[dict[str, Any]]) -> tuple[str | None, float | None]:
    for e in events:
        if e.get("event") == "BREAK_PENDING_4H":
            ts = e.get("signal_available_ts") or e.get("timestamp")
            lvl = e.get("level")
            try:
                level = float(lvl) if lvl is not None and lvl != "" else None
            except (TypeError, ValueError):
                level = None
            return (str(ts) if ts else None), level
    return None, None
