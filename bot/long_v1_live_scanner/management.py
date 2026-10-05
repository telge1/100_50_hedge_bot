"""Incremental 1m shadow management (frozen BE semantics)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from frozen_strategies.long_geometry_ladder24_be100_v1 import config as FC
from frozen_strategies.long_geometry_ladder24_be100_v1.outcome import horizon_bars


@dataclass
class ShadowTrade:
    signal_id: str
    symbol: str
    pool_id: str
    decision_time: str
    entry_time: str
    entry_price: float
    original_stop: float
    active_stop: float
    tp: float
    ladder_age_h: float | None
    hypothetical: bool = False
    status: str = "OPEN"
    be_triggered: bool = False
    be_trigger_time: str | None = None
    pending_be: bool = False
    be_armed: bool = False
    ambiguous: bool = False
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_pct: float | None = None
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    last_processed_1m_close: str | None = None
    management_bars_processed: int = 0
    side: str = "LONG"

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "pool_id": self.pool_id,
            "decision_time": self.decision_time,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "original_stop": self.original_stop,
            "active_stop": self.active_stop,
            "tp": self.tp,
        "ladder_age_h": self.ladder_age_h,
        "hypothetical": self.hypothetical,
        "status": self.status,
            "be_triggered": self.be_triggered,
            "be_trigger_time": self.be_trigger_time,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl_pct": self.pnl_pct,
            "mae_pct": round(self.mae_pct, 4),
            "mfe_pct": round(self.mfe_pct, 4),
            "last_processed_1m_close": self.last_processed_1m_close,
            "management_bars_processed": self.management_bars_processed,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ShadowTrade:
        return cls(
            signal_id=d["signal_id"],
            symbol=d["symbol"],
            pool_id=d["pool_id"],
            decision_time=d["decision_time"],
            entry_time=d["entry_time"],
            entry_price=float(d["entry_price"]),
            original_stop=float(d["original_stop"]),
            active_stop=float(d["active_stop"]),
            tp=float(d["tp"]),
            ladder_age_h=d.get("ladder_age_h"),
            hypothetical=bool(d.get("hypothetical")),
            status=d.get("status", "OPEN"),
            be_triggered=bool(d.get("be_triggered")),
            be_trigger_time=d.get("be_trigger_time"),
            pending_be=bool(d.get("pending_be")),
            be_armed=bool(d.get("be_armed")),
            ambiguous=bool(d.get("ambiguous")),
            exit_time=d.get("exit_time"),
            exit_price=float(d["exit_price"]) if d.get("exit_price") is not None else None,
            exit_reason=d.get("exit_reason"),
            pnl_pct=float(d["pnl_pct"]) if d.get("pnl_pct") is not None else None,
            mae_pct=float(d.get("mae_pct") or 0),
            mfe_pct=float(d.get("mfe_pct") or 0),
            last_processed_1m_close=d.get("last_processed_1m_close"),
            management_bars_processed=int(d.get("management_bars_processed") or 0),
        )


def _close_trade(trade: ShadowTrade, bar: dict, reason: str, pnl_pct: float, exit_price: float) -> None:
    trade.status = reason
    trade.exit_reason = reason
    trade.exit_time = bar["close_time"].isoformat() if hasattr(bar["close_time"], "isoformat") else str(bar["close_time"])
    trade.exit_price = exit_price
    trade.pnl_pct = round(pnl_pct, 4)


def process_one_1m_bar(trade: ShadowTrade, bar: dict) -> tuple[bool, bool]:
    """
    Apply one closed 1m bar to an OPEN shadow trade.
    Returns (closed, be_triggered_this_bar).
    """
    if trade.status != "OPEN":
        return False, False
    be_triggered_bar = False

    entry = trade.entry_price
    fav = (bar["high"] - entry) / entry * 100.0
    adv = (entry - bar["low"]) / entry * 100.0
    trade.mae_pct = max(trade.mae_pct, adv)
    trade.mfe_pct = max(trade.mfe_pct, fav)

    if trade.pending_be:
        trade.be_armed = True
        trade.active_stop = entry * (1.0 + FC.BE_STOP_OFFSET_PCT / 100.0)
        trade.pending_be = False

    active_stop = trade.active_stop
    tp = trade.tp
    trigger = entry * (1.0 + FC.BE_TRIGGER_MFE_PCT / 100.0)

    stop_hit = bar["low"] <= active_stop
    tp_hit = bar["high"] >= tp

    if stop_hit and tp_hit:
        pnl = (active_stop - entry) / entry * 100.0
        _close_trade(trade, bar, "INTRABAR_AMBIGUOUS", pnl, active_stop)
        trade.status = "BE" if trade.be_armed and abs(pnl) < 1e-6 else "SL"
        return True, be_triggered_bar

    if stop_hit:
        leg = (active_stop - entry) / entry * 100.0
        if trade.be_armed and abs(leg) < 1e-6:
            _close_trade(trade, bar, "BE", leg, active_stop)
        else:
            _close_trade(trade, bar, "SL", leg, active_stop)
        return True, be_triggered_bar

    if tp_hit:
        leg = (tp - entry) / entry * 100.0
        _close_trade(trade, bar, "TP", leg, tp)
        return True, be_triggered_bar

    if not trade.be_armed and not trade.pending_be and bar["high"] >= trigger:
        trade.be_triggered = True
        be_triggered_bar = True
        trade.be_trigger_time = bar["close_time"].isoformat()
        be_level = entry * (1.0 + FC.BE_STOP_OFFSET_PCT / 100.0)
        if bar["low"] <= be_level:
            trade.ambiguous = True
        trade.pending_be = True

    trade.management_bars_processed += 1
    ct = bar["close_time"]
    trade.last_processed_1m_close = ct.isoformat() if hasattr(ct, "isoformat") else str(ct)

    max_bars = horizon_bars(1)
    if trade.management_bars_processed >= max_bars:
        last = bar["close"]
        pnl = (last - entry) / entry * 100.0
        _close_trade(trade, bar, "HORIZON", pnl, last)
        return True, be_triggered_bar

    return False, be_triggered_bar


def log_row_from_trade(trade: ShadowTrade, *, signal_status: str, timestamp: datetime) -> dict:
    row = trade.to_dict()
    row.update(
        {
            "event": "signal",
            "timestamp": timestamp.isoformat(),
            "signal_status": signal_status,
            "be_trigger_pct": FC.BE_TRIGGER_MFE_PCT,
        }
    )
    return row
