"""Synthetic Phase A1+A2 contract tests (offline/tmp_path only)."""

from __future__ import annotations

import errno
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive import (
    ARCHIVE_FATAL_EXIT_CODE,
    COMPLETE,
    GAP,
    PARTIAL,
    FullObContinuousRawArchive,
    FullObContinuousRawArchiveSettings,
    SegmentWriter,
    book_sha256,
    build_checkpoint_record,
    load_full_ob_continuous_raw_archive_settings,
    replay_segment,
    scan_orphan_tmps,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.disk_safety import (
    DiskState,
    classify_disk,
    classify_os_error,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.envelope import (
    build_envelope,
    build_marker_envelope,
    payload_sha256,
    serialize_envelope,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.queue import (
    ArchiveQueueItem,
    ArchiveQueueOverflow,
    FatalBoundedQueue,
)

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
T0_NS = int(T0.timestamp() * 1e9)


def _state(symbol: str = "BTCUSDT", u: int = 10) -> FullBookState:
    state = FullBookState(symbol)
    state.apply_snapshot(
        bids=[["100.00", "2.500"], ["99", "3"]],
        asks=[["101.0", "4"], ["102", "5"]],
        u=u,
        seq=100,
        ts_ms=int(T0.timestamp() * 1000),
        receive_time_ns=T0_NS,
    )
    return state


def _settings(root: Path, **changes) -> FullObContinuousRawArchiveSettings:
    values = dict(
        enabled=True,
        archive_root=root,
        symbols=frozenset({"BTCUSDT"}),
        queue_size=32,
        flush_interval_sec=0.02,
        warn_free_disk_gb=0,
        red_free_disk_gb=0,
        warn_free_disk_percent=0,
        red_free_disk_percent=0,
    )
    values.update(changes)
    return FullObContinuousRawArchiveSettings(**values)


def _delta(u: int, qty: str = "3") -> dict:
    return {
        "topic": "orderbook.full.BTCUSDT",
        "type": "delta",
        "ts": int(T0.timestamp() * 1000) + u,
        "data": {"s": "BTCUSDT", "b": [["100", qty]], "a": [], "u": u, "seq": 100 + u},
    }


def _writer(root: Path) -> SegmentWriter:
    writer = SegmentWriter(
        root=root,
        symbol="BTCUSDT",
        start_time=T0,
        archive_instance_id="archive-a",
        collector_instance_id="collector-a",
    )
    writer.open()
    return writer


def test_config_contract_default_off_alias_and_fixed_scope(tmp_path: Path) -> None:
    # Requirements 1-6: name/version, default OFF, alias, prefix, symbols, fixed cadence.
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = load_full_ob_continuous_raw_archive_settings()
    assert cfg.enabled is False
    assert cfg.segment_minutes == 60 and cfg.checkpoint_minutes == 5
    assert cfg.symbols == frozenset({"BTCUSDT", "DOGEUSDT"})
    with mock.patch.dict(os.environ, {"FULL_OB_CONTINUOUS_ARCHIVE_ENABLED": "true"}, clear=True):
        assert load_full_ob_continuous_raw_archive_settings().enabled is True
    with mock.patch.dict(
        os.environ,
        {
            "OB_V3_FULL_OB_RAW_ARCHIVE_ENABLED": "1",
            "OB_V3_FULL_OB_RAW_ARCHIVE_ROOT": str(tmp_path),
        },
        clear=True,
    ):
        prefixed = load_full_ob_continuous_raw_archive_settings()
    assert prefixed.enabled and prefixed.archive_root == tmp_path
    assert ARCHIVE_FATAL_EXIT_CODE == 75


def test_envelope_preserves_payload_and_is_deterministic() -> None:
    # Requirements 7-10: fields, unchanged payload, deterministic bytes, payload hash.
    payload = _delta(11)
    original = json.loads(json.dumps(payload))
    env = build_envelope(
        payload,
        archive_instance_id="a",
        collector_instance_id="c",
        symbol="BTCUSDT",
        receive_time_ns=T0_NS,
        archive_time_ns=T0_NS + 1,
    )
    assert payload == original and env["original_payload"] == original
    assert env["payload_sha256"] == payload_sha256(original)
    assert env["u"] == 11 and env["seq"] == 111
    assert serialize_envelope(env) == serialize_envelope(dict(reversed(list(env.items()))))


def test_checkpoint_is_full_canonical_and_linked() -> None:
    # Requirements 11-15: full levels/counts/best/hash/reason and instance links.
    snap = _state().copy_consistent_snapshot()
    record = build_checkpoint_record(
        snap,
        reason="segment_start",
        archive_instance_id="a",
        collector_instance_id="c",
        checkpoint_time=T0,
    )
    cp = record["original_payload"]
    assert cp["bids"] == [["100", "2.5"], ["99", "3"]]
    assert cp["asks"] == [["101", "4"], ["102", "5"]]
    assert (cp["bid_level_count"], cp["ask_level_count"]) == (2, 2)
    assert (cp["best_bid"], cp["best_ask"]) == ("100", "101")
    assert cp["book_sha256"] == book_sha256(cp["bids"], cp["asks"])
    assert cp["checkpoint_reason"] == "segment_start"
    assert record["archive_instance_id"] == "a" and record["collector_instance_id"] == "c"


def test_complete_segment_manifest_and_atomic_finalize(tmp_path: Path) -> None:
    # Requirements 16-24: zstd/tmp rename, anchor, continuity, counts, hashes and manifest.
    writer = _writer(tmp_path)
    assert writer.tmp_path.exists()
    writer.write(
        build_checkpoint_record(
            _state().copy_consistent_snapshot(),
            reason="segment_start",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            checkpoint_time=T0,
        )
    )
    env = build_envelope(
        _delta(11),
        archive_instance_id="archive-a",
        collector_instance_id="collector-a",
        symbol="BTCUSDT",
        receive_time_ns=T0_NS + 1_000_000,
        archive_time_ns=T0_NS + 2_000_000,
    )
    writer.write(env)
    writer.flush()
    final, manifest_path, manifest = writer.close(end_time=T0 + timedelta(seconds=1))
    assert final.exists() and manifest_path.exists() and not writer.tmp_path.exists()
    assert manifest["completion_status"] == COMPLETE
    assert manifest["has_valid_anchor"] is True
    assert manifest["continuity_status"] == "CONTIGUOUS"
    assert manifest["message_count"] == 2 and manifest["level_update_count"] == 1
    assert manifest["checkpoint_count"] == 1 and manifest["delta_count"] == 1
    assert manifest["payload_sha256"] and manifest["segment_sha256"]
    assert manifest["archive_instance_id"] == "archive-a"


def test_completion_rules_missing_anchor_gap_and_overflow(tmp_path: Path) -> None:
    # Requirements 25-27: missing anchor PARTIAL, gap GAP, overflow never COMPLETE.
    no_anchor = _writer(tmp_path / "a")
    no_anchor.write(
        build_envelope(
            _delta(11),
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            receive_time_ns=T0_NS,
        )
    )
    assert no_anchor.close()[2]["completion_status"] == PARTIAL

    gap = _writer(tmp_path / "b")
    gap.write(
        build_checkpoint_record(
            _state().copy_consistent_snapshot(),
            reason="segment_start",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
        )
    )
    gap.write(
        build_envelope(
            _delta(15),
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            receive_time_ns=T0_NS,
        )
    )
    assert gap.close()[2]["completion_status"] == GAP

    overflow = _writer(tmp_path / "c")
    overflow.write(
        build_checkpoint_record(
            _state().copy_consistent_snapshot(),
            reason="segment_start",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
        )
    )
    overflow.note_overflow()
    assert overflow.close()[2]["completion_status"] == PARTIAL


def test_orphan_tmp_inventory_never_promotes_or_deletes(tmp_path: Path) -> None:
    # Requirements 28-30: orphan inventory is PARTIAL; .tmp remains; no COMPLETE rename.
    orphan = tmp_path / "BTCUSDT_open.zst.tmp"
    orphan.write_bytes(b"crash-tail")
    rows = scan_orphan_tmps(tmp_path)
    assert rows[0]["completion_status"] == PARTIAL
    assert rows[0]["renamed_to_complete"] is False
    assert orphan.exists() and Path(str(orphan) + ".partial.json").exists()


def test_disk_safety_green_amber_red_and_os_failures(tmp_path: Path) -> None:
    # Requirements 31-34: GB+percent states and ENOSPC/RO/permission detection.
    green = classify_disk(tmp_path, warn_gb=0, red_gb=0, warn_percent=0, red_percent=0)
    assert green.state is DiskState.GREEN
    red = classify_disk(tmp_path, warn_gb=10**9, red_gb=10**9, warn_percent=100, red_percent=100)
    assert red.state is DiskState.RED
    amber = classify_disk(tmp_path, warn_gb=10**9, red_gb=0, warn_percent=100, red_percent=0)
    assert amber.state is DiskState.AMBER
    assert classify_os_error(OSError(errno.ENOSPC, "x")) == "ENOSPC"
    assert classify_os_error(OSError(errno.EROFS, "x")) == "READ_ONLY_FILESYSTEM"
    assert classify_os_error(OSError(errno.EACCES, "x")) == "PERMISSION_DENIED"


def test_bounded_queue_overflow_is_exception_and_manager_fatal(tmp_path: Path) -> None:
    # Requirements 35-36: bounded queue and fail-fast overflow, no silent drop.
    q = FatalBoundedQueue(1)
    q.put(ArchiveQueueItem("BTCUSDT", {}))
    with pytest.raises(ArchiveQueueOverflow):
        q.put(ArchiveQueueItem("BTCUSDT", {}))
    manager = FullObContinuousRawArchive(_settings(tmp_path, queue_size=1))
    manager.enqueue_message("BTCUSDT", _delta(11), receive_time_ns=T0_NS)
    manager.enqueue_message("BTCUSDT", _delta(12), receive_time_ns=T0_NS + 1)
    assert manager.fatal_state == "queue_overflow"
    assert manager.health_dict()["full_ob_raw_archive_no_silent_discard"] is True


def test_manager_periodic_flush_checkpoint_stop_and_health(tmp_path: Path) -> None:
    # Requirements 37-40: threaded writer, ~1s durable flush, checkpoints and health.
    state = _state()
    manager = FullObContinuousRawArchive(
        _settings(tmp_path),
        snapshot_provider=lambda symbol: state.copy_consistent_snapshot(),
    )
    manager.start()
    manager.enqueue_message("BTCUSDT", _delta(11), receive_time_ns=T0_NS + 1_000_000)
    manager.tick(T0 + timedelta(minutes=5))
    time.sleep(0.08)
    manager.stop()
    health = manager.health_dict()
    assert health["full_ob_raw_archive_fatal"] is False
    assert health["full_ob_raw_archive_flush_count"] >= 1
    assert health["full_ob_raw_archive_checkpoint_count"] >= 2
    assert next(tmp_path.rglob("*.ndjson.zst")).exists()


def test_hourly_rotation_btc_doge_separation_and_disk_red_fatal(tmp_path: Path) -> None:
    # Hourly UTC rotation, symbol isolation, disk RED fail-fast (no silent pause).
    books = {
        "BTCUSDT": _state("BTCUSDT", 10),
        "DOGEUSDT": _state("DOGEUSDT", 10),
    }

    def provider(symbol: str):
        return books[symbol].copy_consistent_snapshot()

    fatal: list[str] = []
    manager = FullObContinuousRawArchive(
        _settings(
            tmp_path / "ok",
            symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
            queue_size=64,
        ),
        snapshot_provider=provider,
        fatal_callback=lambda reason: fatal.append(reason),
    )
    manager.start()
    t0 = T0
    t1 = T0 + timedelta(hours=1, minutes=1)
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        manager.enqueue_message(
            symbol,
            {
                "topic": f"orderbook.full.{symbol}",
                "type": "delta",
                "ts": int(t0.timestamp() * 1000) + 1,
                "data": {"s": symbol, "b": [["100", "1"]], "a": [], "u": 11, "seq": 111},
            },
            receive_time_ns=int(t0.timestamp() * 1e9) + 1,
        )
        manager.enqueue_message(
            symbol,
            {
                "topic": f"orderbook.full.{symbol}",
                "type": "delta",
                "ts": int(t1.timestamp() * 1000),
                "data": {"s": symbol, "b": [["100", "2"]], "a": [], "u": 12, "seq": 112},
            },
            receive_time_ns=int(t1.timestamp() * 1e9),
        )
    time.sleep(0.2)
    manager.stop()
    segments = sorted((tmp_path / "ok").rglob("*.ndjson.zst"))
    assert len(segments) == 4  # 2 symbols × 2 hours
    assert {p.parent.parent.parent.parent.name for p in segments} == {"BTCUSDT", "DOGEUSDT"}
    assert {p.name.split("_")[1] for p in segments} == {"20260905T120000Z", "20260905T130000Z"}

    red_fatal: list[str] = []
    red = FullObContinuousRawArchive(
        _settings(
            tmp_path / "red",
            red_free_disk_gb=10**9,
            red_free_disk_percent=100,
            warn_free_disk_gb=10**9,
            warn_free_disk_percent=100,
        ),
        fatal_callback=lambda reason: red_fatal.append(reason),
    )
    red.start()
    time.sleep(0.05)
    red.enqueue_message("BTCUSDT", _delta(11), receive_time_ns=T0_NS)
    assert red.fatal_state is not None
    assert red_fatal and red.health_dict()["full_ob_raw_archive_fatal"] is True
    assert red.health_dict()["full_ob_raw_archive_no_silent_discard"] is True
    red.stop(clean=False)


def test_checkpoint_rejects_crossed_or_negative_and_reconnect_chain(tmp_path: Path) -> None:
    # No crossed book / negative sizes; reconnect without snapshot blocks COMPLETE;
    # reconnect with snapshot starts a new replayable chain.
    bad = _state().copy_consistent_snapshot()
    bad.asks = {99.0: 1.0}  # crosses best bid 100
    with pytest.raises(ValueError, match="crossed"):
        build_checkpoint_record(
            bad,
            reason="segment_start",
            archive_instance_id="a",
            collector_instance_id="c",
        )
    neg = _state().copy_consistent_snapshot()
    neg.bids = {100.0: -1.0}
    with pytest.raises(ValueError, match="negative"):
        build_checkpoint_record(
            neg,
            reason="segment_start",
            archive_instance_id="a",
            collector_instance_id="c",
        )

    writer = _writer(tmp_path / "reconnect")
    writer.write(
        build_checkpoint_record(
            _state().copy_consistent_snapshot(),
            reason="segment_start",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
        )
    )
    writer.write(
        build_envelope(
            _delta(11),
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            receive_time_ns=T0_NS,
        )
    )
    # Reconnect/gap without replacing chain → GAP, not COMPLETE.
    writer.write(
        build_marker_envelope(
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            message_type="gap_marker",
            details={"reason": "reconnect"},
            receive_time_ns=T0_NS,
        )
    )
    final, _, manifest = writer.close()
    assert manifest["completion_status"] == GAP

    # New chain after exchange snapshot / resync checkpoint is independently replayable.
    writer2 = _writer(tmp_path / "resync")
    writer2.write(
        build_checkpoint_record(
            _state(u=10).copy_consistent_snapshot(),
            reason="segment_start",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
        )
    )
    writer2.write(
        build_marker_envelope(
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            message_type="gap_marker",
            details={"reason": "reconnect"},
            receive_time_ns=T0_NS,
        )
    )
    snap_state = _state(u=50)
    writer2.write(
        build_checkpoint_record(
            snap_state.copy_consistent_snapshot(),
            reason="reconnect_resync",
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
        )
    )
    writer2.write(
        build_envelope(
            _delta(51),
            archive_instance_id="archive-a",
            collector_instance_id="collector-a",
            symbol="BTCUSDT",
            receive_time_ns=T0_NS + 5,
        )
    )
    final2, _, manifest2 = writer2.close()
    assert manifest2["completion_status"] == GAP  # gap remains visible
    replay = replay_segment(final2, verify_hashes=True, verify_sequence=True, verify_final_book=True)
    assert replay["ok"] is True and replay["anchored"] is True
    assert replay["last_u"] == 51


def test_independent_replay_hash_sequence_and_final_book(tmp_path: Path) -> None:
    # Independently anchored replay, hash/sequence/final-book checks.
    state = _state()
    manager = FullObContinuousRawArchive(
        _settings(tmp_path),
        snapshot_provider=lambda symbol: state.copy_consistent_snapshot(),
    )
    manager.start()
    manager.enqueue_message("BTCUSDT", _delta(11), receive_time_ns=T0_NS + 1_000_000)
    time.sleep(0.05)
    manager.stop()
    segment = next(tmp_path.rglob("*.ndjson.zst"))
    result = replay_segment(segment, verify_hashes=True, verify_sequence=True, verify_final_book=True)
    assert result["ok"] is True and result["anchored"] is True
    assert result["final_bid_level_count"] == 2 and result["final_book_sha256"]
