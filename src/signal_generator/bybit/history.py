"""Bybit linear USDT perpetual historical kline fetch & normalize."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, Sequence
from urllib.parse import urlencode

import httpx

from signal_generator.db.candles import Candle1m

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
DEFAULT_CATEGORY = "linear"
DEFAULT_INTERVAL = "1"  # Bybit 1-minute
CANONICAL_INTERVAL = "1m"
EXCHANGE = "bybit"
SOURCE = "bybit_history"
MAX_LIMIT = 1000
MINUTE_MS = 60_000


class BybitApiError(RuntimeError):
    """Raised when Bybit returns a non-success payload or HTTP error after retries."""


class HttpTransport(Protocol):
    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class HttpxTransport:
    """Production HTTP transport with bounded retries and exponential backoff."""

    timeout: float = 30.0
    max_retries: int = 5
    backoff_base: float = 0.5
    client: httpx.Client | None = None

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            last_exc: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    response = client.get(url, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise BybitApiError(
                            f"HTTP {response.status_code}: {response.text[:200]}"
                        )
                    response.raise_for_status()
                    payload = response.json()
                    ret_code = payload.get("retCode")
                    if ret_code != 0:
                        raise BybitApiError(
                            f"Bybit retCode={ret_code} retMsg={payload.get('retMsg')!r}"
                        )
                    return payload
                except (httpx.TimeoutException, httpx.TransportError, BybitApiError) as exc:
                    last_exc = exc
                    if attempt + 1 >= self.max_retries:
                        break
                    time.sleep(self.backoff_base * (2**attempt))
            assert last_exc is not None
            raise BybitApiError(f"Bybit request failed after retries: {last_exc}") from last_exc
        finally:
            if owns_client:
                client.close()


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def to_millis(ts: datetime) -> int:
    ts = ensure_utc(ts)
    return int(ts.timestamp() * 1000)


def millis_to_utc(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def parse_kline_row(row: Sequence[Any]) -> dict[str, Any]:
    """Parse one Bybit kline list row into typed fields (Decimal, UTC).

    Bybit list item:
      [startTime, open, high, low, close, volume, turnover]
    """
    if len(row) < 7:
        raise ValueError(f"Invalid Bybit kline row (need 7 fields): {row!r}")
    open_time = millis_to_utc(row[0])
    return {
        "open_time": open_time,
        "close_time": open_time + timedelta(minutes=1),
        "open": Decimal(str(row[1])),
        "high": Decimal(str(row[2])),
        "low": Decimal(str(row[3])),
        "close": Decimal(str(row[4])),
        "volume": Decimal(str(row[5])),
        "turnover": Decimal(str(row[6])),
    }


def is_closed_candle(close_time: datetime, *, as_of: datetime | None = None) -> bool:
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    return as_of >= ensure_utc(close_time)


def normalize_kline(
    row: Sequence[Any],
    *,
    symbol: str,
    as_of: datetime | None = None,
    source: str = SOURCE,
) -> Candle1m | None:
    """Normalize a Bybit kline into Candle1m. Returns None if not yet closed."""
    parsed = parse_kline_row(row)
    if not is_closed_candle(parsed["close_time"], as_of=as_of):
        return None
    return Candle1m(
        exchange=EXCHANGE,
        symbol=symbol.upper(),
        interval=CANONICAL_INTERVAL,
        open_time=parsed["open_time"],
        close_time=parsed["close_time"],
        open=parsed["open"],
        high=parsed["high"],
        low=parsed["low"],
        close=parsed["close"],
        volume=parsed["volume"],
        turnover=parsed["turnover"],
        is_closed=True,
        source=source,
        source_event_time=None,
    )


def filter_half_open(
    candles: Sequence[Candle1m],
    start: datetime,
    end: datetime,
) -> list[Candle1m]:
    """Keep candles with open_time in [start, end)."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    return [c for c in candles if start <= ensure_utc(c.open_time) < end]


def sort_candles_asc(candles: Sequence[Candle1m]) -> list[Candle1m]:
    return sorted(candles, key=lambda c: ensure_utc(c.open_time))


