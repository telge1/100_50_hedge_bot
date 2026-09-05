"""Optional read-only ClickHouse joins for trades / OI / liquidations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def try_load_market_context(
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Best-effort CH pull; returns availability flags when tables/columns missing."""
    out: dict[str, Any] = {
        "available": False,
        "trades_1s": None,
        "oi": None,
        "liquidations": None,
        "ob_1s": None,
        "error": None,
        "notes": [],
    }
    try:
        from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

        load_clickhouse_settings()
        client = get_clickhouse_client()
    except Exception as exc:
        out["error"] = f"ch_connect:{type(exc).__name__}:{exc}"
        return out

    sym_list = ", ".join(f"'{s}'" for s in symbols)
    start_s = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # orderbook 1s (for cross-check only)
    try:
        sql = f"""
        SELECT symbol, bucket_start, mid_price, spread_bps, ofi,
               bid_wall_price, bid_wall_qty, ask_wall_price, ask_wall_qty
        FROM orderbook_analysis.orderbook_features_1s_v2
        WHERE depth=200 AND parser_version='ob200_v3'
          AND symbol IN ({sym_list})
          AND bucket_start >= toDateTime64('{start_s}', 3, 'UTC')
          AND bucket_start < toDateTime64('{end_s}', 3, 'UTC')
        ORDER BY symbol, bucket_start
        """
        result = client.query(sql)
        cols = ["symbol", "bucket_start", "mid_price", "spread_bps", "ofi",
                "bid_wall_price", "bid_wall_qty", "ask_wall_price", "ask_wall_qty"]
        out["ob_1s"] = pd.DataFrame(result.result_rows, columns=cols)
        out["notes"].append(f"ob_1s_rows={len(out['ob_1s'])}")
        out["available"] = True
    except Exception as exc:
        out["notes"].append(f"ob_1s_unavailable:{type(exc).__name__}")

    # public trades 1s if present
    for table in (
        "orderbook_analysis.public_trades_canonical",
        "orderbook_analysis.public_trades_1s",
        "orderbook_analysis.bybit_public_trades_1s",
        "orderbook_analysis.trades_1s",
    ):
        try:
            # Probe columns via LIMIT; time column may be ts / bucket_start / trade_time
            for tcol in ("bucket_start", "ts", "trade_time", "timestamp", "event_time"):
                try:
                    sql = f"""
                    SELECT count() AS n
                    FROM {table}
                    WHERE symbol IN ({sym_list})
                      AND {tcol} >= toDateTime64('{start_s}', 3, 'UTC')
                      AND {tcol} < toDateTime64('{end_s}', 3, 'UTC')
                    """
                    n = int(client.query(sql).result_rows[0][0])
                    out["trades_1s"] = pd.DataFrame([{"table": table, "time_col": tcol, "n": n}])
                    out["notes"].append(f"trades_table={table};time_col={tcol};rows={n}")
                    out["available"] = True
                    break
                except Exception:
                    continue
            else:
                continue
            break
        except Exception:
            continue
    else:
        out["notes"].append("public_trades_unavailable")

    # OI / liquidations — best-effort table probe
    for label, tables in (
        (
            "oi",
            (
                "orderbook_analysis.open_interest_5m_history",
                "orderbook_analysis.open_interest",
                "orderbook_analysis.open_interest_1s",
                "orderbook_analysis.oi_1s",
                "orderbook_analysis.bybit_open_interest",
            ),
        ),
        (
            "liquidations",
            (
                "orderbook_analysis.all_liquidations",
                "orderbook_analysis.liquidations",
                "orderbook_analysis.liquidations_1s",
                "orderbook_analysis.liquidation_events",
                "orderbook_analysis.bybit_liquidations",
            ),
        ),
    ):
        found = False
        for table in tables:
            for tcol in ("bucket_start", "ts", "timestamp", "event_time"):
                try:
                    sql = f"""
                    SELECT symbol, count() AS n
                    FROM {table}
                    WHERE symbol IN ({sym_list})
                      AND {tcol} >= toDateTime64('{start_s}', 3, 'UTC')
                      AND {tcol} < toDateTime64('{end_s}', 3, 'UTC')
                    GROUP BY symbol
                    """
                    rows = client.query(sql).result_rows
                    out[label] = pd.DataFrame(rows, columns=["symbol", "n"])
                    out["notes"].append(f"{label}_table={table};time_col={tcol};groups={len(rows)}")
                    out["available"] = True
                    found = True
                    break
                except Exception:
                    continue
            if found:
                break
        if not found:
            out["notes"].append(f"{label}_unavailable")

    return out
