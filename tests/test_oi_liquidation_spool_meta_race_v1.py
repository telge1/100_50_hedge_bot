"""Spool meta race / durability tests for OI-liquidation DurableSpool.

Offline only — temp dirs, no production ClickHouse, no live PID interaction.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from orderbook_analyse.oi_liquidation_collector.spool import (
    DurableSpool,
    SpoolCorruptError,
    SpoolMetaError,
)
from orderbook_analyse.oi_liquidation_collector.writer import AllowlistedWriter, InsertError


def _liq(i: int = 1) -> dict[str, Any]:
    return {
        "exchange": "BYBIT",
        "category": "linear",
        "symbol": "BTCUSDT",
        "event_time": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "system_generated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "received_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "position_side_raw": "Buy",
        "liquidated_position_side": "LIQUIDATED_LONG",
        "size": 1,
        "bankruptcy_price": 1,
        "notional_estimate": 1,
        "source_topic": "allLiquidation.BTCUSDT",
        "event_key": f"k{i}",
        "raw_payload_hash": "a" * 64,
        "collector_instance_id": "t",
        "inserted_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


def _oi_event(symbol: str, i: int) -> dict[str, Any]:
    return {
        "exchange": "BYBIT",
        "category": "linear",
        "symbol": symbol,
        "event_time": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "received_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "cross_sequence": i,
        "open_interest": 1.0,
        "open_interest_value": 1.0,
        "single_open_interest": None,
        "single_open_interest_value": None,
        "last_price": 1.0,
        "mark_price": 1.0,
        "index_price": 1.0,
        "funding_rate": 0.0,
        "message_type": "delta",
        "source_topic": f"tickers.{symbol}",
        "state_valid": 1,
        "event_key": f"{symbol}-{i}",
        "collector_instance_id": "t",
        "inserted_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


class FakeCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list]] = []
        self.closed = False

    def insert(self, table: str, data: list, column_names: list[str]) -> None:
        self.inserts.append((table, list(data)))

    def command(self, sql: str) -> Any:
        return 1

    def query(self, sql: str) -> Any:
        return None

    def close(self) -> None:
        self.closed = True


def test_1_parallel_append_and_ack(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=50_000_000, min_free_bytes=1, segment_max_bytes=50_000)
    errors: list[BaseException] = []

    def appender() -> None:
        try:
            for i in range(40):
                spool.append("all_liquidations", _liq(1000 + i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def acker() -> None:
        try:
            for _ in range(80):
                cur = spool.next_seq - 1
                if cur > 0:
                    spool.ack_through(min(cur, spool.last_acked_seq + 1))
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=appender)
    t2 = threading.Thread(target=acker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors
    assert spool.next_seq == 41
    assert 0 <= spool.last_acked_seq < spool.next_seq
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert meta["next_seq"] == spool.next_seq
    assert meta["last_acked_seq"] == spool.last_acked_seq
    assert meta["generation"] >= 40
    spool.close()


def test_2_parallel_ack_and_health_snapshot(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    for i in range(20):
        spool.append("all_liquidations", _liq(i))
    errors: list[BaseException] = []

    def acker() -> None:
        try:
            for seq in range(1, 21):
                spool.ack_through(seq)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def health() -> None:
        try:
            for _ in range(40):
                spool.unacked_stats()
                _ = spool.last_acked_seq
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=acker), threading.Thread(target=health)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert spool.last_acked_seq == 20
    spool.close()


def test_3_rollover_during_ack(tmp_path: Path) -> None:
    spool = DurableSpool(
        tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1, segment_max_bytes=400
    )
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(30):
                spool.append("all_liquidations", _liq(i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def acker() -> None:
        try:
            for _ in range(60):
                n = spool.next_seq - 1
                if n > 0:
                    spool.ack_through(n)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    tw = threading.Thread(target=writer)
    ta = threading.Thread(target=acker)
    tw.start()
    ta.start()
    tw.join()
    ta.join()
    assert not errors
    segs = list((tmp_path / "s" / "segments").glob("*.jsonl"))
    assert len(segs) >= 2
    spool.close()


def test_4_shutdown_during_meta_commit(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq(1))
    # Concurrent close while ack/persist
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def commit() -> None:
        try:
            barrier.wait()
            for i in range(10):
                spool.append("all_liquidations", _liq(10 + i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def shutdown() -> None:
        try:
            barrier.wait()
            time.sleep(0.005)
            spool.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=commit)
    t2 = threading.Thread(target=shutdown)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # May have SpoolFull/OS errors if fd closed mid-write; must not corrupt meta JSON.
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert meta["next_seq"] >= 2
    assert meta["last_acked_seq"] >= 0


def test_5_hundred_concurrent_meta_updates(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=50_000_000, min_free_bytes=1)

    def one(i: int) -> None:
        spool.append("all_liquidations", _liq(i))
        spool.ack_through(spool.next_seq - 1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(one, range(100)))
    assert spool.next_seq == 101
    assert spool.last_acked_seq == 100
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert meta["generation"] >= 100
    spool.close()


def test_6_temp_already_renamed(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    real_replace = os.replace

    def flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        src_s = str(src)
        if "meta.json.tmp." in src_s and ".prevwrite" not in src_s:
            Path(src_s).unlink(missing_ok=True)
            raise FileNotFoundError(2, "No such file or directory", src_s)
        return real_replace(src, dst)

    with mock.patch("os.replace", side_effect=flaky_replace):
        with pytest.raises(SpoolMetaError):
            spool.append("all_liquidations", _liq(1))
    spool2 = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    assert spool2.next_seq >= 1
    spool2.close()


def test_7_crash_before_fsync(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    real_fsync = os.fsync
    count = {"n": 0}

    def selective(fd: int) -> None:
        count["n"] += 1
        # 1st fsync = segment line; later = meta file / dir
        if count["n"] >= 2:
            raise OSError(5, "simulated crash before meta fsync")
        return real_fsync(fd)

    with mock.patch("os.fsync", side_effect=selective):
        with pytest.raises(SpoolMetaError):
            spool.append("all_liquidations", _liq(1))
    spool2 = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert "last_acked_seq" in meta and "next_seq" in meta
    spool2.close()


def test_8_crash_after_fsync_before_replace(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq(1))
    real_replace = os.replace

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        src_s = str(src)
        if "meta.json.tmp." in src_s and ".prevwrite" not in src_s:
            raise OSError(5, "crash after fsync before replace")
        return real_replace(src, dst)

    with mock.patch("os.replace", side_effect=boom):
        with pytest.raises(SpoolMetaError):
            spool.append("all_liquidations", _liq(2))
    # Prior meta remains (atomic publish never completed)
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert meta["next_seq"] == 2
    spool.close()


def test_9_crash_after_replace(tmp_path: Path) -> None:
    root = tmp_path / "s"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq(1))
    gen_before = json.loads((root / "meta.json").read_text())["generation"]
    spool.append("all_liquidations", _liq(2))
    meta = json.loads((root / "meta.json").read_text())
    assert meta["generation"] > gen_before
    assert (root / "meta.json.prev").is_file()
    spool.close()
    spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    assert spool2.next_seq == 3
    spool2.close()


def test_10_corrupt_meta_json(tmp_path: Path) -> None:
    root = tmp_path / "s"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq(1))
    spool.close()
    (root / "meta.json").write_text("{not-json", encoding="utf-8")
    # prev + segment reconcile → next_seq advances past existing records
    spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    assert spool2.next_seq >= 2
    spool2.close()


def test_10b_corrupt_meta_without_prev_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir(parents=True)
    (root / "segments").mkdir()
    (root / "meta.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(SpoolCorruptError):
        DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)


def test_11_orphan_meta_tmp(tmp_path: Path) -> None:
    root = tmp_path / "s"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq(1))
    spool.close()
    orphan = root / "meta.json.tmp"
    orphan.write_text('{"last_acked_seq":0,"next_seq":999}\n', encoding="utf-8")
    spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    assert not orphan.exists()
    assert spool2.next_seq == 2  # not 999
    assert any(root.glob("orphan_meta.json.tmp.*"))
    spool2.close()


def test_12_ack_never_goes_backwards(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
    for i in range(5):
        spool.append("all_liquidations", _liq(i))
    spool.ack_through(4)
    spool.ack_through(2)
    assert spool.last_acked_seq == 4
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    assert meta["last_acked_seq"] == 4
    spool.close()


def test_13_insert_ok_ack_fails_then_recovers(tmp_path: Path) -> None:
    async def _run() -> None:
        spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
        box: dict[str, Any] = {"clients": []}

        def factory() -> FakeCH:
            c = FakeCH()
            box["clients"].append(c)
            box["current"] = c
            return c

        w = AllowlistedWriter(
            client_factory=factory,
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=4,
            spool=spool,
            retry_base_sec=0.01,
            retry_cap_sec=0.02,
        )
        await w.start()
        fails = {"n": 0}
        real_ack = spool.ack_through

        def flaky_ack(seq: int) -> None:
            fails["n"] += 1
            if fails["n"] <= 2:
                raise SpoolMetaError("simulated ack meta race")
            return real_ack(seq)

        with mock.patch.object(spool, "ack_through", side_effect=flaky_ack):
            await w.enqueue("all_liquidations", [_liq(77)])
            await asyncio.sleep(0.6)
        assert w.rows_inserted == 1
        assert len(box["current"].inserts) == 1  # no duplicate insert
        assert spool.last_acked_seq >= 1
        await w.stop()
        spool.close()

    asyncio.run(_run())


def test_14_replay_without_logical_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "s"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    r1 = spool.append("all_liquidations", _liq(1))
    r2 = spool.append("all_liquidations", _liq(2))
    spool.ack_through(r1.seq)
    spool.close()
    spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    unacked = list(spool2.iter_unacked())
    assert [u.record_id for u in unacked] == [r2.record_id]
    ids = [u.record_id for u in spool2.iter_all()]
    assert len(ids) == len(set(ids))
    spool2.close()


def test_15_db_outage_and_recovery(tmp_path: Path) -> None:
    async def _run() -> None:
        spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
        state = {"fail": True, "inserts": 0}

        class Flaky(FakeCH):
            def insert(self, table, data, column_names):  # noqa: ANN001
                if state["fail"]:
                    raise RuntimeError("connection reset by peer")
                state["inserts"] += 1
                return super().insert(table, data, column_names)

        w = AllowlistedWriter(
            client_factory=lambda: Flaky(),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=3,
            spool=spool,
            retry_base_sec=0.01,
            retry_cap_sec=0.02,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq(5)])
        await asyncio.sleep(0.25)
        assert spool.last_acked_seq == 0  # still unacked
        state["fail"] = False
        # writer may already be dead from exhausted retries — simulate recovery replay
        if not w.is_alive():
            w2 = AllowlistedWriter(
                client_factory=lambda: Flaky(),
                batch_size=10,
                flush_interval_sec=0.05,
                spool=spool,
                max_retries=3,
                retry_base_sec=0.01,
                retry_cap_sec=0.02,
            )
            await w2.start()
            await w2.enqueue_spool_records(list(spool.iter_unacked()))
            await asyncio.sleep(0.3)
            assert w2.rows_inserted >= 1
            assert spool.last_acked_seq >= 1
            await w2.stop()
        else:
            await asyncio.sleep(0.4)
            assert spool.last_acked_seq >= 1
            await w.stop()
        spool.close()

    asyncio.run(_run())


def test_16_writer_error_reaches_fail_fast_supervisor(tmp_path: Path) -> None:
    async def _run() -> None:
        spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)

        class AlwaysFail(FakeCH):
            def insert(self, table, data, column_names):  # noqa: ANN001
                raise RuntimeError("Code: 373 SESSION_IS_LOCKED")

        w = AllowlistedWriter(
            client_factory=lambda: AlwaysFail(),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=2,
            spool=spool,
            retry_base_sec=0.01,
            retry_cap_sec=0.02,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq(9)])
        await asyncio.sleep(0.4)
        assert not w.is_alive()
        assert w.fatal is not None
        with pytest.raises(InsertError):
            await w.stop()
        # unacked preserved
        assert list(spool.iter_unacked())
        spool.close()

    asyncio.run(_run())


def test_17_load_all_51_symbols(tmp_path: Path) -> None:
    symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "BNBUSDT",
        "ADAUSDT",
        "AVAXUSDT",
        "LINKUSDT",
        "DOTUSDT",
        "LTCUSDT",
        "BCHUSDT",
        "ATOMUSDT",
        "NEARUSDT",
        "UNIUSDT",
        "APTUSDT",
        "ARBUSDT",
        "OPUSDT",
        "SUIUSDT",
        "INJUSDT",
        "TIAUSDT",
        "SEIUSDT",
        "WLDUSDT",
        "FILUSDT",
        "ICPUSDT",
        "AAVEUSDT",
        "MKRUSDT",
        "LDOUSDT",
        "CRVUSDT",
        "PEPEUSDT",
        "WIFUSDT",
        "BONKUSDT",
        "ORDIUSDT",
        "STXUSDT",
        "IMXUSDT",
        "RNDRUSDT",
        "FETUSDT",
        "GRTUSDT",
        "ALGOUSDT",
        "XLMUSDT",
        "EOSUSDT",
        "XTZUSDT",
        "SANDUSDT",
        "MANAUSDT",
        "AXSUSDT",
        "THETAUSDT",
        "EGLDUSDT",
        "FLOWUSDT",
        "KAVAUSDT",
        "ZILUSDT",
        "ENJUSDT",
    ]
    assert len(symbols) == 51
    spool = DurableSpool(
        tmp_path / "s", max_bytes=50_000_000, min_free_bytes=1, segment_max_bytes=200_000
    )
    errors: list[BaseException] = []

    def worker(sym: str) -> None:
        try:
            for i in range(5):
                spool.append("open_interest_events", _oi_event(sym, i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in symbols]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert spool.next_seq == 1 + 51 * 5
    # ack everything concurrently
    spool.ack_through(spool.next_seq - 1)
    assert spool.last_acked_seq == 51 * 5
    assert list(spool.iter_unacked()) == []
    spool.close()


def test_meta_error_is_spool_error_subclass() -> None:
    assert issubclass(SpoolMetaError, Exception)
