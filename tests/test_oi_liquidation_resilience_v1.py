"""Offline resilience tests for OI/liquidation writer, spool, health, supervision.

No production ClickHouse writes. Uses mocks and temp directories only.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from orderbook_analyse.oi_liquidation_collector.health_logic import liquidation_stream_healthy
from orderbook_analyse.oi_liquidation_collector.health_snapshot import (
    HealthSnapshot,
    evaluate_health,
    write_health_atomic,
)
from orderbook_analyse.oi_liquidation_collector.spool import (
    DurableSpool,
    SpoolCorruptError,
    SpoolFullError,
)
from orderbook_analyse.oi_liquidation_collector.writer import (
    AllowlistedWriter,
    InsertError,
    is_session_locked_error,
)


class FakeCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.closed = False
        self.fail_mode: str | None = None
        self.fail_times = 0

    def insert(self, table: str, data: list[tuple[Any, ...]], column_names: list[str]) -> None:
        if self.fail_mode == "session_locked" and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(
                "Code: 373. DB::Exception: Session abc is locked by a concurrent client. (SESSION_IS_LOCKED)"
            )
        if self.fail_mode == "connection" and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Unexpected Http Driver Exception connection reset by peer")
        if self.fail_mode == "always_session_locked":
            raise RuntimeError("Code: 373. SESSION_IS_LOCKED")
        self.inserts.append((table, list(data)))

    def command(self, sql: str) -> Any:
        if self.fail_mode == "ping_fail":
            raise RuntimeError("connection refused")
        return 1

    def query(self, sql: str) -> Any:
        return None

    def close(self) -> None:
        self.closed = True


def _factory(box: dict[str, Any]):
    def factory() -> FakeCH:
        c = FakeCH()
        c.fail_mode = box.get("fail_mode")
        # Shared remaining-fail budget across reconnects (not reset per client).
        c.fail_times = 0

        def insert(table: str, data: list[tuple[Any, ...]], column_names: list[str]) -> None:
            remaining = int(box.get("fail_times") or 0)
            mode = box.get("fail_mode")
            if mode == "always_session_locked":
                raise RuntimeError("Code: 373. SESSION_IS_LOCKED")
            if remaining > 0:
                box["fail_times"] = remaining - 1
                if mode == "session_locked":
                    raise RuntimeError(
                        "Code: 373. DB::Exception: Session abc is locked by a concurrent client. (SESSION_IS_LOCKED)"
                    )
                if mode == "connection":
                    raise RuntimeError("Unexpected Http Driver Exception connection reset by peer")
            c.inserts.append((table, list(data)))

        c.insert = insert  # type: ignore[method-assign]
        box.setdefault("clients", []).append(c)
        box["current"] = c
        return c

    return factory


def _liq_rec(i: int = 1) -> dict[str, Any]:
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


def test_session_locked_detection() -> None:
    assert is_session_locked_error(RuntimeError("Code: 373 SESSION_IS_LOCKED"))


def test_session_locked_recreates_client(tmp_path: Path) -> None:
    async def _run() -> None:
        box: dict[str, Any] = {"fail_mode": "session_locked", "fail_times": 1}
        spool = DurableSpool(tmp_path / "spool", max_bytes=10_000_000, min_free_bytes=1)
        w = AllowlistedWriter(
            client_factory=_factory(box),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=4,
            spool=spool,
            retry_base_sec=0.01,
            retry_cap_sec=0.05,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq_rec(1)])
        await asyncio.sleep(0.4)
        assert w.rows_inserted >= 1
        assert len(box["clients"]) >= 2
        assert spool.last_acked_seq >= 1
        await w.stop()
        spool.close()

    asyncio.run(_run())


def test_parallel_flush_serialized(tmp_path: Path) -> None:
    async def _run() -> None:
        active = {"n": 0, "max": 0}

        class Guarded(FakeCH):
            def insert(self, table, data, column_names):  # noqa: ANN001
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                time.sleep(0.02)
                active["n"] -= 1
                return super().insert(table, data, column_names)

        def factory():
            return Guarded()

        w = AllowlistedWriter(
            client_factory=factory,
            batch_size=1,
            flush_interval_sec=0.02,
            max_retries=2,
            retry_base_sec=0.01,
            retry_cap_sec=0.02,
        )
        await w.start()
        for i in range(8):
            await w.enqueue("all_liquidations", [_liq_rec(i)])
        await asyncio.sleep(0.6)
        assert active["max"] == 1
        await w.stop()

    asyncio.run(_run())


def test_connection_error_rebuild(tmp_path: Path) -> None:
    async def _run() -> None:
        box: dict[str, Any] = {"fail_mode": "connection", "fail_times": 2}
        w = AllowlistedWriter(
            client_factory=_factory(box),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=5,
            retry_base_sec=0.01,
            retry_cap_sec=0.05,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq_rec(9)])
        await asyncio.sleep(0.5)
        assert w.rows_inserted >= 1
        assert w.clickhouse_reconnect_count >= 2
        await w.stop()

    asyncio.run(_run())


def test_writer_death_sets_not_alive() -> None:
    async def _run() -> None:
        box: dict[str, Any] = {"fail_mode": "always_session_locked"}
        w = AllowlistedWriter(
            client_factory=_factory(box),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=2,
            retry_base_sec=0.01,
            retry_cap_sec=0.02,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq_rec(3)])
        await asyncio.sleep(0.4)
        assert not w.is_alive()
        assert w.fatal is not None
        with pytest.raises(InsertError):
            await w.stop()

    asyncio.run(_run())


def test_spool_replay_after_restart(tmp_path: Path) -> None:
    async def _run() -> None:
        root = tmp_path / "spool"
        spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1, segment_max_bytes=1000)
        r1 = spool.append("all_liquidations", _liq_rec(1))
        r2 = spool.append("all_liquidations", _liq_rec(2))
        spool.ack_through(r1.seq)
        spool.close()

        spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
        unacked = list(spool2.iter_unacked())
        assert [u.seq for u in unacked] == [r2.seq]
        box: dict[str, Any] = {}
        w = AllowlistedWriter(
            client_factory=_factory(box),
            batch_size=10,
            flush_interval_sec=0.05,
            spool=spool2,
        )
        await w.start()
        await w.enqueue_spool_records(unacked)
        await asyncio.sleep(0.3)
        assert w.rows_inserted >= 1
        assert spool2.last_acked_seq >= r2.seq
        await w.stop()
        spool2.close()

    asyncio.run(_run())


def test_retry_same_insert_idempotent_keys(tmp_path: Path) -> None:
    async def _run() -> None:
        box: dict[str, Any] = {"fail_mode": "session_locked", "fail_times": 2}
        spool = DurableSpool(tmp_path / "s", max_bytes=10_000_000, min_free_bytes=1)
        w = AllowlistedWriter(
            client_factory=_factory(box),
            batch_size=1,
            flush_interval_sec=0.05,
            max_retries=5,
            spool=spool,
            retry_base_sec=0.01,
            retry_cap_sec=0.05,
        )
        await w.start()
        await w.enqueue("all_liquidations", [_liq_rec(42)])
        await asyncio.sleep(0.5)
        assert w.rows_inserted == 1
        client = box["current"]
        assert len(client.inserts) == 1
        await w.stop()
        spool.close()

    asyncio.run(_run())


def test_corrupt_trailing_spool_record(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    spool.append("all_liquidations", _liq_rec(1))
    seg = next((root / "segments").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as fh:
        fh.write('{"seq":99,"table":"all_liquidations"')
    spool.close()
    with pytest.raises(SpoolCorruptError):
        list(DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1).iter_all())


def test_spool_full_disk_limit(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "s", max_bytes=200, min_free_bytes=1, segment_max_bytes=50)
    with pytest.raises(SpoolFullError):
        for i in range(50):
            spool.append("all_liquidations", _liq_rec(i))
    spool.close()


def test_queue_full_increments_drops(tmp_path: Path) -> None:
    async def _run() -> None:
        w = AllowlistedWriter(
            client_factory=lambda: FakeCH(),
            batch_size=1000,
            flush_interval_sec=60,
            queue_maxsize=1,
            max_retries=1,
        )
        # Do not start worker — queue never drains → fills immediately.
        await w.enqueue("all_liquidations", [_liq_rec(1)])
        dropped = await w.enqueue("all_liquidations", [_liq_rec(2)])
        assert dropped >= 1
        assert w.queue_drops >= 1

    asyncio.run(_run())


def test_no_liquidation_not_alarm() -> None:
    assert liquidation_stream_healthy(
        ws_connected=True,
        subscription_confirmed=True,
        ping_ok=True,
        liq_topic_subscribed=True,
        last_liquidation_at=None,
    )
    snap = HealthSnapshot(
        process_alive=True,
        websocket_alive=True,
        writer_alive=True,
        clickhouse_reachable=True,
        last_oi_received_ts=time.time(),
        last_successful_insert_ts=time.time(),
        last_liquidation_received_ts=None,
    )
    out = evaluate_health(snap, persistence_lag_fail_sec=120, oi_stale_fail_sec=60)
    assert out.health_status == "GREEN"
    assert "liquidation" not in ",".join(out.health_reasons)


def test_stale_oi_yellow_or_reasons() -> None:
    snap = HealthSnapshot(
        process_alive=True,
        websocket_alive=True,
        writer_alive=True,
        clickhouse_reachable=True,
        last_oi_received_ts=time.time() - 120,
        last_successful_insert_ts=time.time() - 5,
    )
    out = evaluate_health(snap, persistence_lag_fail_sec=120, oi_stale_fail_sec=60)
    assert "oi_receive_stale" in out.health_reasons


def test_writer_dead_health_red() -> None:
    snap = HealthSnapshot(
        process_alive=True,
        websocket_alive=True,
        writer_alive=False,
        clickhouse_reachable=False,
        last_oi_received_ts=time.time(),
        last_successful_insert_ts=time.time() - 1000,
    )
    out = evaluate_health(snap, persistence_lag_fail_sec=60, oi_stale_fail_sec=60)
    assert out.health_status == "RED"
    assert "writer_dead" in out.health_reasons


def test_health_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    snap = HealthSnapshot(process_alive=True, writer_alive=True, websocket_alive=True)
    snap = evaluate_health(snap, persistence_lag_fail_sec=60, oi_stale_fail_sec=60)
    write_health_atomic(path, snap)
    data = json.loads(path.read_text())
    assert data["process_alive"] is True


def test_crash_between_insert_and_ack_replays(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    spool = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    rec = spool.append("all_liquidations", _liq_rec(7))
    spool.close()
    spool2 = DurableSpool(root, max_bytes=10_000_000, min_free_bytes=1)
    unacked = list(spool2.iter_unacked())
    assert len(unacked) == 1 and unacked[0].seq == rec.seq
    spool2.close()


def test_default_client_factory_disables_session() -> None:
    import clickhouse_connect

    from orderbook_analyse.oi_liquidation_collector.collector import default_client_factory
    from orderbook_analyse.oi_liquidation_collector import settings as settings_mod
    from orderbook_analyse.oi_liquidation_collector.settings import OICollectorSettings

    s = OICollectorSettings(
        bybit_ws_url="wss://example",
        bybit_rest_url="https://example",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="orderbook_analysis",
        clickhouse_user="u",
        clickhouse_password="p",
        universe_path=settings_mod.DEFAULT_UNIVERSE,
        lock_path=settings_mod.DEFAULT_LOCK,
        pid_path=settings_mod.DEFAULT_PID,
    )
    factory = default_client_factory(s)
    called: dict[str, Any] = {}

    def fake_get_client(**kwargs):  # noqa: ANN003
        called.update(kwargs)

        class C:
            def close(self) -> None:
                pass

        return C()

    orig = clickhouse_connect.get_client
    clickhouse_connect.get_client = fake_get_client  # type: ignore[assignment]
    try:
        factory()
    finally:
        clickhouse_connect.get_client = orig  # type: ignore[assignment]
    assert called.get("autogenerate_session_id") is False
