"""Unit tests for public-trade queue overflow spool + fail-closed writer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from signal_generator.bybit.live.public_trade_spool import (
    PublicTradeSpool,
    PublicTradeSpoolCorruptError,
)
from signal_generator.bybit.live.trade_buffer import PublicTradeInsertBuffer
from signal_generator.bybit.live.ws_public_trade import WsPublicTrade


def _trade(i: int, symbol: str = "BTCUSDT") -> WsPublicTrade:
    price = Decimal("100")
    size = Decimal("0.01")
    return WsPublicTrade(
        symbol=symbol,
        trade_id=f"id-{i}",
        trade_ts=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
        side="Buy",
        price=price,
        size=size,
        notional=price * size,
        tick_direction="PlusTick",
        is_rpi_trade=0,
    )


class _FakeRepo:
    def __init__(self, *, fail_times: int = 0, delay_s: float = 0.0) -> None:
        self.fail_times = fail_times
        self.delay_s = delay_s
        self.calls = 0
        self.inserted: list = []
        self.lock_fail = 0

    def insert_trades(self, trades, *, ingest_timestamp, source):  # noqa: ANN001
        import time

        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.calls <= self.fail_times:
            raise RuntimeError("clickhouse_down")
        self.inserted.extend(list(trades))
        return len(trades)


def test_spool_roundtrip_and_ack(tmp_path: Path):
    spool = PublicTradeSpool(tmp_path / "spool", min_free_bytes=1)
    seq = spool.append_trades([_trade(1), _trade(2)])
    batches = list(spool.iter_unacked())
    assert len(batches) == 1
    assert batches[0].seq == seq
    assert [t.trade_id for t in batches[0].trades] == ["id-1", "id-2"]
    spool.ack(seq)
    assert list(spool.iter_unacked()) == []
    spool.close()


def test_spool_corrupt_tail_fail_closed(tmp_path: Path):
    spool = PublicTradeSpool(tmp_path / "spool", min_free_bytes=1)
    spool.append_trades([_trade(1)])
    # Corrupt active segment with truncated line.
    assert spool._active_path is not None
    with open(spool._active_path, "a", encoding="utf-8") as fp:
        fp.write('{"seq":99,"trades":[')
    with pytest.raises(PublicTradeSpoolCorruptError):
        list(spool.iter_unacked())
    spool.close()


def test_enqueue_spills_to_spool_not_drop(tmp_path: Path):
    repo = _FakeRepo()
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=2,
        batch_size=10,
        flush_interval_s=0.05,
        spool_dir=tmp_path / "spool",
        overflow_batch_size=2,
    )
    assert buf.enqueue(_trade(1))
    assert buf.enqueue(_trade(2))
    # Queue full → overflow then spool
    assert buf.enqueue(_trade(3))
    assert buf.enqueue(_trade(4))
    assert buf.metrics.dropped_events == 0
    assert buf.metrics.spool_batches_written >= 1
    assert buf.metrics.writer_fatal is False


@pytest.mark.asyncio
async def test_writer_retry_then_success(tmp_path: Path):
    repo = _FakeRepo(fail_times=2)
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=100,
        batch_size=2,
        flush_interval_s=0.05,
        spool_dir=tmp_path / "spool",
    )
    buf.start()
    buf.enqueue(_trade(1))
    buf.enqueue(_trade(2))
    await asyncio.sleep(0.6)
    await buf.stop()
    assert repo.calls >= 3
    assert len(repo.inserted) == 2
    assert buf.metrics.writer_fatal is False
    assert buf.metrics.dropped_events == 0


@pytest.mark.asyncio
async def test_spool_replay_after_crash_between_insert_and_ack(tmp_path: Path):
    spool_dir = tmp_path / "spool"
    spool = PublicTradeSpool(spool_dir, min_free_bytes=1)
    seq = spool.append_trades([_trade(10), _trade(11)])
    spool.close()
    # New buffer resumes unacked
    repo = _FakeRepo()
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=10,
        batch_size=10,
        flush_interval_s=0.05,
        spool_dir=spool_dir,
    )
    buf.start()
    await asyncio.sleep(0.4)
    await buf.stop()
    assert [t.trade_id for t in repo.inserted] == ["id-10", "id-11"]
    assert buf.metrics.spool_batches_replayed >= 1
    # acked
    spool2 = PublicTradeSpool(spool_dir, min_free_bytes=1)
    assert list(spool2.iter_unacked()) == []
    spool2.close()
    assert seq >= 1


@pytest.mark.asyncio
async def test_duplicate_trade_id_insert_idempotent_replacing(tmp_path: Path):
    """Direct insert twice is ok — ReplacingMergeTree logical uniq on (symbol, trade_id)."""
    repo = _FakeRepo()
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=10,
        batch_size=1,
        flush_interval_s=0.05,
        spool_dir=tmp_path / "spool",
    )
    buf.start()
    t = _trade(42)
    buf.enqueue(t)
    await asyncio.sleep(0.2)
    buf.enqueue(t)
    await asyncio.sleep(0.2)
    await buf.stop()
    assert len(repo.inserted) == 2
    assert all(x.trade_id == "id-42" for x in repo.inserted)


@pytest.mark.asyncio
async def test_burst_and_peak_multipliers(tmp_path: Path):
    """Synthetic 51-symbol burst + 3x peak without drops."""
    repo = _FakeRepo(delay_s=0.001)
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=5_000,
        batch_size=500,
        flush_interval_s=0.05,
        spool_dir=tmp_path / "spool",
        overflow_batch_size=100,
    )
    buf.start()
    symbols = [f"S{i}USDT" for i in range(51)]
    n = 0
    # ~measured live peak ~70 trades/s across 51; 3x = 210/s for 2s → 420
    for burst in range(3):
        for i in range(500):
            sym = symbols[i % 51]
            assert buf.enqueue(_trade(n, symbol=sym))
            n += 1
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.5)
    await buf.stop()
    assert buf.metrics.dropped_events == 0
    assert buf.metrics.writer_fatal is False
    assert buf.metrics.rows_inserted == n


@pytest.mark.asyncio
async def test_writer_fatal_on_exhausted_retries_without_losing_to_void(tmp_path: Path):
    repo = _FakeRepo(fail_times=100)
    buf = PublicTradeInsertBuffer(
        repo,  # type: ignore[arg-type]
        queue_maxsize=10,
        batch_size=1,
        flush_interval_s=0.01,
        spool_dir=tmp_path / "spool",
    )
    # Shrink attempts via monkeypatch constant would be ideal; use fail_times high
    # and await stop which drains. Instead call flush_with_retry directly.
    with pytest.raises(RuntimeError, match="insert_exhausted"):
        await buf._flush_with_retry([_trade(1)])
    assert buf.metrics.writer_fatal is True
    # spilled to spool
    spool = PublicTradeSpool(tmp_path / "spool", min_free_bytes=1)
    assert len(list(spool.iter_unacked())) >= 1
    spool.close()
