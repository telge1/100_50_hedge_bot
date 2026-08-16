from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from gold_shadow.queries import SelectOnlyExecutor, assert_select
from gold_shadow.service import build_summary, list_signals, list_slots, list_trades


def _ts():
    return datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def empty_fetch(sql, params):
    assert_select(sql)
    u = sql.upper()
    if "FROM GOLD_SLOTS" in u and "GROUP BY" in u:
        return [{"status": "FREE", "n": 6}]
    if "FROM GOLD_DECISIONS" in u and "GROUP BY" in u:
        return []
    if "FROM GOLD_TRADES" in u and "GROUP BY" in u:
        return []
    if "COUNT(*)" in u:
        return [{"n": 0}]
    if "FROM GOLD_SLOTS" in u:
        return [
            {
                "slot_id": i,
                "status": "FREE",
                "symbol": None,
                "direction": None,
                "timeframe": None,
                "current_signal_id": None,
                "current_trade_id": None,
                "fixed_notional_usdt": Decimal("10"),
                "version": 0,
                "updated_at": _ts(),
                "reserved_at": None,
            }
            for i in range(1, 7)
        ]
    return []


def fixture_fetch(sql, params):
    assert_select(sql)
    u = sql.upper()
    if "GOLD_EXCHANGE_ORDERS" in u:
        return [{"n": 1}]
    if "GOLD_FILLS" in u:
        return [{"n": 0}]
    if "FROM GOLD_SLOTS" in u and "GROUP BY" in u:
        return [{"status": "OPEN", "n": 1}, {"status": "FREE", "n": 5}]
    if "COUNT(*) AS N FROM GOLD_SLOTS" in u:
        return [{"n": 6}]
    if "COUNT(*) AS N FROM GOLD_SIGNALS" in u:
        return [{"n": 3}]
    if "FROM GOLD_DECISIONS" in u and "GROUP BY" in u:
        return [
            {"decision": "ACCEPTED", "n": 1},
            {"decision": "SKIPPED_DUPLICATE", "n": 1},
            {"decision": "SKIPPED_NO_FREE_SLOT", "n": 1},
        ]
    if "FROM GOLD_TRADES" in u and "GROUP BY" in u:
        return [
            {"status": "OPEN", "exit_reason": None, "n": 1},
            {"status": "CLOSED", "exit_reason": "TP", "n": 1},
            {"status": "CLOSED", "exit_reason": "SL", "n": 1},
        ]
    if "SUM(NET_PNL)" in u:
        return [{"n": Decimal("1.5")}]
    if "COUNT(*) AS N FROM GOLD_TRADES" in u:
        return [{"n": 3}]
    if "FROM GOLD_SLOTS" in u:
        rows = empty_fetch("SELECT * FROM gold_slots ORDER BY slot_id", ())
        rows[0]["status"] = "OPEN"
        rows[0]["symbol"] = "ETHUSDT"
        rows[0]["direction"] = "LONG"
        rows[0]["timeframe"] = "4h"
        return rows
    if "FROM GOLD_SIGNALS" in u and "COUNT(*)" in u:
        return [{"n": 3}]
    if "FROM GOLD_SIGNALS S" in u:
        return [
            {
                "signal_id": "sig-4h",
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "timeframe": "4h",
                "confirmation_time": _ts(),
                "entry_time": _ts(),
                "theoretical_entry_price": Decimal("100"),
                "tp_pct": Decimal("1"),
                "sl_pct": Decimal("1"),
                "tier_a": True,
                "strategy_version": "wave_fade_frozen_f16ae32",
                "created_at": _ts(),
                "source_payload_json": {"strategy_pin": "5636a7d"},
                "decision": "ACCEPTED",
                "reason": "reserved",
                "slot_id": 1,
                "trade_id": "tr-open",
            },
            {
                "signal_id": "sig-15m",
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "timeframe": "15m",
                "confirmation_time": _ts(),
                "entry_time": _ts(),
                "theoretical_entry_price": Decimal("100"),
                "tp_pct": Decimal("1"),
                "sl_pct": Decimal("1"),
                "tier_a": True,
                "strategy_version": "wave_fade_frozen_f16ae32",
                "created_at": _ts(),
                "source_payload_json": {},
                "decision": "SKIPPED_DUPLICATE",
                "reason": "duplicate_symbol_entry_time",
                "slot_id": None,
                "trade_id": None,
            },
        ]
    if "FROM GOLD_TRADES" in u:
        return [
            {
                "trade_id": "tr-open",
                "signal_id": "sig-4h",
                "slot_id": 1,
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "timeframe": "4h",
                "status": "OPEN",
                "theoretical_entry": Decimal("100"),
                "actual_entry": Decimal("100"),
                "tp": Decimal("101"),
                "sl": Decimal("99"),
                "entry_time": _ts(),
                "exit_time": None,
                "exit_reason": None,
                "gross_pnl": Decimal("0"),
                "fees": Decimal("0"),
                "net_pnl": Decimal("0"),
            },
            {
                "trade_id": "tr-tp",
                "signal_id": "sig-tp",
                "slot_id": 2,
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "timeframe": "1h",
                "status": "CLOSED",
                "theoretical_entry": Decimal("100"),
                "actual_entry": Decimal("100"),
                "tp": Decimal("101"),
                "sl": Decimal("99"),
                "entry_time": _ts(),
                "exit_time": _ts(),
                "exit_reason": "TP",
                "gross_pnl": Decimal("1"),
                "fees": Decimal("0"),
                "net_pnl": Decimal("1"),
            },
            {
                "trade_id": "tr-sl",
                "signal_id": "sig-sl",
                "slot_id": 3,
                "symbol": "SOLUSDT",
                "direction": "SHORT",
                "timeframe": "30m",
                "status": "CLOSED",
                "theoretical_entry": Decimal("100"),
                "actual_entry": Decimal("100"),
                "tp": Decimal("99"),
                "sl": Decimal("101"),
                "entry_time": _ts(),
                "exit_time": _ts(),
                "exit_reason": "SL",
                "gross_pnl": Decimal("-1"),
                "fees": Decimal("0"),
                "net_pnl": Decimal("-1"),
            },
        ]
    if "FROM GOLD_SLOT_EVENTS" in u and "COUNT" in u:
        return [{"n": 1}]
    if "FROM GOLD_SLOT_EVENTS" in u:
        return [
            {
                "created_at": _ts(),
                "slot_id": 1,
                "old_status": "FREE",
                "new_status": "RESERVED",
                "signal_id": "sig-4h",
                "trade_id": None,
                "reason": "accepted",
            }
        ]
    return []
