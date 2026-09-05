"""Read-only ClickHouse trade loader (individual trades for wall-band attribution)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

_SETTINGS = {"max_execution_time": 300, "receive_timeout": 320}


def _client() -> Any:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    return get_clickhouse_client()


def load_public_trades(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Load canonical public trades; side is taker/aggressor Buy|Sell."""
    client = _client()
    rows = client.query(
        """
        SELECT
          trade_ts,
          trade_id,
          side,
          toFloat64(price) AS price,
          toFloat64(size) AS size,
          toFloat64(price) * toFloat64(size) AS notional
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = {s:String}
          AND trade_ts >= {a:DateTime64(3,'UTC')}
          AND trade_ts < {b:DateTime64(3,'UTC')}
        ORDER BY trade_ts, trade_id
        """,
        parameters={"s": symbol, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    frame = pd.DataFrame(
        rows, columns=["trade_ts", "trade_id", "side", "price", "size", "notional"]
    )
    if frame.empty:
        return frame
    frame["trade_ts"] = pd.to_datetime(frame["trade_ts"], utc=True)
    # Normalize to ns then ms — pandas may ingest CH DateTime64 as us.
    frame["ts_ms"] = (
        frame["trade_ts"].dt.tz_convert("UTC").astype("int64") // 1_000_000
    ).astype("int64")
    # If values look like seconds (CH/pandas us path already divided wrong), repair.
    if len(frame) and int(frame["ts_ms"].iloc[0]) < 10_000_000_000:
        frame["ts_ms"] = (frame["trade_ts"].astype("int64") // 1000).astype("int64")
    # dedupe reconnect duplicates
    frame = frame.drop_duplicates(subset=["trade_id"], keep="first")
    return frame.reset_index(drop=True)


def source_integrity_rows(
    trades_by_symbol: dict[str, pd.DataFrame],
    *,
    start: datetime,
    end: datetime,
    n_samples: dict[str, int],
    n_segments: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sym, df in trades_by_symbol.items():
        rows.append(
            {
                "symbol": sym,
                "source": "public_trades_canonical",
                "rows": int(len(df)),
                "min_ts": None if df.empty else str(df["trade_ts"].min()),
                "max_ts": None if df.empty else str(df["trade_ts"].max()),
                "side_semantics": "taker/aggressor Buy|Sell",
                "notional": "price*size",
                "status": "OK" if not df.empty else "EMPTY",
                "window_start_utc": start.isoformat().replace("+00:00", "Z"),
                "window_end_utc": end.isoformat().replace("+00:00", "Z"),
                "l2_samples": n_samples.get(sym, 0),
                "closed_segments": n_segments,
            }
        )
    return rows
