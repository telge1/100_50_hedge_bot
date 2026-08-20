"""Tests for Orderbook V3 live collector (ADAUSDT pilot) and batch/live parity."""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2.book import BookState, apply_delta, apply_snapshot, sorted_asks, sorted_bids
from orderbook_analyse.orderbook_v2.dynamics import (
    build_carry_forward_row,
    build_event_feature_row,
    compute_dynamics,
    snapshot_is_usable,
)
from orderbook_analyse.orderbook_v2.features import compute_features
from orderbook_analyse.orderbook_v2.parser import parse_day_zip
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, SequenceBreak, floor_second_ms
from orderbook_analyse.orderbook_v2_live.collector import OrderbookV3LiveCollector
from orderbook_analyse.orderbook_v2_live.health import percentile
from orderbook_analyse.orderbook_v2_live.locks import SingleInstanceLock, cmdline_is_live_ob_collector
from orderbook_analyse.orderbook_v2_live.settings import (
    LiveCollectorConfigError,
    PILOT_SYMBOL,
    parse_symbols,
    redact_settings,
    load_live_settings,
)

T0 = 1_700_000_000_000
SKIP_COMPARE = {"created_at"}


def _event(ts_ms: int, type_: str, bids: list, asks: list, u: int, seq: int = 0, symbol: str = "ADAUSDT") -> dict:
    return {
        "topic": f"orderbook.200.{symbol}",
        "type": type_,
        "ts": ts_ms,
        "cts": None,
        "data": {"s": symbol, "b": bids, "a": asks, "u": u, "seq": seq},
    }


def _make_zip(messages: list[dict]) -> Path:
    inner_name = "2026-01-01_ADAUSDT_ob200.data"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        content = "\n".join(json.dumps(m) for m in messages).encode()
        zf.writestr(inner_name, content)
    buf.seek(0)
    f = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    f.write(buf.read())
    f.close()
    return Path(f.name)


def _comparable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key in SKIP_COMPARE:
            continue
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        else:
            out[key] = value
    return out


def _replay_messages() -> list[dict]:
    bids = [["0.50", "100"], ["0.49", "80"], ["0.48", "60"]]
    asks = [["0.51", "90"], ["0.52", "70"], ["0.53", "40"]]
    return [
        _event(T0 + 10, "snapshot", bids, asks, u=10, seq=100),
        _event(T0 + 200, "delta", [["0.50", "110"]], [], u=11, seq=101),
        _event(T0 + 800, "delta", [["0.47", "25"]], [["0.51", "0"]], u=12, seq=102),
        _event(T0 + 1200, "delta", [["0.50", "90"]], [["0.51", "50"]], u=13, seq=103),
        _event(T0 + 2500, "delta", [["0.49", "0"]], [], u=14, seq=104),
        _event(T0 + 3100, "delta", [["0.50", "95"]], [["0.52", "80"]], u=15, seq=105),
    ]


def test_snapshot_build_and_sort_and_depth():
    levels_b = [[str(1 - i * 0.001), str(i + 1)] for i in range(201)]
    levels_a = [[str(1.01 + i * 0.001), str(i + 1)] for i in range(201)]
    book = apply_snapshot({"b": levels_b, "a": levels_a, "u": 1, "seq": 1})
    assert book.is_valid
    bids = sorted_bids(book)
    asks = sorted_asks(book)
    assert bids[0][0] > bids[-1][0]
    assert asks[0][0] < asks[-1][0]
    assert len(bids) == 201
    assert len(asks) == 201


