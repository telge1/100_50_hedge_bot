"""Allowlisted ClickHouse writer with single-flight inserts, reconnect, spool ack."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Any, Callable, Protocol

from . import ALLOWED_TABLES, FORBIDDEN_TABLES
from .spool import DurableSpool, SpoolError, SpoolMetaError, SpoolRecord

logger = logging.getLogger(__name__)

TABLE_COLUMNS: dict[str, list[str]] = {
    "all_liquidations": [
        "exchange",
        "category",
        "symbol",
        "event_time",
        "system_generated_at",
        "received_at",
        "position_side_raw",
        "liquidated_position_side",
        "size",
        "bankruptcy_price",
        "notional_estimate",
        "source_topic",
        "event_key",
        "raw_payload_hash",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_events": [
        "exchange",
        "category",
        "symbol",
        "event_time",
        "received_at",
        "cross_sequence",
        "open_interest",
        "open_interest_value",
        "single_open_interest",
        "single_open_interest_value",
        "last_price",
        "mark_price",
        "index_price",
        "funding_rate",
        "message_type",
        "source_topic",
        "state_valid",
        "event_key",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_5s": [
        "exchange",
        "category",
        "symbol",
        "bucket_time",
        "source_event_time",
        "received_at",
        "open_interest",
        "open_interest_value",
        "single_open_interest",
        "single_open_interest_value",
        "last_price",
        "mark_price",
        "index_price",
        "state_age_ms",
        "state_valid",
        "source",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_5m_history": [
        "exchange",
        "category",
        "symbol",
        "bucket_time",
        "open_interest",
        "open_interest_value",
        "source",
        "collector_instance_id",
        "inserted_at",
    ],
    "oi_liquidation_health": [
        "event_ts",
        "collector_instance_id",
        "symbol",
        "source",
        "event_type",
        "ws_connected",
        "ping_ok",
        "subscription_confirmed",
        "oi_state_valid",
        "oi_state_age_ms",
        "last_event_time",
        "last_received_at",
        "last_liquidation_time",
        "lag_ms",
        "messages_received",
        "rows_inserted",
        "duplicates_suppressed",
        "parse_errors",
        "insert_errors",
        "reconnect_count",
        "subscription_count",
        "queue_size",
        "queue_drops",
        "clock_offset_ms",
        "message",
    ],
}


class InsertError(RuntimeError):
    pass


class ClickHouseClient(Protocol):
    def insert(self, table: str, data: list[tuple[Any, ...]], column_names: list[str]) -> Any: ...
    def command(self, sql: str) -> Any: ...
    def query(self, sql: str) -> Any: ...
    def close(self) -> None: ...


def row_tuple(table: str, rec: dict[str, Any]) -> tuple[Any, ...]:
    cols = TABLE_COLUMNS[table]
    return tuple(rec.get(c) for c in cols)


def assert_table_allowed(table: str) -> None:
    if table in FORBIDDEN_TABLES or table not in ALLOWED_TABLES:
        raise ValueError(f"refusing write to table {table!r}")


def is_session_locked_error(exc: BaseException) -> bool:
    text = str(exc).upper()
    return "SESSION_IS_LOCKED" in text or "CODE: 373" in text or "CODE:373" in text


def is_connection_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "connection",
        "timeout",
        "timed out",
        "reset by peer",
        "broken pipe",
        "close_wait",
        "operationalerror",
        "unexpected http",
        "network",
        "refused",
    )
    return any(n in text for n in needles) or is_session_locked_error(exc)


class AllowlistedWriter:
    """Single-flight ClickHouse writer. One task owns the client; no shared sessions."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], ClickHouseClient],
        batch_size: int = 500,
        flush_interval_sec: float = 1.0,
        queue_maxsize: int = 20_000,
        max_retries: int = 6,
        spool: DurableSpool | None = None,
        retry_base_sec: float = 0.1,
        retry_cap_sec: float = 8.0,
    ) -> None:
        self._client_factory = client_factory
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.max_retries = max_retries
        self.retry_base_sec = retry_base_sec
        self.retry_cap_sec = retry_cap_sec
        self.queue_maxsize = queue_maxsize
        self.spool = spool
        self._queue: asyncio.Queue[
            tuple[str, list[tuple[Any, ...]], list[int] | None] | None
        ] = asyncio.Queue(maxsize=queue_maxsize)
        self._buffers: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        self._buffer_seqs: dict[str, list[int]] = defaultdict(list)
        self._client: ClickHouseClient | None = None
        self.rows_inserted = 0
        self.insert_errors = 0
        self.queue_drops = 0
        self.clickhouse_reconnect_count = 0
        self.writer_restart_count = 0
        self.last_successful_insert_mono: float | None = None
        self.last_successful_insert_unix: float | None = None
        self.last_oi_persisted_unix: float | None = None
        self.last_liquidation_persisted_unix: float | None = None
        self.clickhouse_reachable = False
        self._worker: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._fatal: BaseException | None = None
        self._insert_lock = asyncio.Lock()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    def is_alive(self) -> bool:
        return self._worker is not None and not self._worker.done() and self._fatal is None

    async def start(self) -> None:
        self._stop.clear()
        self._fatal = None
        self._worker = asyncio.create_task(self._run(), name="oi-liq-writer")
        self.writer_restart_count += 1

    async def enqueue(self, table: str, recs: list[dict[str, Any]]) -> int:
        assert_table_allowed(table)
        if not recs:
            return 0
        spool_seqs: list[int] | None = None
        if self.spool is not None:
            written = self.spool.append_many(table, recs)
            spool_seqs = [w.seq for w in written]
        rows = [row_tuple(table, r) for r in recs]
        try:
            self._queue.put_nowait((table, rows, spool_seqs))
            return 0
        except asyncio.QueueFull:
            self.queue_drops += len(rows)
            return len(rows)

    async def enqueue_spool_records(self, records: list[SpoolRecord]) -> int:
        if not records:
            return 0
        drops = 0
        by_table: dict[str, list[SpoolRecord]] = defaultdict(list)
        for rec in records:
            assert_table_allowed(rec.table)
            by_table[rec.table].append(rec)
        for table, group in by_table.items():
            rows = [row_tuple(table, r.payload) for r in group]
            seqs = [r.seq for r in group]
            try:
                self._queue.put_nowait((table, rows, seqs))
            except asyncio.QueueFull:
                drops += len(rows)
                self.queue_drops += len(rows)
        return drops

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._worker:
            await self._worker
            self._worker = None
        self._discard_client()
        if self._fatal:
            raise InsertError(str(self._fatal)) from self._fatal

    @staticmethod
    def _qualified(table: str) -> str:
        assert_table_allowed(table)
        return f"orderbook_analysis.{table}"

    def _discard_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.exception("close clickhouse")
            self._client = None
        self.clickhouse_reachable = False

    def _ensure_client(self) -> ClickHouseClient:
        if self._client is None:
            self._client = self._client_factory()
            self.clickhouse_reconnect_count += 1
        return self._client

    def ping_sync(self) -> bool:
        try:
            client = self._ensure_client()
            client.command("SELECT 1")
            self.clickhouse_reachable = True
            return True
        except Exception as exc:
            logger.warning("clickhouse ping failed: %s", exc)
            self._discard_client()
            return False

    def _backoff_sleep(self, attempt: int) -> None:
        base = min(self.retry_cap_sec, self.retry_base_sec * (2 ** attempt))
        delay = base * (0.5 + random.random())
        time.sleep(delay)

    def _ack_spool_seqs(self, spool_seqs: list[int]) -> None:
        """Ack after a successful insert. Never re-insert on meta/ack failure."""
        valid = [s for s in spool_seqs if s >= 0]
        if not valid or self.spool is None:
            return
        target = max(valid)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.spool.ack_through(target)
                # Free disk used by fully-acked prefix segments so max_bytes
                # capacity tracks unacked backlog, not historical WAL size.
                try:
                    removed = self.spool.truncate_acked_segments()
                    if removed:
                        logger.info("spool truncated %s acked segment file(s)", removed)
                except Exception as trunc_exc:  # noqa: BLE001
                    logger.warning("spool truncate after ack failed: %s", trunc_exc)
                return
            except SpoolError as exc:
                last_exc = exc
                self.insert_errors += 1
                logger.warning(
                    "spool ack attempt %s/%s failed after insert (seq=%s): %s",
                    attempt + 1,
                    self.max_retries,
                    target,
                    exc,
                )
                self._backoff_sleep(attempt)
        # Meta/ack failure is critical: fail writer without duplicating CH rows.
        raise InsertError(
            f"spool ack failed after successful insert for seq={target}: {last_exc}"
        ) from last_exc

    def _insert_sync(
        self,
        table: str,
        rows: list[tuple[Any, ...]],
        spool_seqs: list[int] | None = None,
    ) -> None:
        assert_table_allowed(table)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                client = self._ensure_client()
                client.insert(self._qualified(table), rows, column_names=TABLE_COLUMNS[table])
                self.rows_inserted += len(rows)
                now = time.time()
                self.last_successful_insert_mono = time.monotonic()
                self.last_successful_insert_unix = now
                self.clickhouse_reachable = True
                if table == "all_liquidations":
                    self.last_liquidation_persisted_unix = now
                elif table.startswith("open_interest"):
                    self.last_oi_persisted_unix = now
                break
            except SpoolMetaError:
                # Should not happen during insert; propagate as fatal path.
                raise
            except Exception as exc:
                last_exc = exc
                self.insert_errors += 1
                logger.warning(
                    "insert attempt %s/%s failed for %s: %s",
                    attempt + 1,
                    self.max_retries,
                    table,
                    exc,
                )
                if is_session_locked_error(exc) or is_connection_error(exc):
                    self._discard_client()
                self._backoff_sleep(attempt)
        else:
            raise InsertError(f"insert failed for {table}: {last_exc}") from last_exc
        if self.spool is not None and spool_seqs:
            self._ack_spool_seqs(spool_seqs)

    async def _flush_table(self, table: str) -> None:
        chunk = self._buffers[table]
        seqs = self._buffer_seqs[table]
        if not chunk:
            return
        self._buffers[table] = []
        self._buffer_seqs[table] = []
        try:
            async with self._insert_lock:
                await asyncio.to_thread(self._insert_sync, table, chunk, list(seqs) if seqs else None)
        except Exception:
            self._buffers[table] = chunk + self._buffers[table]
            self._buffer_seqs[table] = seqs + self._buffer_seqs[table]
            raise

    async def _flush_all(self) -> None:
        for table in list(self._buffers):
            if self._buffers[table]:
                await self._flush_table(table)

    async def _run(self) -> None:
        last = time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty() or any(self._buffers.values()):
                timeout = max(0.01, self.flush_interval_sec - (time.monotonic() - last))
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._flush_all()
                    last = time.monotonic()
                    continue
                if item is None:
                    await self._flush_all()
                    last = time.monotonic()
                    if self._stop.is_set():
                        break
                    continue
                table, rows, spool_seqs = item
                self._buffers[table].extend(rows)
                if spool_seqs:
                    self._buffer_seqs[table].extend(spool_seqs)
                else:
                    self._buffer_seqs[table].extend([-1] * len(rows))
                if len(self._buffers[table]) >= self.batch_size:
                    await self._flush_table(table)
                    last = time.monotonic()
        except Exception as exc:
            self._fatal = exc
            logger.exception("writer failed")
        finally:
            try:
                await self._flush_all()
            except Exception as exc:
                self._fatal = self._fatal or exc
                logger.exception("writer final flush failed")
