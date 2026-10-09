"""Synthetic capture-revision fixtures. No ClickHouse and no production tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    SnapshotError,
    load_snapshot,
    ohlcv_content_hash,
    publish_snapshot,
    revision_metadata_hash,
)


def _row(
    i: int,
    *,
    price: str = "1.00000000",
    ingested: str = "2026-06-01T01:00:00.000Z",
    source: str = "bybit_live",
) -> CandleInputRow:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return CandleInputRow(
        exchange="bybit",
        symbol="DOGEUSDT",
        interval="1m",
        open_time=start,
        close_time=start + timedelta(minutes=1),
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=Decimal("1.00000000"),
        turnover=Decimal("1.00000000"),
        is_closed=1,
        source=source,
        source_event_time=None,
        ingested_at=datetime.strptime(ingested, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc),
    )


def _publish(tmp_path, rows):
    start = rows[0].open_time
    end = rows[-1].open_time + timedelta(minutes=1)
    return publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="synthetic")


def _final_winners(rows: list[CandleInputRow]) -> list[CandleInputRow]:
    """Same rule as ReplacingMergeTree(ingested_at): one row, greatest ingested_at."""
    best: dict[tuple, CandleInputRow] = {}
    for row in rows:
        prev = best.get(row.key())
        if prev is None or row.ingested_at > prev.ingested_at:
            best[row.key()] = row
    return [best[k] for k in sorted(best)]


def test_correction_during_two_chunk_capture_is_detected_when_keys_overlap(tmp_path):
    chunk_a = [_row(0), _row(1, price="1.10000000", ingested="2026-06-01T01:00:00.000Z")]
    chunk_b = [_row(1, price="1.20000000", ingested="2026-06-01T01:00:05.000Z"), _row(2)]
    with pytest.raises(SnapshotError, match="duplicate"):
        _publish(tmp_path, chunk_a + chunk_b)


def test_final_collapse_hides_a_correction(tmp_path):
    older = _row(0, price="1.10000000", ingested="2026-06-01T01:00:00.000Z")
    newer = _row(0, price="1.20000000", ingested="2026-06-01T01:00:05.000Z")
    winners = _final_winners([older, newer, _row(1)])
    manifest = _publish(tmp_path, winners)
    loaded = load_snapshot(tmp_path, manifest["snapshot_id"])
    assert loaded[0].close == Decimal("1.20000000")
    assert all(r.close != Decimal("1.10000000") for r in loaded)


def test_two_revisions_of_same_minute_fail_closed_before_publish(tmp_path):
    with pytest.raises(SnapshotError, match="duplicate"):
        _publish(
            tmp_path,
            [
                _row(0, price="1.00000000", ingested="2026-06-01T01:00:00.000Z"),
                _row(0, price="1.50000000", ingested="2026-06-01T01:00:09.000Z"),
            ],
        )


def test_mixed_generations_with_unique_keys_are_not_detected(tmp_path):
    rows = [
        _row(0, price="1.00000000", ingested="2026-06-01T01:00:00.000Z", source="bybit_live"),
        _row(1, price="2.00000000", ingested="2026-06-01T02:00:00.000Z", source="bybit_history"),
    ]
    manifest = _publish(tmp_path, rows)
    assert manifest["snapshot_completeness_status"] == "COMPLETE"
    assert manifest["row_count"] == 2


def test_missing_minute_fail_closed(tmp_path):
    with pytest.raises(SnapshotError, match="missing"):
        _publish(tmp_path, [_row(0), _row(2)])


def test_same_prices_different_revision_metadata_change_identity(tmp_path):
    prices = [_row(i, ingested="2026-06-01T01:00:00.000Z") for i in range(3)]
    revised = [_row(i, ingested="2026-06-01T03:00:00.000Z", source="bybit_history") for i in range(3)]
    a = _publish(tmp_path / "a", prices)
    b = _publish(tmp_path / "b", revised)
    assert a["ohlcv_content_hash"] == b["ohlcv_content_hash"]
    assert a["revision_metadata_hash"] != b["revision_metadata_hash"]
    assert a["snapshot_id"] != b["snapshot_id"]
    assert ohlcv_content_hash(prices) == ohlcv_content_hash(revised)
    assert revision_metadata_hash(prices) != revision_metadata_hash(revised)


def test_different_prices_same_open_time_are_rejected(tmp_path):
    with pytest.raises(SnapshotError, match="duplicate"):
        _publish(
            tmp_path,
            [
                _row(0, price="1.00000000"),
                _row(0, price="9.00000000", ingested="2026-06-01T01:00:08.000Z"),
                _row(1),
            ],
        )


def test_published_file_unchanged_when_another_version_is_written(tmp_path):
    original = [_row(i) for i in range(4)]
    first = _publish(tmp_path / "a", original)
    _publish(tmp_path / "b", [_row(i, price="3.00000000", ingested="2026-06-01T04:00:00.000Z") for i in range(4)])
    again = load_snapshot(tmp_path / "a", first["snapshot_id"])
    assert [r.close for r in again] == [r.close for r in original]
    assert revision_metadata_hash(again) == first["revision_metadata_hash"]
