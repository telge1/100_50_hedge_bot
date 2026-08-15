"""Trade loading + side/timestamp helpers for pull/consumption analysis."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from research.orderbook.bybit_historical_trades_download import parse_trade_timestamp


@dataclass(frozen=True, slots=True)
class Trade:
    ts_ms: int
    side: str  # Buy | Sell (taker aggressor)
    price: float
    size: float
    trade_id: str


def trade_ts_seconds_to_ms(raw: str | float | Decimal) -> int:
    """Convert Bybit historical trade timestamp (Unix seconds + fraction) to ms."""
    dt, _unit, _iso = parse_trade_timestamp(str(raw))
    return int(dt.timestamp() * 1000)


def aggressor_side_for_direction(direction: str) -> str:
    """Break-side aggressor that attacks the structure wall."""
    d = direction.lower()
    if d == "bearish":
        return "Sell"  # hits bids / protected low
    if d == "bullish":
        return "Buy"  # hits asks / protected high
    raise ValueError(f"unknown direction: {direction!r}")


def wall_book_side(direction: str) -> str:
    d = direction.lower()
    if d == "bearish":
        return "bid"
    if d == "bullish":
        return "ask"
    raise ValueError(f"unknown direction: {direction!r}")


def day_trade_csv_path(data_root: Path, symbol: str, day: str) -> Path | None:
    day_dir = data_root / symbol / day
    preferred = day_dir / f"{symbol}{day}.csv"
    if preferred.is_file():
        return preferred
    cands = sorted(p for p in day_dir.glob("*.csv") if p.is_file() and not p.name.endswith(".part"))
    return cands[0] if cands else None


def iter_trades_in_window(
    path: Path,
    *,
    start_ms: int,
    end_ms: int,
    expected_symbol: str | None = None,
) -> Iterator[Trade]:
    """Stream trades with start_ms <= trade_ts_ms <= end_ms (causal-ready)."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return
        cols = list(reader.fieldnames)
        ts_col = next(c for c in cols if c.lower() == "timestamp")
        side_col = next(c for c in cols if c.lower() == "side")
        price_col = next(c for c in cols if c.lower() == "price")
        size_col = next(c for c in cols if c.lower() == "size")
        id_col = next((c for c in cols if c.lower() == "trdmatchid"), None)
        sym_col = next((c for c in cols if c.lower() == "symbol"), None)

        for row in reader:
            if expected_symbol and sym_col and str(row.get(sym_col) or "").strip() != expected_symbol:
                continue
            raw_ts = str(row.get(ts_col) or "").strip()
            if not raw_ts:
                continue
            try:
                ts_ms = trade_ts_seconds_to_ms(raw_ts)
            except ValueError:
                continue
            if ts_ms < start_ms:
                continue
            if ts_ms > end_ms:
                # files are time-ordered; can stop early
                break
            side = str(row.get(side_col) or "").strip()
            if side not in {"Buy", "Sell"}:
                continue
            try:
                price = float(str(row.get(price_col) or "").strip())
                size = float(str(row.get(size_col) or "").strip())
            except ValueError:
                continue
            if price <= 0 or size <= 0:
                continue
            tid = str(row.get(id_col) or "").strip() if id_col else ""
            yield Trade(ts_ms=ts_ms, side=side, price=price, size=size, trade_id=tid)


def load_trades_window(
    path: Path,
    *,
    start_ms: int,
    end_ms: int,
    expected_symbol: str | None = None,
) -> list[Trade]:
    return list(
        iter_trades_in_window(
            path, start_ms=start_ms, end_ms=end_ms, expected_symbol=expected_symbol
        )
    )


def filter_trades_causal(trades: list[Trade], *, asof_ms: int) -> list[Trade]:
    """Only trades with trade_ts <= asof (no future leakage)."""
    return [t for t in trades if t.ts_ms <= asof_ms]


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