def dedupe_by_open_time(candles: Sequence[Candle1m]) -> list[Candle1m]:
    """Keep last occurrence per open_time (after ASC sort, last wins = later page overwrite)."""
    by_ot: dict[datetime, Candle1m] = {}
    for c in candles:
        by_ot[ensure_utc(c.open_time)] = c
    return sort_candles_asc(list(by_ot.values()))


def chunk_batches(items: Sequence[Candle1m], batch_size: int) -> list[list[Candle1m]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    return [list(items[i : i + batch_size]) for i in range(0, len(items), batch_size)]


def expected_candle_count(start: datetime, end: datetime) -> int:
    """Expected number of 1m candles in [start, end) if fully continuous."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


@dataclass(slots=True)
class FetchPageResult:
    rows: list[list[Any]]
    request_params: dict[str, Any]


class BybitHistoryClient:
    """Fetch historical linear 1m klines with time-based pagination."""

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        base_url: str = BYBIT_KLINE_URL,
        category: str = DEFAULT_CATEGORY,
        request_pause_s: float = 0.05,
    ) -> None:
        self._transport = transport or HttpxTransport()
        self.base_url = base_url
        self.category = category
        self.request_pause_s = request_pause_s

    def fetch_page(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        limit: int = MAX_LIMIT,
    ) -> FetchPageResult:
        if limit < 1 or limit > MAX_LIMIT:
            raise ValueError(f"limit must be in [1, {MAX_LIMIT}], got {limit}")
        if end_ms < start_ms:
            return FetchPageResult(rows=[], request_params={})
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": symbol.upper(),
            "interval": DEFAULT_INTERVAL,
            "start": start_ms,
            "end": end_ms,
            "limit": limit,
        }
        payload = self._transport.get_json(self.base_url, params)
        result = payload.get("result") or {}
        rows = result.get("list") or []
        if not isinstance(rows, list):
            raise BybitApiError(f"Unexpected list payload: {type(rows)}")
        return FetchPageResult(rows=rows, request_params=params)

    def fetch_closed_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        limit: int = MAX_LIMIT,
        as_of: datetime | None = None,
        max_pages: int = 500,
    ) -> list[Candle1m]:
        """Fetch closed 1m candles for symbol in half-open UTC window [start, end).

        Bybit returns newest-first pages (max 1000). We walk ``end`` backwards
        until coverage reaches ``start``, then sort ASC and dedupe.
        """
        start = ensure_utc(start)
        end = ensure_utc(end)
        if end <= start:
            return []

        start_ms = to_millis(start)
        # Bybit end is inclusive on startTime; request last ms still inside [start, end).
        cursor_end_ms = to_millis(end) - 1
        collected: list[Candle1m] = []
        seen_open_ms: set[int] = set()
        pages = 0

        while cursor_end_ms >= start_ms and pages < max_pages:
            pages += 1
            page = self.fetch_page(
                symbol,
                start_ms=start_ms,
                end_ms=cursor_end_ms,
                limit=limit,
            )
            if self.request_pause_s > 0:
                time.sleep(self.request_pause_s)

            if not page.rows:
                break

            page_open_ms: list[int] = []
            for row in page.rows:
                open_ms = int(row[0])
                page_open_ms.append(open_ms)
                if open_ms in seen_open_ms:
                    continue
                candle = normalize_kline(row, symbol=symbol, as_of=as_of)
                if candle is None:
                    continue
                if not (start <= ensure_utc(candle.open_time) < end):
                    continue
                seen_open_ms.add(open_ms)
                collected.append(candle)

            oldest_ms = min(page_open_ms)
            if oldest_ms <= start_ms:
                break
            next_end = oldest_ms - 1
            if next_end >= cursor_end_ms:
                # Defensive: avoid infinite loop if API returns unexpected data.
                raise BybitApiError(
                    f"Pagination did not advance for {symbol}: oldest_ms={oldest_ms}"
                )
            cursor_end_ms = next_end

            if len(page.rows) < limit:
                # Partial page within [start, cursor_end] → no older data in range.
                break

        if pages >= max_pages:
            raise BybitApiError(
                f"Pagination exceeded max_pages={max_pages} for {symbol}"
            )

        return sort_candles_asc(collected)


def request_url_for_debug(params: dict[str, Any]) -> str:
    return f"{BYBIT_KLINE_URL}?{urlencode(params)}"
