"""Read-only loader for public_trades_canonical (no orderbook_deltas)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.public_trade_bubbles.contract import (
    PublicTradeRecord,
    aggressor_flags,
)

CANONICAL_FQN = "orderbook_analysis.public_trades_canonical"
_SETTINGS = {
    "max_execution_time": 60,
    "max_memory_usage": 250_000_000,
    "max_threads": 2,
}


def _client() -> Any:
    """Read-only CH client; prefer collector .env (same as Research Charts)."""
    import clickhouse_connect

    env_path = Path(
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
        "signal_generator_stoch_waves/.env"
    )
    file_env: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            file_env[k.strip()] = v.strip().strip("'").strip('"')
    host = os.environ.get("CLICKHOUSE_HOST") or file_env.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = int(
        os.environ.get("CLICKHOUSE_HTTP_PORT")
        or os.environ.get("CLICKHOUSE_PORT")
        or file_env.get("CLICKHOUSE_HTTP_PORT")
        or file_env.get("CLICKHOUSE_PORT")
        or "8123"
    )
    user = os.environ.get("CLICKHOUSE_USER") or file_env.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD") or file_env.get("CLICKHOUSE_PASSWORD", "")
    return clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password
    )


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_public_trade_records(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    client: Any | None = None,
) -> list[PublicTradeRecord]:
    """Load trades in [start, end). Side = taker aggressor Buy|Sell."""
    sym = str(symbol).strip().upper()
    start = _utc(start)
    end = _utc(end)
    cl = client or _client()
    rows = cl.query(
        f"""
        SELECT
          trade_ts,
          trade_id,
          side,
          toFloat64(price) AS price,
          toFloat64(size) AS size,
          toFloat64(notional) AS notional,
          source,
          ingest_timestamp
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{s:String}}
        WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
          AND trade_ts < {{b:DateTime64(3,'UTC')}}
        ORDER BY trade_ts, trade_id
        """,
        parameters={"s": sym, "a": start, "b": end},
        settings=_SETTINGS,
    ).result_rows
    out: list[PublicTradeRecord] = []
    seen: set[str] = set()
    for trade_ts, trade_id, side, price, size, notional, source, ingest_ts in rows:
        tid = str(trade_id)
        if tid in seen:
            continue
        seen.add(tid)
        ts = trade_ts
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        recv = ingest_ts
        if recv is not None:
            if getattr(recv, "tzinfo", None) is None:
                recv = recv.replace(tzinfo=timezone.utc)
            else:
                recv = recv.astimezone(timezone.utc)
        buy, sell = aggressor_flags(str(side))
        n = float(notional) if notional is not None else float(price) * float(size)
        src = str(source or "unknown")
        quality = "ok" if src in ("live", "archive") else "unknown_source"
        out.append(
            PublicTradeRecord(
                trade_id=tid,
                symbol=sym,
                trade_timestamp=ts,
                received_at=recv,
                price=float(price),
                quantity_base=float(size),
                notional_quote=n,
                taker_side="Buy" if buy else ("Sell" if sell else str(side)),
                is_aggressive_buy=buy,
                is_aggressive_sell=sell,
                source=src,
                source_quality=quality,
            )
        )
    return out


def coverage_summary(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    client: Any | None = None,
) -> dict[str, Any]:
    sym = str(symbol).strip().upper()
    start = _utc(start)
    end = _utc(end)
    cl = client or _client()
    row = cl.query(
        f"""
        SELECT
          count() AS n,
          min(trade_ts) AS tmin,
          max(trade_ts) AS tmax,
          countIf(side = 'Buy') AS buys,
          countIf(side = 'Sell') AS sells
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{s:String}}
        WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
          AND trade_ts < {{b:DateTime64(3,'UTC')}}
        """,
        parameters={"s": sym, "a": start, "b": end},
        settings=_SETTINGS,
    ).first_item
    return {
        "symbol": sym,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": int(row["n"] or 0),
        "min_ts": str(row["tmin"]) if row["tmin"] else None,
        "max_ts": str(row["tmax"]) if row["tmax"] else None,
        "buys": int(row["buys"] or 0),
        "sells": int(row["sells"] or 0),
        "table": CANONICAL_FQN,
        "side_semantics": "taker_aggressor Buy|Sell (Bybit publicTrade)",
    }
