"""Realtime Bybit linear WebSocket collector for OI + allLiquidations."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from . import SOURCE_WS
from .health_logic import (
    DeadConnection,
    is_bybit_fatal_error,
    is_pong_payload,
    liquidation_stream_healthy,
    market_data_stale,
    next_backoff,
    pong_timed_out,
    resubscribe_topics,
    session_healthy,
)
from .locks import SingleInstanceLock
from .logic import (
    DedupCache,
    OIState,
    floor_5s,
    ms_to_dt,
    parse_liquidation_records,
    utc_now,
)
from .schema import apply_schema
from .settings import OICollectorSettings, load_oi_settings, redact_settings
from .universe import EXCLUDED_SYMBOLS, UniversePlan, fetch_bybit_linear_usdt_perps, plan_universe
from .writer import AllowlistedWriter, InsertError

logger = logging.getLogger(__name__)


def make_instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def default_client_factory(settings: OICollectorSettings):
    def factory():
        import clickhouse_connect

        return clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_http_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database="default",
        )

    return factory


@dataclass
class CollectorStats:
    messages_received: int = 0
    parse_errors: int = 0
    reconnect_count: int = 0
    subscription_count: int = 0
    duplicates_suppressed: int = 0
    ping_count: int = 0
    pong_count: int = 0
    last_ping_mono: float | None = None
    last_pong_mono: float | None = None
    last_ping_at: datetime | None = None
    last_pong_at: datetime | None = None
    pong_latency_ms: int | None = None
    pong_latencies_ms: list[int] = field(default_factory=list)
    last_market_mono: float | None = None
    last_bybit_ts: datetime | None = None
    clock_offset_ms: int | None = None
    ws_connected: bool = False
    ping_ok: bool = False
    subscription_confirmed: bool = False
    last_liquidation_time: datetime | None = None
    last_received_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    last_reconnect_at: datetime | None = None
    last_disconnect_duration_ms: int | None = None
    started_at: datetime | None = None
    confirmed_topics: list[str] = field(default_factory=list)
    per_symbol_valid: dict[str, bool] = field(default_factory=dict)
    connection_generation: int = 0


class OILiquidationCollector:
    def __init__(
        self,
        settings: OICollectorSettings,
        plan: UniversePlan,
        writer: AllowlistedWriter,
        *,
        duration_sec: float = 0.0,
        instance_id: str | None = None,
        reconnect_after_sec: float = 0.0,
    ) -> None:
        self.settings = settings
        self.plan = plan
        self.writer = writer
        self.duration_sec = duration_sec
        self.reconnect_after_sec = reconnect_after_sec
        self.instance_id = instance_id or make_instance_id()
        self.stats = CollectorStats()
        self.stats.started_at = utc_now()
        self.states = {s: OIState(symbol=s) for s in plan.subscribed}
        self.dedup = DedupCache()
        self._written_5s: dict[str, datetime] = {}
        self._stop = asyncio.Event()
        self._force_reconnect = asyncio.Event()
        self._ws = None
        self._forced_reconnect_used = False
        self._pending_subscribe_topics: list[str] = []
        self._chunk_ack = False

    def request_stop(self) -> None:
        self._stop.set()

    def request_reconnect(self) -> None:
        self._force_reconnect.set()

    def _topics(self) -> list[str]:
        return [f"tickers.{s}" for s in self.plan.subscribed] + [
            f"allLiquidation.{s}" for s in self.plan.subscribed
        ]

    def note_ping_sent(self, now_mono: float | None = None) -> dict[str, Any]:
        now = utc_now()
        mono = time.monotonic() if now_mono is None else now_mono
        self.stats.last_ping_at = now
        self.stats.last_ping_mono = mono
        self.stats.ping_count += 1
        return {"op": "ping", "at": now.isoformat(), "count": self.stats.ping_count}

    def note_pong(self, now_mono: float | None = None) -> int | None:
        now = utc_now()
        mono = time.monotonic() if now_mono is None else now_mono
        self.stats.ping_ok = True
        self.stats.last_pong_at = now
        self.stats.last_pong_mono = mono
        self.stats.pong_count += 1
        latency = None
        if self.stats.last_ping_mono is not None:
            latency = int((mono - self.stats.last_ping_mono) * 1000)
            self.stats.pong_latency_ms = latency
            self.stats.pong_latencies_ms.append(latency)
            if len(self.stats.pong_latencies_ms) > 500:
                self.stats.pong_latencies_ms = self.stats.pong_latencies_ms[-250:]
        return latency

    def check_liveness(self, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        if pong_timed_out(
            last_ping_mono=self.stats.last_ping_mono,
            last_pong_mono=self.stats.last_pong_mono,
            now_mono=now,
            timeout_sec=self.settings.ping_timeout_sec,
        ):
            raise DeadConnection("pong_timeout")
        if market_data_stale(
            last_market_mono=self.stats.last_market_mono,
            now_mono=now,
            stale_sec=self.settings.stale_data_sec,
        ):
            raise DeadConnection("stale_market_data")

    def mark_disconnect(self, reason: str) -> None:
        self.stats.last_disconnect_at = utc_now()
        self.stats.ws_connected = False
        self.stats.subscription_confirmed = False
        self.stats.ping_ok = False
        self.stats.confirmed_topics = []
        self._invalidate_all()
        logger.warning("disconnect %s", reason)

    def mark_reconnect_complete(self) -> None:
        now = utc_now()
        self.stats.last_reconnect_at = now
        if self.stats.last_disconnect_at is not None:
            self.stats.last_disconnect_duration_ms = int(
                (now - self.stats.last_disconnect_at).total_seconds() * 1000
            )

    def _health_payload(self, event_type: str, *, symbol: str = "*", message: str = "") -> dict[str, Any]:
        now = utc_now()
        if symbol != "*" and symbol in self.states:
            st = self.states[symbol]
            valid_flag = 1 if st.valid else 0
            age = (
                int((now - st.received_at).total_seconds() * 1000)
                if st.valid and st.received_at is not None
                else None
            )
            last_bucket = self._written_5s.get(symbol)
            ticker_ok = f"tickers.{symbol}" in self.stats.confirmed_topics
            liq_ok = f"allLiquidation.{symbol}" in self.stats.confirmed_topics
        else:
            valid_n = sum(1 for st in self.states.values() if st.valid)
            valid_flag = 1 if valid_n == len(self.states) and self.states else 0
            ages = [
                int((now - st.received_at).total_seconds() * 1000)
                for st in self.states.values()
                if st.valid and st.received_at is not None
            ]
            age = max(ages) if ages else None
            last_bucket = next(iter(self._written_5s.values()), None)
            ticker_ok = any(t.startswith("tickers.") for t in self.stats.confirmed_topics)
            liq_ok = any(t.startswith("allLiquidation.") for t in self.stats.confirmed_topics)
        lag = None
        if self.stats.last_bybit_ts is not None:
            lag = int((now - self.stats.last_bybit_ts).total_seconds() * 1000)
        extra = {
            "pid": os.getpid(),
            "started_at": self.stats.started_at.isoformat() if self.stats.started_at else None,
            "connection_generation": self.stats.connection_generation,
            "subscribed_topics": self._topics() if symbol == "*" else [f"tickers.{symbol}", f"allLiquidation.{symbol}"],
            "confirmed_topics": list(self.stats.confirmed_topics)
            if symbol == "*"
            else [t for t in (f"tickers.{symbol}", f"allLiquidation.{symbol}") if t in self.stats.confirmed_topics],
            "ticker_subscribed": ticker_ok,
            "liq_subscribed": liq_ok,
            "last_ping_at": self.stats.last_ping_at.isoformat() if self.stats.last_ping_at else None,
            "last_pong_at": self.stats.last_pong_at.isoformat() if self.stats.last_pong_at else None,
            "pong_latency_ms": self.stats.pong_latency_ms,
            "ping_count": self.stats.ping_count,
            "pong_count": self.stats.pong_count,
            "last_disconnect_at": (
                self.stats.last_disconnect_at.isoformat() if self.stats.last_disconnect_at else None
            ),
            "last_reconnect_at": (
                self.stats.last_reconnect_at.isoformat() if self.stats.last_reconnect_at else None
            ),
            "last_disconnect_duration_ms": self.stats.last_disconnect_duration_ms,
            "last_5s_bucket": last_bucket.isoformat() if last_bucket else None,
            "liq_sub_healthy": liquidation_stream_healthy(
                ws_connected=self.stats.ws_connected,
                subscription_confirmed=liq_ok and self.stats.subscription_confirmed,
                ping_ok=self.stats.ping_ok,
                liq_topic_subscribed=liq_ok,
            ),
            "session_healthy": session_healthy(
                ws_connected=self.stats.ws_connected,
                subscription_confirmed=self.stats.subscription_confirmed,
                ping_ok=self.stats.ping_ok,
                has_recent_market=self.stats.last_market_mono is not None,
            ),
        }
        if message:
            extra["note"] = message
        return {
            "event_ts": now,
            "collector_instance_id": self.instance_id,
            "symbol": symbol,
            "source": SOURCE_WS,
            "event_type": event_type,
            "ws_connected": 1 if self.stats.ws_connected else 0,
            "ping_ok": 1 if self.stats.ping_ok else 0,
            "subscription_confirmed": 1 if (self.stats.subscription_confirmed and (symbol == "*" or ticker_ok)) else 0,
            "oi_state_valid": valid_flag,
            "oi_state_age_ms": age,
            "last_event_time": self.stats.last_bybit_ts if symbol == "*" else (self.states[symbol].event_time if symbol in self.states else None),
            "last_received_at": self.stats.last_received_at if symbol == "*" else (self.states[symbol].received_at if symbol in self.states else None),
            "last_liquidation_time": self.stats.last_liquidation_time,
            "lag_ms": lag,
            "messages_received": self.stats.messages_received,
            "rows_inserted": self.writer.rows_inserted,
            "duplicates_suppressed": self.stats.duplicates_suppressed,
            "parse_errors": self.stats.parse_errors,
            "insert_errors": self.writer.insert_errors,
            "reconnect_count": self.stats.reconnect_count,
            "subscription_count": self.stats.subscription_count,
            "queue_size": self.writer.queue_size,
            "queue_drops": self.writer.queue_drops,
            "clock_offset_ms": self.stats.clock_offset_ms,
            "message": json.dumps(extra, default=str),
        }

    async def _health(self, event_type: str, *, symbol: str = "*", message: str = "") -> None:
        recs = [self._health_payload(event_type, symbol=symbol, message=message)]
        if event_type == "HEARTBEAT":
            recs.extend(self._health_payload("HEARTBEAT", symbol=s) for s in self.plan.subscribed)
        await self.writer.enqueue("oi_liquidation_health", recs)

    def _invalidate_all(self) -> None:
        for st in self.states.values():
            st.invalidate()
            self.stats.per_symbol_valid[st.symbol] = False

    async def handle_message(self, raw: bytes | str) -> None:
        received = utc_now()
        self.stats.last_received_at = received
        self.stats.messages_received += 1
        try:
            payload = orjson.loads(raw if isinstance(raw, (bytes, bytearray)) else raw.encode())
        except Exception:
            self.stats.parse_errors += 1
            return
        if not isinstance(payload, dict):
            self.stats.parse_errors += 1
            return
        if is_pong_payload(payload):
            self.note_pong()
            return
        if is_bybit_fatal_error(payload):
            raise DeadConnection(f"bybit_error:{payload.get('ret_msg')}")
        op = payload.get("op")
        if op == "subscribe":
            ok = bool(payload.get("success", True))
            if ok:
                self.stats.subscription_count += 1
                pending = list(self._pending_subscribe_topics)
                for topic in pending:
                    if topic not in self.stats.confirmed_topics:
                        self.stats.confirmed_topics.append(topic)
                self._pending_subscribe_topics = []
                self._chunk_ack = True
                wanted = set(self._topics())
                self.stats.subscription_confirmed = wanted.issubset(set(self.stats.confirmed_topics))
            else:
                raise DeadConnection(f"subscribe failed: {payload.get('ret_msg')}")
            return
        topic = payload.get("topic")
        if not isinstance(topic, str):
            return
        self.stats.last_market_mono = time.monotonic()
        ts = ms_to_dt(payload.get("ts"))
        if ts is not None:
            self.stats.last_bybit_ts = ts
            self.stats.clock_offset_ms = int((received - ts).total_seconds() * 1000)
        if topic.startswith("allLiquidation."):
            await self._on_liq(payload, received)
        elif topic.startswith("tickers."):
            await self._on_ticker(payload, received)

    async def _on_liq(self, msg: dict[str, Any], received: datetime) -> None:
        try:
            recs = parse_liquidation_records(
                msg, received_at=received, collector_instance_id=self.instance_id
            )
        except Exception:
            self.stats.parse_errors += 1
            logger.exception("liquidation parse")
            return
        kept = []
        for rec in recs:
            if not self.dedup.check_and_add(rec["event_key"]):
                self.stats.duplicates_suppressed += 1
                continue
            kept.append(rec)
            self.stats.last_liquidation_time = rec["event_time"]
        if kept:
            await self.writer.enqueue("all_liquidations", kept)

    async def _on_ticker(self, msg: dict[str, Any], received: datetime) -> None:
        payload = msg.get("data")
        symbol = None
        if isinstance(payload, dict):
            symbol = payload.get("symbol")
        topic = str(msg.get("topic") or "")
        if symbol is None and topic.startswith("tickers."):
            symbol = topic.split(".", 1)[1]
        if not isinstance(symbol, str) or symbol not in self.states:
            return
        result = self.states[symbol].apply_ticker(msg, received_at=received)
        self.stats.per_symbol_valid[symbol] = self.states[symbol].valid
        if result.get("action") == "parse_error":
            self.stats.parse_errors += 1
            return
        if result.get("changed"):
            row = self.states[symbol].change_event_row(self.instance_id)
            if row:
                await self.writer.enqueue("open_interest_events", [row])

    async def _subscribe(self, ws) -> None:
        topics = self._topics()
        chunk = max(1, self.settings.subscribe_chunk)
        self.stats.subscription_confirmed = False
        self.stats.confirmed_topics = []
        for i in range(0, len(topics), chunk):
            args = topics[i : i + chunk]
            self._pending_subscribe_topics = list(args)
            self._chunk_ack = False
            await ws.send(orjson.dumps({"op": "subscribe", "args": args}).decode())
            ack_deadline = time.monotonic() + 10.0
            while not self._chunk_ack:
                remaining = ack_deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadConnection("subscribe_ack_timeout")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                await self.handle_message(raw)
        missing = [t for t in topics if t not in self.stats.confirmed_topics]
        if missing:
            raise DeadConnection(f"incomplete_subscribe:{len(missing)}")
        self.stats.subscription_confirmed = True

    async def _send_ping(self, ws) -> None:
        self.note_ping_sent()
        await ws.send(orjson.dumps({"op": "ping"}).decode())

    async def _session(self, deadline: float | None) -> None:
        ping_every = self.settings.ping_interval_sec
        hb_every = self.settings.heartbeat_interval_sec
        next_hb = time.monotonic() + hb_every
        # Application ping only; library transport ping is disabled.
        async with websockets.connect(
            self.settings.bybit_ws_url, ping_interval=None, ping_timeout=None
        ) as ws:
            self._ws = ws
            self.stats.ws_connected = True
            self.stats.ping_ok = False
            self.stats.last_market_mono = None
            self.stats.connection_generation += 1
            self._invalidate_all()
            if self.stats.reconnect_count > 0:
                self.mark_reconnect_complete()
            await self._subscribe(ws)
            await self._send_ping(ws)
            await self._health("STARTED", message="ws_connected")
            session_start = time.monotonic()
            next_ping = session_start + ping_every
            while not self._stop.is_set():
                now = time.monotonic()
                if self._force_reconnect.is_set():
                    self._force_reconnect.clear()
                    raise DeadConnection("forced_reconnect")
                if (
                    self.reconnect_after_sec > 0
                    and not self._forced_reconnect_used
                    and (now - session_start) >= self.reconnect_after_sec
                ):
                    self._forced_reconnect_used = True
                    raise DeadConnection("forced_reconnect")
                if deadline is not None and now >= deadline:
                    self.request_stop()
                    break
                timeout = max(0.01, min(next_ping, next_hb, deadline or now + 30) - now)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    await self.handle_message(raw)
                except asyncio.TimeoutError:
                    pass
                self.check_liveness()
                now = time.monotonic()
                if now >= next_ping:
                    await self._send_ping(ws)
                    next_ping = now + ping_every
                if now >= next_hb:
                    await self._emit_5s()
                    await self._health("HEARTBEAT")
                    next_hb = now + hb_every

    async def _emit_5s(self) -> None:
        now = utc_now()
        bucket = floor_5s(now)
        rows = []
        for symbol, st in self.states.items():
            if not st.valid:
                continue
            prev = self._written_5s.get(symbol)
            if prev == bucket:
                continue
            row = st.snapshot_5s_row(bucket_time=bucket, now=now, collector_instance_id=self.instance_id)
            if row is None:
                continue
            rows.append(row)
            self._written_5s[symbol] = bucket
        if rows:
            await self.writer.enqueue("open_interest_5s", rows)

    async def _five_second_loop(self) -> None:
        while not self._stop.is_set():
            now = utc_now()
            bucket = floor_5s(now)
            next_edge = bucket.timestamp() + 5
            sleep_for = max(0.01, next_edge - now.timestamp())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                await self._emit_5s()

    async def run(self) -> dict[str, Any]:
        started = utc_now()
        self.stats.started_at = started
        deadline = None if self.duration_sec <= 0 else time.monotonic() + self.duration_sec
        five_task = asyncio.create_task(self._five_second_loop())
        backoff = self.settings.reconnect_initial_sec
        try:
            while not self._stop.is_set():
                try:
                    await self._session(deadline)
                    break
                except (ConnectionClosed, OSError, InsertError, DeadConnection, RuntimeError) as exc:
                    if self._stop.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    reason = str(exc) or type(exc).__name__
                    self.mark_disconnect(reason)
                    self.stats.reconnect_count += 1
                    logger.warning("reconnect after %s", reason)
                    await self._health("RECONNECT", message=reason)
                    jitter = random.random() * backoff * 0.2
                    await asyncio.sleep(backoff + jitter)
                    backoff = next_backoff(
                        backoff,
                        initial=self.settings.reconnect_initial_sec,
                        cap=self.settings.reconnect_max_sec,
                    )
        finally:
            self.request_stop()
            five_task.cancel()
            self.stats.ws_connected = False
            await self._health("STOPPED")
        return {
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "started_utc": started.isoformat(),
            "finished_utc": utc_now().isoformat(),
            "subscribed": list(self.plan.subscribed),
            "universe_hash": self.plan.universe_hash,
            "reconnect_count": self.stats.reconnect_count,
            "messages_received": self.stats.messages_received,
            "parse_errors": self.stats.parse_errors,
            "rows_inserted": self.writer.rows_inserted,
            "queue_drops": self.writer.queue_drops,
            "duplicates_suppressed": self.stats.duplicates_suppressed,
            "ping_count": self.stats.ping_count,
            "pong_count": self.stats.pong_count,
            "last_disconnect_duration_ms": self.stats.last_disconnect_duration_ms,
            "oi_valid": dict(self.stats.per_symbol_valid),
        }


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bybit OI + allLiquidation collector")
    parser.add_argument("--mode", choices=("smoke", "live"), default="smoke")
    parser.add_argument("--duration", type=float, default=0.0, help="0 = until SIGTERM")
    parser.add_argument("--symbols", type=str, default="", help="comma list override")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--reconnect-after-sec", type=float, default=0.0)
    parser.add_argument("--stability-gate", action="store_true", help="BTCUSDT 5min + 2min reconnect")
    parser.add_argument("--skip-lock", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_oi_settings()
    logger.info("settings %s", redact_settings(settings))
    lock = SingleInstanceLock(settings.lock_path, settings.pid_path)
    if not args.skip_lock:
        lock.acquire()
    loop = asyncio.get_running_loop()
    collector_holder: dict[str, OILiquidationCollector] = {}

    def _stop(*_a) -> None:
        col = collector_holder.get("c")
        if col:
            col.request_stop()

    def _reconnect(*_a) -> None:
        col = collector_holder.get("c")
        if col:
            col.request_reconnect()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)
    loop.add_signal_handler(signal.SIGUSR1, _reconnect)

    try:
        bybit = fetch_bybit_linear_usdt_perps(settings.bybit_rest_url)
        plan = plan_universe(universe_path=settings.universe_path, bybit_symbols=bybit, subscribe=True)
        if args.stability_gate:
            args.symbols = args.symbols or "BTCUSDT"
            if args.duration <= 0:
                args.duration = 420.0
            if args.reconnect_after_sec <= 0:
                args.reconnect_after_sec = 300.0
        if args.symbols:
            override = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
            if any(s in EXCLUDED_SYMBOLS for s in override):
                raise RuntimeError("XAUUSDT/XAU explicitly excluded")
            for s in override:
                if s not in plan.supported:
                    raise RuntimeError(f"symbol not supported: {s}")
            plan.subscribed = override
            for decision in plan.decisions:
                decision.subscribed = decision.symbol in override and decision.supported
        elif args.mode == "smoke":
            smoke = ("BTCUSDT",)
            if smoke[0] not in plan.supported:
                raise RuntimeError("smoke symbol not supported: BTCUSDT")
            plan.subscribed = smoke
            for decision in plan.decisions:
                decision.subscribed = decision.symbol in smoke and decision.supported
        plan.subscribed = tuple(s for s in plan.subscribed if s not in EXCLUDED_SYMBOLS)
        if "XAUUSDT" in plan.subscribed:
            raise RuntimeError("XAUUSDT must not be subscribed")
        if args.mode == "live" and not args.symbols and not args.stability_gate and len(plan.subscribed) != 51:
            raise RuntimeError(f"expected 51 subscribed symbols, got {len(plan.subscribed)}")
        out_dir = Path("/home/telgenbuescher/projects/orderbook_analyse/results/oi_liquidation_collector")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "universe_plan.json").write_text(json.dumps(plan.to_dict(), indent=2))
        duration = args.duration
        if args.mode == "smoke" and duration <= 0:
            duration = 300.0
        factory = default_client_factory(settings)
        client = factory()
        apply_schema(client)
        client.close()
        writer = AllowlistedWriter(
            client_factory=factory,
            batch_size=settings.batch_size,
            flush_interval_sec=settings.flush_interval_sec,
            queue_maxsize=settings.queue_maxsize,
        )
        await writer.start()
        collector = OILiquidationCollector(
            settings,
            plan,
            writer,
            duration_sec=duration,
            reconnect_after_sec=float(args.reconnect_after_sec or 0),
        )
        collector_holder["c"] = collector
        result = await collector.run()
        await writer.stop()
        logger.info("stopped %s", {k: result[k] for k in result if k != "oi_valid"})
        print(orjson.dumps(result, default=str).decode())
        return 0
    finally:
        if not args.skip_lock:
            lock.release()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))
