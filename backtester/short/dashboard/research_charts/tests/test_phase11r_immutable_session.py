"""Phase 11R negative tests for the immutable session adapter. No ClickHouse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    publish_snapshot,
    reject_mixed,
)
from research_charts.live_micro_lld_v2_ch_test.immutable_snapshot_candle_session import (
    LEGACY_ENGINE_INPUT_HASH_NOTE,
    ImmutableInputCoverageError,
    ImmutableSnapshotCandleSession,
    content_based_input_id,
)


def _row(i: int, price: str = "1.00000000") -> CandleInputRow:
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
        source="test",
        source_event_time=None,
        ingested_at=datetime(2026, 6, 1, 1, tzinfo=timezone.utc),
    )


def _publish(tmp_path, n=10, price="1.00000000"):
    rows = [_row(i, price=price) for i in range(n)]
    start = rows[0].open_time
    end = rows[-1].open_time + timedelta(minutes=1)
    manifest = publish_snapshot(rows, tmp_path, range_start=start, range_end_exclusive=end, source_table="t")
    return rows, manifest


def test_legacy_hash_is_documented():
    assert "first/last close" in LEGACY_ENGINE_INPUT_HASH_NOTE
    a = content_based_input_id(
        snapshot_id="s",
        price_content_hash="p",
        revision_metadata_hash="r",
        config_hash="c",
        history_policy="ROLLING_4_CALENDAR_MONTHS",
        decision_close=datetime(2026, 9, 24, 12, tzinfo=timezone.utc),
    )
    b = content_based_input_id(
        snapshot_id="s",
        price_content_hash="p2",
        revision_metadata_hash="r",
        config_hash="c",
        history_policy="ROLLING_4_CALENDAR_MONTHS",
        decision_close=datetime(2026, 9, 24, 12, tzinfo=timezone.utc),
    )
    assert a != b
    assert a == content_based_input_id(
        snapshot_id="s",
        price_content_hash="p",
        revision_metadata_hash="r",
        config_hash="c",
        history_policy="ROLLING_4_CALENDAR_MONTHS",
        decision_close=datetime(2026, 9, 24, 12, tzinfo=timezone.utc),
    )


def test_session_reads_snapshot_and_refuses_fallback(tmp_path):
    _rows, manifest = _publish(tmp_path)
    session = ImmutableSnapshotCandleSession(
        tmp_path, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
    )
    start = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    session._fetch_1m_range(start, start + 4 * 60)
    assert session.stats.ch_fetch_calls == 0
    assert len(session._1m_by_unix) == 5
    with pytest.raises(ImmutableInputCoverageError):
        session._source.get_1m_candles("DOGEUSDT")


def test_coverage_and_missing_minute(tmp_path):
    _rows, manifest = _publish(tmp_path)
    session = ImmutableSnapshotCandleSession(
        tmp_path, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
    )
    start = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ImmutableInputCoverageError):
        session._fetch_1m_range(start - 60, start)
    session._snap_rows.pop(start + 120)
    with pytest.raises(ImmutableInputCoverageError, match="missing"):
        session._fetch_1m_range(start, start + 180)


def test_wrong_hash_and_mixed_revision(tmp_path):
    _rows, manifest = _publish(tmp_path)
    with pytest.raises(Exception):
        ImmutableSnapshotCandleSession(tmp_path, manifest["snapshot_id"], ohlcv_hash="0" * 64)
    _b, other = _publish(tmp_path / "b", price="2.00000000")
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import load_snapshot

    a = load_snapshot(tmp_path, manifest["snapshot_id"])
    b = load_snapshot(tmp_path / "b", other["snapshot_id"])
    with pytest.raises(Exception, match="mixed"):
        reject_mixed(a, b)
    again = load_snapshot(tmp_path, manifest["snapshot_id"])
    assert again[0].close == Decimal("1.00000000")
