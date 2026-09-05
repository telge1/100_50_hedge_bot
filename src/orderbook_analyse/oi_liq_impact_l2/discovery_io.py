"""Read-only ClickHouse loaders for OI/liquidation/impact/L2 discovery.

The module does not create a client or execute a query at import time.
All windows use half-open UTC semantics: ``[start, end)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.contracts import (
    ORDERBOOK_DEPTH,
    ORDERBOOK_GENUINE_SQL,
    ORDERBOOK_PARSER_VERSION,
    ORDERBOOK_TABLE,
)

_SETTINGS = {"max_execution_time": 300, "receive_timeout": 320}


def _query(
    client: Any,
    sql: str,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[tuple[Any, ...]]:
    return client.query(
        sql,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows


def _frame(
    rows: list[tuple[Any, ...]], columns: tuple[str, ...], time_col: str
) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(columns))
    if not frame.empty:
        frame[time_col] = pd.to_datetime(frame[time_col], utc=True)
    return frame


def load_discovery_inputs(
    client: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    label_end: datetime,
) -> dict[str, pd.DataFrame]:
    """Load one symbol without materializing raw 1s rows.

    Candles extend to ``label_end`` solely for the separate outcome sidecar.
    All predictor sources stop at ``end``.
    """
    candles = _frame(
        _query(
            client,
            """
            SELECT open_time, open, high, low, close, volume
            FROM signal_generator.candles_1m FINAL
            WHERE symbol = {s:String} AND interval = '1m'
              AND open_time >= {a:DateTime64(3,'UTC')}
              AND open_time < {b:DateTime64(3,'UTC')}
            ORDER BY open_time
            """,
            symbol=symbol,
            start=start,
            end=label_end,
        ),
        ("open_time", "open", "high", "low", "close", "volume"),
        "open_time",
    )
    trades = _frame(
        _query(
            client,
            """
            SELECT
              toStartOfMinute(trade_ts) AS minute,
              count() AS trade_count,
              sumIf(toFloat64(size) * toFloat64(price), side = 'Buy')
                AS buy_notional,
              sumIf(toFloat64(size) * toFloat64(price), side = 'Sell')
                AS sell_notional,
              sumIf(toFloat64(size), side = 'Buy') AS buy_size,
              sumIf(toFloat64(size), side = 'Sell') AS sell_size,
              sum(toFloat64(size) * toFloat64(price)) AS total_notional
            FROM orderbook_analysis.public_trades_canonical
            WHERE symbol = {s:String}
              AND trade_ts >= {a:DateTime64(3,'UTC')}
              AND trade_ts < {b:DateTime64(3,'UTC')}
            GROUP BY minute
            ORDER BY minute
            """,
            symbol=symbol,
            start=start,
            end=end,
        ),
        (
            "minute",
            "trade_count",
            "buy_notional",
            "sell_notional",
            "buy_size",
            "sell_size",
            "total_notional",
        ),
        "minute",
    )
    oi = _frame(
        _query(
            client,
            """
            SELECT
              toStartOfMinute(bucket_time) AS minute,
              argMax(open_interest, bucket_time) AS open_interest,
              argMax(open_interest_value, bucket_time) AS open_interest_value,
              argMax(state_valid, bucket_time) AS state_valid,
              count() AS samples
            FROM orderbook_analysis.open_interest_5s
            WHERE symbol = {s:String}
              AND bucket_time >= {a:DateTime64(3,'UTC')}
              AND bucket_time < {b:DateTime64(3,'UTC')}
            GROUP BY minute
            ORDER BY minute
            """,
            symbol=symbol,
            start=start,
            end=end,
        ),
        ("minute", "open_interest", "open_interest_value", "state_valid", "samples"),
        "minute",
    )
    liquidations = _frame(
        _query(
            client,
            """
            SELECT
              toStartOfMinute(event_time) AS minute,
              countIf(liquidated_position_side = 'LIQUIDATED_LONG')
                AS liquidated_long_count,
              countIf(liquidated_position_side = 'LIQUIDATED_SHORT')
                AS liquidated_short_count,
              sumIf(toFloat64(notional_estimate),
                    liquidated_position_side = 'LIQUIDATED_LONG')
                AS liquidated_long_notional,
              sumIf(toFloat64(notional_estimate),
                    liquidated_position_side = 'LIQUIDATED_SHORT')
                AS liquidated_short_notional
            FROM orderbook_analysis.all_liquidations
            WHERE symbol = {s:String}
              AND event_time >= {a:DateTime64(3,'UTC')}
              AND event_time < {b:DateTime64(3,'UTC')}
            GROUP BY minute
            ORDER BY minute
            """,
            symbol=symbol,
            start=start,
            end=end,
        ),
        (
            "minute",
            "liquidated_long_count",
            "liquidated_short_count",
            "liquidated_long_notional",
            "liquidated_short_notional",
        ),
        "minute",
    )
    orderbook = _frame(
        _query(
            client,
            f"""
            WITH
              {ORDERBOOK_GENUINE_SQL} AS genuine
            SELECT
              toStartOfMinute(bucket_start) AS minute,
              count() AS seconds,
              countIf(is_valid = 1) AS valid_seconds,
              countIf(is_valid = 0) AS invalid_seconds,
              countIf(has(splitByChar(',', quality_flags), 'carried_forward'))
                AS carried_forward_seconds,
              countIf(genuine) AS genuine_seconds,
              avgIf(spread_bps, genuine) AS genuine_spread_bps_mean,
              avgIf(imbalance_l50, genuine) AS genuine_imbalance_l50_mean,
              avgIf(bid_qty_l50, genuine) AS genuine_bid_depth_l50_mean,
              avgIf(ask_qty_l50, genuine) AS genuine_ask_depth_l50_mean,
              sumIf(ofi, genuine AND ofi IS NOT NULL) AS genuine_ofi_sum,
              sumIf(bid_qty_added, genuine AND bid_qty_added IS NOT NULL)
                AS genuine_bid_qty_added,
              sumIf(bid_qty_removed, genuine AND bid_qty_removed IS NOT NULL)
                AS genuine_bid_qty_removed,
              sumIf(ask_qty_added, genuine AND ask_qty_added IS NOT NULL)
                AS genuine_ask_qty_added,
              sumIf(ask_qty_removed, genuine AND ask_qty_removed IS NOT NULL)
                AS genuine_ask_qty_removed,
              sumIf(bid_add_count, genuine AND bid_add_count IS NOT NULL)
                AS genuine_bid_add_count,
              sumIf(bid_remove_count, genuine AND bid_remove_count IS NOT NULL)
                AS genuine_bid_remove_count,
              sumIf(ask_add_count, genuine AND ask_add_count IS NOT NULL)
                AS genuine_ask_add_count,
              sumIf(ask_remove_count, genuine AND ask_remove_count IS NOT NULL)
                AS genuine_ask_remove_count
            FROM {ORDERBOOK_TABLE} FINAL
            WHERE symbol = {{s:String}}
              AND parser_version = '{ORDERBOOK_PARSER_VERSION}'
              AND depth = {ORDERBOOK_DEPTH}
              AND bucket_start >= {{a:DateTime64(3,'UTC')}}
              AND bucket_start < {{b:DateTime64(3,'UTC')}}
            GROUP BY minute
            ORDER BY minute
            """,
            symbol=symbol,
            start=start,
            end=end,
        ),
        (
            "minute",
            "seconds",
            "valid_seconds",
            "invalid_seconds",
            "carried_forward_seconds",
            "genuine_seconds",
            "genuine_spread_bps_mean",
            "genuine_imbalance_l50_mean",
            "genuine_bid_depth_l50_mean",
            "genuine_ask_depth_l50_mean",
            "genuine_ofi_sum",
            "genuine_bid_qty_added",
            "genuine_bid_qty_removed",
            "genuine_ask_qty_added",
            "genuine_ask_qty_removed",
            "genuine_bid_add_count",
            "genuine_bid_remove_count",
            "genuine_ask_add_count",
            "genuine_ask_remove_count",
        ),
        "minute",
    )
    return {
        "candles": candles,
        "trades": trades,
        "open_interest": oi,
        "liquidations": liquidations,
        "orderbook": orderbook,
    }
