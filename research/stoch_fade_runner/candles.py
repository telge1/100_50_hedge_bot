"""Read-only 1m candle access. Writer methods are not reachable."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import pandas as pd

from .universe import load_tradeable_universe

ALLOWED_INTERVAL = "1m"


def _as_utc(ts: Any):
    """ClickHouse DateTime is UTC. Naive values must not use local astimezone()."""
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class MutatingMethodBlocked(RuntimeError):
    pass


class CandleSource(Protocol):
    def get_candles(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame: ...


class MemoryCandleSource:
    def __init__(self, frames: dict[str, pd.DataFrame] | None = None) -> None:
        self.frames = frames or {}
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_candles(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        df = self.frames.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        ts = pd.to_datetime(df["open_time"], utc=True)
        mask = (ts >= start) & (ts < end)
        return df.loc[mask].copy()


class ReadOnlyCandleFetcher:
    """Binds only ``get_candles``. Insert/command/delete are blocked."""

    def __init__(
        self,
        get_candles_fn: Callable[..., list],
        *,
        allowed_symbols: frozenset[str],
    ) -> None:
        self._get_candles = get_candles_fn
        self.allowed_symbols = allowed_symbols

    def get_candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        exchange: str = "bybit",
        interval: str = ALLOWED_INTERVAL,
    ) -> list:
        if symbol not in self.allowed_symbols:
            raise ValueError(f"SYMBOL_NOT_ALLOWLISTED:{symbol}")
        if interval != ALLOWED_INTERVAL:
            raise ValueError(f"INTERVAL_NOT_ALLOWED:{interval}")
        return self._get_candles(
            symbol, start, end, exchange=exchange, interval=interval
        )

    def insert_candles(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("insert_candles")

    def insert(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("insert")

    def command(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("command")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("delete")


class ClickHouseReadOnlyCandleSource:
    """SELECT-only 1m FINAL candles via bound get_candles. No Bybit fallback."""

    def __init__(self, fetcher: ReadOnlyCandleFetcher) -> None:
        if not isinstance(fetcher, ReadOnlyCandleFetcher):
            raise TypeError("ClickHouseReadOnlyCandleSource requires ReadOnlyCandleFetcher")
        self._fetcher = fetcher
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.last_stats: dict[str, int] = {
            "loaded_count": 0,
            "uniq_open_time": 0,
            "duplicate_count": 0,
            "rows_removed_by_normalization": 0,
        }

    def get_candles(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        rows = self._fetcher.get_candles(symbol, start, end)
        if not rows:
            return pd.DataFrame()
        out = []
        for r in rows:
            rec = dict(r) if not isinstance(r, dict) else r
            out.append(
                {
                    "open_time": _as_utc(rec.get("open_time")),
                    "open": float(rec["open"]),
                    "high": float(rec["high"]),
                    "low": float(rec["low"]),
                    "close": float(rec["close"]),
                    "volume": float(rec.get("volume") or 0.0),
                    "close_time": _as_utc(rec.get("close_time"))
                    if rec.get("close_time") is not None
                    else None,
                }
            )
        df = pd.DataFrame(out)
        before = int(len(df))
        uniq = int(df["open_time"].nunique())
        # FINAL already logically dedupes. Do not drop_duplicates as a timezone repair.
        df = df.sort_values("open_time").reset_index(drop=True)
        self.last_stats = {
            "loaded_count": before,
            "uniq_open_time": uniq,
            "duplicate_count": before - uniq,
            "rows_removed_by_normalization": 0,
        }
        return df

    def insert_candles(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("insert_candles")

    def insert(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("insert")


def bind_readonly_fetcher(
    get_candles_fn: Callable[..., list],
    *,
    allowed_symbols: frozenset[str] | None = None,
) -> ReadOnlyCandleFetcher:
    allowlist = allowed_symbols if allowed_symbols is not None else load_tradeable_universe()["allowlist"]
    return ReadOnlyCandleFetcher(get_candles_fn, allowed_symbols=allowlist)
