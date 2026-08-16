"""Bybit public linear WebSocket kline stream + official ping/pong heartbeat.

Spec (Bybit v5 Connect / public kline):
- URL: wss://stream.bybit.com/v5/public/linear
- Topic: kline.{interval}.{symbol}  (interval ``1`` for 1m)
- Heartbeat: client sends ``{"op":"ping"}`` every **20 seconds**;
  server replies with pong (``op``/``ret_msg`` containing pong).
- Closed candle: ``confirm=true``
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Sequence

from signal_generator.bybit.history import ensure_utc, millis_to_utc
from signal_generator.db.candles import Candle1m

logger = logging.getLogger(__name__)

BYBIT_LINEAR_WS_URL = "wss://stream.bybit.com/v5/public/linear"
PING_INTERVAL_S = 20.0
# Miss ~2 ping cycles without pong/message → stale
STALE_CONNECTION_TIMEOUT_S = 45.0
# Bybit public subscribe: keep args well under per-request topic limits.
SUBSCRIBE_CHUNK_SIZE = 10
LIVE_SOURCE = "bybit_live"


OnClosedCandle = Callable[[Candle1m], Awaitable[None] | None]
OnEvent = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class WsKlineTick:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    confirm: bool
    event_time: datetime | None


def parse_ws_kline_item(item: dict[str, Any], *, symbol: str) -> WsKlineTick:
    open_time = millis_to_utc(item["start"])
    # Canonical close_time is open+60s (matches REST normalizer), not Bybit end-1ms.
    close_time = open_time + timedelta(minutes=1)
    event_ms = item.get("timestamp")
    return WsKlineTick(
        symbol=symbol.upper(),
        open_time=open_time,
        close_time=close_time,
        open=Decimal(str(item["open"])),
        high=Decimal(str(item["high"])),
        low=Decimal(str(item["low"])),
        close=Decimal(str(item["close"])),
        volume=Decimal(str(item["volume"])),
        turnover=Decimal(str(item["turnover"])),
        confirm=bool(item.get("confirm")),
        event_time=millis_to_utc(event_ms) if event_ms is not None else None,
    )


def ws_tick_to_candle(tick: WsKlineTick) -> Candle1m | None:
    """Only confirmed closed candles become canonical rows."""
    if not tick.confirm:
        return None
    return Candle1m(
        exchange="bybit",
        symbol=tick.symbol,
        interval="1m",
        open_time=tick.open_time,
        close_time=tick.close_time,
        open=tick.open,
        high=tick.high,
        low=tick.low,
        close=tick.close,
        volume=tick.volume,
        turnover=tick.turnover,
        is_closed=True,
        source=LIVE_SOURCE,
        source_event_time=tick.event_time,
    )


def topic_for_symbol(symbol: str, interval: str = "1") -> str:
    return f"kline.{interval}.{symbol.upper()}"


def chunk_sequence(items: Sequence[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    seq = list(items)
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def subscribe_arg_chunks(
    symbols: Sequence[str],
    *,
    chunk_size: int = SUBSCRIBE_CHUNK_SIZE,
    interval: str = "1",
) -> list[list[str]]:
    topics = [topic_for_symbol(s, interval=interval) for s in symbols]
    return chunk_sequence(topics, chunk_size)


def parse_topic_symbol(topic: str) -> str | None:
    # kline.1.BTCUSDT
    parts = topic.split(".")
    if len(parts) >= 3 and parts[0] == "kline":
        return parts[-1].upper()
    return None


def is_pong_message(payload: dict[str, Any]) -> bool:
    op = str(payload.get("op") or "").lower()
    ret = str(payload.get("ret_msg") or "").lower()
    if op == "pong":
        return True
    if op == "ping" and ret == "pong":
        return True
    if payload.get("success") is True and ret == "pong":
        return True
    return False


class BybitKlineWebSocket:
    """Async WebSocket client for multi-symbol 1m klines."""

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        url: str = BYBIT_LINEAR_WS_URL,
        ping_interval_s: float = PING_INTERVAL_S,
        stale_timeout_s: float = STALE_CONNECTION_TIMEOUT_S,
        on_closed_candle: OnClosedCandle | None = None,
        on_event: OnEvent | None = None,
        open_connection: Callable[..., Awaitable[Any]] | None = None,
        subscribe_chunk_size: int = SUBSCRIBE_CHUNK_SIZE,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.url = url
        self.ping_interval_s = ping_interval_s
        self.stale_timeout_s = stale_timeout_s
        self.on_closed_candle = on_closed_candle
        self.on_event = on_event
        self._open_connection = open_connection
        self.subscribe_chunk_size = subscribe_chunk_size
        self._stop = asyncio.Event()
        self.last_message_at: datetime | None = None
        self.last_pong_at: datetime | None = None
        self.last_ping_at: datetime | None = None
        self.subscribed_topics: set[str] = set()
        self.subscribe_success: bool | None = None
        self._open_candles: dict[str, WsKlineTick] = {}
        self._pending_subscribe_args: list[str] = []
        self._subscribe_chunks: list[list[str]] = []
        self._subscribe_chunk_index: int = 0
        self._ws_conn: Any = None

    def request_stop(self) -> None:
        self._stop.set()

    def _touch(self) -> None:
        self.last_message_at = datetime.now(timezone.utc)

    async def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self.on_event:
            res = self.on_event(name, data)
            if asyncio.iscoroutine(res):
                await res

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        self._touch()
        if is_pong_message(payload):
            self.last_pong_at = datetime.now(timezone.utc)
            await self._emit("pong", payload)
            return

        op = str(payload.get("op") or "").lower()
        if op == "subscribe":
            success = bool(payload.get("success", False))
            args = list(self._pending_subscribe_args)
            if success:
                self.subscribe_success = True
                self.subscribed_topics.update(args)
            await self._emit(
                "subscribe_ack",
                {
                    "success": success,
                    "ret_msg": payload.get("ret_msg"),
                    "conn_id": payload.get("conn_id"),
                    "args": args,
                    "raw": payload,
                },
            )
            if success and self._ws_conn is not None:
                await self._send_next_subscribe_chunk(self._ws_conn)
            return

        topic = payload.get("topic")
        if not topic:
            await self._emit("other", payload)
            return

        symbol = parse_topic_symbol(str(topic))
        if not symbol:
            return
        # Any kline topic message implies subscription is live for that symbol
        await self._emit("topic_message", {"symbol": symbol})
        for item in payload.get("data") or []:
            tick = parse_ws_kline_item(item, symbol=symbol)
            if tick.confirm:
                candle = ws_tick_to_candle(tick)
                if candle and self.on_closed_candle:
                    res = self.on_closed_candle(candle)
                    if asyncio.iscoroutine(res):
                        await res
                self._open_candles.pop(symbol, None)
            else:
                self._open_candles[symbol] = tick
                await self._emit(
                    "unconfirmed",
                    {
                        "symbol": symbol,
                        "open_time": tick.open_time.isoformat(),
                        "time": int(tick.open_time.timestamp()),
                        "open": float(tick.open),
                        "high": float(tick.high),
                        "low": float(tick.low),
                        "close": float(tick.close),
                        "volume": float(tick.volume),
                        "confirm": False,
                    },
                )

    async def _send_next_subscribe_chunk(self, ws: Any) -> None:
        if self._subscribe_chunk_index >= len(self._subscribe_chunks):
            return
        args = self._subscribe_chunks[self._subscribe_chunk_index]
        self._subscribe_chunk_index += 1
        self._pending_subscribe_args = list(args)
        sub = {"op": "subscribe", "args": args}
        await ws.send(json.dumps(sub))
        await self._emit("subscribe_sent", sub)

    async def run_forever(self) -> None:
        """Connect, subscribe, ping loop until stop or stale/disconnect."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("websockets package required") from exc

        open_conn = self._open_connection or websockets.connect
        async with open_conn(self.url, ping_interval=None) as ws:
            self._ws_conn = ws
            await self._emit("connected", {"url": self.url})
            self._subscribe_chunks = subscribe_arg_chunks(
                self.symbols, chunk_size=self.subscribe_chunk_size
            )
            self._subscribe_chunk_index = 0
            self.subscribed_topics.clear()
            await self._send_next_subscribe_chunk(ws)

            async def ping_loop() -> None:
                while not self._stop.is_set():
                    try:
                        await ws.send(json.dumps({"op": "ping"}))
                        self.last_ping_at = datetime.now(timezone.utc)
                        await self._emit(
                            "ping_sent",
                            {"at": self.last_ping_at.isoformat()},
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("ping failed: %s", exc)
                        return
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.ping_interval_s)
                        return
                    except asyncio.TimeoutError:
                        continue

            async def stale_watch() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(1.0)
                    ref = self.last_pong_at or self.last_message_at
                    if ref is None:
                        continue
                    age = (datetime.now(timezone.utc) - ensure_utc(ref)).total_seconds()
                    if age > self.stale_timeout_s:
                        logger.warning(
                            "stale websocket: no pong/message for %.1fs > %.1fs",
                            age,
                            self.stale_timeout_s,
                        )
                        await self._emit("stale", {"age_s": age})
                        try:
                            await ws.close()
                        except Exception:  # noqa: BLE001
                            pass
                        return

            ping_task = asyncio.create_task(ping_loop())
            stale_task = asyncio.create_task(stale_watch())
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        raise
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        await self._handle_payload(payload)
            finally:
                ping_task.cancel()
                stale_task.cancel()
                for t in (ping_task, stale_task):
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