def test_delta_insert_update_delete():
    book = apply_snapshot({"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    book, _ = apply_delta(book, {"b": [["0.9", "5"]], "a": [], "u": 2, "seq": 2})
    assert book.bids[Decimal("0.9")] == Decimal("5")
    book, _ = apply_delta(book, {"b": [["1.0", "20"]], "a": [], "u": 3, "seq": 3})
    assert book.bids[Decimal("1.0")] == Decimal("20")
    book, _ = apply_delta(book, {"b": [["1.0", "0"]], "a": [], "u": 4, "seq": 4})
    assert Decimal("1.0") not in book.bids


def test_sequence_gap_and_delta_before_snapshot():
    clock = LiveSecondClock("ADAUSDT")
    rows = clock.ingest("delta", T0, {"b": [["1", "1"]], "a": [["2", "1"]], "u": 2, "seq": 2})
    assert rows == []
    assert clock.waiting_for_snapshot
    assert clock.stats.dropped_events == 1
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 10, "seq": 10})
    with pytest.raises(SequenceBreak):
        clock.ingest("delta", T0 + 10, {"b": [], "a": [], "u": 12, "seq": 12})
    assert clock.waiting_for_snapshot
    assert clock.stats.sequence_gaps >= 1


def test_duplicate_and_stale_delta():
    clock = LiveSecondClock("ADAUSDT")
    snap = {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 10, "seq": 10}
    clock.ingest("snapshot", T0, snap)
    clock.ingest("delta", T0 + 1, {"b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11})
    rows = clock.ingest("delta", T0 + 2, {"b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11})
    assert rows == []
    assert clock.stats.duplicate_u >= 1
    with pytest.raises(SequenceBreak):
        clock.ingest("delta", T0 + 3, {"b": [], "a": [], "u": 9, "seq": 9})


def test_reconnect_requires_new_snapshot():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    clock.invalidate("reconnect")
    assert clock.waiting_for_snapshot
    assert clock.last_valid_book is None
    rows = clock.ingest("delta", T0 + 1000, {"b": [["1.0", "11"]], "a": [], "u": 2, "seq": 2})
    assert rows == []
    clock.ingest("snapshot", T0 + 2000, {"b": [["1.0", "12"]], "a": [["1.1", "9"]], "u": 50, "seq": 50})
    emitted = clock.close_through(T0 + 4000)
    assert clock.first_valid_live_bucket_ms == T0 + 2000
    assert all(r["is_valid"] == 1 for r in emitted)


def test_utc_one_row_per_second_and_carry_forward():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0 + 10, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    clock.ingest("delta", T0 + 200, {"b": [["1.0", "11"]], "a": [], "u": 2, "seq": 2})
    rows = clock.close_through(T0 + 4000)
    buckets = [int(r["bucket_start"].timestamp() * 1000) for r in rows]
    assert buckets == [T0, T0 + 1000, T0 + 2000, T0 + 3000]
    assert rows[1]["quality_flags"] == "carried_forward"
    assert rows[1]["processed_updates"] == 0
    assert rows[1]["bid_qty_added"] == Decimal("0")


def test_no_carry_forward_when_invalid():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    clock.invalidate("gap")
    assert clock.close_through(T0 + 5000) == []


def test_shared_feature_builder_batch_vs_live():
    messages = _replay_messages()
    zp = _make_zip(messages)
    try:
        batch_rows, _ = parse_day_zip(
            zp, symbol="ADAUSDT", day_start_ms=T0, expected_seconds=4
        )
    finally:
        zp.unlink()
    clock = LiveSecondClock("ADAUSDT")
    live_rows: list[dict] = []
    for msg in messages:
        live_rows.extend(clock.ingest(msg["type"], msg["ts"], msg["data"]))
    live_rows.extend(clock.close_through(T0 + 4000))
    assert len(batch_rows) == 4
    assert len(live_rows) == 4
    for b, live in zip(batch_rows, live_rows, strict=True):
        assert b["parser_version"] == live["parser_version"] == PARSER_VERSION
        assert _comparable(b) == _comparable(live)
        assert build_event_feature_row.__module__.endswith("dynamics")
        assert compute_features.__module__.endswith("features")


def test_feature_invariants_on_live_rows():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    rows = clock.close_through(T0 + 2000)
    for row in rows:
        assert row["is_valid"] == 1
        assert row["best_bid_price"] < row["best_ask_price"]
        assert row["parser_version"] == "ob200_v3"
        assert row["spread_abs"] >= 0


def test_idempotent_bucket_skip():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    first = clock.close_through(T0 + 2000)
    second = clock.close_through(T0 + 2000)
    assert first
    assert second == []
    assert clock.stats.duplicate_buckets_skipped >= 0


def test_skip_before_does_not_rewrite_db_seconds():
    clock = LiveSecondClock("ADAUSDT", skip_before_ms=T0 + 2000)
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    rows = clock.close_through(T0 + 4000)
    buckets = [int(r["bucket_start"].timestamp() * 1000) for r in rows]
    assert T0 not in buckets
    assert T0 + 1000 not in buckets
    assert min(buckets) == T0 + 2000


def test_config_ada_only_gate():
    with pytest.raises(LiveCollectorConfigError):
        parse_symbols("BTCUSDT", ada_only_pilot=True)
    with pytest.raises(LiveCollectorConfigError):
        parse_symbols("ADAUSDT,BTCUSDT", ada_only_pilot=True)
    assert parse_symbols("ADAUSDT", ada_only_pilot=True) == (PILOT_SYMBOL,)


def test_redact_password(monkeypatch, tmp_path):
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "8123")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "orderbook_analysis")
    monkeypatch.setenv("CLICKHOUSE_USER", "writer")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "super-secret-value")
    settings = load_live_settings(symbols_raw="ADAUSDT", dotenv_path=tmp_path / "missing.env")
    red = redact_settings(settings)
    assert red["clickhouse_password"] == "***"
    dumped = json.dumps(red)
    assert "super-secret-value" not in dumped


def test_lock_single_instance(tmp_path):
    lock_path = tmp_path / "c.lock"
    pid_path = tmp_path / "c.pid"
    a = SingleInstanceLock(lock_path, pid_path)
    b = SingleInstanceLock(lock_path, pid_path)
    a.acquire()
    with pytest.raises(RuntimeError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_cmdline_gate_does_not_match_oi_collector():
    assert cmdline_is_live_ob_collector(
        "python -m orderbook_analyse.orderbook_v2_live --symbols ADAUSDT", "python"
    )
    assert not cmdline_is_live_ob_collector(
        "python -m orderbook_analyse.oi_liquidation_collector --mode live", "python"
    )


def test_percentile_and_floor():
    assert percentile([], 50) is None
    assert percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert floor_second_ms(T0 + 999) == T0


def test_snapshot_usable():
    bad = apply_snapshot({"b": [["2.0", "1"]], "a": [["1.0", "1"]], "u": 1, "seq": 1})
    assert not snapshot_is_usable(bad)


def test_collector_shutdown_and_ada_subscription_topics():
    settings = load_live_settings(mode="ada")
    coll = OrderbookV3LiveCollector(settings, client_factory=lambda: None, duration_sec=1)
    coll.request_stop()
    assert coll._stop.is_set()
    assert settings.orderbook_topics() == ["orderbook.200.ADAUSDT"]
    assert "BTCUSDT" not in settings.orderbook_topics()[0]


def test_idempotent_insert_mock():
    written = []

    class FakeCH:
        def insert(self, table, data, column_names=None):
            written.extend(data)

        def query(self, *a, **k):
            return SimpleNamespace(result_rows=[[None]])

    from orderbook_analyse.orderbook_v2.ch_writer import insert_features

    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1})
    rows = clock.close_through(T0 + 2000)
    insert_features(FakeCH(), rows)
    n = len(written)
    insert_features(FakeCH(), rows)
    assert len(written) == 2 * n


def test_replay_fixture_file_parity():
    fixture = Path(__file__).parent / "fixtures" / "orderbook_v3_live_replay.jsonl"
    messages = [json.loads(line) for line in fixture.read_text().splitlines() if line.strip()]
    zp = _make_zip(messages)
    try:
        batch_rows, _ = parse_day_zip(
            zp, symbol="ADAUSDT", day_start_ms=T0, expected_seconds=4
        )
    finally:
        zp.unlink()
    clock = LiveSecondClock("ADAUSDT")
    live_rows: list[dict] = []
    for msg in messages:
        live_rows.extend(clock.ingest(msg["type"], int(msg["ts"]), msg["data"]))
    live_rows.extend(clock.close_through(T0 + 4000))
    assert [_comparable(r) for r in batch_rows] == [_comparable(r) for r in live_rows]
