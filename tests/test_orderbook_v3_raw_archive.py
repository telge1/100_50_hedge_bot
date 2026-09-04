"""Tests for parallel raw OB200 live archival."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import orjson
import pytest

from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_v2.book import BookState, apply_snapshot
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock
from orderbook_analyse.orderbook_v2_live.collector import OrderbookV3LiveCollector
from orderbook_analyse.orderbook_v2_live.locks import SingleInstanceLock
from orderbook_analyse.orderbook_v2_live.raw_archive.config import (
    RawArchiveSettings,
    load_raw_archive_settings,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.disk import check_disk
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    book_state_to_data,
    is_replayable_line,
    line_to_replay_payload,
    serialize_lifecycle,
    serialize_market_payload,
    serialize_rotation_checkpoint,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.manager import RawArchiveManager
from orderbook_analyse.orderbook_v2_live.raw_archive.replay import (
    iter_segment_lines,
    load_manifest,
    replay_segment,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.segment import SegmentWriter
from orderbook_analyse.orderbook_v2_live.settings import (
    LiveCollectorConfigError,
    LiveCollectorSettings,
    load_raw_archive_only_settings,
)
from orderbook_analyse.orderbook_v2_live.writer import FeatureWriter, NullFeatureWriter
from orderbook_analyse.orderbook_replay import OrderBookReplayer

T0 = 1_750_000_000_000


def _event(ts: int, type_: str, bids, asks, u: int, seq: int, symbol: str = "BTCUSDT") -> dict:
    return {
        "topic": f"orderbook.200.{symbol}",
        "type": type_,
        "ts": ts,
        "cts": None,
        "data": {"s": symbol, "b": bids, "a": asks, "u": u, "seq": seq},
    }


def _settings(tmp: Path, **kwargs) -> RawArchiveSettings:
    base = RawArchiveSettings(
        enabled=True,
        archive_root=tmp,
        symbols=frozenset({"BTCUSDT"}),
        queue_size=8,
        rotation="hour",
        compression="zstd",
    )
    return RawArchiveSettings(**{**base.__dict__, **kwargs})


@pytest.fixture
def tmp_archive(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def test_config_default_disabled() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = load_raw_archive_settings()
    assert cfg.enabled is False
    assert cfg.symbols == frozenset()


def test_snapshot_serialization_roundtrip() -> None:
    payload = _event(T0, "snapshot", [["90000.1", "1.5"]], [["90001.2", "2"]], 1, 100)
    received = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    line = serialize_market_payload(payload, received_at=received)
    obj = orjson.loads(line)
    assert obj["type"] == "snapshot"
    assert obj["data"]["b"][0] == ["90000.1", "1.5"]
    assert obj["local_receive_ts"].endswith("Z")
    msg = parse_ob200_obj(line_to_replay_payload(obj))
    assert msg.bids[0][0] == Decimal("90000.1")


def test_delta_delete_semantics_preserved() -> None:
    payload = _event(T0, "delta", [["90000", "0"]], [], 2, 101)
    line = serialize_market_payload(payload, received_at=datetime.now(timezone.utc))
    obj = orjson.loads(line)
    assert obj["data"]["b"] == [["90000", "0"]]


def test_rotation_checkpoint_distinct_from_native_snapshot() -> None:
    book = apply_snapshot({"b": [["1", "1"]], "a": [["2", "1"]], "u": 5, "seq": 50})
    line = serialize_rotation_checkpoint(
        book,
        "BTCUSDT",
        topic="orderbook.200.BTCUSDT",
        ts_ms=T0,
        received_at=datetime.fromtimestamp(T0 / 1000, tz=timezone.utc),
    )
    obj = orjson.loads(line)
    assert obj["type"] == "rotation_checkpoint"
    assert obj["source"] == "local_book_state"


def test_lifecycle_marker_not_replayable() -> None:
    line = serialize_lifecycle("CONNECT", symbol="BTCUSDT")
    obj = orjson.loads(line)
    assert obj["archive_event"] == "CONNECT"
    assert is_replayable_line(obj) is False


@pytest.mark.asyncio
async def test_bounded_queue_overflow_marks_loss(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive, queue_size=2))
    manager.start()
    received = datetime.now(timezone.utc)
    for i in range(5):
        manager.try_enqueue_market(
            "BTCUSDT",
            _event(T0 + i, "delta", [], [], i + 1, 100 + i),
            received,
        )
    await asyncio.sleep(0.05)
    assert manager.metrics.events_dropped_overflow > 0
    assert manager.metrics.overflow_count > 0
    await manager.stop()


@pytest.mark.asyncio
async def test_streaming_compression_and_atomic_close(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive))
    manager.start()
    received = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    manager.try_enqueue_market(
        "BTCUSDT",
        _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], 1, 1),
        received,
    )
    await asyncio.sleep(0.05)
    await manager.stop()
    segments = list(tmp_archive.glob("**/*.zst"))
    assert segments
    assert not list(tmp_archive.glob("**/*.tmp"))
    manifest = load_manifest(segments[0])
    assert manifest["event_count"] >= 1
    assert manifest["sha256"]


@pytest.mark.asyncio
async def test_rotation_produces_two_segments(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive))
    manager.start()
    received = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    snap = _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], 1, 1)
    manager.try_enqueue_market("BTCUSDT", snap, received)
    await asyncio.sleep(0.05)
    book = apply_snapshot(snap["data"])
    await manager.rotate_with_checkpoint(
        "BTCUSDT",
        book,
        ts_ms=T0 + 1000,
        received_at=datetime.fromtimestamp((T0 + 1000) / 1000, tz=timezone.utc),
        topic="orderbook.200.BTCUSDT",
    )
    manager.try_enqueue_market(
        "BTCUSDT",
        _event(T0 + 1100, "delta", [["1", "2"]], [], 2, 2),
        received,
    )
    await asyncio.sleep(0.05)
    await manager.stop()
    segments = sorted(tmp_archive.glob("**/*.zst"))
    assert len(segments) >= 2


def test_replay_parity_wall_lifecycle(tmp_archive: Path) -> None:
    messages = [
        _event(T0 + 10, "snapshot", [["100", "10"], ["99", "5"]], [["101", "8"]], 1, 100),
        _event(T0 + 100, "delta", [["100", "7"]], [], 2, 101),
        _event(T0 + 200, "delta", [["100", "3"]], [], 3, 102),
        _event(T0 + 300, "delta", [["100", "0"]], [], 4, 103),
        _event(T0 + 400, "delta", [["100", "5"]], [], 5, 104),
        _event(T0 + 500, "delta", [["100", "0"]], [], 6, 105),
        _event(T0 + 600, "delta", [["98", "2"]], [["102", "1"]], 7, 106),
    ]
    direct = OrderBookReplayer()
    events = []
    for msg in messages:
        parsed = parse_ob200_obj(msg)
        events.extend(parsed.to_book_level_events())
    direct_book = direct.replay(events)

    writer = SegmentWriter(
        symbol="BTCUSDT",
        directory=tmp_archive / "BTCUSDT" / "2025" / "08" / "20",
        start_utc=datetime.fromtimestamp(T0 / 1000, tz=timezone.utc),
    )
    writer.open()
    for msg in messages:
        writer.write_line(
            serialize_market_payload(msg, received_at=datetime.now(timezone.utc)),
            kind=msg["type"],
            sequence=msg["data"]["seq"],
            update_id=msg["data"]["u"],
        )
    path, manifest_path = writer.close(
        end_utc=datetime.fromtimestamp((T0 + 700) / 1000, tz=timezone.utc)
    )
    man = json.loads(manifest_path.read_text())
    assert man["completion_status"] == "closed"
    assert man["continuity_status"] == "contiguous_u"
    assert man["replayable"] is True
    assert man["replay_source"] == "native_snapshot"
    replay_book = replay_segment(path, expected_symbol="BTCUSDT")
    assert replay_book.bids == direct_book.bids
    assert replay_book.asks == direct_book.asks


def test_seq_jump_does_not_mark_non_replayable(tmp_archive: Path) -> None:
    """Bybit seq may jump; continuity is data.u."""
    writer = SegmentWriter(
        symbol="BTCUSDT",
        directory=tmp_archive,
        start_utc=datetime.fromtimestamp(T0 / 1000, tz=timezone.utc),
        compression="none",
    )
    writer.open()
    snap = _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], u=10, seq=100)
    d1 = _event(T0 + 1, "delta", [["1", "2"]], [], u=11, seq=500)  # seq jump
    d2 = _event(T0 + 2, "delta", [["1", "3"]], [], u=12, seq=900)
    for msg in (snap, d1, d2):
        writer.write_line(
            serialize_market_payload(msg, received_at=datetime.now(timezone.utc)),
            kind=msg["type"],
            sequence=msg["data"]["seq"],
            update_id=msg["data"]["u"],
        )
    _, mp = writer.close(end_utc=datetime.fromtimestamp((T0 + 3) / 1000, tz=timezone.utc))
    man = json.loads(mp.read_text())
    assert man["replayable"] is True
    assert man["continuity_status"] == "contiguous_u"
    assert man["completion_status"] == "closed"
    assert man["u_gaps"] == []


def test_u_gap_marks_non_replayable(tmp_archive: Path) -> None:
    writer = SegmentWriter(
        symbol="BTCUSDT",
        directory=tmp_archive,
        start_utc=datetime.fromtimestamp(T0 / 1000, tz=timezone.utc),
        compression="none",
    )
    writer.open()
    snap = _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], u=10, seq=1)
    d1 = _event(T0 + 1, "delta", [["1", "2"]], [], u=15, seq=2)  # u gap
    for msg in (snap, d1):
        writer.write_line(
            serialize_market_payload(msg, received_at=datetime.now(timezone.utc)),
            kind=msg["type"],
            sequence=msg["data"]["seq"],
            update_id=msg["data"]["u"],
        )
    _, mp = writer.close(end_utc=datetime.fromtimestamp((T0 + 3) / 1000, tz=timezone.utc))
    man = json.loads(mp.read_text())
    assert man["replayable"] is False
    assert man["continuity_status"] == "u_gap"
    assert man["completion_status"] == "closed"  # still closed after finalize
    assert man["u_gaps"]


@pytest.mark.asyncio
async def test_feature_parity_direct_vs_archived_path(tmp_archive: Path) -> None:
    messages = [
        _event(T0 + 10, "snapshot", [["1.0", "10"]], [["1.1", "10"]], 1, 1),
        _event(T0 + 200, "delta", [["1.0", "11"]], [], 2, 2),
    ]
    clock_direct = LiveSecondClock("BTCUSDT")
    direct_rows = []
    for msg in messages:
        direct_rows.extend(clock_direct.ingest(msg["type"], msg["ts"], msg["data"]))
    direct_rows.extend(clock_direct.close_through(T0 + 3000))

    manager = RawArchiveManager(_settings(tmp_archive))
    manager.start()
    received = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    for msg in messages:
        manager.try_enqueue_market("BTCUSDT", msg, received)
    await asyncio.sleep(0.05)
    await manager.stop()
    seg = next(tmp_archive.glob("**/*.zst"))
    replay_book = replay_segment(seg, expected_symbol="BTCUSDT")

    clock_from_replay = LiveSecondClock("BTCUSDT")
    for obj in iter_segment_lines(seg):
        if not is_replayable_line(obj):
            continue
        payload = line_to_replay_payload(obj)
        msg = parse_ob200_obj(payload)
        data = {
            "s": msg.symbol,
            "b": [[format(p, "f"), format(q, "f")] for p, q in msg.bids],
            "a": [[format(p, "f"), format(q, "f")] for p, q in msg.asks],
            "u": msg.update_id,
            "seq": msg.cross_sequence,
        }
        clock_from_replay.ingest(msg.message_type, msg.raw_ts_ms, data)
    replay_rows = clock_from_replay.close_through(T0 + 3000)
    assert len(direct_rows) == len(replay_rows)
    assert replay_book.mid_price() is not None


def test_disk_protection_pauses_archive(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive, min_free_disk_gb=10_000.0))
    assert manager._check_disk() is False
    assert manager.metrics.paused is True


def test_collector_without_raw_archive_unchanged() -> None:
    settings = LiveCollectorSettings(
        bybit_ws_url="wss://x",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="db",
        clickhouse_user="",
        clickhouse_password="",
        symbols=("ADAUSDT",),
        mode="ada",
        lock_path=Path("/tmp/x.lock"),
        pid_path=Path("/tmp/x.pid"),
        health_path=None,
        ada_only_pilot=False,
    )
    collector = OrderbookV3LiveCollector(settings)
    collector._reset_runtimes()
    assert collector.raw_archive is None
    health = collector.health_payload()
    assert "raw_archive_enabled" not in health


def test_health_metrics_include_raw_fields(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive))
    health = manager.health_dict()
    assert health["raw_archive_enabled"] is True
    assert health["raw_archive_symbols"] == ["BTCUSDT"]


def test_sequence_gap_marks_non_replayable(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive))
    manager.note_sequence_gap("BTCUSDT", details={"reason": "test"})
    assert manager.metrics.gap_count >= 1
    assert manager.metrics.segment_replayable is False


def test_book_state_to_data_decimal_precision() -> None:
    book = BookState(
        bids={Decimal("90000.1"): Decimal("0.001")},
        asks={Decimal("90001.2"): Decimal("10")},
        last_u=1,
        last_seq=1,
        is_valid=True,
    )
    data = book_state_to_data(book, "BTCUSDT")
    assert data["b"][0] == ["90000.1", "0.001"]


def test_deterministic_segment_hash(tmp_archive: Path) -> None:
    directory = tmp_archive / "BTCUSDT" / "2025" / "08" / "20"
    start = datetime(2025, 8, 20, 12, 0, tzinfo=timezone.utc)
    msg = _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], 1, 1)
    line = serialize_market_payload(msg, received_at=start)

    paths = []
    for idx in range(2):
        sub = directory / str(idx)
        writer = SegmentWriter(symbol="BTCUSDT", directory=sub, start_utc=start)
        writer.open()
        writer.write_line(line, kind="snapshot", sequence=1)
        path, manifest_path = writer.close(end_utc=start)
        paths.append((path.read_bytes(), json.loads(manifest_path.read_text())["sha256"]))
    assert paths[0] == paths[1]


def test_disk_check_reports_free_space(tmp_path: Path) -> None:
    status = check_disk(tmp_path, warn_gb=0.0, min_gb=0.0)
    assert status.free_gb > 0


def _live_settings(tmp_path: Path, **kwargs) -> LiveCollectorSettings:
    base = dict(
        bybit_ws_url="wss://x",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="db",
        clickhouse_user="",
        clickhouse_password="",
        symbols=("BTCUSDT",),
        mode="raw-archive-only",
        lock_path=tmp_path / "archive.lock",
        pid_path=tmp_path / "archive.pid",
        health_path=tmp_path / "archive.health.ndjson",
        ada_only_pilot=False,
    )
    base.update(kwargs)
    return LiveCollectorSettings(**base)


def test_raw_archive_only_config_fail_closed() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(LiveCollectorConfigError, match="OB_V3_RAW_ARCHIVE_ENABLE"):
            load_raw_archive_only_settings(symbols_raw="BTCUSDT")
    with mock.patch.dict(os.environ, {"OB_V3_RAW_ARCHIVE_ENABLE": "true"}, clear=True):
        with pytest.raises(LiveCollectorConfigError, match="explicit --symbols"):
            load_raw_archive_only_settings(symbols_raw="")


def test_raw_archive_only_multi_symbol_requires_confirm() -> None:
    env = {"OB_V3_RAW_ARCHIVE_ENABLE": "true"}
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(LiveCollectorConfigError, match="confirm-raw-archive-symbols"):
            load_raw_archive_only_settings(symbols_raw="BTCUSDT,ETHUSDT")


def test_archive_only_uses_null_writer_not_feature_writer(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    collector = OrderbookV3LiveCollector(settings, archive_only=True)
    assert isinstance(collector.writer, NullFeatureWriter)
    assert not isinstance(collector.writer, FeatureWriter)


def test_archive_only_null_writer_rejects_feature_rows() -> None:
    writer = NullFeatureWriter()
    with pytest.raises(RuntimeError, match="must not receive"):
        writer.enqueue([{"bucket_start": datetime.now(timezone.utc)}])


def test_archive_only_health_identity(tmp_path: Path, tmp_archive: Path) -> None:
    settings = _live_settings(tmp_path)
    raw = RawArchiveManager(
        _settings(tmp_archive, symbols=frozenset({"BTCUSDT"})),
        depth=200,
    )
    collector = OrderbookV3LiveCollector(
        settings, archive_only=True, raw_archive=raw
    )
    collector._reset_runtimes()
    health = collector.health_payload()
    assert health["collector_identity"] == "raw_archive_only"
    assert health["feature_writer_enabled"] is False
    assert health["writer_state"] == "DISABLED"
    assert health["mode"] == "raw-archive-only"


def test_archive_only_does_not_enqueue_features(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    collector = OrderbookV3LiveCollector(settings, archive_only=True)
    collector._reset_runtimes()
    rt = collector.runtimes["BTCUSDT"]
    rt.active_generation = rt.clock.generation
    payload = _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], 1, 1)
    collector._ingest_ready(rt, payload, datetime.fromtimestamp(T0 / 1000, tz=timezone.utc))
    assert rt.rows_enqueued == 0
    assert collector.writer.rows_written == 0


def test_archive_only_lock_separate_from_live_collector(tmp_path: Path) -> None:
    live_lock = tmp_path / "live.lock"
    live_pid = tmp_path / "live.pid"
    archive_lock = tmp_path / "archive.lock"
    archive_pid = tmp_path / "archive.pid"
    live = SingleInstanceLock(live_lock, live_pid)
    archive = SingleInstanceLock(archive_lock, archive_pid)
    live.acquire()
    archive.acquire()
    live.release()
    archive.release()


def test_archive_only_lock_blocks_second_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    pid_path = tmp_path / "archive.pid"
    first = SingleInstanceLock(lock_path, pid_path)
    second = SingleInstanceLock(lock_path, pid_path)
    first.acquire()
    with pytest.raises(RuntimeError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


@pytest.mark.asyncio
async def test_checkpoint_rotation_at_hour_boundary(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive, rotation="hour"))
    manager.start()
    t0 = datetime(2025, 8, 20, 11, 59, 30, tzinfo=timezone.utc)
    snap = _event(int(t0.timestamp() * 1000), "snapshot", [["1", "1"]], [["2", "1"]], 1, 100)
    manager.try_enqueue_market("BTCUSDT", snap, t0)
    await asyncio.sleep(0.05)
    book = apply_snapshot(snap["data"])
    t1 = datetime(2025, 8, 20, 12, 0, 1, tzinfo=timezone.utc)
    await manager.rotate_with_checkpoint(
        "BTCUSDT",
        book,
        ts_ms=int(t1.timestamp() * 1000),
        received_at=t1,
        topic="orderbook.200.BTCUSDT",
    )
    delta = _event(int(t1.timestamp() * 1000), "delta", [["1", "2"]], [], 2, 101)
    manager.try_enqueue_market("BTCUSDT", delta, t1)
    await asyncio.sleep(0.05)
    await manager.stop()
    segments = sorted(tmp_archive.glob("**/*.zst"))
    assert len(segments) >= 2
    checkpoint_found = False
    for seg in segments[1:]:
        for obj in iter_segment_lines(seg):
            if obj.get("type") == "rotation_checkpoint":
                checkpoint_found = True
                assert obj["data"]["u"] == book.last_u
                assert obj["data"]["seq"] == book.last_seq
    assert checkpoint_found


@pytest.mark.asyncio
async def test_shutdown_closes_segment_cleanly(tmp_archive: Path) -> None:
    manager = RawArchiveManager(_settings(tmp_archive))
    manager.start()
    received = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    manager.try_enqueue_market(
        "BTCUSDT",
        _event(T0, "snapshot", [["1", "1"]], [["2", "1"]], 1, 1),
        received,
    )
    await asyncio.sleep(0.05)
    await manager.stop()
    assert not list(tmp_archive.glob("**/*.tmp"))
    closed = list(tmp_archive.glob("**/*.zst"))
    assert closed
    manifest = load_manifest(closed[0])
    assert manifest["completion_status"] == "closed"
    assert "continuity_status" in manifest
    assert "replay_source" in manifest
