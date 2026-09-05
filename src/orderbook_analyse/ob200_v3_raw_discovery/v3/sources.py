"""Read-only market source loaders + semantics for OB200 V3."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import load_trades_1s
from orderbook_analyse.oi_liq_impact_l2.contracts import (
    AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION,
    AGGRESSOR_SIDE_BY_DIRECTION,
    LIQUIDATION_SIDE_BY_DIRECTION,
)

_SETTINGS = {"max_execution_time": 300, "receive_timeout": 320}

SOURCE_SEMANTICS: dict[str, Any] = {
    "public_trades": {
        "database": "orderbook_analysis",
        "table": "public_trades_canonical",
        "time_column": "trade_ts",
        "timezone": "UTC",
        "side_field": "side",
        "side_semantics": "taker/aggressor Buy|Sell (Bybit WS / CH ingest; NOT maker)",
        "evidence": [
            "src/orderbook_analyse/public_trade_source/protocol.py NormalizedPublicTrade.side",
            "src/orderbook_analyse/market_event_report/loaders.py SIDE_SEMANTICS",
        ],
        "notional": "price * size (USDT)",
        "grain_used_for_join": "1s aggregate via load_trades_1s",
        "aggressor_by_direction": dict(AGGRESSOR_SIDE_BY_DIRECTION),
        "aggressive_notional_column_by_direction": dict(AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION),
    },
    "open_interest": {
        "primary_for_asof": {
            "database": "orderbook_analysis",
            "table": "open_interest_5s",
            "time_column": "bucket_time",
            "reason": (
                "Regular 5s snapshots; stable for backward/as-of joins. "
                "F1 discovery also uses open_interest_5s for minute OI."
            ),
        },
        "secondary_event_stream": {
            "database": "orderbook_analysis",
            "table": "open_interest_events",
            "time_column": "event_time",
            "reason": "Denser delta/event stream; not preferred for as-of without dedupe.",
        },
        "unit": "contracts (open_interest) + USDT value (open_interest_value)",
        "asof_policy": "backward only; max_staleness_s configurable; never future",
    },
    "liquidations": {
        "database": "orderbook_analysis",
        "table": "all_liquidations",
        "time_column": "event_time",
        "side_field": "liquidated_position_side",
        "side_semantics": (
            "Bybit allLiquidation S is liquidated POSITION side: "
            "S=Buy→LIQUIDATED_LONG; S=Sell→LIQUIDATED_SHORT (not aggressor)."
        ),
        "evidence": [
            "src/orderbook_analyse/oi_liquidation_collector/logic.py interpret_liquidated_position_side"
        ],
        "direction_map": dict(LIQUIDATION_SIDE_BY_DIRECTION),
        "notional_field": "notional_estimate",
    },
    "flush_direction_economics": {
        "LONG": "price down + OI down + LIQUIDATED_LONG + aggressive Sell (F1 directional_flush_observed)",
        "SHORT": "price up + OI down + LIQUIDATED_SHORT + aggressive Buy",
        "evidence": "src/orderbook_analyse/oi_liq_impact_l2/discovery.py + contracts.py",
    },
}


def _client() -> Any:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    return get_clickhouse_client()


def load_oi_5s(client: Any, *, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = client.query(
        """
        SELECT bucket_time, toFloat64(open_interest) AS open_interest,
               toFloat64(open_interest_value) AS open_interest_value
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol = {s:String}
          AND bucket_time >= {a:DateTime64(3,'UTC')}
          AND bucket_time < {b:DateTime64(3,'UTC')}
        ORDER BY bucket_time
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(rows, columns=["bucket_time", "open_interest", "open_interest_value"])
    if not frame.empty:
        frame["bucket_time"] = pd.to_datetime(frame["bucket_time"], utc=True)
    return frame


def load_liquidations(client: Any, *, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = client.query(
        """
        SELECT event_time, liquidated_position_side, position_side_raw,
               toFloat64(size) AS size,
               toFloat64(bankruptcy_price) AS bankruptcy_price,
               toFloat64(notional_estimate) AS notional_estimate
        FROM orderbook_analysis.all_liquidations
        WHERE symbol = {s:String}
          AND event_time >= {a:DateTime64(3,'UTC')}
          AND event_time < {b:DateTime64(3,'UTC')}
        ORDER BY event_time
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(
        rows,
        columns=[
            "event_time",
            "liquidated_position_side",
            "position_side_raw",
            "size",
            "bankruptcy_price",
            "notional_estimate",
        ],
    )
    if not frame.empty:
        frame["event_time"] = pd.to_datetime(frame["event_time"], utc=True)
    return frame


def load_market_bundle(
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, pd.DataFrame]]:
    client = _client()
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in symbols:
        out[sym] = {
            "trades_1s": load_trades_1s(client, symbol=sym, start=start, end=end),
            "oi_5s": load_oi_5s(client, symbol=sym, start=start, end=end),
            "liquidations": load_liquidations(client, symbol=sym, start=start, end=end),
        }
    return out


def write_source_semantics(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(SOURCE_SEMANTICS, indent=2) + "\n", encoding="utf-8")


def source_integrity_rows(
    bundle: dict[str, dict[str, pd.DataFrame]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sym, tables in bundle.items():
        for name, df in tables.items():
            if df.empty:
                rows.append(
                    {
                        "symbol": sym,
                        "table_alias": name,
                        "rows": 0,
                        "min_ts": None,
                        "max_ts": None,
                        "status": "EMPTY",
                        "window_start_utc": start.isoformat().replace("+00:00", "Z"),
                        "window_end_utc": end.isoformat().replace("+00:00", "Z"),
                    }
                )
                continue
            tcol = {"trades_1s": "second", "oi_5s": "bucket_time", "liquidations": "event_time"}[name]
            rows.append(
                {
                    "symbol": sym,
                    "table_alias": name,
                    "rows": int(len(df)),
                    "min_ts": str(df[tcol].min()),
                    "max_ts": str(df[tcol].max()),
                    "status": "OK",
                    "window_start_utc": start.isoformat().replace("+00:00", "Z"),
                    "window_end_utc": end.isoformat().replace("+00:00", "Z"),
                }
            )
    return rows


def ms_to_utc(ts_ms: int | None) -> datetime | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
