"""Memory-bounded dedupe regression for Orderbook V3 live clock."""

from __future__ import annotations

import gc
import tracemalloc
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, SequenceBreak
from orderbook_analyse.orderbook_v2_live.collector import OrderbookV3LiveCollector
from orderbook_analyse.orderbook_v2_live.dedupe import DEFAULT_DEDUPE_CAPACITY, BoundedRecentU
from orderbook_analyse.orderbook_v2_live.settings import LiveCollectorSettings
from orderbook_analyse.orderbook_v2_live.universe import SYMBOLS_51
from orderbook_analyse.orderbook_v2_live.writer import NullFeatureWriter

T0 = 1_700_000_000_000


def test_bounded_recent_u_strict_capacity() -> None:
    d = BoundedRecentU(capacity=8)
    for i in range(100):
        d.add(i)
    assert len(d) == 8
    assert d.evictions == 92
    assert 99 in d
    assert 0 not in d


def test_millions_of_updates_no_linear_container_growth() -> None:
    clock = LiveSecondClock("BTCUSDT", dedupe_capacity=1024)
    clock.ingest(
        "snapshot",
        T0,
        {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
    )
    for i in range(2, 200_002):
        rows = clock.ingest(
            "delta",
            T0 + i,
            {"b": [["1.0", str(10 + (i % 5))]], "a": [], "u": i, "seq": i},
        )
        for row in rows:
            clock.note_enqueued(int(row["bucket_start"].timestamp() * 1000))
    assert len(clock.recent_us) == 1024
    assert clock.recent_us.evictions >= 200_000 - 1024
    assert len(clock.written_buckets) == 0
    assert len(clock.in_flight_buckets) <= 2


def test_duplicates_within_window_dropped() -> None:
    clock = LiveSecondClock("ADAUSDT", dedupe_capacity=64)
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 10, "seq": 10})
    clock.ingest("delta", T0 + 1, {"b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11})
    rows = clock.ingest("delta", T0 + 2, {"b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11})
    assert rows == []
    assert clock.stats.duplicate_u >= 1
    assert clock.book.last_u == 11


def test_stale_u_triggers_gap() -> None:
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 10, "seq": 10})
    clock.ingest("delta", T0 + 1, {"b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11})
    with pytest.raises(SequenceBreak):
        clock.ingest("delta", T0 + 3, {"b": [], "a": [], "u": 9, "seq": 9})
    assert clock.waiting_for_snapshot
    assert clock.stats.sequence_gaps >= 1


def test_sequence_gap_recognized() -> None:
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 10, "seq": 10})
    with pytest.raises(SequenceBreak):
        clock.ingest("delta", T0 + 10, {"b": [], "a": [], "u": 12, "seq": 12})


def test_snapshot_resync_clears_dedupe() -> None:
    clock = LiveSecondClock("ADAUSDT", dedupe_capacity=32)
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    for i in range(2, 20):
        clock.ingest("delta", T0 + i, {"b": [["1.0", "11"]], "a": [], "u": i, "seq": i})
    assert len(clock.recent_us) > 0
    clock.begin_resync()
    assert len(clock.recent_us) == 0
    assert clock.waiting_for_snapshot
    clock.ingest("snapshot", T0 + 1000, {"b": [["1.0", "12"]], "a": [["1.1", "9"]], "u": 50, "seq": 50})
    assert 50 in clock.recent_us


def test_generation_switch_clears_dedupe() -> None:
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest(
        "snapshot",
        T0,
        {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
        generation=0,
    )
    gen = clock.begin_resync()
    assert gen == 1
    assert len(clock.recent_us) == 0
    rows = clock.ingest(
        "delta",
        T0 + 1,
        {"b": [["1.0", "11"]], "a": [], "u": 2, "seq": 2},
        generation=0,
    )
    assert rows == []
    assert clock.stale_generation_dropped >= 1


def test_51_symbols_remain_bounded() -> None:
    clocks = [LiveSecondClock(s, dedupe_capacity=256) for s in SYMBOLS_51]
    for clock in clocks:
        clock.ingest(
            "snapshot",
            T0,
            {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
        )
        for u in range(2, 5002):
            clock.ingest(
                "delta",
                T0 + u,
                {"b": [["1.0", "11"]], "a": [], "u": u, "seq": u},
            )
        assert len(clock.recent_us) == 256
    total = sum(len(c.recent_us) for c in clocks)
    assert total == 256 * 51


def test_feature_parity_and_carry_forward_unchanged() -> None:
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest(
        "snapshot",
        T0 + 10,
        {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
    )
    clock.ingest("delta", T0 + 200, {"b": [["1.0", "11"]], "a": [], "u": 2, "seq": 2})
    rows = clock.close_through(T0 + 4000)
    buckets = [int(r["bucket_start"].timestamp() * 1000) for r in rows]
    assert buckets == [T0, T0 + 1000, T0 + 2000, T0 + 3000]
    assert rows[1]["quality_flags"] == "carried_forward"
    for row in rows:
        clock.note_enqueued(int(row["bucket_start"].timestamp() * 1000))
    assert clock.close_through(T0 + 4000) == []


def test_health_exposes_memory_fields(tmp_path: Path) -> None:
    settings = LiveCollectorSettings(
        bybit_ws_url="wss://x",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="db",
        clickhouse_user="",
        clickhouse_password="",
        symbols=("ADAUSDT",),
        mode="ada",
        lock_path=tmp_path / "a.lock",
        pid_path=tmp_path / "a.pid",
        health_path=None,
        ada_only_pilot=True,
    )
    collector = OrderbookV3LiveCollector(settings)
    collector._reset_runtimes()
    health = collector.health_payload()
    assert "rss_mb" in health
    assert "dedupe_entries_total" in health
    assert "dedupe_capacity_total" in health
    assert health["dedupe_capacity_total"] == DEFAULT_DEDUPE_CAPACITY
    assert health["per_symbol"][0]["dedupe_capacity"] == DEFAULT_DEDUPE_CAPACITY


def test_default_capacity_protocol_justified() -> None:
    assert DEFAULT_DEDUPE_CAPACITY == 8192


def test_langlauf_memory_smoke_plateau() -> None:
    tracemalloc.start()
    gc.collect()
    clocks = [LiveSecondClock(f"S{i}", dedupe_capacity=512) for i in range(51)]
    for clock in clocks:
        clock.ingest(
            "snapshot",
            T0,
            {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
        )
    gc.collect()
    snap1 = tracemalloc.take_snapshot()
    for step in range(2, 50_002):
        for clock in clocks:
            clock.ingest(
                "delta",
                T0 + step,
                {"b": [["1.0", str(10 + step % 3)]], "a": [], "u": step, "seq": step},
            )
    gc.collect()
    snap2 = tracemalloc.take_snapshot()
    stats = snap2.compare_to(snap1, "filename")
    growth = sum(s.size_diff for s in stats if s.size_diff > 0)
    assert growth < 5 * 1024 * 1024
    for clock in clocks:
        assert len(clock.recent_us) == 512
    tracemalloc.stop()


def test_raw_archive_only_still_uses_null_writer(tmp_path: Path) -> None:
    settings = LiveCollectorSettings(
        bybit_ws_url="wss://x",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="db",
        clickhouse_user="",
        clickhouse_password="",
        symbols=("BTCUSDT", "DOGEUSDT"),
        mode="raw-archive-only",
        lock_path=tmp_path / "r.lock",
        pid_path=tmp_path / "r.pid",
        health_path=None,
        ada_only_pilot=False,
    )
    collector = OrderbookV3LiveCollector(settings, archive_only=True)
    assert isinstance(collector.writer, NullFeatureWriter)
