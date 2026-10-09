"""Phase 11Q immutable candle snapshot fixtures. No ClickHouse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data.models import Candle
from indicators.live_micro_lld_v2.availability import bar_close
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    SnapshotConflictError,
    SnapshotError,
    deterministic_snapshot_id,
    load_snapshot,
    ohlcv_content_hash,
    publish_snapshot,
    reject_mixed,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import project_pool_v1
from research_charts.trp_import import load_trp
from indicators.live_micro_lld_v2.models import LiquidityPoolV2


def _row(i: int, price: str = "1.00000000", ingested: str = "2026-09-24T00:00:01.000Z", source_event: bool = True) -> CandleInputRow:
    start = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(minutes=i)
    return CandleInputRow(
        exchange="bybit",
        symbol="DOGEUSDT",
        interval="1m",
        open_time=start,
        close_time=start + timedelta(minutes=1),
        open=Decimal(price),
        high=Decimal(price) + Decimal("0.01000000"),
        low=Decimal(price) - Decimal("0.01000000"),
        close=Decimal(price),
        volume=Decimal("10.00000000"),
        turnover=Decimal("10.00000000"),
        is_closed=1,
        source="test",
        source_event_time=start if source_event else None,
        ingested_at=datetime.strptime(ingested, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc),
    )


def _series(n: int = 5, **kwargs) -> list[CandleInputRow]:
    return [_row(i, **kwargs) for i in range(n)]


def _range(n: int = 5):
    start = datetime(2026, 9, 24, tzinfo=timezone.utc)
    return start, start + timedelta(minutes=n)


def test_hash_changes_on_ohlcv_not_on_reingest():
    a = _series()
    b = [_row(i, ingested="2026-09-24T01:00:01.000Z") for i in range(5)]
    changed = [_row(i, price="2.00000000") for i in range(5)]
    assert ohlcv_content_hash(a) == ohlcv_content_hash(b)
    assert revision_metadata_hash(a) != revision_metadata_hash(b)
    assert ohlcv_content_hash(a) != ohlcv_content_hash(changed)


def test_snapshot_id_deterministic_and_roundtrip(tmp_path):
    rows = _series()
    start, end = _range()
    m1 = publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="signal_generator.candles_1m")
    m2 = publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="signal_generator.candles_1m")
    assert m1["snapshot_id"] == m2["snapshot_id"]
    sid = deterministic_snapshot_id(
        symbol="DOGEUSDT",
        exchange="bybit",
        interval="1m",
        range_start=start,
        range_end_exclusive=end,
        ohlcv_hash=ohlcv_content_hash(rows),
        revision_hash=revision_metadata_hash(rows),
    )
    assert sid == m1["snapshot_id"]
    loaded = load_snapshot(tmp_path, sid)
    assert ohlcv_content_hash(loaded) == ohlcv_content_hash(rows)
    assert revision_metadata_hash(loaded) == revision_metadata_hash(rows)
    assert loaded[0].source_event_time == rows[0].source_event_time


def test_null_source_event_roundtrip(tmp_path):
    rows = [_row(i, source_event=False) for i in range(5)]
    start, end = _range()
    manifest = publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    loaded = load_snapshot(tmp_path, manifest["snapshot_id"])
    assert loaded[0].source_event_time is None
    assert revision_metadata_hash(loaded) == revision_metadata_hash(rows)


def test_duplicate_and_gap_fail(tmp_path):
    start, end = _range(3)
    dup = [_row(0), _row(0), _row(1)]
    with pytest.raises(SnapshotError, match="duplicate"):
        publish_snapshot(dup, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    gap = [_row(0), _row(2)]
    with pytest.raises(SnapshotError, match="missing"):
        publish_snapshot(gap, tmp_path, range_start=start, range_end_exclusive=start + timedelta(minutes=3), source_table="t")


def test_file_and_manifest_tamper(tmp_path):
    start, end = _range()
    manifest = publish_snapshot(_series(), tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    sid = manifest["snapshot_id"]
    data = tmp_path / f"{sid}.parquet"
    blob = bytearray(data.read_bytes())
    blob[-1] ^= 0xFF
    data.write_bytes(blob)
    with pytest.raises(SnapshotError, match="checksum"):
        load_snapshot(tmp_path, sid)
    data.write_bytes(data.read_bytes())  # still tampered
    # restore by republish is blocked while manifest exists with old checksum; rewrite original via a fresh dir
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path, sid)


def test_manifest_hash_conflict(tmp_path):
    start, end = _range()
    manifest = publish_snapshot(_series(), tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    path = tmp_path / f"{manifest['snapshot_id']}.manifest.json"
    text = path.read_text(encoding="utf-8").replace(manifest["ohlcv_content_hash"], "0" * 64)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path, manifest["snapshot_id"])


def test_crash_before_and_after_publish(tmp_path):
    start, end = _range()
    rows = _series()
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        deterministic_snapshot_id,
        ohlcv_content_hash,
        revision_metadata_hash,
        _write_parquet,
    )

    sid = deterministic_snapshot_id(
        symbol="DOGEUSDT",
        exchange="bybit",
        interval="1m",
        range_start=start,
        range_end_exclusive=end,
        ohlcv_hash=ohlcv_content_hash(rows),
        revision_hash=revision_metadata_hash(rows),
    )
    tmp = tmp_path / f".{sid}.data.tmp"
    _write_parquet(tmp, rows)
    with pytest.raises(SnapshotError, match="manifest missing"):
        load_snapshot(tmp_path, sid)
    # crash after data rename, before manifest
    (tmp_path / f"{sid}.parquet").write_bytes(tmp.read_bytes())
    with pytest.raises(SnapshotError, match="manifest missing"):
        load_snapshot(tmp_path, sid)


def test_three_workers_and_foreign_id(tmp_path):
    start, end = _range()
    a = publish_snapshot(_series(), tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    b_rows = [_row(i, price="3.00000000") for i in range(5)]
    b = publish_snapshot(b_rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    assert a["snapshot_id"] != b["snapshot_id"]
    seen = [require_snapshot(tmp_path, a["snapshot_id"], ohlcv_hash=a["ohlcv_content_hash"]) for _ in range(3)]
    assert ohlcv_content_hash(seen[0]) == ohlcv_content_hash(seen[1]) == ohlcv_content_hash(seen[2])
    with pytest.raises(SnapshotError):
        require_snapshot(tmp_path, b["snapshot_id"], ohlcv_hash=a["ohlcv_content_hash"])
    with pytest.raises(SnapshotError, match="mixed"):
        reject_mixed(seen[0], load_snapshot(tmp_path, b["snapshot_id"]))


def test_published_a_unchanged_after_b(tmp_path):
    start, end = _range()
    a_rows = _series()
    a = publish_snapshot(a_rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    b_rows = [_row(i, price="9.00000000", ingested="2026-09-25T00:00:01.000Z") for i in range(5)]
    publish_snapshot(b_rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    again = load_snapshot(tmp_path, a["snapshot_id"])
    assert ohlcv_content_hash(again) == ohlcv_content_hash(a_rows)


def test_conflict_same_id_different_manifest(tmp_path):
    start, end = _range()
    rows = _series()
    sid = deterministic_snapshot_id(
        symbol="DOGEUSDT",
        exchange="bybit",
        interval="1m",
        range_start=start,
        range_end_exclusive=end,
        ohlcv_hash=ohlcv_content_hash(rows),
        revision_hash=revision_metadata_hash(rows),
    )
    (tmp_path / f"{sid}.manifest.json").write_text(
        '{"snapshot_id": "%s", "ohlcv_content_hash": "%s", "revision_metadata_hash": "%s"}'
        % (sid, "a" * 64, "b" * 64),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotConflictError):
        publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")


def test_strict_5m_and_remaining_parity():
    trp = load_trp()
    aggregate = trp["aggregate"]
    rows = _series(10)
    candles = []
    for r in rows:
        candles.append(
            Candle(
                timestamp=r.open_time,
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(r.volume),
                symbol="DOGEUSDT",
                timeframe="1m",
            )
        )
    five = aggregate(candles, "5m", strict_complete_buckets=True)
    assert len(five) == 2
    incomplete = aggregate(candles[:7], "5m", strict_complete_buckets=True)
    assert len(incomplete) == 1
    known_open = candles[0].timestamp - timedelta(minutes=5)
    pool = LiquidityPoolV2(
        pool_id="lldv2:DOGEUSDT:5m:normal:upper:1",
        pool_type="normal",
        symbol="DOGEUSDT",
        timeframe="5m",
        side="upper",
        source_timestamp=known_open,
        created_timestamp=known_open,
        known_at=bar_close(known_open, "5m"),
        top_price=float(five[0].high) + 1,
        bottom_price=float(five[0].low),
        strength=4,
        strength_class="Medium",
        active=True,
    )
    dc = bar_close(five[-1].timestamp, "5m")
    p1 = project_pool_v1(pool, five, decision_close=dc, candle_input_reference="snap")
    p2 = project_pool_v1(pool, list(five), decision_close=dc, candle_input_reference="snap")
    assert p1.remaining_bottom == p2.remaining_bottom
    assert p1.remaining_fraction == p2.remaining_fraction


def test_incomplete_window_rejected(tmp_path):
    start, end = _range()
    manifest = publish_snapshot(_series(), tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    rows = load_snapshot(tmp_path, manifest["snapshot_id"])
    with pytest.raises(SnapshotError, match="coverage"):
        from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import assert_unique_complete

        assert_unique_complete(rows[:-1], range_start=start, range_end_exclusive=end, symbol="DOGEUSDT", exchange="bybit")
