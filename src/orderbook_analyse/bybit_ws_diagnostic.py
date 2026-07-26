"""60-second Bybit WebSocket diagnostic recorder (no persistence)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson
import websockets
from dotenv import load_dotenv

TICKER_FIELDS = (
    "lastPrice",
    "markPrice",
    "indexPrice",
    "bid1Price",
    "ask1Price",
    "openInterest",
    "openInterestValue",
    "fundingRate",
    "volume24h",
    "turnover24h",
)

PING_INTERVAL_SEC = 20.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_data_elements(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    return 0


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TopicStats:
    message_count: int = 0
    data_element_count: int = 0
    first_message_at: str | None = None
    last_message_at: str | None = None

    def note_message(self, received_at: str, data: Any) -> None:
        self.message_count += 1
        self.data_element_count += count_data_elements(data)
        if self.first_message_at is None:
            self.first_message_at = received_at
        self.last_message_at = received_at


@dataclass
class OrderbookStats(TopicStats):
    snapshot_count: int = 0
    delta_count: int = 0
    bid_level_count: int = 0
    ask_level_count: int = 0
    min_u: int | None = None
    max_u: int | None = None
    min_seq: int | None = None
    max_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": self.message_count,
            "data_element_count": self.data_element_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "snapshot_count": self.snapshot_count,
            "delta_count": self.delta_count,
            "bid_level_count": self.bid_level_count,
            "ask_level_count": self.ask_level_count,
            "min_u": self.min_u,
            "max_u": self.max_u,
            "min_seq": self.min_seq,
            "max_seq": self.max_seq,
        }


@dataclass
class TradeStats(TopicStats):
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": self.message_count,
            "data_element_count": self.data_element_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "trade_count": self.trade_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
        }


@dataclass
class TickerStats(TopicStats):
    fields_seen: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": self.message_count,
            "data_element_count": self.data_element_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "fields_seen": {
                name: name in self.fields_seen for name in TICKER_FIELDS
            },
        }


@dataclass
class LiquidationStats(TopicStats):
    event_count: int = 0
    buy_count: int = 0
    sell_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": self.message_count,
            "data_element_count": self.data_element_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "event_count": self.event_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
        }


@dataclass
class DiagnosticCounters:
    orderbook: OrderbookStats = field(default_factory=OrderbookStats)
    public_trade: TradeStats = field(default_factory=TradeStats)
    ticker: TickerStats = field(default_factory=TickerStats)
    liquidation: LiquidationStats = field(default_factory=LiquidationStats)
    subscription_ack_count: int = 0
    pong_count: int = 0
    ping_sent_count: int = 0
    other_message_count: int = 0
    parse_error_count: int = 0

    def to_summary(
        self,
        *,
        symbol: str,
        depth: int,
        duration_sec: float,
        ws_url: str,
        topics: list[str],
        started_at: str,
        finished_at: str,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "orderbook_depth": depth,
            "duration_sec": duration_sec,
            "ws_url": ws_url,
            "topics": topics,
            "started_at": started_at,
            "finished_at": finished_at,
            "subscription_ack_count": self.subscription_ack_count,
            "pong_count": self.pong_count,
            "ping_sent_count": self.ping_sent_count,
            "other_message_count": self.other_message_count,
            "parse_error_count": self.parse_error_count,
            "orderbook": self.orderbook.to_dict(),
            "public_trade": self.public_trade.to_dict(),
            "ticker": self.ticker.to_dict(),
            "liquidation": self.liquidation.to_dict(),
        }


def process_orderbook(msg: dict[str, Any], stats: OrderbookStats, received_at: str) -> None:
    data = msg.get("data")
    stats.note_message(received_at, data)
    msg_type = msg.get("type")
    if msg_type == "snapshot":
        stats.snapshot_count += 1
    elif msg_type == "delta":
        stats.delta_count += 1

    if isinstance(data, dict):
        bids = data.get("b") or []
        asks = data.get("a") or []
        if isinstance(bids, list):
            stats.bid_level_count += len(bids)
        if isinstance(asks, list):
            stats.ask_level_count += len(asks)

        u_val = _as_int(data.get("u"))
        if u_val is not None:
            stats.min_u = u_val if stats.min_u is None else min(stats.min_u, u_val)
            stats.max_u = u_val if stats.max_u is None else max(stats.max_u, u_val)

        seq_val = _as_int(data.get("seq"))
        if seq_val is not None:
            stats.min_seq = seq_val if stats.min_seq is None else min(stats.min_seq, seq_val)
            stats.max_seq = seq_val if stats.max_seq is None else max(stats.max_seq, seq_val)


def process_public_trade(msg: dict[str, Any], stats: TradeStats, received_at: str) -> None:
    data = msg.get("data")
    stats.note_message(received_at, data)
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        stats.trade_count += 1
        side = item.get("S")
        if side == "Buy":
            stats.buy_count += 1
        elif side == "Sell":
            stats.sell_count += 1


def process_ticker(msg: dict[str, Any], stats: TickerStats, received_at: str) -> None:
    data = msg.get("data")
    stats.note_message(received_at, data)
    payload = data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        payload = data[0]
    if isinstance(payload, dict):
        for name in TICKER_FIELDS:
            if name in payload and payload[name] is not None and payload[name] != "":
                stats.fields_seen.add(name)


def process_liquidation(
    msg: dict[str, Any], stats: LiquidationStats, received_at: str
) -> None:
    data = msg.get("data")
    stats.note_message(received_at, data)
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        stats.event_count += 1
        side = item.get("S")
        if side == "Buy":
            stats.buy_count += 1
        elif side == "Sell":
            stats.sell_count += 1


def is_pong_message(msg: dict[str, Any]) -> bool:
    """Bybit answers client pings with op=ping + ret_msg=pong (or op=pong)."""
    op = msg.get("op")
    if op == "pong":
        return True
    if op == "ping" and str(msg.get("ret_msg", "")).lower() == "pong":
        return True
    return False


def classify_and_process(msg: dict[str, Any], counters: DiagnosticCounters) -> None:
    received_at = _utc_now_iso()
    op = msg.get("op")

    if is_pong_message(msg):
        counters.pong_count += 1
        return

    if op == "subscribe":
        counters.subscription_ack_count += 1
        return

    topic = msg.get("topic")
    if not isinstance(topic, str):
        counters.other_message_count += 1
        return

    if topic.startswith("orderbook."):
        process_orderbook(msg, counters.orderbook, received_at)
    elif topic.startswith("publicTrade."):
        process_public_trade(msg, counters.public_trade, received_at)
    elif topic.startswith("tickers."):
        process_ticker(msg, counters.ticker, received_at)
    elif topic.startswith("allLiquidation."):
        process_liquidation(msg, counters.liquidation, received_at)
    else:
        counters.other_message_count += 1


def build_topics(symbol: str, depth: int) -> list[str]:
    return [
        f"orderbook.{depth}.{symbol}",
        f"publicTrade.{symbol}",
        f"tickers.{symbol}",
        f"allLiquidation.{symbol}",
    ]


def process_raw_message(raw: bytes | str, counters: DiagnosticCounters) -> None:
    try:
        if isinstance(raw, str):
            raw_bytes = raw.encode("utf-8")
        else:
            raw_bytes = raw
        msg = orjson.loads(raw_bytes)
    except Exception:
        counters.parse_error_count += 1
        return

    if not isinstance(msg, dict):
        counters.other_message_count += 1
        return

    classify_and_process(msg, counters)


async def run_diagnostic(
    *,
    ws_url: str,
    symbol: str,
    depth: int,
    duration_sec: float,
) -> dict[str, Any]:
    topics = build_topics(symbol, depth)
    counters = DiagnosticCounters()
    started_at = _utc_now_iso()
    deadline = time.monotonic() + duration_sec
    next_ping_at = time.monotonic() + PING_INTERVAL_SEC

    async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None) as ws:
        subscribe = {"op": "subscribe", "args": topics}
        await ws.send(orjson.dumps(subscribe).decode("utf-8"))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            timeout = min(remaining, max(0.0, next_ping_at - time.monotonic()))
            if timeout <= 0:
                await ws.send(orjson.dumps({"op": "ping"}).decode("utf-8"))
                counters.ping_sent_count += 1
                next_ping_at = time.monotonic() + PING_INTERVAL_SEC
                continue

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue

            process_raw_message(raw, counters)

    finished_at = _utc_now_iso()
    return counters.to_summary(
        symbol=symbol,
        depth=depth,
        duration_sec=duration_sec,
        ws_url=ws_url,
        topics=topics,
        started_at=started_at,
        finished_at=finished_at,
    )


def load_config() -> tuple[str, str, int]:
    load_dotenv()
    ws_url = os.environ.get("BYBIT_WS_URL")
    symbol = os.environ.get("SYMBOL")
    depth_raw = os.environ.get("ORDERBOOK_DEPTH", "200")
    if not ws_url:
        raise RuntimeError("BYBIT_WS_URL fehlt in der Umgebung/.env")
    if not symbol:
        raise RuntimeError("SYMBOL fehlt in der Umgebung/.env")
    try:
        depth = int(depth_raw)
    except ValueError as exc:
        raise RuntimeError(f"ORDERBOOK_DEPTH ist ungültig: {depth_raw!r}") from exc
    return ws_url, symbol, depth


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bybit WebSocket Diagnoserecorder (ohne Persistenz)."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Laufzeit in Sekunden (default: 60)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.duration <= 0:
            raise RuntimeError("--duration muss > 0 sein")
        ws_url, symbol, depth = load_config()
        summary = asyncio.run(
            run_diagnostic(
                ws_url=ws_url,
                symbol=symbol,
                depth=depth,
                duration_sec=float(args.duration),
            )
        )
        sys.stdout.buffer.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
        sys.stdout.buffer.write(b"\n")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI soll jeden Fehler klar melden
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
