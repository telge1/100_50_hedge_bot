from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import ALLOWED_OUTCOMES
from .dedup import chronological_key, trade_pnl_usdt
from .timeutil import iso_z, parse_ts


@dataclass
class Position:
    slot: int
    signal_id: str
    symbol: str
    timeframe: str
    direction: str
    entry_time: datetime
    exit_time: datetime | None
    outcome: str
    pnl_pct_gross: float
    pnl_usdt: float
    duration_seconds: float | None
    exit_reason: str | None
    exit_price: object
    entry_price: object


@dataclass
class SimResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    slot_history: list[dict[str, Any]] = field(default_factory=list)
    open_at_end: list[dict[str, Any]] = field(default_factory=list)
    skip_reason_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    free_cash: float = 0.0
    reserved: float = 0.0
    realized_pnl: float = 0.0
    peak_open: int = 0
    occupancy_seconds: dict[int, float] = field(default_factory=dict)
    event_order_rule: str = (
        "Before processing an entry at T, close positions with exit_time < T only. "
        "Positions with exit_time == T remain open. Among signals sharing T, order is "
        "timeframe rank descending, then symbol ascending, then signal_id ascending. "
        "Free slots are assigned lowest index first."
    )


def _skip_bucket(stats: dict[str, dict[str, Any]], reason: str, row: dict[str, Any], notional: float) -> None:
    bucket = stats.setdefault(
        reason,
        {"count": 0, "wins": 0, "losses": 0, "open": 0, "theoretical_pnl_usdt": 0.0},
    )
    bucket["count"] += 1
    oc = row["outcome"]
    if oc == "WIN":
        bucket["wins"] += 1
        bucket["theoretical_pnl_usdt"] += trade_pnl_usdt(row.get("pnl_pct_gross"), notional)
    elif oc == "LOSS":
        bucket["losses"] += 1
        bucket["theoretical_pnl_usdt"] += trade_pnl_usdt(row.get("pnl_pct_gross"), notional)
    elif oc == "OPEN":
        bucket["open"] += 1


def _pos_payload(pos: Position, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "slot": pos.slot,
        "signal_id": pos.signal_id,
        "symbol": pos.symbol,
        "timeframe": pos.timeframe,
        "direction": pos.direction,
        "entry_time": iso_z(pos.entry_time),
        "exit_time": iso_z(pos.exit_time),
        "outcome": pos.outcome,
        "pnl_pct_gross": pos.pnl_pct_gross,
        "pnl_usdt": pos.pnl_usdt,
        "duration_seconds": pos.duration_seconds,
        "exit_reason": pos.exit_reason,
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price,
    }
    if extra:
        out.update(extra)
    return out


