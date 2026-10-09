"""Phase 11U-C coverage plan and synthetic adapter checks. No ClickHouse writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    publish_snapshot,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    INSUFFICIENT_CANDLE_COVERAGE,
    MISSING_PUBLICATION,
    READY,
    AdapterStop,
    CandleCache,
    CatalogRow,
    MemberRow,
    PublicationHit,
    load_historical_remaining,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_c.coverage import (
    BASE_RANGE_END_EXCLUSIVE,
    BASE_RANGE_START,
    MISSING_FIRST,
    MISSING_LAST,
    READY_LAST,
    base_file_status,
    independent_parity_status,
    preset_warmup_bars,
    support_matrix,
    tail_extension_plan,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import STRENGTH_CLASS_COLORS
from indicators.live_micro_lld_v2.presets import preset_for_pool_detail


class MemorySource:
    def __init__(self, hits, members, catalog):
        self.hits = list(hits)
        self._members = list(members)
        self._catalog = list(catalog)

    def find(self, *, symbol, timeframe, pool_detail, decision_close):
        return list(self.hits)

    def members(self, snapshot_id):
        return list(self._members)

    def catalog(self, snapshot_id):
        return list(self._catalog)


def _dt(i: int) -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)


def _row(i: int, high: str = "1.20000000", low: str = "0.80000000") -> CandleInputRow:
    start = _dt(i)
    return CandleInputRow(
        exchange="bybit",
        symbol="DOGEUSDT",
        interval="1m",
        open_time=start,
        close_time=start + timedelta(minutes=1),
        open=Decimal("1.00000000"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("1.00000000"),
        volume=Decimal("1.00000000"),
        turnover=Decimal("1.00000000"),
        is_closed=1,
        source="test",
        source_event_time=None,
        ingested_at=datetime(2026, 6, 1, 2, tzinfo=timezone.utc),
    )


def _publish(tmp_path: Path, n: int = 40, high: str = "1.20000000", low: str = "0.80000000"):
    rows = [_row(i, high=high, low=low) for i in range(n)]
    return publish_snapshot(
        rows,
        tmp_path,
        range_start=rows[0].open_time,
        range_end_exclusive=rows[-1].open_time + timedelta(minutes=1),
        source_table="phase11u-c",
    )


def _pool(side: str, bottom: float, top: float, known: datetime, pool_id: str = "p1") -> CatalogRow:
    return CatalogRow(pool_id, side, "normal", top, bottom, 5.0, "Strong", known)


def test_tail_plan_counts_and_keeps_the_base_file():
    plan = tail_extension_plan()
    assert plan["missing_close_count"] == 143
    assert plan["missing_close_first"] == "2026-09-30T12:05:00Z"
    assert plan["missing_close_last"] == "2026-09-30T23:55:00Z"
    assert plan["additional_1m_count"] == 714
    assert plan["additional_1m_first_open"] == "2026-09-30T12:01:00Z"
    assert plan["additional_1m_last_open"] == "2026-09-30T23:54:00Z"
    assert plan["contiguous_with_base"] is True
    assert plan["replace_base"] is False
    assert plan["base_file_unchanged"] is True
    assert base_file_status(MISSING_FIRST, "5m", 10) == "INSUFFICIENT_CANDLE_COVERAGE"
    assert base_file_status(READY_LAST, "5m", 10) == "READY"


def test_preset_selection_is_the_original_table():
    pd5 = preset_for_pool_detail(5)
    pd10 = preset_for_pool_detail(10)
    assert (pd5.swing_len, pd5.volume_lookback, pd5.min_strength) == (12, 2000, 0.0)
    assert (pd10.swing_len, pd10.volume_lookback, pd10.zone_extension) == (2, 300, 15)
    assert preset_warmup_bars(5) == 2000 + 12 + 64
    assert preset_warmup_bars(10) == 300 + 2 + 64
    assert STRENGTH_CLASS_COLORS["Weak"] == "#005BFF"
    assert STRENGTH_CLASS_COLORS["Extreme"] == "#F00032"


def test_reference_close_coverage_matrix_uses_full_warmup():
    by_key = {(row["timeframe"], row["pool_detail"]): row["base_file"] for row in support_matrix()}
    assert by_key[("5m", 10)] == "READY"
    assert by_key[("15m", 10)] == "READY"
    assert by_key[("5m", 5)] == "READY"
    assert by_key[("30m", 10)] == "INSUFFICIENT_CANDLE_COVERAGE"
    assert by_key[("1h", 10)] == "INSUFFICIENT_CANDLE_COVERAGE"
    assert by_key[("4h", 10)] == "INSUFFICIENT_CANDLE_COVERAGE"
    assert by_key[("5m", 1)] == "INSUFFICIENT_CANDLE_COVERAGE"
    assert BASE_RANGE_START < BASE_RANGE_END_EXCLUSIVE


def test_independent_parity_is_not_pass_without_one_publication():
    assert independent_parity_status(0) == "NOT_PROVEN"
    assert independent_parity_status(2) == "NOT_PROVEN"
    assert independent_parity_status(1) != "PASS"


def test_adapter_full_traversal_upper_and_lower(tmp_path):
    manifest = _publish(tmp_path, high="3.00000000", low="0.10000000")
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    upper = _pool("upper", 1.0, 2.0, known, "up")
    lower = _pool("lower", 1.0, 2.0, known, "lo")
    members = [MemberRow("up", "upper"), MemberRow("lo", "lower")]
    fingerprint = active_state_fingerprint(["up", "lo"], ["upper", "lower"])
    hit = PublicationHit("pub", "phase10", fingerprint, "cfg", 1)
    out = load_historical_remaining(
        MemorySource([hit], members, [upper, lower]),
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
        immutable_candle_snapshot_id=manifest["snapshot_id"],
        price_content_hash=manifest["ohlcv_content_hash"],
        revision_metadata_hash=manifest["revision_metadata_hash"],
        snapshot_root=tmp_path,
    )
    assert out["coverage"] == READY
    assert set(out["fully_traversed_active_ids"]) == {"up", "lo"}
    assert out["remaining_segment_count"] == 0
    assert out["active_count"] == 2
    assert {row["strength"] for row in out["active"]} == {5.0}


def test_reappearance_does_not_restore_a_traversed_pool(tmp_path):
    manifest = _publish(tmp_path, high="3.00000000", low="0.80000000")
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("upper", 1.0, 2.0, known)
    member = MemberRow("p1", "upper")
    hit = PublicationHit("pub", "line", active_state_fingerprint(["p1"], ["upper"]), "cfg", 1)
    empty = PublicationHit("gone", "line", active_state_fingerprint([], []), "cfg", 1)
    hidden = load_historical_remaining(
        MemorySource([empty], [], []),
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
        immutable_candle_snapshot_id=manifest["snapshot_id"],
        price_content_hash=manifest["ohlcv_content_hash"],
        revision_metadata_hash=manifest["revision_metadata_hash"],
        snapshot_root=tmp_path,
        cache=CandleCache(),
    )
    again = load_historical_remaining(
        MemorySource([hit], [member], [pool]),
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 15, tzinfo=timezone.utc),
        immutable_candle_snapshot_id=manifest["snapshot_id"],
        price_content_hash=manifest["ohlcv_content_hash"],
        revision_metadata_hash=manifest["revision_metadata_hash"],
        snapshot_root=tmp_path,
    )
    assert hidden["active_count"] == 0
    assert again["fully_traversed_active_ids"] == ["p1"]
    assert again["remaining_segment_count"] == 0


def test_twelve_synthetic_closes_ignore_later_wicks(tmp_path):
    rows = []
    for i in range(80):
        high = "9.00000000" if i >= 70 else "1.10000000"
        rows.append(_row(i, high=high, low="0.90000000"))
    manifest = publish_snapshot(
        rows,
        tmp_path,
        range_start=rows[0].open_time,
        range_end_exclusive=rows[-1].open_time + timedelta(minutes=1),
        source_table="phase11u-c-seq",
    )
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("upper", 1.0, 2.0, known)
    member = MemberRow("p1", "upper")
    hit = PublicationHit("pub", "line", active_state_fingerprint(["p1"], ["upper"]), "cfg", 1)
    source = MemorySource([hit], [member], [pool])
    cache = CandleCache()
    spans = []
    for step in range(12):
        dc = datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc) + timedelta(minutes=5 * step)
        out = load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=dc,
            immutable_candle_snapshot_id=manifest["snapshot_id"],
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash=manifest["revision_metadata_hash"],
            snapshot_root=tmp_path,
            cache=cache,
        )
        assert out["decision_close"] == dc.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert out["fully_traversed_active_ids"] == []
        span = max(seg["top"] for seg in out["remaining"]) - min(seg["bottom"] for seg in out["remaining"])
        spans.append(span)
    assert spans[0] == pytest.approx(spans[-1])
    assert cache.loads == 1
    assert cache.hits == 11


def test_missing_publication_stays_closed(tmp_path):
    manifest = _publish(tmp_path)
    with pytest.raises(AdapterStop) as caught:
        load_historical_remaining(
            MemorySource([], [], []),
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 0, 15, tzinfo=timezone.utc),
            immutable_candle_snapshot_id=manifest["snapshot_id"],
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash=manifest["revision_metadata_hash"],
            snapshot_root=tmp_path,
        )
    assert caught.value.status == MISSING_PUBLICATION
    assert base_file_status(datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc), "5m", 10) == (
        INSUFFICIENT_CANDLE_COVERAGE
    )
