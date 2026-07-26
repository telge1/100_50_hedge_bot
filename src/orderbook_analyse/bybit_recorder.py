"""
Bybit public WebSocket recorder writing batched rows into ClickHouse.

Orderbook sequence / gap policy (exact):
----------------------------------------
After (re)subscribe, the first orderbook payload for the symbol must be a
``snapshot``. A ``delta`` before any snapshot is a SEQUENCE_ERROR.

On each ``snapshot``:
  - local ``last_update_id`` / ``last_seq`` are reset to the snapshot values
  - previous delta continuity is discarded (intentional Bybit resync)

On each ``delta`` after a snapshot with previous update_id ``U``:
  - GAP / SEQUENCE_ERROR if ``u != U + 1``
    (Bybit V5 orderbook continuity rule)
  - SEQUENCE_ERROR if ``u == 1`` on a delta (service-restart signal without
    a proper snapshot in this connection)
  - SEQUENCE_ERROR if ``seq < last_seq`` (cross_sequence moved backwards
    without an intervening snapshot)
  - ``seq`` may jump forward by more than 1; that alone is NOT a gap

On SEQUENCE_ERROR the recorder reconnects (bounded exponential backoff),
logs SEQUENCE_ERROR + RECONNECT to recorder_health, and never continues
applying deltas on a broken sequence.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from orderbook_analyse.bybit_ws_diagnostic import build_topics, is_pong_message
from orderbook_analyse.clickhouse_writer import (
    AsyncClickHouseWriter,
    InsertError,
    SequenceRow,
    default_client_factory,
)
from orderbook_analyse.config import Settings, load_settings, redact_settings

logger = logging.getLogger(__name__)

HEALTH_STARTED = "STARTED"
HEALTH_SUBSCRIBED = "SUBSCRIBED"
HEALTH_HEARTBEAT = "HEARTBEAT"
HEALTH_RECONNECT = "RECONNECT"
HEALTH_SEQUENCE_ERROR = "SEQUENCE_ERROR"
HEALTH_INSERT_ERROR = "INSERT_ERROR"
HEALTH_STOPPED = "STOPPED"


class SequenceError(RuntimeError):
    """Orderbook update_id / seq continuity broken."""


class QueueOverflowError(RuntimeError):
    """Reserved; queue uses blocking backpressure instead of dropping."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_dt(ms: Any) -> datetime | None:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def to_decimal_required(value: Any, *, default: str = "0") -> Decimal:
    parsed = to_decimal(value)
    if parsed is None:
        return Decimal(default)
    return parsed


@dataclass
class OrderbookSequenceState:
    last_update_id: int | None = None
    last_seq: int | None = None
    has_snapshot: bool = False

    def reset(self) -> None:
        self.last_update_id = None
        self.last_seq = None
        self.has_snapshot = False

    def apply_snapshot(self, update_id: int, seq: int) -> None:
        self.last_update_id = update_id
        self.last_seq = seq
        self.has_snapshot = True

    def apply_delta(self, update_id: int, seq: int) -> None:
        if not self.has_snapshot or self.last_update_id is None or self.last_seq is None:
            raise SequenceError("delta received before snapshot")
        if update_id == 1:
            raise SequenceError("delta with update_id=1 (service restart) without snapshot")
        expected = self.last_update_id + 1
        if update_id != expected:
            raise SequenceError(
                f"update_id gap: expected {expected}, got {update_id} "
                f"(last_u={self.last_update_id})"
            )
        if seq < self.last_seq:
            raise SequenceError(
                f"cross_sequence moved backwards: last_seq={self.last_seq}, got {seq}"
            )
        self.last_update_id = update_id
        self.last_seq = seq


