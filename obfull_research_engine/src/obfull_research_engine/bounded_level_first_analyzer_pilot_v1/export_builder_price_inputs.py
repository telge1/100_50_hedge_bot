"""Export CH-backed trades/candles JSONL for the generic defense builder (read-only CH)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..timeparse import format_utc_z
from .persist import atomic_write_json, atomic_write_jsonl
from .prices import load_candles_1m, load_pilot_trades


def _ts_z(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if hasattr(value, "isoformat"):
        return format_utc_z(value)
    return str(value)


def export_trades_and_candles_jsonl(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    out_dir: Path | str,
) -> dict[str, Any]:
    """
    Write public_trades_window.jsonl + candles_1m_window.jsonl under out_dir.

    ClickHouse is used read-only via existing loaders. Does not touch Episode-1 fixtures.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    trade_index, trade_meta = load_pilot_trades(symbol=symbol, start=start, end=end)
    candles, candle_meta = load_candles_1m(symbol=symbol, start=start, end=end)

    trade_rows: list[dict[str, Any]] = []
    for t in trade_index.trades:
        ingest = getattr(t, "ingest_timestamp", None)
        recv = _ts_z(ingest)
        side = getattr(t, "side", None) or getattr(t, "taker_side", None)
        trade_rows.append(
            {
                "trade_id": t.trade_id,
                "trade_ts": _ts_z(t.trade_ts),
                "price": float(t.price),
                "size": float(t.size),
                "taker_side": side,
                "collector_received_at": recv,
                "ingest_timestamp": recv,
            }
        )

    candle_rows: list[dict[str, Any]] = []
    for c in candles:
        row = dict(c)
        if "open_time" in row:
            row["open_time"] = _ts_z(row["open_time"]) or row["open_time"]
        candle_rows.append(row)

    trades_path = out / "public_trades_window.jsonl"
    candles_path = out / "candles_1m_window.jsonl"
    atomic_write_jsonl(trades_path, trade_rows)
    atomic_write_jsonl(candles_path, candle_rows)
    meta = {
        "symbol": symbol.upper(),
        "window_start": format_utc_z(start),
        "window_end": format_utc_z(end),
        "n_trades": len(trade_rows),
        "n_candles": len(candle_rows),
        "trades_path": str(trades_path),
        "candles_path": str(candles_path),
        "trade_meta": trade_meta,
        "candle_meta": candle_meta,
        "clickhouse_writes": 0,
    }
    atomic_write_json(out / "builder_price_inputs_manifest.json", meta)
    return meta
