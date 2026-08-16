"""Assemble Gold Shadow read models. No writes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import (
    DEFAULT_PAGE_SIZE,
    FROZEN_PIN,
    MAX_PAGE_SIZE,
    SLOT_COUNT,
    STRATEGY_ID,
    TIMEFRAMES,
    UNIVERSE_SIZE,
)
from .queries import SelectOnlyExecutor, clamp_limit, clamp_offset

SKIP_DECISIONS = (
    "SKIPPED_DUPLICATE",
    "SKIPPED_NO_FREE_SLOT",
    "SKIPPED_SYMBOL_ALREADY_OPEN",
    "SKIPPED_INSUFFICIENT_CASH",
    "SKIPPED_RISK_LIMIT",
    "SKIPPED_STALE_SIGNAL",
)


def _dec(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _duration_s(updated: Any) -> int | None:
    if not isinstance(updated, datetime):
        return None
    moment = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()))


def empty_summary(*, connected: bool, message: str) -> dict[str, Any]:
    return {
        "success": True,
        "connected": connected,
        "offline": not connected,
        "message": message,
        "mode": "SHADOW",
        "strategy_id": STRATEGY_ID,
        "frozen_pin": FROZEN_PIN,
        "universe": UNIVERSE_SIZE,
        "timeframes": list(TIMEFRAMES),
        "slot_count": SLOT_COUNT,
        "slots_by_status": {},
        "signals_total": 0,
        "accepted": 0,
        "skipped": 0,
        "open_trades": 0,
        "closed_trades": 0,
        "tp": 0,
        "sl": 0,
        "net_pnl": "0",
        "exchange_orders": 0,
        "exchange_fills": 0,
        "unexpected_exchange_activity": False,
        "wallet": "Noch nicht implementiert",
        "empty_forward": True,
    }


def build_summary(ex: SelectOnlyExecutor) -> dict[str, Any]:
    payload = empty_summary(connected=True, message="wave_fade_gold_live_dev")
    slots = ex.fetchall("SELECT status, COUNT(*) AS n FROM gold_slots GROUP BY status")
    payload["slots_by_status"] = {str(row["status"]): int(row["n"]) for row in slots}
    payload["slot_count"] = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_slots")[0]["n"])
    payload["signals_total"] = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_signals")[0]["n"])
    decisions = ex.fetchall("SELECT decision, COUNT(*) AS n FROM gold_decisions GROUP BY decision")
    by_dec = {str(row["decision"]): int(row["n"]) for row in decisions}
    payload["accepted"] = by_dec.get("ACCEPTED", 0)
    payload["skipped"] = sum(by_dec.get(name, 0) for name in SKIP_DECISIONS)
    payload["decision_counts"] = by_dec
    trades = ex.fetchall("SELECT status, exit_reason, COUNT(*) AS n FROM gold_trades GROUP BY status, exit_reason")
    open_n = closed_n = tp = sl = 0
    for row in trades:
        if str(row["status"]) == "OPEN":
            open_n += int(row["n"])
        elif str(row["status"]) == "CLOSED":
            closed_n += int(row["n"])
            if row["exit_reason"] == "TP":
                tp += int(row["n"])
            elif row["exit_reason"] == "SL":
                sl += int(row["n"])
    payload["open_trades"] = open_n
    payload["closed_trades"] = closed_n
    payload["tp"] = tp
    payload["sl"] = sl
    pnl_rows = ex.fetchall("SELECT COALESCE(SUM(net_pnl), 0) AS n FROM gold_trades")
    payload["net_pnl"] = _dec(pnl_rows[0]["n"]) if pnl_rows else "0"
    payload["exchange_orders"] = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_exchange_orders")[0]["n"])
    payload["exchange_fills"] = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_fills")[0]["n"])
    payload["unexpected_exchange_activity"] = payload["exchange_orders"] > 0 or payload["exchange_fills"] > 0
    payload["empty_forward"] = payload["signals_total"] == 0
    return payload


def list_slots(ex: SelectOnlyExecutor) -> list[dict[str, Any]]:
    rows = ex.fetchall(
        "SELECT slot_id, status, symbol, direction, timeframe, current_signal_id, "
        "current_trade_id, fixed_notional_usdt, version, updated_at, reserved_at "
        "FROM gold_slots ORDER BY slot_id"
    )
    out = []
    for row in rows:
        out.append(
            {
                "slot_id": int(row["slot_id"]),
                "status": str(row["status"] or "UNKNOWN"),
                "symbol": row["symbol"],
                "direction": row["direction"],
                "timeframe": row["timeframe"],
                "signal_id": row["current_signal_id"],
                "trade_id": row["current_trade_id"],
                "notional": _dec(row["fixed_notional_usdt"]),
                "version": int(row["version"] or 0),
                "updated_at": _iso(row["updated_at"]),
                "duration_s": _duration_s(row["updated_at"]),
            }
        )
    return out


def list_signals(
    ex: SelectOnlyExecutor,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    direction: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    where = ["1=1"]
    params: list[Any] = []
    if symbol:
        where.append("s.symbol = %s")
        params.append(symbol)
    if timeframe:
        where.append("s.timeframe = %s")
        params.append(timeframe)
    if direction:
        where.append("s.direction = %s")
        params.append(direction)
    if decision == "ACCEPTED":
        where.append("d.decision = %s")
        params.append("ACCEPTED")
    elif decision == "SKIPPED":
        where.append("d.decision LIKE %s")
        params.append("SKIPPED_%")
    elif decision:
        where.append("d.decision = %s")
        params.append(decision)
    if reason:
        where.append("d.reason = %s")
        params.append(reason)
    if start:
        where.append("s.entry_time >= %s")
        params.append(start)
    if end:
        where.append("s.entry_time <= %s")
        params.append(end)
    clause = " AND ".join(where)
    total = int(
        ex.fetchall(
            f"SELECT COUNT(*) AS n FROM gold_signals s LEFT JOIN gold_decisions d ON d.signal_id = s.signal_id WHERE {clause}",
            params,
        )[0]["n"]
    )
    lim = clamp_limit(limit, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    off = clamp_offset(offset)
    rows = ex.fetchall(
        f"SELECT s.signal_id, s.symbol, s.direction, s.timeframe, s.confirmation_time, s.entry_time, "
        f"s.theoretical_entry_price, s.tp_pct, s.sl_pct, s.tier_a, s.strategy_version, s.created_at, "
        f"s.source_payload_json, d.decision, d.reason, d.slot_id, t.trade_id "
        f"FROM gold_signals s "
        f"LEFT JOIN gold_decisions d ON d.signal_id = s.signal_id "
        f"LEFT JOIN gold_trades t ON t.signal_id = s.signal_id "
        f"WHERE {clause} ORDER BY s.created_at DESC LIMIT %s OFFSET %s",
        [*params, lim, off],
    )
    items = []
    for row in rows:
        payload = row.get("source_payload_json") or {}
        pin = ""
        if isinstance(payload, dict):
            pin = str(payload.get("strategy_pin") or payload.get("edges_version") or "")
        items.append(
            {
                "signal_id": row["signal_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "timeframe": row["timeframe"],
                "confirmation_time": _iso(row["confirmation_time"]),
                "entry_time": _iso(row["entry_time"]),
                "theoretical_entry": _dec(row["theoretical_entry_price"]),
                "tp_pct": _dec(row["tp_pct"]),
                "sl_pct": _dec(row["sl_pct"]),
                "tier_a": bool(row["tier_a"]),
                "strategy_version": row["strategy_version"],
                "candle_pin": pin,
                "created_at": _iso(row["created_at"]),
                "decision": row["decision"],
                "reason": row["reason"],
                "slot_id": row["slot_id"],
                "trade_id": row["trade_id"],
            }
        )
    return {"items": items, "total": total, "limit": lim, "offset": off}


def list_trades(
    ex: SelectOnlyExecutor,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    lim = clamp_limit(limit, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    off = clamp_offset(offset)
    total = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_trades")[0]["n"])
    rows = ex.fetchall(
        "SELECT trade_id, signal_id, slot_id, symbol, direction, timeframe, status, "
        "theoretical_entry, actual_entry, tp, sl, entry_time, exit_time, exit_reason, "
        "gross_pnl, fees, net_pnl FROM gold_trades "
        "ORDER BY CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END, created_at DESC "
        "LIMIT %s OFFSET %s",
        (lim, off),
    )
    items = []
    for row in rows:
        items.append(
            {
                "trade_id": row["trade_id"],
                "signal_id": row["signal_id"],
                "slot_id": row["slot_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "timeframe": row["timeframe"],
                "status": row["status"],
                "theoretical_entry": _dec(row["theoretical_entry"]),
                "shadow_entry": _dec(row["actual_entry"]),
                "tp": _dec(row["tp"]),
                "sl": _dec(row["sl"]),
                "entry_time": _iso(row["entry_time"]),
                "exit_time": _iso(row["exit_time"]),
                "exit_reason": row["exit_reason"],
                "gross_pnl": _dec(row["gross_pnl"]),
                "fees": _dec(row["fees"]),
                "net_pnl": _dec(row["net_pnl"]),
                "shadow_label": "SHADOW – KEINE ECHTE ORDER",
            }
        )
    return {"items": items, "total": total, "limit": lim, "offset": off}


def list_decisions(ex: SelectOnlyExecutor) -> dict[str, int]:
    rows = ex.fetchall("SELECT decision, COUNT(*) AS n FROM gold_decisions GROUP BY decision")
    counts = {name: 0 for name in ("ACCEPTED", *SKIP_DECISIONS)}
    for row in rows:
        counts[str(row["decision"])] = int(row["n"])
    return counts


def list_events(
    ex: SelectOnlyExecutor,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    lim = clamp_limit(limit, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    off = clamp_offset(offset)
    total = int(ex.fetchall("SELECT COUNT(*) AS n FROM gold_slot_events")[0]["n"])
    rows = ex.fetchall(
        "SELECT created_at, slot_id, old_status, new_status, signal_id, trade_id, reason "
        "FROM gold_slot_events ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (lim, off),
    )
    items = [
        {
            "created_at": _iso(row["created_at"]),
            "slot_id": row["slot_id"],
            "old_status": row["old_status"],
            "new_status": row["new_status"],
            "signal_id": row["signal_id"],
            "trade_id": row["trade_id"],
            "reason": row["reason"],
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": lim, "offset": off}