@dataclass
class TickerState:
    symbol: str | None = None
    exchange_ts: datetime | None = None
    last_price: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    best_bid_price: Decimal | None = None
    best_ask_price: Decimal | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    funding_rate: Decimal | None = None
    volume_24h: Decimal | None = None
    turnover_24h: Decimal | None = None
    _last_sample_mono: float | None = None

    def merge(self, payload: dict[str, Any], exchange_ts: datetime | None) -> None:
        if exchange_ts is not None:
            self.exchange_ts = exchange_ts
        symbol = payload.get("symbol")
        if isinstance(symbol, str) and symbol:
            self.symbol = symbol

        mapping = {
            "lastPrice": "last_price",
            "markPrice": "mark_price",
            "indexPrice": "index_price",
            "bid1Price": "best_bid_price",
            "ask1Price": "best_ask_price",
            "openInterest": "open_interest",
            "openInterestValue": "open_interest_value",
            "fundingRate": "funding_rate",
            "volume24h": "volume_24h",
            "turnover24h": "turnover_24h",
        }
        for src, dst in mapping.items():
            if src not in payload:
                continue
            raw = payload[src]
            if raw is None or raw == "":
                continue
            parsed = to_decimal(raw)
            if parsed is not None:
                setattr(self, dst, parsed)

    @property
    def ready(self) -> bool:
        return self.exchange_ts is not None and bool(self.symbol)

    def maybe_sample(
        self, *, received_ts: datetime, interval_sec: float, force: bool = False
    ) -> SequenceRow | None:
        if not self.ready:
            return None
        now_mono = time.monotonic()
        if (
            not force
            and self._last_sample_mono is not None
            and (now_mono - self._last_sample_mono) < interval_sec
        ):
            return None
        self._last_sample_mono = now_mono
        assert self.exchange_ts is not None
        assert self.symbol is not None
        return (
            self.exchange_ts,
            received_ts,
            self.symbol,
            self.last_price,
            self.mark_price,
            self.index_price,
            self.best_bid_price,
            self.best_ask_price,
            self.open_interest,
            self.open_interest_value,
            self.funding_rate,
            self.volume_24h,
            self.turnover_24h,
        )


def parse_orderbook_rows(
    msg: dict[str, Any],
    *,
    received_ts: datetime,
    seq_state: OrderbookSequenceState,
) -> list[SequenceRow]:
    msg_type = msg.get("type")
    if msg_type not in ("snapshot", "delta"):
        raise ValueError(f"unsupported orderbook type: {msg_type!r}")

    data = msg.get("data")
    if not isinstance(data, dict):
        raise ValueError("orderbook data must be an object")

    symbol = str(data.get("s") or "")
    if not symbol:
        raise ValueError("orderbook missing symbol")

    update_id = int(data["u"])
    seq = int(data["seq"])
    exchange_ts = ms_to_dt(msg.get("ts")) or received_ts

    if msg_type == "snapshot":
        seq_state.apply_snapshot(update_id, seq)
    else:
        seq_state.apply_delta(update_id, seq)

    rows: list[SequenceRow] = []
    for side_key, side_name in (("b", "bid"), ("a", "ask")):
        levels = data.get(side_key) or []
        if not isinstance(levels, list):
            continue
        for idx, level in enumerate(levels):
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price = to_decimal_required(level[0])
            qty = to_decimal_required(level[1])
            rows.append(
                (
                    exchange_ts,
                    received_ts,
                    symbol,
                    side_name,
                    price,
                    qty,
                    msg_type,
                    update_id,
                    seq,
                    idx,
                )
            )
    return rows