def simulate_portfolio(
    rows: list[dict[str, Any]],
    *,
    initial_balance: float,
    max_slots: int,
    notional: float,
    enforce_slots: bool = True,
    enforce_symbol: bool = True,
    enforce_cash: bool = True,
) -> SimResult:
    result = SimResult()
    free_cash = float(initial_balance)
    reserved = 0.0
    realized_pnl = 0.0
    slots: list[Position | None] = [None] * max_slots
    last_exit_by_slot: list[datetime | None] = [None] * max_slots
    last_event_ts: datetime | None = None
    occupancy = {i: 0.0 for i in range(max_slots + 1)}

    def open_count() -> int:
        return sum(1 for p in slots if p is not None)

    def snapshot(ts: datetime, kind: str) -> None:
        n = open_count()
        result.peak_open = max(result.peak_open, n)
        eq = free_cash + reserved
        result.equity_curve.append(
            {
                "time": iso_z(ts),
                "event": kind,
                "open_positions": n,
                "free_cash_usdt": round(free_cash, 8),
                "reserved_open_notional_usdt": round(reserved, 8),
                "realized_pnl_usdt": round(realized_pnl, 8),
                "realized_equity_usdt": round(eq, 8),
            }
        )
        result.slot_history.append(
            {
                "time": iso_z(ts),
                "event": kind,
                "open_positions": n,
                "slots": [
                    None if p is None else {"signal_id": p.signal_id, "symbol": p.symbol, "outcome": p.outcome}
                    for p in slots
                ],
            }
        )

    def accrue(until: datetime) -> None:
        nonlocal last_event_ts
        if last_event_ts is None:
            last_event_ts = until
            return
        if until <= last_event_ts:
            last_event_ts = until
            return
        dt = (until - last_event_ts).total_seconds()
        occupancy[open_count()] += dt
        last_event_ts = until

    def close_pos(pos: Position, ts: datetime, kind: str) -> None:
        nonlocal free_cash, reserved, realized_pnl
        slots[pos.slot] = None
        last_exit_by_slot[pos.slot] = pos.exit_time
        reserved -= notional
        if pos.outcome in {"WIN", "LOSS"}:
            realized_pnl += pos.pnl_usdt
            free_cash += notional + pos.pnl_usdt
        else:
            free_cash += notional
        snapshot(ts, kind)

    def close_due(as_of: datetime) -> None:
        due = [
            p
            for p in slots
            if p is not None and p.exit_time is not None and p.exit_time < as_of
        ]
        due.sort(key=lambda p: (p.exit_time, p.slot))
        for pos in due:
            accrue(pos.exit_time)
            close_pos(pos, pos.exit_time, "exit")

    ordered = sorted(rows, key=chronological_key)
    for row in ordered:
        entry = parse_ts(row.get("entry_time"))
        if entry is None:
            _skip_bucket(result.skip_reason_stats, "INVALID_TIMESTAMP", row, notional)
            result.skipped.append({**row, "skip_reason": "INVALID_TIMESTAMP"})
            continue
        outcome = str(row.get("outcome") or "").upper()
        if outcome not in ALLOWED_OUTCOMES:
            _skip_bucket(result.skip_reason_stats, "INVALID_OUTCOME", row, notional)
            result.skipped.append({**row, "skip_reason": "INVALID_OUTCOME"})
            continue
        exit_ts = parse_ts(row.get("exit_time"))
        if outcome in {"WIN", "LOSS"} and exit_ts is None:
            _skip_bucket(result.skip_reason_stats, "MISSING_EXIT_FOR_CLOSED_OUTCOME", row, notional)
            result.skipped.append({**row, "skip_reason": "MISSING_EXIT_FOR_CLOSED_OUTCOME"})
            continue

        close_due(entry)
        accrue(entry)

        if enforce_symbol:
            blocked = False
            for p in slots:
                if p is None:
                    continue
                if p.symbol != row["symbol"]:
                    continue
                if p.exit_time is None or entry <= p.exit_time:
                    blocked = True
                    break
            if blocked:
                _skip_bucket(result.skip_reason_stats, "SYMBOL_ALREADY_OPEN", row, notional)
                result.skipped.append({**row, "skip_reason": "SYMBOL_ALREADY_OPEN"})
                continue

        free_slot = None
        for idx, p in enumerate(slots):
            if p is not None:
                continue
            prev = last_exit_by_slot[idx]
            if prev is not None and not (entry > prev):
                continue
            free_slot = idx
            break
        if enforce_slots and free_slot is None:
            _skip_bucket(result.skip_reason_stats, "NO_FREE_SLOT", row, notional)
            result.skipped.append({**row, "skip_reason": "NO_FREE_SLOT"})
            continue
        if not enforce_slots:
            if free_slot is None:
                slots.append(None)
                last_exit_by_slot.append(None)
                free_slot = len(slots) - 1
                max_slots = len(slots)

        if enforce_cash and free_cash + 1e-12 < notional:
            _skip_bucket(result.skip_reason_stats, "INSUFFICIENT_FREE_CASH", row, notional)
            result.skipped.append({**row, "skip_reason": "INSUFFICIENT_FREE_CASH"})
            continue

        pnl_pct = float(row.get("pnl_pct_gross") or 0.0) if outcome in {"WIN", "LOSS"} else 0.0
        pnl_usdt = trade_pnl_usdt(pnl_pct, notional) if outcome in {"WIN", "LOSS"} else 0.0
        pos = Position(
            slot=free_slot,
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            direction=row["direction"],
            entry_time=entry,
            exit_time=None if outcome == "OPEN" else exit_ts,
            outcome=outcome,
            pnl_pct_gross=pnl_pct,
            pnl_usdt=pnl_usdt,
            duration_seconds=None if row.get("duration_seconds") is None else float(row["duration_seconds"]),
            exit_reason=row.get("exit_reason"),
            exit_price=row.get("exit_price"),
            entry_price=row.get("entry_price"),
        )
        slots[free_slot] = pos
        free_cash -= notional
        reserved += notional
        result.accepted.append(_pos_payload(pos, {"accepted_at": iso_z(entry)}))
        snapshot(entry, "entry")

    remaining = [p for p in slots if p is not None and p.exit_time is not None]
    remaining.sort(key=lambda p: (p.exit_time, p.slot))
    for pos in remaining:
        accrue(pos.exit_time)
        close_pos(pos, pos.exit_time, "exit")

    end_ts = last_event_ts
    for p in slots:
        if p is not None:
            end_ts = p.entry_time if end_ts is None else max(end_ts, p.entry_time)
    if end_ts is not None:
        accrue(end_ts)
        snapshot(end_ts, "end")

    for p in slots:
        if p is not None:
            result.open_at_end.append(_pos_payload(p))

    result.free_cash = free_cash
    result.reserved = reserved
    result.realized_pnl = realized_pnl
    result.occupancy_seconds = occupancy
    return result
