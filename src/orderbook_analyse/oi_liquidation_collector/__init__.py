"""Bybit linear Open Interest + allLiquidation collector (no orderbook, no trading)."""

from __future__ import annotations

ALLOWED_TABLES = frozenset(
    {
        "all_liquidations",
        "open_interest_events",
        "open_interest_5s",
        "open_interest_5m_history",
        "oi_liquidation_health",
    }
)
FORBIDDEN_TABLES = frozenset(
    {
        "orderbook_deltas",
        "public_trades",
        "ticker_samples",
        "liquidations",
        "candles_1m",
        "public_trades_canonical",
        "public_trades_archive",
    }
)
EXCHANGE = "BYBIT"
CATEGORY = "linear"
SOURCE_WS = "BYBIT_WS_REALTIME"
SOURCE_REST_5M = "BYBIT_REST_5M_HISTORY"
LIQUIDATED_LONG = "LIQUIDATED_LONG"
LIQUIDATED_SHORT = "LIQUIDATED_SHORT"
