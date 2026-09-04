"""Orderbook V3 live collector with isolated multi-symbol state.

SequenceBreak of one symbol resyncs only that symbol on the same WebSocket
(unsubscribe + subscribe). Connection-level failures still reconnect the
session and resubscribe all chunks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, SequenceBreak
from orderbook_analyse.orderbook_v2_live.depth import parse_orderbook_topic
from orderbook_analyse.orderbook_v2_live.health import dt_iso, percentile, utc_now, write_health_line
from orderbook_analyse.orderbook_v2_live.locks import SingleInstanceLock
from orderbook_analyse.orderbook_v2_live.raw_archive.config import load_raw_archive_settings
from orderbook_analyse.orderbook_v2_live.raw_archive.manager import RawArchiveManager
from orderbook_analyse.orderbook_v2_live.settings import (
    LiveCollectorSettings,
    load_live_settings,
    load_raw_archive_only_settings,
    redact_settings,
)
from orderbook_analyse.orderbook_v2_live.skip_before import load_skip_map
from orderbook_analyse.orderbook_v2_live.subscribe import chunk_topics
from orderbook_analyse.orderbook_v2_live.writer import FeatureWriter, InsertError, NullFeatureWriter, QueueFullError

logger = logging.getLogger(__name__)


class DeadConnection(RuntimeError):
    pass


def next_backoff(current: float, *, initial: float = 1.0, cap: float = 8.0) -> float:
    nxt = current * 2.0 if current > 0 else initial
    return min(cap, max(initial, nxt))


@dataclass
class SymbolRuntime:
    symbol: str
    clock: LiveSecondClock
    skip_before_ms: int | None = None
    last_db_bucket: datetime | None = None
    detected_gap_seconds: int | None = None
    catchup_required: bool = False
    skip_error: str | None = None
    subscribed: bool = False
    subscription_confirmed: bool = False
    active_generation: int | None = None
    dropping_until_subscribe_ack: bool = False
    last_event_timestamp: datetime | None = None
    last_received_at: datetime | None = None
    last_bucket_written: datetime | None = None
    last_error: str = ""
    event_lag_samples_ms: list[float] = field(default_factory=list)
    receive_lag_samples_ms: list[float] = field(default_factory=list)
    messages_received: int = 0
    rows_enqueued: int = 0
    rows_written_ack: int = 0
    symbol_resyncs: int = 0
    unwritten_seconds: int = 0
    pending_raw: list[tuple[dict[str, Any], datetime]] = field(default_factory=list)


class OrderbookV3LiveCollector:
    def __init__(
        self,
        settings: LiveCollectorSettings,
        *,
        client_factory=None,
        duration_sec: float = 0.0,
        writer: FeatureWriter | NullFeatureWriter | None = None,
        raw_archive: RawArchiveManager | None = None,
        archive_only: bool = False,
    ) -> None:
        self.settings = settings
        self.archive_only = archive_only or settings.mode == "raw-archive-only"
        self.duration_sec = duration_sec
        self._client_factory = client_factory or (lambda: get_clickhouse_client())
        if self.archive_only:
            if writer is not None and not isinstance(writer, NullFeatureWriter):
                raise ValueError("archive_only mode requires NullFeatureWriter or no writer")
            self.writer = writer or NullFeatureWriter()
        else:
            self.writer = writer or FeatureWriter(
                self._client_factory,
                queue_capacity=settings.queue_capacity,
                insert_batch_size=settings.insert_batch_size,
                flush_interval_sec=settings.flush_interval_sec,
                insert_retry_count=settings.insert_retry_count,
                shutdown_flush_timeout_sec=settings.shutdown_flush_timeout_sec,
            )
        self.raw_archive = raw_archive
        self.runtimes: dict[str, SymbolRuntime] = {}
        self.collector_state = "STARTING"
        self.connected = False
        self.reconnects_total = 0
        self.fail_closed = False
        self.process_start_time = utc_now()
        self.live_start_time: datetime | None = None
        self.last_error = ""
        self.subscription_chunks: list[list[str]] = []
        self.confirmed_topics: list[str] = []
        self._stop = asyncio.Event()
        self._ws = None
        self._writer_task: asyncio.Task | None = None
        self._pending_chunk: list[str] = []
        self._chunk_ack = False
        self._unsub_ack = False
        self._archive_rotation_bucket: dict[str, str] = {}
        self.exit_code = 0
        self._on_demand_socket = None
        from orderbook_analyse.orderbook_v2_live.on_demand_manager import (
            OnDemandDepthManager,
            load_on_demand_settings,
        )

        od_cfg = load_on_demand_settings()
        self.on_demand: OnDemandDepthManager | None = None
        if od_cfg["enabled"]:
            self.on_demand = OnDemandDepthManager(
                exchange=settings.exchange,
                market=settings.market,
                send_chunk=self._send_chunk,
                confirmed_topics=self.confirmed_topics,
                settings=od_cfg,
            )
            from orderbook_analyse.orderbook_v2_live.on_demand_socket import OnDemandSocketServer

            self._on_demand_socket = OnDemandSocketServer(
                self.on_demand.socket_path,
                self.on_demand.handle_request,
            )

    def request_stop(self) -> None:
        self.collector_state = "STOPPING"
        self._stop.set()

    def _reset_runtimes(self, skip_map: dict[str, dict[str, Any]] | None = None) -> None:
        skip_map = skip_map or {}
        self.runtimes = {}
        for symbol in self.settings.symbols:
            info = skip_map.get(symbol) or {}
            skip_ms = info.get("skip_before_ms")
            clock = LiveSecondClock(
                symbol=symbol,
                depth=self.settings.depth,
                exchange=self.settings.exchange,
                market=self.settings.market,
                skip_before_ms=skip_ms,
            )
            self.runtimes[symbol] = SymbolRuntime(
                symbol=symbol,
                clock=clock,
                skip_before_ms=skip_ms,
                last_db_bucket=info.get("last_db_bucket"),
                detected_gap_seconds=info.get("detected_gap_seconds"),
                catchup_required=bool(info.get("catchup_required")),
                skip_error=info.get("error"),
            )
        if self.connected:
            self.collector_state = "WAITING_FOR_SNAPSHOT"

    def isolate_symbol(self, symbol: str, reason: str) -> None:
        """Invalidate only one symbol. Does not reset other clocks."""
        rt = self.runtimes[symbol]
        rt.clock.begin_resync()
        rt.dropping_until_subscribe_ack = True
        rt.active_generation = None
        rt.pending_raw = []
        rt.symbol_resyncs += 1
        rt.last_error = reason
        if self.raw_archive is not None and self.raw_archive.enabled:
            self.raw_archive.note_sequence_gap(symbol, details={"reason": reason})
            self.raw_archive.note_lifecycle("RESYNC", symbol=symbol, details={"reason": reason})
        logger.warning("symbol_resync %s reason=%s gen=%s", symbol, reason, rt.clock.generation)

    def _enqueue_rows(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        if self.archive_only or not rows:
            return
        rt = self.runtimes[symbol]
        try:
            self.writer.enqueue(rows)
        except QueueFullError:
            self.fail_closed = True
            self.collector_state = "ERROR"
            self.last_error = "queue_full"
            rt.last_error = "queue_full"
            rt.unwritten_seconds += len(rows)
            for row in rows:
                bs = row["bucket_start"]
                ms = int(bs.timestamp() * 1000)
                rt.clock.note_enqueue_failed(ms)
            logger.error("queue_full fail-closed symbol=%s n=%s", symbol, len(rows))
            return
        rt.rows_enqueued += len(rows)
        for row in rows:
            bs = row["bucket_start"]
            ms = int(bs.timestamp() * 1000)
            rt.clock.note_enqueued(ms)
            if getattr(bs, "tzinfo", None) is None:
                bs = bs.replace(tzinfo=timezone.utc)
            rt.last_bucket_written = bs

    def handle_orderbook_message(self, payload: dict[str, Any], received_at: datetime) -> None:
        if self.fail_closed or self._stop.is_set():
            return
        if self.on_demand is not None and self.on_demand.handle_message(payload, received_at):
            return
        topic = str(payload.get("topic") or "")
        expected = {f"orderbook.{self.settings.depth}.{s}": s for s in self.settings.symbols}
        symbol = expected.get(topic)
        if symbol is None:
            return
        rt = self.runtimes[symbol]
        if rt.dropping_until_subscribe_ack or rt.active_generation is None:
            rt.pending_raw.append((payload, received_at))
            return
        self._ingest_ready(rt, payload, received_at)

    def _ingest_ready(self, rt: SymbolRuntime, payload: dict[str, Any], received_at: datetime) -> None:
        symbol = rt.symbol
        msg_type = str(payload.get("type") or "")
        ts_ms = int(payload.get("ts") or 0)
        data = payload.get("data") or {}
        if str(data.get("s") or symbol) != symbol:
            rt.clock.stats.dropped_events += 1
            return
        if self.raw_archive is not None and self.raw_archive.enabled:
            self.raw_archive.try_enqueue_market(symbol, payload, received_at)
        rt.messages_received += 1
        now = utc_now()
        event_lag = (received_at.timestamp() * 1000.0) - float(ts_ms)
        recv_lag = (now.timestamp() - received_at.timestamp()) * 1000.0
        rt.event_lag_samples_ms.append(event_lag)
        rt.receive_lag_samples_ms.append(recv_lag)
        if len(rt.event_lag_samples_ms) > 2000:
            rt.event_lag_samples_ms = rt.event_lag_samples_ms[-1000:]
            rt.receive_lag_samples_ms = rt.receive_lag_samples_ms[-1000:]
        rt.last_event_timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        rt.last_received_at = received_at
        try:
            rows = rt.clock.ingest(
                msg_type, ts_ms, data, generation=rt.active_generation
            )
        except SequenceBreak as exc:
            self.isolate_symbol(symbol, str(exc))
            if self._ws is not None:
                try:
                    asyncio.get_running_loop().create_task(self._resync_symbol(symbol))
                except RuntimeError:
                    pass
            return
        if not self.archive_only:
            self._enqueue_rows(symbol, rows)
        else:
            for row in rows:
                bs = row["bucket_start"]
                ms = int(bs.timestamp() * 1000)
                rt.clock.note_enqueued(ms)
        self._maybe_rotate_archive(symbol, rt, received_at, ts_ms)

    def _maybe_rotate_archive(
        self,
        symbol: str,
        rt: SymbolRuntime,
        received_at: datetime,
        ts_ms: int,
    ) -> None:
        if self.raw_archive is None or not self.raw_archive.enabled:
            return
        book = rt.clock.last_valid_book
        if book is None or not book.is_valid:
            return
        bucket = self.raw_archive._rotation_bucket(received_at)
        prev = self._archive_rotation_bucket.get(symbol)
        if prev is not None and prev != bucket:
            topic = f"orderbook.{self.settings.depth}.{symbol}"
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.raw_archive.rotate_with_checkpoint(
                        symbol,
                        book,
                        ts_ms=ts_ms,
                        received_at=received_at,
                        topic=topic,
                    )
                )
            except RuntimeError:
                pass
        self._archive_rotation_bucket[symbol] = bucket

    def _arm_symbol(self, symbol: str) -> None:
        rt = self.runtimes[symbol]
        rt.dropping_until_subscribe_ack = False
        rt.active_generation = rt.clock.generation
        pending = rt.pending_raw
        rt.pending_raw = []
        for payload, received_at in pending:
            self._ingest_ready(rt, payload, received_at)

    def _memory_health(self) -> dict[str, Any]:
        rss_bytes = None
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_bytes = int(line.split()[1]) * 1024
                        break
        except OSError:
            rss_bytes = None
        dedupe_entries = sum(len(rt.clock.recent_us) for rt in self.runtimes.values())
        dedupe_capacity = sum(rt.clock.recent_us.capacity for rt in self.runtimes.values())
        dedupe_evictions = sum(rt.clock.recent_us.evictions for rt in self.runtimes.values())
        book_levels = 0
        for rt in self.runtimes.values():
            book = rt.clock.last_valid_book or rt.clock.book
            book_levels += len(book.bids) + len(book.asks)
        return {
            "rss_bytes": rss_bytes,
            "rss_mb": None if rss_bytes is None else round(rss_bytes / (1024 * 1024), 2),
            "dedupe_entries_total": dedupe_entries,
            "dedupe_capacity_total": dedupe_capacity,
            "dedupe_evictions_total": dedupe_evictions,
            "book_levels_total": book_levels,
            "pending_raw_total": sum(len(rt.pending_raw) for rt in self.runtimes.values()),
        }

    def health_payload(self) -> dict[str, Any]:
        clocks = [rt.clock for rt in self.runtimes.values()]
        waiting = sum(1 for c in clocks if c.waiting_for_snapshot)
        valid = sum(1 for c in clocks if c.last_valid_book is not None and c.last_valid_book.is_valid)
        invalid = sum(1 for rt in self.runtimes.values() if rt.clock.waiting_for_snapshot and rt.symbol_resyncs)
        if self.fail_closed:
            state = "ERROR"
        elif not self.connected:
            state = self.collector_state
        elif waiting == len(clocks) and clocks:
            state = "WAITING_FOR_SNAPSHOT"
        elif waiting:
            state = "LIVE"
        else:
            state = self.collector_state if self.collector_state == "STOPPING" else "LIVE"
        if self._stop.is_set() and self.collector_state in {"STOPPING", "STOPPED"}:
            state = self.collector_state
        per_symbol = []
        for symbol in self.settings.symbols:
            rt = self.runtimes[symbol]
            c = rt.clock
            first = None
            if c.first_valid_live_bucket_ms is not None:
                first = datetime.fromtimestamp(c.first_valid_live_bucket_ms / 1000.0, tz=timezone.utc)
            if c.waiting_for_snapshot:
                st = "WAITING_FOR_SNAPSHOT"
            elif c.last_valid_book and c.last_valid_book.is_valid:
                st = "LIVE"
            else:
                st = "INVALID"
            per_symbol.append({
                "symbol": symbol,
                "state": st,
                "subscribed": rt.subscribed,
                "subscription_confirmed": rt.subscription_confirmed,
                "snapshot_received": not c.waiting_for_snapshot,
                "book_valid": bool(c.last_valid_book and c.last_valid_book.is_valid),
                "last_event_timestamp": dt_iso(rt.last_event_timestamp),
                "last_received_at": dt_iso(rt.last_received_at),
                "last_bucket_written": dt_iso(rt.last_bucket_written),
                "first_valid_live_bucket": dt_iso(first),
                "skip_before_ms": rt.skip_before_ms,
                "last_db_bucket": dt_iso(rt.last_db_bucket),
                "detected_gap_seconds": rt.detected_gap_seconds,
                "catchup_required": rt.catchup_required,
                "skip_error": rt.skip_error,
                "event_lag_ms": rt.event_lag_samples_ms[-1] if rt.event_lag_samples_ms else None,
                "receive_lag_ms": rt.receive_lag_samples_ms[-1] if rt.receive_lag_samples_ms else None,
                "event_lag_p50": percentile(rt.event_lag_samples_ms, 50),
                "event_lag_p95": percentile(rt.event_lag_samples_ms, 95),
                "event_lag_p99": percentile(rt.event_lag_samples_ms, 99),
                "messages_received": rt.messages_received,
                "snapshots": c.stats.snapshots,
                "deltas": c.stats.deltas,
                "rows_enqueued": rt.rows_enqueued,
                "rows_written": rt.rows_enqueued,
                "sequence_gaps": c.stats.sequence_gaps,
                "symbol_resyncs": rt.symbol_resyncs,
                "invalid_book_count": c.stats.invalid_book,
                "stale_generation_dropped": c.stale_generation_dropped,
                "unwritten_seconds": rt.unwritten_seconds,
                "last_error": rt.last_error,
                "generation": c.generation,
                **c.memory_stats(),
            })
        wanted = self.settings.orderbook_topics()
        mem = self._memory_health()
        payload = {
            "collector_state": state,
            "connected": self.connected,
            "configured_symbols": list(self.settings.symbols),
            "subscribed_symbols": [s for s, rt in self.runtimes.items() if rt.subscription_confirmed],
            "wanted_topics": wanted,
            "confirmed_topics": list(self.confirmed_topics),
            "valid_books": valid,
            "invalid_books": len(clocks) - valid,
            "waiting_for_snapshot": waiting,
            "queue_size": self.writer.queue.qsize(),
            "queue_capacity": self.writer.queue_capacity,
            "queue_high_watermark": self.writer.queue_high_watermark,
            "writer_state": self.writer.state,
            "rows_written_total": self.writer.rows_written,
            "rows_enqueued_total": sum(rt.rows_enqueued for rt in self.runtimes.values()),
            "insert_batches": self.writer.batches_flushed,
            "insert_batch_last": self.writer.batch_sizes[-1] if self.writer.batch_sizes else None,
            "insert_batch_p50": percentile(self.writer.batch_sizes, 50),
            "insert_batch_p95": percentile(self.writer.batch_sizes, 95),
            "insert_batch_max": max(self.writer.batch_sizes) if self.writer.batch_sizes else None,
            "insert_latency_ms_p50": percentile(self.writer.insert_latencies_ms, 50),
            "insert_latency_ms_p95": percentile(self.writer.insert_latencies_ms, 95),
            "insert_latency_ms_p99": percentile(self.writer.insert_latencies_ms, 99),
            "insert_latency_ms_last": self.writer.insert_latencies_ms[-1] if self.writer.insert_latencies_ms else None,
            "reconnects_total": self.reconnects_total,
            "sequence_gaps_total": sum(rt.clock.stats.sequence_gaps for rt in self.runtimes.values()),
            "insert_failures_total": self.writer.insert_failures,
            "dropped_events_total": sum(rt.clock.stats.dropped_events for rt in self.runtimes.values()),
            "symbol_resyncs_total": sum(rt.symbol_resyncs for rt in self.runtimes.values()),
            "uptime_seconds": round((utc_now() - self.process_start_time).total_seconds(), 3),
            "parser_version": PARSER_VERSION,
            "mode": self.settings.mode,
            "fail_closed": self.fail_closed,
            "last_error": self.last_error,
            **mem,
            "per_symbol": per_symbol,
        }
        if self.archive_only:
            payload["collector_identity"] = "raw_archive_only"
            payload["feature_writer_enabled"] = False
        if self.raw_archive is not None:
            payload.update(self.raw_archive.health_dict())
        if self.on_demand is not None:
            payload.update(self.on_demand.health_dict())
        return payload

    def _log_health(self) -> None:
        payload = self.health_payload()
        logger.info("health %s", json.dumps(payload, separators=(",", ":")))
        write_health_line(self.settings.health_path, payload)

    async def _handle_raw(self, raw: str | bytes) -> None:
        received_at = utc_now()
        try:
            payload = orjson.loads(raw)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        op = str(payload.get("op") or "")
        ret = str(payload.get("ret_msg") or "").lower()
        if op == "pong" or ret == "pong":
            self._last_pong_mono = time.monotonic()
            return
        if op == "unsubscribe":
            if payload.get("success") is False:
                logger.error("unsubscribe_rejected %s", payload.get("ret_msg"))
                self.last_error = "unsubscribe_rejected"
            self._unsub_ack = True
            return
        if op == "subscribe":
            if payload.get("success") is False:
                raise DeadConnection("subscribe_rejected")
            for topic in self._pending_chunk:
                if topic not in self.confirmed_topics:
                    self.confirmed_topics.append(topic)
                parsed = parse_orderbook_topic(topic)
                if parsed is None:
                    continue
                sym, depth = parsed
                if depth != self.settings.depth:
                    continue
                if sym in self.runtimes:
                    self.runtimes[sym].subscribed = True
                    self.runtimes[sym].subscription_confirmed = True
                    self._arm_symbol(sym)
            self._chunk_ack = True
            return
        if payload.get("topic"):
            self._last_market_mono = time.monotonic()
            self.handle_orderbook_message(payload, received_at)

    async def _send_chunk(self, ws, op: str, args: list[str]) -> None:
        ack_attr = "_chunk_ack" if op == "subscribe" else "_unsub_ack"
        setattr(self, ack_attr, False)
        self._pending_chunk = list(args)
        await ws.send(orjson.dumps({"op": op, "args": args}).decode())
        deadline = time.monotonic() + 10.0
        while not getattr(self, ack_attr):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadConnection(f"{op}_ack_timeout")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            await self._handle_raw(raw)

    async def _subscribe_all(self, ws) -> None:
        topics = self.settings.orderbook_topics()
        self.subscription_chunks = chunk_topics(topics, self.settings.subscribe_chunk_size)
        self.confirmed_topics = []
        for chunk in self.subscription_chunks:
            await self._send_chunk(ws, "subscribe", chunk)
            if self.settings.subscribe_chunk_pause_sec:
                await asyncio.sleep(self.settings.subscribe_chunk_pause_sec)
        missing = [t for t in topics if t not in self.confirmed_topics]
        if missing:
            raise DeadConnection(f"incomplete_subscribe:{len(missing)}")
        for rt in self.runtimes.values():
            rt.subscription_confirmed = True
            rt.subscribed = True
            self._arm_symbol(rt.symbol)

    async def _resync_symbol(self, symbol: str) -> None:
        ws = self._ws
        if ws is None:
            return
        topic = f"orderbook.{self.settings.depth}.{symbol}"
        try:
            await self._send_chunk(ws, "unsubscribe", [topic])
            if topic in self.confirmed_topics:
                self.confirmed_topics.remove(topic)
            self.runtimes[symbol].subscription_confirmed = False
            await self._send_chunk(ws, "subscribe", [topic])
        except Exception as exc:
            logger.exception("symbol_resync_failed %s", symbol)
            self.runtimes[symbol].last_error = f"resync_failed:{type(exc).__name__}"
            raise DeadConnection(f"symbol_resync_failed:{symbol}") from exc

    async def _session(self, deadline: float | None) -> None:
        ping_every = self.settings.ping_interval_sec
        hb_every = self.settings.heartbeat_interval_sec
        self._last_pong_mono = None
        self._last_ping_mono = None
        self._last_market_mono = None
        async with websockets.connect(
            self.settings.bybit_ws_url, ping_interval=None, ping_timeout=None
        ) as ws:
            self._ws = ws
            self.connected = True
            self.collector_state = "WAITING_FOR_SNAPSHOT"
            if self.raw_archive is not None and self.raw_archive.enabled:
                self.raw_archive.note_lifecycle("CONNECT")
            if self.on_demand is not None:
                self.on_demand.on_reconnect()
            for rt in self.runtimes.values():
                rt.clock.begin_resync()
                rt.dropping_until_subscribe_ack = True
                rt.active_generation = None
                rt.subscription_confirmed = False
            await self._subscribe_all(ws)
            self._last_ping_mono = time.monotonic()
            await ws.send(orjson.dumps({"op": "ping"}).decode())
            logger.info("subscribed chunks=%s symbols=%s", len(self.subscription_chunks), list(self.settings.symbols))
            next_ping = time.monotonic() + ping_every
            next_hb = time.monotonic() + hb_every
            while not self._stop.is_set() and not self.fail_closed:
                now = time.monotonic()
                if self._writer_task is not None and self._writer_task.done():
                    exc = self._writer_task.exception()
                    if exc:
                        raise InsertError(str(exc))
                    self.fail_closed = True
                    break
                if deadline is not None and now >= deadline:
                    self.request_stop()
                    break
                if not self.archive_only:
                    wall_ms = int(utc_now().timestamp() * 1000)
                    for symbol, rt in self.runtimes.items():
                        if rt.dropping_until_subscribe_ack or rt.active_generation is None:
                            continue
                        rows = rt.clock.close_through(wall_ms)
                        self._enqueue_rows(symbol, rows)
                else:
                    wall_ms = int(utc_now().timestamp() * 1000)
                    for symbol, rt in self.runtimes.items():
                        if rt.dropping_until_subscribe_ack or rt.active_generation is None:
                            continue
                        rows = rt.clock.close_through(wall_ms)
                        for row in rows:
                            bs = row["bucket_start"]
                            ms = int(bs.timestamp() * 1000)
                            rt.clock.note_enqueued(ms)
                if self._last_ping_mono is not None and self._last_pong_mono is None:
                    if now - self._last_ping_mono >= self.settings.ping_timeout_sec:
                        raise DeadConnection("pong_timeout")
                if self._last_market_mono is not None:
                    if now - self._last_market_mono >= self.settings.stale_data_sec:
                        self.collector_state = "STALE"
                        raise DeadConnection("stale_market_data")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                    await self._handle_raw(raw)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if now >= next_ping:
                    self._last_ping_mono = now
                    self._last_pong_mono = None
                    await ws.send(orjson.dumps({"op": "ping"}).decode())
                    next_ping = now + ping_every
                if now >= next_hb:
                    self._log_health()
                    next_hb = now + hb_every
                if self.on_demand is not None:
                    await self.on_demand.tick(ws)
            try:
                await ws.close()
            except Exception:
                pass
        self._ws = None
        self.connected = False
        if self.raw_archive is not None and self.raw_archive.enabled:
            self.raw_archive.note_lifecycle("DISCONNECT")

    async def run(self) -> dict[str, Any]:
        self.live_start_time = utc_now()
        skip_map: dict[str, dict[str, Any]] = {}
        if not self.archive_only:
            client = self._client_factory()
            skip_map = load_skip_map(
                client, self.settings.symbols, now=self.live_start_time, depth=self.settings.depth
            )
            for symbol, info in skip_map.items():
                logger.info(
                    "skip_before symbol=%s last_db=%s skip_ms=%s gap=%s catchup=%s error=%s",
                    symbol,
                    dt_iso(info.get("last_db_bucket")),
                    info.get("skip_before_ms"),
                    info.get("detected_gap_seconds"),
                    info.get("catchup_required"),
                    info.get("error"),
                )
        self._reset_runtimes(skip_map)
        if self.raw_archive is not None and self.raw_archive.enabled:
            self.raw_archive.start()
        if self._on_demand_socket is not None:
            await self._on_demand_socket.start()
        if not self.archive_only:
            self._writer_task = asyncio.create_task(self.writer.run())
        deadline = None if self.duration_sec <= 0 else time.monotonic() + self.duration_sec
        backoff = self.settings.reconnect_initial_sec
        try:
            while not self._stop.is_set() and not self.fail_closed:
                try:
                    await self._session(deadline)
                    if self._stop.is_set() or self.fail_closed:
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                except (ConnectionClosed, OSError, DeadConnection, InsertError) as exc:
                    if self._stop.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    reason = str(exc) or type(exc).__name__
                    self.last_error = reason
                    self.connected = False
                    self.reconnects_total += 1
                    self.collector_state = "RECONNECTING"
                    if self.raw_archive is not None and self.raw_archive.enabled:
                        self.raw_archive.note_lifecycle("RECONNECT", details={"reason": reason})
                    logger.warning("reconnect after %s", reason)
                    self._log_health()
                    jitter = random.random() * backoff * 0.2
                    await asyncio.sleep(backoff + jitter)
                    backoff = next_backoff(
                        backoff,
                        initial=self.settings.reconnect_initial_sec,
                        cap=self.settings.reconnect_max_sec,
                    )
        finally:
            self.request_stop()
            if self._on_demand_socket is not None:
                await self._on_demand_socket.stop()
            flushed = True
            if self._writer_task is not None:
                flushed = await self.writer.join(self._writer_task)
            elif self.archive_only:
                flushed = True
            if self.raw_archive is not None and self.raw_archive.enabled:
                await self.raw_archive.stop()
            self.collector_state = "STOPPED"
            self.connected = False
            self._log_health()
            if not flushed or self.fail_closed or (
                not self.archive_only and self.writer.state == "ERROR"
            ):
                self.exit_code = 1
        return self.health_payload()


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orderbook V3 live collector")
    parser.add_argument(
        "--mode",
        choices=("ada", "shadow3", "universe51", "raw-archive-only"),
        default="ada",
    )
    parser.add_argument("--symbols", default="")
    parser.add_argument("--confirm-universe-51", action="store_true")
    parser.add_argument("--confirm-raw-archive-symbols", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--skip-lock", action="store_true")
    parser.add_argument("--health-file", default="")
    parser.add_argument("--allow-multi-symbol", action="store_true", help="deprecated; use --mode shadow3")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mode = args.mode
    if args.allow_multi_symbol and mode == "ada" and args.symbols:
        mode = "shadow3" if args.symbols.upper() == "ADAUSDT,BTCUSDT,ETHUSDT" else mode
        if mode == "ada":
            mode = "shadow3"
    health_path = Path(args.health_file) if args.health_file else None
    archive_only = mode == "raw-archive-only"
    if archive_only:
        if args.skip_lock:
            logger.warning("--skip-lock ignored for raw-archive-only (uses dedicated lock)")
        settings = load_raw_archive_only_settings(
            symbols_raw=args.symbols,
            confirm_raw_archive_symbols=args.confirm_raw_archive_symbols,
            health_path=health_path,
        )
    else:
        settings = load_live_settings(
            symbols_raw=args.symbols or None,
            mode=mode,
            confirm_universe_51=args.confirm_universe_51,
            health_path=health_path,
        )
    logger.info("settings %s", json.dumps(redact_settings(settings), separators=(",", ":")))
    if not archive_only:
        load_clickhouse_settings()
    lock = None
    if archive_only or not args.skip_lock:
        lock = SingleInstanceLock(settings.lock_path, settings.pid_path)
        lock.acquire()
    collector = OrderbookV3LiveCollector(settings, duration_sec=args.duration, archive_only=archive_only)
    raw_settings = load_raw_archive_settings(collector_symbols=settings.symbols)
    if archive_only:
        if not raw_settings.enabled:
            raise RuntimeError("raw-archive-only requires OB_V3_RAW_ARCHIVE_ENABLE=true")
        if not raw_settings.symbols:
            raise RuntimeError("raw-archive-only requires OB_V3_RAW_ARCHIVE_SYMBOLS")
        collector.raw_archive = RawArchiveManager(raw_settings, depth=settings.depth)
    elif raw_settings.enabled:
        collector.raw_archive = RawArchiveManager(raw_settings, depth=settings.depth)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, collector.request_stop)
    try:
        await collector.run()
    finally:
        if lock is not None:
            lock.release()
    return collector.exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))
