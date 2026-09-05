"""Read-only canonical flow source reader for BTC research analyses.

Correct sources (Sep 2026):
- PUBLIC_TRADES: orderbook_analysis.public_trades_canonical (trade_ts, UTC)
- OPEN_INTEREST: orderbook_analysis.open_interest_events / open_interest_5s
- LIQUIDATIONS: orderbook_analysis.all_liquidations

Do NOT use btc_doge_research.research_* mirrors as live coverage oracles —
they may lag or end earlier than the live canonical tables.

Never treat an empty wrong-table query as proof that data is missing.
Always report the exact FQN + timestamp column queried.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PUBLIC_TRADES_FQN = "orderbook_analysis.public_trades_canonical"
PUBLIC_TRADES_TS = "trade_ts"
OI_EVENTS_FQN = "orderbook_analysis.open_interest_events"
OI_EVENTS_TS = "event_time"
LIQUIDATIONS_FQN = "orderbook_analysis.all_liquidations"
LIQUIDATIONS_TS = "event_time"

# Stale / non-canonical mirrors (do not use for live availability)
STALE_PUBLIC_TRADES_MIRROR = "btc_doge_research.research_public_trades"
STALE_OI_MIRROR = "btc_doge_research.research_open_interest_observations"
STALE_LIQ_MIRROR = "btc_doge_research.research_liquidation_events"


@dataclass(frozen=True)
class FlowQueryResult:
    source: str
    fqn: str
    ts_column: str
    symbol: str
    start_utc: str
    end_utc: str
    row_count: int
    empty: bool
    availability: str  # DATA_PRESENT | DATA_ABSENT | QUERY_ERROR
    detail: dict[str, Any]


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def query_public_trades_window(client: Any, *, symbol: str, start: datetime, end: datetime) -> FlowQueryResult:
    a, b = _iso(start), _iso(end)
    sql = f"""
    SELECT
      count() AS n,
      uniqExact(trade_id) AS uniq_ids,
      min({PUBLIC_TRADES_TS}), max({PUBLIC_TRADES_TS}),
      sumIf(notional, side='Buy'), sumIf(notional, side='Sell'),
      sumIf(notional, side='Buy') - sumIf(notional, side='Sell')
    FROM {PUBLIC_TRADES_FQN}
    WHERE symbol = {{symbol:String}}
      AND {PUBLIC_TRADES_TS} >= toDateTime64({{start:String}}, 3, 'UTC')
      AND {PUBLIC_TRADES_TS} <  toDateTime64({{end:String}}, 3, 'UTC')
    """
    try:
        # clickhouse_connect parameter style varies; use literal-safe formatting via client params if available
        row = client.query(
            f"""
            SELECT
              count() AS n,
              uniqExact(trade_id) AS uniq_ids,
              min({PUBLIC_TRADES_TS}), max({PUBLIC_TRADES_TS}),
              sumIf(notional, side='Buy'), sumIf(notional, side='Sell'),
              sumIf(notional, side='Buy') - sumIf(notional, side='Sell')
            FROM {PUBLIC_TRADES_FQN}
            WHERE symbol = %(symbol)s
              AND {PUBLIC_TRADES_TS} >= toDateTime64(%(start)s, 3, 'UTC')
              AND {PUBLIC_TRADES_TS} <  toDateTime64(%(end)s, 3, 'UTC')
            """,
            parameters={"symbol": symbol, "start": a, "end": b},
        ).result_rows[0]
        n = int(row[0])
        return FlowQueryResult(
            source="PUBLIC_TRADES",
            fqn=PUBLIC_TRADES_FQN,
            ts_column=PUBLIC_TRADES_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=n,
            empty=n == 0,
            availability="DATA_PRESENT" if n > 0 else "DATA_ABSENT",
            detail={
                "uniq_ids": int(row[1]),
                "min_ts": str(row[2]),
                "max_ts": str(row[3]),
                "buy_quote": float(row[4] or 0),
                "sell_quote": float(row[5] or 0),
                "taker_delta_quote": float(row[6] or 0),
                "queried_stale_mirror": False,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface exact failure, never silent "unavailable"
        return FlowQueryResult(
            source="PUBLIC_TRADES",
            fqn=PUBLIC_TRADES_FQN,
            ts_column=PUBLIC_TRADES_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=0,
            empty=True,
            availability="QUERY_ERROR",
            detail={"error": str(exc)[:500]},
        )


def query_oi_window(client: Any, *, symbol: str, start: datetime, end: datetime) -> FlowQueryResult:
    a, b = _iso(start), _iso(end)
    try:
        row = client.query(
            f"""
            SELECT count(), min({OI_EVENTS_TS}), max({OI_EVENTS_TS}),
                   argMin(open_interest, {OI_EVENTS_TS}), argMax(open_interest, {OI_EVENTS_TS})
            FROM {OI_EVENTS_FQN}
            WHERE symbol = %(symbol)s
              AND {OI_EVENTS_TS} >= toDateTime64(%(start)s, 3, 'UTC')
              AND {OI_EVENTS_TS} <  toDateTime64(%(end)s, 3, 'UTC')
            """,
            parameters={"symbol": symbol, "start": a, "end": b},
        ).result_rows[0]
        n = int(row[0])
        return FlowQueryResult(
            source="OPEN_INTEREST",
            fqn=OI_EVENTS_FQN,
            ts_column=OI_EVENTS_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=n,
            empty=n == 0,
            availability="DATA_PRESENT" if n > 0 else "DATA_ABSENT",
            detail={
                "min_ts": str(row[1]),
                "max_ts": str(row[2]),
                "oi_start": float(row[3] or 0),
                "oi_end": float(row[4] or 0),
                "oi_delta": float(row[4] or 0) - float(row[3] or 0),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return FlowQueryResult(
            source="OPEN_INTEREST",
            fqn=OI_EVENTS_FQN,
            ts_column=OI_EVENTS_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=0,
            empty=True,
            availability="QUERY_ERROR",
            detail={"error": str(exc)[:500]},
        )


def query_liquidations_window(client: Any, *, symbol: str, start: datetime, end: datetime) -> FlowQueryResult:
    a, b = _iso(start), _iso(end)
    try:
        row = client.query(
            f"""
            SELECT count(), min({LIQUIDATIONS_TS}), max({LIQUIDATIONS_TS}),
                   countIf(liquidated_position_side='LIQUIDATED_LONG'),
                   countIf(liquidated_position_side='LIQUIDATED_SHORT'),
                   sum(size), sum(notional_estimate)
            FROM {LIQUIDATIONS_FQN}
            WHERE symbol = %(symbol)s
              AND {LIQUIDATIONS_TS} >= toDateTime64(%(start)s, 3, 'UTC')
              AND {LIQUIDATIONS_TS} <  toDateTime64(%(end)s, 3, 'UTC')
            """,
            parameters={"symbol": symbol, "start": a, "end": b},
        ).result_rows[0]
        n = int(row[0])
        return FlowQueryResult(
            source="LIQUIDATIONS",
            fqn=LIQUIDATIONS_FQN,
            ts_column=LIQUIDATIONS_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=n,
            empty=n == 0,
            availability="DATA_PRESENT" if n > 0 else "DATA_ABSENT",
            detail={
                "min_ts": str(row[1]),
                "max_ts": str(row[2]),
                "n_long": int(row[3]),
                "n_short": int(row[4]),
                "base_sum": float(row[5] or 0),
                "notional_sum": float(row[6] or 0),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return FlowQueryResult(
            source="LIQUIDATIONS",
            fqn=LIQUIDATIONS_FQN,
            ts_column=LIQUIDATIONS_TS,
            symbol=symbol,
            start_utc=a,
            end_utc=b,
            row_count=0,
            empty=True,
            availability="QUERY_ERROR",
            detail={"error": str(exc)[:500]},
        )


def availability_label(result: FlowQueryResult) -> str:
    """Human-readable status that never silently says 'nicht verfügbar' on wrong table."""
    return (
        f"{result.availability} via {result.fqn}.{result.ts_column} "
        f"rows={result.row_count} window=[{result.start_utc} .. {result.end_utc})"
    )
