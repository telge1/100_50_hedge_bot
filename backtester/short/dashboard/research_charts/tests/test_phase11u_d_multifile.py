"""Synthetic multi-file coverage. No ClickHouse and no Phase 11R rewrite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    file_checksum,
    publish_snapshot,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    AdapterStop,
    CatalogRow,
    MemberRow,
    PublicationHit,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.capture import publish_tail
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.coverage_check import classify_published_closes
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.multi_file import (
    CompositeCandleCache,
    FilePart,
    MultiFileInputError,
    composite_input_id,
    load_composite,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.wrapper import load_composite_remaining


def _row(i: int, high: str = "1.20000000", ingested: str = "2026-06-01T02:00:00Z") -> CandleInputRow:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return CandleInputRow(
        exchange="bybit",
        symbol="DOGEUSDT",
        interval="1m",
        open_time=start,
        close_time=start + timedelta(minutes=1),
        open=Decimal("1.00000000"),
        high=Decimal(high),
        low=Decimal("0.90000000"),
        close=Decimal("1.00000000"),
        volume=Decimal("1.00000000"),
        turnover=Decimal("1.00000000"),
        is_closed=1,
        source="test",
        source_event_time=None,
        ingested_at=datetime.strptime(ingested, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
    )


def _publish(directory: Path, start: int, end: int, high: str = "1.20000000", ingested: str = "2026-06-01T02:00:00Z"):
    rows = [_row(i, high=high, ingested=ingested) for i in range(start, end)]
    begin = rows[0].open_time
    return publish_snapshot(
        rows,
        directory,
        range_start=begin,
        range_end_exclusive=rows[-1].open_time + timedelta(minutes=1),
        source_table="synthetic",
    )


def _part(directory: Path, manifest: dict) -> FilePart:
    return FilePart(directory, manifest["snapshot_id"], manifest["ohlcv_content_hash"], manifest["revision_metadata_hash"])


class MemorySource:
    def __init__(self, hits, members, catalog):
        self._hits = list(hits)
        self._members = list(members)
        self._catalog = list(catalog)

    def find(self, *, symbol, timeframe, pool_detail, decision_close):
        return list(self._hits)

    def members(self, snapshot_id):
        return list(self._members)

    def catalog(self, snapshot_id):
        return list(self._catalog)


def test_contiguous_files_and_reversed_order_share_one_id(tmp_path):
    left = _publish(tmp_path / "a", 0, 5)
    right = _publish(tmp_path / "b", 5, 10)
    forward = load_composite([_part(tmp_path / "a", left), _part(tmp_path / "b", right)])
    backward = load_composite([_part(tmp_path / "b", right), _part(tmp_path / "a", left)])
    assert forward.composite_id == backward.composite_id
    assert forward.range_start == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert forward.range_end_exclusive == datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc)
    assert composite_input_id(list(forward.parts)) == forward.composite_id


def test_gap_overlap_and_conflicting_revision(tmp_path):
    left = _publish(tmp_path / "a", 0, 5)
    gapped = _publish(tmp_path / "gap", 6, 10)
    with pytest.raises(MultiFileInputError) as gap:
        load_composite([_part(tmp_path / "a", left), _part(tmp_path / "gap", gapped)])
    assert gap.value.status == "INSUFFICIENT_CANDLE_COVERAGE"
    overlap = _publish(tmp_path / "over", 4, 8, high="3.00000000", ingested="2026-06-01T03:00:00Z")
    with pytest.raises(MultiFileInputError) as clash:
        load_composite([_part(tmp_path / "a", left), _part(tmp_path / "over", overlap)])
    assert clash.value.status == "MULTI_FILE_INPUT_CONFLICT"


def test_identity_mismatches_fail_closed(tmp_path):
    left = _publish(tmp_path / "a", 0, 5)
    with pytest.raises(MultiFileInputError) as missing:
        load_composite([FilePart(tmp_path / "a", "0" * 64, left["ohlcv_content_hash"], left["revision_metadata_hash"])])
    assert missing.value.status == "MISSING_IMMUTABLE_INPUT"
    with pytest.raises(MultiFileInputError) as price:
        load_composite([FilePart(tmp_path / "a", left["snapshot_id"], "a" * 64, left["revision_metadata_hash"])])
    assert price.value.status == "INPUT_IDENTITY_MISMATCH"
    with pytest.raises(MultiFileInputError) as revision:
        load_composite([FilePart(tmp_path / "a", left["snapshot_id"], left["ohlcv_content_hash"], "b" * 64)])
    assert revision.value.status == "INPUT_IDENTITY_MISMATCH"


def test_boundary_projection_uses_both_files_and_ignores_the_later_bar(tmp_path):
    left_rows = [_row(0, high="1.10000000")]
    right_rows = [_row(i, high="1.40000000" if i < 5 else "9.00000000") for i in range(1, 10)]
    left = publish_snapshot(
        left_rows,
        tmp_path / "a",
        range_start=left_rows[0].open_time,
        range_end_exclusive=left_rows[-1].open_time + timedelta(minutes=1),
        source_table="synthetic",
    )
    right = publish_snapshot(
        right_rows,
        tmp_path / "b",
        range_start=right_rows[0].open_time,
        range_end_exclusive=right_rows[-1].open_time + timedelta(minutes=1),
        source_table="synthetic",
    )
    known = datetime(2026, 6, 1, tzinfo=timezone.utc)
    pool = CatalogRow("p1", "upper", "normal", 2.0, 1.0, 5.0, "Strong", known)
    member = MemberRow("p1", "upper")
    hit = PublicationHit("pub", "phase10", active_state_fingerprint(["p1"], ["upper"]), "cfg", 1)
    source = MemorySource([hit], [member], [pool])
    cache = CompositeCandleCache()
    parts = [_part(tmp_path / "a", left), _part(tmp_path / "b", right)]
    early = load_composite_remaining(
        source,
        parts,
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc),
        cache=cache,
    )
    later = load_composite_remaining(
        source,
        list(reversed(parts)),
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
        cache=cache,
    )
    assert early["fully_traversed_active_ids"] == []
    assert early["active"][0]["top"] == 2.0
    assert early["active"][0]["strength"] == 5.0
    assert min(seg["bottom"] for seg in early["remaining"]) == pytest.approx(1.4)
    assert max(seg["top"] for seg in early["remaining"]) == pytest.approx(2.0)
    assert later["fully_traversed_active_ids"] == ["p1"]
    assert early["composite_input_id"] == later["composite_input_id"]
    assert cache.loads == 1
    assert cache.hits == 1


def test_cache_misses_when_revision_changes(tmp_path):
    first = _publish(tmp_path / "a", 0, 5)
    other = _publish(tmp_path / "b", 0, 5, ingested="2026-06-01T04:00:00Z")
    cache = CompositeCandleCache()
    cache.get([_part(tmp_path / "a", first)], "5m")
    cache.get([_part(tmp_path / "b", other)], "5m")
    assert cache.loads == 2
    assert cache.hits == 0
    assert first["revision_metadata_hash"] != other["revision_metadata_hash"]


def test_coverage_predicate_and_base_directory_guard(tmp_path):
    start = datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
    close = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
    rows = [
        {
            "decision_close": close,
            "snapshot_ids": 1,
            "fingerprints": 1,
            "member_rows": 3,
            "catalog_rows": 3,
            "min_known_at": datetime(2026, 8, 1, 18, 55, tzinfo=timezone.utc),
        }
    ]
    assert classify_published_closes(rows, range_start=start, range_end_exclusive=end)["ready"] == 1
    short = classify_published_closes(rows, range_start=start, range_end_exclusive=datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc))
    assert short["failed"][0]["reasons"] == ["INSUFFICIENT_CANDLE_COVERAGE"]
    base = tmp_path / "base"
    base.mkdir()
    sentinel = base / "keep.parquet"
    sentinel.write_bytes(b"phase11r")
    before = file_checksum(sentinel)
    with pytest.raises(RuntimeError):
        publish_tail([], base, base_directory=base)
    assert file_checksum(sentinel) == before


def test_missing_publication_does_not_read_candles(tmp_path):
    left = _publish(tmp_path / "a", 0, 5)
    with pytest.raises(AdapterStop) as caught:
        load_composite_remaining(
            MemorySource([], [], []),
            [_part(tmp_path / "a", left)],
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc),
        )
    assert caught.value.status == "MISSING_PUBLICATION"
