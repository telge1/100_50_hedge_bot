"""Extended read-only loaders for explanatory audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from research.btc_ob_fight.config import utc
from research.btc_ob_fight.loaders import _dt_sql, query_rows


def load_liquidation_events(
    cl,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = query_rows(
        cl,
        f"""
        SELECT event_time, liquidated_position_side, position_side_raw,
               toFloat64(size), toFloat64(notional_estimate), toFloat64(bankruptcy_price),
               event_key, source_topic
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{symbol}'
          AND event_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND event_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        ORDER BY event_time, event_key
        """,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        key = str(r[6])
        if key in seen:
            continue
        seen.add(key)
        side = str(r[1])
        out.append(
            {
                "event_time": utc(r[0]).isoformat().replace("+00:00", "Z"),
                "liquidated_side": side,
                "position_side_raw": str(r[2]),
                "forced_trade_direction": "FORCED_BUY" if side == "LIQUIDATED_SHORT" else "FORCED_SELL",
                "base_volume": float(r[3]),
                "quote_notional": float(r[4]),
                "bankruptcy_price": float(r[5]),
                "event_key": key,
                "dedup_key": key,
                "source_topic": str(r[7]),
                "data_quality": "DEDUPED_BY_EVENT_KEY",
            }
        )
    return out
