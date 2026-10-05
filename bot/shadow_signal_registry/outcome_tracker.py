"""Incremental 1m shadow outcome (long with BE, short without)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

HORIZON_HOURS = 48
HORIZON_1M_BARS = HORIZON_HOURS * 60

Side = Literal["long", "short"]


@dataclass
class TrackedShadow:
    signal_id: str
    symbol: str
    side: Side
    decision_time: str
    entry_price: float
    initial_sl: float
    active_sl: float
    tp: float
    hypothetical: bool
    pool_id: str = ""
    # long
    be_trigger_pct: float = 1.0
    be_triggered: bool = False
    be_trigger_time: str | None = None
    pending_be: bool = False
    be_armed: bool = False
    ambiguous: bool = False
    # tracking
    status: str = "OPEN"
    outcome: str | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_time: str | None = None
    mfe_time: str | None = None
    duration_min: int | None = None
    horizon_time: str | None = None
    last_processed_1m: str | None = None
    bars_processed: int = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> TrackedShadow:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _iso(t: datetime) -> str:
    return t.isoformat()


def process_long_bar(trade: TrackedShadow, bar: dict) -> tuple[bool, bool]:
    """Returns (closed, be_triggered_this_bar)."""
    entry = trade.entry_price
    fav = (bar["high"] - entry) / entry * 100.0
    adv = (entry - bar["low"]) / entry * 100.0
    if adv > trade.mae_pct:
        trade.mae_pct = adv
        trade.mae_time = _iso(bar["close_time"])
    if fav > trade.mfe_pct:
        trade.mfe_pct = fav
        trade.mfe_time = _iso(bar["close_time"])

    be_fired = False
    if trade.pending_be:
        trade.be_armed = True
        trade.active_sl = entry
        trade.pending_be = False

    stop_hit = bar["low"] <= trade.active_sl
    tp_hit = bar["high"] >= trade.tp
    trigger = entry * (1.0 + trade.be_trigger_pct / 100.0)

    if stop_hit and tp_hit:
        trade.status = "INTRABAR_AMBIGUOUS"
        trade.outcome = trade.status
        trade.exit_price = trade.active_sl
        trade.pnl_pct = round((trade.exit_price - entry) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True, be_fired

    if stop_hit:
        leg = (trade.active_sl - entry) / entry * 100.0
        if trade.be_armed and abs(leg) < 1e-6:
            trade.status = "BE"
        else:
            trade.status = "SL"
        trade.outcome = trade.status
        trade.exit_price = trade.active_sl
        trade.pnl_pct = round(leg, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True, be_fired

    if tp_hit:
        trade.status = "TP"
        trade.outcome = "TP"
        trade.exit_price = trade.tp
        trade.pnl_pct = round((trade.tp - entry) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True, be_fired

    if not trade.be_armed and not trade.pending_be and bar["high"] >= trigger:
        trade.be_triggered = True
        trade.be_trigger_time = _iso(bar["close_time"])
        be_fired = True
        if bar["low"] <= entry:
            trade.ambiguous = True
        trade.pending_be = True

    trade.bars_processed += 1
    trade.last_processed_1m = _iso(bar["close_time"])
    if trade.bars_processed >= HORIZON_1M_BARS:
        last = bar["close"]
        trade.status = "HORIZON"
        trade.outcome = "HORIZON"
        trade.exit_price = last
        trade.pnl_pct = round((last - entry) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.horizon_time = trade.exit_time
        trade.duration_min = trade.bars_processed
        return True, be_fired
    return False, be_fired


def process_short_bar(trade: TrackedShadow, bar: dict) -> bool:
    entry = trade.entry_price
    adv = (bar["high"] - entry) / entry * 100.0
    fav = (entry - bar["low"]) / entry * 100.0
    if adv > trade.mae_pct:
        trade.mae_pct = adv
        trade.mae_time = _iso(bar["close_time"])
    if fav > trade.mfe_pct:
        trade.mfe_pct = fav
        trade.mfe_time = _iso(bar["close_time"])

    sl_hit = bar["high"] >= trade.active_sl
    tp_hit = bar["low"] <= trade.tp

    if sl_hit and tp_hit:
        trade.status = "INTRABAR_AMBIGUOUS"
        trade.outcome = "SL"
        trade.exit_price = trade.active_sl
        trade.pnl_pct = round((entry - trade.exit_price) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True
    if sl_hit:
        trade.status = "SL"
        trade.outcome = "SL"
        trade.exit_price = trade.active_sl
        trade.pnl_pct = round((entry - trade.exit_price) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True
    if tp_hit:
        trade.status = "TP"
        trade.outcome = "TP"
        trade.exit_price = trade.tp
        trade.pnl_pct = round((entry - trade.exit_price) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.duration_min = trade.bars_processed + 1
        return True

    trade.bars_processed += 1
    trade.last_processed_1m = _iso(bar["close_time"])
    if trade.bars_processed >= HORIZON_1M_BARS:
        last = bar["close"]
        trade.status = "HORIZON"
        trade.outcome = "HORIZON"
        trade.exit_price = last
        trade.pnl_pct = round((entry - last) / entry * 100.0, 6)
        trade.exit_time = _iso(bar["close_time"])
        trade.horizon_time = trade.exit_time
        trade.duration_min = trade.bars_processed
        return True
    return False


def new_bars_after(bars: list[dict], last_processed: str | None) -> list[dict]:
    if not last_processed:
        return list(bars)
    last = datetime.fromisoformat(last_processed)
    return [b for b in bars if b["open_time"] >= last or b["close_time"] > last]


def incremental_step(
    trade: TrackedShadow,
    bars_1m: list[dict],
    load_bars: Callable[[str, datetime, datetime], list[dict]],
    now: datetime,
) -> list[str]:
    """Process new 1m bars; return list of event types emitted (BE_TRIGGERED, SHADOW_*)."""
    events: list[str] = []
    decision = datetime.fromisoformat(trade.decision_time)
    end = now
    all_bars = load_bars(trade.symbol, decision, end)
    if trade.last_processed_1m:
        last = datetime.fromisoformat(trade.last_processed_1m)
        new = [b for b in all_bars if b["close_time"] > last]
    else:
        new = [b for b in all_bars if b["open_time"] >= decision]

    for bar in new:
        if trade.side == "long":
            closed, be = process_long_bar(trade, bar)
            if be and "BE_TRIGGERED" not in events:
                events.append("BE_TRIGGERED")
        else:
            closed = process_short_bar(trade, bar)
        if closed:
            ev = f"SHADOW_{trade.outcome}"
            if trade.outcome == "INTRABAR_AMBIGUOUS":
                ev = "SHADOW_SL" if trade.side == "short" else "SHADOW_BE"
            events.append(ev)
            break
    return events
