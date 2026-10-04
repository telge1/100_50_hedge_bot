"""Baseline 15m outcomes and 1m break-even simulation (frozen)."""

from __future__ import annotations

from dataclasses import dataclass

from . import config as C


def horizon_bars(bar_minutes: int) -> int:
    return int(C.HORIZON_HOURS * 60 / bar_minutes)


def baseline_path_15m(
    bars: list[dict],
    start_idx: int,
    entry: float,
    stop: float,
    tp: float,
) -> dict:
    mfe = mae = 0.0
    first_hit = "none"
    pnl_pct = None
    end = min(len(bars), start_idx + 1 + horizon_bars(15))
    for i in range(start_idx + 1, end):
        bar = bars[i]
        fav = (bar["high"] - entry) / entry * 100.0
        adv = (entry - bar["low"]) / entry * 100.0
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        stop_hit = bar["low"] <= stop
        tp_hit = bar["high"] >= tp
        if stop_hit and tp_hit:
            first_hit = "SL"
            pnl_pct = (stop - entry) / entry * 100.0
            break
        if stop_hit:
            first_hit = "SL"
            pnl_pct = (stop - entry) / entry * 100.0
            break
        if tp_hit:
            first_hit = "TP"
            pnl_pct = (tp - entry) / entry * 100.0
            break
    return {
        "first_hit": first_hit,
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "mae_pct": round(mae, 4),
        "mfe_pct": round(mfe, 4),
    }


@dataclass
class ManagedOutcome:
    outcome: str
    pnl_pct: float
    intrabar_ambiguous: bool
    trigger_fired: bool
    resolution_tf: str


def simulate_be_management(
    bars: list[dict],
    start_idx: int,
    entry: float,
    stop: float,
    tp: float,
    *,
    bar_minutes: int,
) -> ManagedOutcome:
    """BE arms on bar after high reaches +BE_TRIGGER_MFE_PCT; conservative same-bar ambiguity."""
    end = min(len(bars), start_idx + 1 + horizon_bars(bar_minutes))
    be_armed = False
    pending_be = False
    active_stop = stop
    be_level = entry * (1.0 + C.BE_STOP_OFFSET_PCT / 100.0)
    ambiguous = False
    trigger_fired = False
    trigger = entry * (1.0 + C.BE_TRIGGER_MFE_PCT / 100.0)

    for i in range(start_idx + 1, end):
        bar = bars[i]
        if pending_be:
            be_armed = True
            active_stop = be_level
            pending_be = False

        stop_hit = bar["low"] <= active_stop
        tp_hit = bar["high"] >= tp

        if stop_hit and tp_hit:
            pnl = (active_stop - entry) / entry * 100.0
            return ManagedOutcome("INTRABAR_AMBIGUOUS", round(pnl, 4), True, trigger_fired, f"{bar_minutes}m")

        if stop_hit:
            leg = (active_stop - entry) / entry * 100.0
            if be_armed and abs(leg) < 1e-6:
                out = "BE"
            else:
                out = "SL"
            return ManagedOutcome(out, round(leg, 4), ambiguous, trigger_fired, f"{bar_minutes}m")

        if tp_hit:
            leg = (tp - entry) / entry * 100.0
            return ManagedOutcome("TP", round(leg, 4), ambiguous, trigger_fired, f"{bar_minutes}m")

        if not be_armed and not pending_be and bar["high"] >= trigger:
            trigger_fired = True
            if bar["low"] <= be_level:
                ambiguous = True
            pending_be = True

    last = bars[end - 1]["close"]
    pnl = (last - entry) / entry * 100.0
    return ManagedOutcome("horizon", round(pnl, 4), ambiguous, trigger_fired, f"{bar_minutes}m")


def slice_1m_after_entry(bars1m: list[dict], entry_close_time) -> tuple[list[dict], int]:
    slice_bars = [b for b in bars1m if b["open_time"] >= entry_close_time]
    reindexed = []
    for i, b in enumerate(slice_bars):
        reindexed.append({**b, "candle_index": i})
    return reindexed, 0
