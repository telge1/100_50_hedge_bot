"""Unit tests for AsyncClickHouseWriter (mocked client, no network)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from orderbook_analyse.clickhouse_writer import AsyncClickHouseWriter, InsertError


class FakeClient:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.inserts: list[tuple[str, list[Any], list[str]]] = []
        self.closed = False

    def insert(self, table: str, data: list[Any], column_names: list[str]) -> None:
        if table in self.fail_on:
            raise RuntimeError(f"boom:{table}")
        self.inserts.append((table, list(data), list(column_names)))

    def close(self) -> None:
        self.closed = True


def _row(n: int = 0) -> tuple:
    return (
        datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 26, 12, 0, 0, 1, tzinfo=timezone.utc),
        "APTUSDT",
        "bid",
        1 + n,
        1,
        "delta",
        1,
        1,
        0,
    )


def test_batch_flush_on_size() -> None:
    async def _run() -> None:
        client = FakeClient()
        writer = AsyncClickHouseWriter(
            client_factory=lambda: client,
            batch_size=3,
            flush_interval_sec=30.0,
            queue_maxsize=100,
        )
        await writer.start()
        await writer.enqueue("orderbook_deltas", [_row(0), _row(1)])
        await asyncio.sleep(0.05)
        assert client.inserts == []
        await writer.enqueue("orderbook_deltas", [_row(2)])
        for _ in range(50):
            if client.inserts:
                break
            await asyncio.sleep(0.02)
        assert len(client.inserts) == 1
        assert len(client.inserts[0][1]) == 3
        assert writer.stats.rows_inserted["orderbook_deltas"] == 3
        assert writer.stats.flush_count == 1
        await writer.stop()
        writer.close()
        assert client.closed

    asyncio.run(_run())


def test_flush_on_shutdown() -> None:
    async def _run() -> None:
        client = FakeClient()
        writer = AsyncClickHouseWriter(
            client_factory=lambda: client,
            batch_size=1000,
            flush_interval_sec=30.0,
            queue_maxsize=100,
        )
        await writer.start()
        await writer.enqueue(
            "public_trades",
            [
                (
                    datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 26, 12, 0, 0, 1, tzinfo=timezone.utc),
                    "APTUSDT",
                    "id1",
                    "Buy",
                    1,
                    1,
                    1,
                    "",
                    0,
                    0,
                )
            ],
        )
        await writer.stop()
        assert len(client.inserts) == 1
        assert client.inserts[0][0] == "public_trades"
        assert writer.stats.rows_inserted["public_trades"] == 1
        writer.close()

    asyncio.run(_run())


def test_insert_error_is_surfaced() -> None:
    async def _run() -> None:
        client = FakeClient(fail_on={"orderbook_deltas"})
        writer = AsyncClickHouseWriter(
            client_factory=lambda: client,
            batch_size=1,
            flush_interval_sec=30.0,
            queue_maxsize=100,
        )
        await writer.start()
        await writer.enqueue("orderbook_deltas", [_row()])
        await asyncio.sleep(0.1)
        with pytest.raises(InsertError):
            await writer.stop()
        assert writer.stats.insert_error_count >= 1
        assert writer.stats.last_error is not None
        assert "boom:orderbook_deltas" in writer.stats.last_error
        writer.close()

    asyncio.run(_run())


def test_interval_flush() -> None:
    async def _run() -> None:
        client = FakeClient()
        writer = AsyncClickHouseWriter(
            client_factory=lambda: client,
            batch_size=1000,
            flush_interval_sec=0.05,
            queue_maxsize=100,
        )
        await writer.start()
        await writer.enqueue(
            "liquidations",
            [
                (
                    datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 26, 12, 0, 0, 1, tzinfo=timezone.utc),
                    "APTUSDT",
                    "Sell",
                    1,
                    1,
                    1,
                )
            ],
        )
        for _ in range(50):
            if client.inserts:
                break
            await asyncio.sleep(0.02)
        assert len(client.inserts) == 1
        await writer.stop()
        writer.close()

    asyncio.run(_run())


def test_interval_flush_under_continuous_load() -> None:
    """flush-interval must fire even when the inbound queue never idles."""

    async def _run() -> None:
        client = FakeClient()
        writer = AsyncClickHouseWriter(
            client_factory=lambda: client,
            batch_size=10_000,
            flush_interval_sec=0.05,
            queue_maxsize=1000,
        )
        await writer.start()

        async def producer() -> None:
            for i in range(40):
                await writer.enqueue("orderbook_deltas", [_row(i)])
                await asyncio.sleep(0.01)

        await producer()
        for _ in range(50):
            if writer.stats.flush_count >= 1:
                break
            await asyncio.sleep(0.02)
        assert writer.stats.flush_count >= 1
        assert sum(len(rows) for _, rows, _ in client.inserts) >= 1
        await writer.stop()
        writer.close()

    asyncio.run(_run())