def parse_public_trade_rows(
    msg: dict[str, Any], *, received_ts: datetime
) -> list[SequenceRow]:
    data = msg.get("data")
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    rows: list[SequenceRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        price = to_decimal_required(item.get("p"))
        qty = to_decimal_required(item.get("v"))
        side = item.get("S") or "Buy"
        if side not in ("Buy", "Sell"):
            side = "Buy"
        trade_ts = ms_to_dt(item.get("T")) or ms_to_dt(msg.get("ts")) or received_ts
        rows.append(
            (
                trade_ts,
                received_ts,
                str(item.get("s") or ""),
                str(item.get("i") or ""),
                side,
                price,
                qty,
                price * qty,
                str(item.get("L") or ""),
                1 if item.get("BT") else 0,
                1 if item.get("RPI") else 0,
            )
        )
    return rows


def parse_liquidation_rows(
    msg: dict[str, Any], *, received_ts: datetime
) -> list[SequenceRow]:
    """Parse Bybit allLiquidation payload into ClickHouse liquidations rows.

    ``S`` is the position side (Buy = long liquidated, Sell = short liquidated).
    ``p`` is the bankruptcy price. Invalid/missing ``S`` is dropped (never coerced
    to Buy) because the ClickHouse enum only allows Buy/Sell.
    """
    data = msg.get("data")
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    rows: list[SequenceRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_side = item.get("S")
        if raw_side not in ("Buy", "Sell"):
            logger.warning("Invalid liquidation side: %r", raw_side)
            continue
        side = raw_side
        price = to_decimal_required(item.get("p"))
        qty = to_decimal_required(item.get("v"))
        liq_ts = ms_to_dt(item.get("T")) or ms_to_dt(msg.get("ts")) or received_ts
        rows.append(
            (
                liq_ts,
                received_ts,
                str(item.get("s") or ""),
                side,
                price,
                qty,
                price * qty,
            )
        )
    return rows


def extract_ticker_payload(msg: dict[str, Any]) -> dict[str, Any] | None:
    data = msg.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


@dataclass
class RecorderStats:
    messages_by_stream: dict[str, int] = field(
        default_factory=lambda: {
            "orderbook": 0,
            "publicTrade": 0,
            "tickers": 0,
            "allLiquidation": 0,
            "subscription_ack": 0,
            "pong": 0,
            "other": 0,
        }
    )
    data_objects_by_stream: dict[str, int] = field(
        default_factory=lambda: {
            "orderbook": 0,
            "publicTrade": 0,
            "tickers": 0,
            "allLiquidation": 0,
        }
    )
    parse_error_count: int = 0
    sequence_error_count: int = 0
    reconnect_count: int = 0
    insert_error_count: int = 0
    exit_reason: str = "unknown"
    last_update_id: int | None = None
    last_seq: int | None = None

    @property
    def messages_received(self) -> int:
        return int(sum(self.messages_by_stream.values()))


class BybitRecorder:
    def __init__(
        self,
        settings: Settings,
        writer: AsyncClickHouseWriter,
        *,
        duration_sec: float,
        ticker_sample_interval_sec: float,
    ) -> None:
        self.settings = settings
        self.writer = writer
        self.duration_sec = duration_sec
        self.ticker_sample_interval_sec = ticker_sample_interval_sec
        self.topics = build_topics(settings.symbol, settings.orderbook_depth)
        self.stats = RecorderStats()
        self.seq_state = OrderbookSequenceState()
        self.ticker_state = TickerState()
        self._stop = asyncio.Event()
        self._deadline_mono: float | None = None

    def request_stop(self, reason: str) -> None:
        if self.stats.exit_reason == "unknown":
            self.stats.exit_reason = reason
        self._stop.set()

    async def emit_health(
        self,
        event_type: str,
        *,
        stream: str = "",
        message: str = "",
    ) -> None:
        row: SequenceRow = (
            utc_now(),
            self.settings.symbol,
            event_type,
            stream,
            message,
            self.stats.reconnect_count,
            self.stats.messages_received,
            self.writer.total_rows_inserted,
            self.writer.queue_size,
        )
        await self.writer.enqueue("recorder_health", [row])

    async def handle_message(self, raw: bytes | str) -> None:
        received_ts = utc_now()
        try:
            payload = orjson.loads(raw if isinstance(raw, (bytes, bytearray)) else raw.encode())
        except Exception:
            self.stats.parse_error_count += 1
            logger.warning("JSON parse error")
            return
        if not isinstance(payload, dict):
            self.stats.messages_by_stream["other"] += 1
            return

        if is_pong_message(payload):
            self.stats.messages_by_stream["pong"] += 1
            return

        if payload.get("op") == "subscribe":
            self.stats.messages_by_stream["subscription_ack"] += 1
            ok = bool(payload.get("success", True))
            await self.emit_health(
                HEALTH_SUBSCRIBED,
                stream="all",
                message="ok" if ok else str(payload.get("ret_msg") or "subscribe failed"),
            )
            if not ok:
                raise RuntimeError(f"subscribe failed: {payload}")
            return

        topic = payload.get("topic")
        if not isinstance(topic, str):
            self.stats.messages_by_stream["other"] += 1
            return

        if topic.startswith("orderbook."):
            await self._handle_orderbook(payload, received_ts)
        elif topic.startswith("publicTrade."):
            await self._handle_trades(payload, received_ts)
        elif topic.startswith("tickers."):
            await self._handle_ticker(payload, received_ts)
        elif topic.startswith("allLiquidation."):
            await self._handle_liquidations(payload, received_ts)
        else:
            self.stats.messages_by_stream["other"] += 1

    async def _handle_orderbook(self, msg: dict[str, Any], received_ts: datetime) -> None:
        self.stats.messages_by_stream["orderbook"] += 1
        self.stats.data_objects_by_stream["orderbook"] += 1
        rows = parse_orderbook_rows(msg, received_ts=received_ts, seq_state=self.seq_state)
        self.stats.last_update_id = self.seq_state.last_update_id
        self.stats.last_seq = self.seq_state.last_seq
        if rows:
            await self.writer.enqueue("orderbook_deltas", rows)

    async def _handle_trades(self, msg: dict[str, Any], received_ts: datetime) -> None:
        self.stats.messages_by_stream["publicTrade"] += 1
        data = msg.get("data")
        if isinstance(data, list):
            self.stats.data_objects_by_stream["publicTrade"] += len(data)
        elif isinstance(data, dict):
            self.stats.data_objects_by_stream["publicTrade"] += 1
        rows = parse_public_trade_rows(msg, received_ts=received_ts)
        if rows:
            await self.writer.enqueue("public_trades", rows)

    async def _handle_ticker(self, msg: dict[str, Any], received_ts: datetime) -> None:
        self.stats.messages_by_stream["tickers"] += 1
        self.stats.data_objects_by_stream["tickers"] += 1
        payload = extract_ticker_payload(msg)
        if payload is None:
            return
        exchange_ts = ms_to_dt(msg.get("ts"))
        self.ticker_state.merge(payload, exchange_ts)
        sample = self.ticker_state.maybe_sample(
            received_ts=received_ts,
            interval_sec=self.ticker_sample_interval_sec,
        )
        if sample is not None:
            await self.writer.enqueue("ticker_samples", [sample])

    async def _handle_liquidations(self, msg: dict[str, Any], received_ts: datetime) -> None:
        self.stats.messages_by_stream["allLiquidation"] += 1
        data = msg.get("data")
        if isinstance(data, list):
            self.stats.data_objects_by_stream["allLiquidation"] += len(data)
        elif isinstance(data, dict):
            self.stats.data_objects_by_stream["allLiquidation"] += 1
        rows = parse_liquidation_rows(msg, received_ts=received_ts)
        if rows:
            await self.writer.enqueue("liquidations", rows)

    async def _session(self, deadline_mono: float | None) -> None:
        url = self.settings.bybit_ws_url
        ping_interval = self.settings.ping_interval_sec
        heartbeat_interval = self.settings.heartbeat_interval_sec
        next_ping = time.monotonic() + ping_interval
        next_heartbeat = time.monotonic() + heartbeat_interval

        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as ws:
            await ws.send(orjson.dumps({"op": "subscribe", "args": self.topics}).decode())
            self.seq_state.reset()

            while not self._stop.is_set():
                now = time.monotonic()
                if deadline_mono is not None and now >= deadline_mono:
                    self.request_stop("duration_elapsed")
                    break

                timeouts = [next_ping - now, next_heartbeat - now]
                if deadline_mono is not None:
                    timeouts.append(deadline_mono - now)
                timeout = max(0.0, min(timeouts))

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout if timeout > 0 else 0.01)
                    await self.handle_message(raw)
                except asyncio.TimeoutError:
                    pass
                except SequenceError:
                    raise
                except InsertError:
                    raise

                now = time.monotonic()
                if now >= next_ping:
                    await ws.send(orjson.dumps({"op": "ping"}).decode())
                    next_ping = now + ping_interval
                if now >= next_heartbeat:
                    await self.emit_health(HEALTH_HEARTBEAT, stream="all", message="ok")
                    next_heartbeat = now + heartbeat_interval

    async def run(self) -> dict[str, Any]:
        started_at = utc_now()
        started_mono = time.monotonic()
        deadline_mono = (
            None if self.duration_sec <= 0 else started_mono + float(self.duration_sec)
        )
        self._deadline_mono = deadline_mono

        await self.writer.start()
        await self.emit_health(HEALTH_STARTED, stream="all", message="recorder starting")

        backoff = self.settings.reconnect_initial_sec
        attempts = 0

        try:
            while not self._stop.is_set():
                if deadline_mono is not None and time.monotonic() >= deadline_mono:
                    self.request_stop("duration_elapsed")
                    break
                try:
                    await self._session(deadline_mono)
                    if self._stop.is_set():
                        break
                except SequenceError as exc:
                    self.stats.sequence_error_count += 1
                    logger.error("SEQUENCE_ERROR: %s", exc)
                    await self.emit_health(
                        HEALTH_SEQUENCE_ERROR,
                        stream="orderbook",
                        message=str(exc),
                    )
                    attempts += 1
                    self.stats.reconnect_count += 1
                    if attempts > self.settings.reconnect_max_attempts:
                        self.request_stop("sequence_error_max_reconnects")
                        break
                    await self.emit_health(
                        HEALTH_RECONNECT,
                        stream="orderbook",
                        message=f"backoff={backoff:.1f}s attempt={attempts}",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.settings.reconnect_max_sec)
                    self.seq_state.reset()
                    continue
                except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                    attempts += 1
                    self.stats.reconnect_count += 1
                    logger.warning("websocket disconnected: %s", exc)
                    if attempts > self.settings.reconnect_max_attempts:
                        self.request_stop("websocket_max_reconnects")
                        break
                    if deadline_mono is not None and time.monotonic() >= deadline_mono:
                        self.request_stop("duration_elapsed")
                        break
                    await self.emit_health(
                        HEALTH_RECONNECT,
                        stream="all",
                        message=f"disconnect backoff={backoff:.1f}s attempt={attempts}",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.settings.reconnect_max_sec)
                    self.seq_state.reset()
                    continue
                except InsertError as exc:
                    self.stats.insert_error_count += 1
                    await self.emit_health(
                        HEALTH_INSERT_ERROR,
                        stream="clickhouse",
                        message=str(exc),
                    )
                    self.request_stop("insert_error")
                    break
                # Clean session exit without stop → treat as disconnect
                if not self._stop.is_set():
                    attempts += 1
                    self.stats.reconnect_count += 1
                    await self.emit_health(
                        HEALTH_RECONNECT,
                        stream="all",
                        message=f"session ended backoff={backoff:.1f}s",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.settings.reconnect_max_sec)
                    self.seq_state.reset()
        finally:
            # Final ticker sample if state is ready
            sample = self.ticker_state.maybe_sample(
                received_ts=utc_now(),
                interval_sec=0,
                force=True,
            )
            if sample is not None:
                try:
                    await self.writer.enqueue("ticker_samples", [sample])
                except Exception:  # noqa: BLE001
                    logger.exception("failed to enqueue final ticker sample")

            await self.emit_health(
                HEALTH_STOPPED,
                stream="all",
                message=self.stats.exit_reason,
            )
            try:
                await self.writer.stop()
            except InsertError as exc:
                self.stats.insert_error_count += 1
                self.stats.exit_reason = "insert_error"
                logger.error("insert error on shutdown: %s", exc)
            self.writer.close()

        finished_at = utc_now()
        runtime = (finished_at - started_at).total_seconds()
        return self.build_summary(started_at=started_at, finished_at=finished_at, runtime=runtime)

    def build_summary(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        runtime: float,
    ) -> dict[str, Any]:
        return {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "runtime_sec": runtime,
            "duration_requested_sec": self.duration_sec,
            "symbol": self.settings.symbol,
            "topics": self.topics,
            "messages_by_stream": dict(self.stats.messages_by_stream),
            "data_objects_by_stream": dict(self.stats.data_objects_by_stream),
            "writer": self.writer.stats.to_dict(),
            "reconnects": self.stats.reconnect_count,
            "sequence_errors": self.stats.sequence_error_count,
            "parse_errors": self.stats.parse_error_count,
            "insert_errors": self.stats.insert_error_count
            + self.writer.stats.insert_error_count,
            "max_queue_size": self.writer.stats.max_queue_size,
            "last_update_id": self.stats.last_update_id,
            "last_seq": self.stats.last_seq,
            "exit_reason": self.stats.exit_reason,
            "config": redact_settings(self.settings),
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bybit → ClickHouse market data recorder")
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Laufzeit in Sekunden; 0 = unbegrenzt (default: 300)",
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--flush-interval", type=float, default=1.0)
    parser.add_argument("--ticker-sample-interval", type=float, default=1.0)
    parser.add_argument("--log-level", type=str, default="INFO")
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never attach handlers that might dump env; keep password out of logs via redact.


async def _amain(args: argparse.Namespace) -> int:
    settings = load_settings()
    # Apply CLI overrides onto a shallow runtime copy via object.__setattr__ not needed;
    # pass overrides explicitly.
    if args.batch_size <= 0:
        raise RuntimeError("--batch-size must be > 0")
    if args.flush_interval <= 0:
        raise RuntimeError("--flush-interval must be > 0")
    if args.ticker_sample_interval <= 0:
        raise RuntimeError("--ticker-sample-interval must be > 0")
    if args.duration < 0:
        raise RuntimeError("--duration must be >= 0")

    writer = AsyncClickHouseWriter(
        client_factory=default_client_factory(
            host=settings.clickhouse_host,
            port=settings.clickhouse_http_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
        ),
        batch_size=args.batch_size,
        flush_interval_sec=args.flush_interval,
        queue_maxsize=settings.queue_maxsize,
    )
    recorder = BybitRecorder(
        settings,
        writer,
        duration_sec=float(args.duration),
        ticker_sample_interval_sec=float(args.ticker_sample_interval),
    )

    loop = asyncio.get_running_loop()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info("received %s", sig.name)
        recorder.request_stop(f"signal_{sig.name.lower()}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            signal.signal(sig, lambda *_a, s=sig: recorder.request_stop(f"signal_{s.name.lower()}"))

    summary = await recorder.run()
    sys.stdout.buffer.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
    sys.stdout.buffer.write(b"\n")

    if summary["insert_errors"] > 0 or summary["exit_reason"] in {
        "insert_error",
        "sequence_error_max_reconnects",
        "websocket_max_reconnects",
    }:
        return 1
    if summary["exit_reason"] not in {"duration_elapsed", "signal_sigint", "signal_sigterm"}:
        # unexpected exit
        if summary["sequence_errors"] > 0 and summary["reconnects"] > settings.reconnect_max_attempts:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        return asyncio.run(_amain(args))
    except Exception as exc:  # noqa: BLE001
        # Avoid logging settings objects that might contain secrets
        logger.error("FATAL: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
