"""Synthetic Phase 11U-E checks. Historical files are not rewritten."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from data.models import Candle
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    CatalogRow,
    MemberRow,
    PublicationHit,
    check_membership,
    project_active_pools,
    resolve_publication,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.multi_file import (
    CompositeCandleCache,
    FilePart,
    MultiFileInputError,
    load_composite,
)
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import CandleInputRow, publish_snapshot
from research_charts.live_micro_lld_v2_ch_test.phase11u_e.timeline import (
    TimelineError,
    TimelineResult,
    index_from_candles,
    observe_close,
    project_close,
    removal_classification,
)


def _close(minute: int) -> datetime:
    return datetime(2026, 9, 30, 12, minute, tzinfo=timezone.utc)


def _candle(minute: int, high: float, low: float) -> Candle:
    return Candle(
        timestamp=_close(minute) - timedelta(minutes=5),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=1.0,
        symbol="DOGEUSDT",
        timeframe="5m",
    )


def _pool(pool_id: str, side: str, bottom: float, top: float, known_minute: int = 0, strength: float = 2.0) -> CatalogRow:
    return CatalogRow(
        pool_id=pool_id,
        side=side,
        pool_type="range",
        top=top,
        bottom=bottom,
        strength=strength,
        strength_class="Medium",
        known_at=_close(known_minute),
    )


def _obs(index, pools, minute: int):
    return project_close(index, pools, _close(minute))


def test_chronological_processing_and_causal_wick():
    candles = [_candle(0, 1.2, 0.8), _candle(5, 1.4, 0.7), _candle(10, 9.0, 0.1)]
    index = index_from_candles(candles, "5m")
    upper = _pool("u", "upper", 1.0, 2.0)
    early = _obs(index, [upper], 5)[0]
    later = _obs(index, [upper], 10)[0]
    assert early.remaining_bottom == pytest.approx(1.4)
    assert later.fully_traversed
    book = TimelineResult()
    observe_close(book, _close(5), [early])
    observe_close(book, _close(10), [later])
    assert book.processed == 2


def test_reject_unsorted_closes():
    book = TimelineResult()
    index = index_from_candles([_candle(0, 1.0, 0.5), _candle(5, 1.0, 0.5)], "5m")
    first = _obs(index, [_pool("u", "upper", 2.0, 3.0)], 0)
    second = _obs(index, [_pool("u", "upper", 2.0, 3.0)], 5)
    observe_close(book, _close(5), second)
    with pytest.raises(TimelineError) as unsorted:
        observe_close(book, _close(0), first)
    assert unsorted.value.status == "INVALID_REQUEST"


def test_membership_bounds_strength_and_cross_line():
    hits = [
        PublicationHit("snap", "phase9_line", "fp", "cfg", 1),
        PublicationHit("snap", "phase10_line", "fp", "cfg", 1),
    ]
    snapshot_id, fingerprint, lines = resolve_publication(hits)
    assert snapshot_id == "snap"
    assert set(lines) == {"phase9_line", "phase10_line"}
    members = [MemberRow("p", "upper")]
    catalog = [_pool("p", "upper", 1.0, 2.0, strength=4.0)]
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint

    real_fp = active_state_fingerprint(["p"], ["upper"])
    check_membership("snap", real_fp, members, catalog)
    assert fingerprint == "fp"
    assert catalog[0].bottom == 1.0 and catalog[0].strength == 4.0


def test_upper_and_lower_monotonicity():
    candles = [_candle(0, 1.2, 0.8), _candle(5, 1.5, 0.6)]
    index = index_from_candles(candles, "5m")
    upper = _pool("u", "upper", 1.0, 3.0)
    lower = _pool("l", "lower", 0.2, 1.0)
    book = TimelineResult()
    observe_close(book, _close(0), _obs(index, [upper, lower], 0))
    observe_close(book, _close(5), _obs(index, [upper, lower], 5))
    assert book.upper_monotonic_failures == 0
    assert book.lower_monotonic_failures == 0
    assert book._active["u"].remaining_bottom >= 1.2
    assert book._active["l"].remaining_top <= 0.8


def test_monotonicity_detects_reexpansion():
    book = TimelineResult()
    known = _pool("u", "upper", 1.0, 2.0, known_minute=0)
    known = CatalogRow(
        pool_id=known.pool_id,
        side=known.side,
        pool_type=known.pool_type,
        top=known.top,
        bottom=known.bottom,
        strength=known.strength,
        strength_class=known.strength_class,
        known_at=_close(0) - timedelta(minutes=5),
    )
    first = _obs(index_from_candles([_candle(0, 1.5, 0.5)], "5m"), [known], 0)
    widened = _obs(index_from_candles([_candle(5, 1.1, 0.9)], "5m"), [known], 5)
    observe_close(book, _close(0), first)
    observe_close(book, _close(5), widened)
    assert book.upper_monotonic_failures == 1


def test_full_traversal_fixed_segments_and_reappearance():
    candles = [_candle(0, 1.2, 0.9), _candle(5, 3.0, 0.1), _candle(10, 1.0, 0.9), _candle(15, 1.0, 0.9)]
    index = index_from_candles(candles, "5m")
    pool = _pool("u", "upper", 1.0, 2.0)
    book = TimelineResult()
    observe_close(book, _close(0), _obs(index, [pool], 0))
    observe_close(book, _close(5), _obs(index, [pool], 5))
    assert book.full_still_active == 1
    assert book._active["u"].fixed_edges == (1.0, 2.0)
    observe_close(book, _close(10), [])
    assert book.active_to_inactive == 1
    assert book.full_then_inactive == 1
    assert removal_classification(book) == "UNCLASSIFIED"
    observe_close(book, _close(15), _obs(index, [pool], 15))
    assert book.reappearance == 1
    assert book.upper_monotonic_failures == 0


def test_missing_candle_and_future_known_at_fail_closed():
    index = index_from_candles([_candle(0, 1.0, 0.5)], "5m")
    with pytest.raises(TimelineError) as missing:
        project_close(index, [_pool("u", "upper", 1.0, 2.0)], _close(5))
    assert missing.value.status == "INSUFFICIENT_CANDLE_COVERAGE"
    with pytest.raises(TimelineError) as future:
        project_close(index, [_pool("u", "upper", 1.0, 2.0, known_minute=5)], _close(0))
    assert future.value.status == "HISTORICAL_CAUSALITY"


def test_oracle_matches_adapter_projection():
    candles = [_candle(0, 1.3, 0.7), _candle(5, 1.6, 0.4)]
    index = index_from_candles(candles, "5m")
    pools = [_pool("u", "upper", 1.0, 2.0), _pool("l", "lower", 0.2, 0.8)]
    shared = project_close(index, pools, _close(5))
    adapter = project_active_pools(pools, candles, decision_close=_close(5), timeframe="5m")
    assert {row.pool_id for row in shared} == {row["pool_id"] for row in adapter["active"]}
    assert sum(row.visible_segments for row in shared) == len(adapter["remaining"])


def test_composite_identity_hash_and_cache(tmp_path: Path):
    def row(i: int, high: str = "1.20000000") -> CandleInputRow:
        start = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
        return CandleInputRow(
            exchange="bybit",
            symbol="DOGEUSDT",
            interval="1m",
            open_time=start,
            close_time=start + timedelta(minutes=1),
            open=Decimal("1"),
            high=Decimal(high),
            low=Decimal("0.9"),
            close=Decimal("1"),
            volume=Decimal("1"),
            turnover=Decimal("1"),
            is_closed=1,
            source="test",
            source_event_time=None,
            ingested_at=datetime(2026, 6, 1, 2, tzinfo=timezone.utc),
        )

    left = publish_snapshot(rows=[row(i) for i in range(3)], directory=tmp_path / "a", range_start=row(0).open_time, range_end_exclusive=row(2).open_time + timedelta(minutes=1), source_table="synthetic")
    right = publish_snapshot(rows=[row(i, "1.40000000") for i in range(3, 6)], directory=tmp_path / "b", range_start=row(3).open_time, range_end_exclusive=row(5).open_time + timedelta(minutes=1), source_table="synthetic")
    parts = [
        FilePart(tmp_path / "a", left["snapshot_id"], left["ohlcv_content_hash"], left["revision_metadata_hash"]),
        FilePart(tmp_path / "b", right["snapshot_id"], right["ohlcv_content_hash"], right["revision_metadata_hash"]),
    ]
    cache = CompositeCandleCache()
    first = cache.get(parts, "5m")
    second = cache.get(list(reversed(parts)), "5m")
    assert first.composite_id == second.composite_id
    assert cache.loads == 1 and cache.hits == 1
    assert first.clickhouse_candle_reads == 0
    bad = [FilePart(tmp_path / "a", left["snapshot_id"], "0" * 64, left["revision_metadata_hash"]), parts[1]]
    with pytest.raises(MultiFileInputError) as mismatch:
        load_composite(bad)
    assert mismatch.value.status == "INPUT_IDENTITY_MISMATCH"
    with pytest.raises(MultiFileInputError) as missing_file:
        load_composite([FilePart(tmp_path / "missing", "absent", "0" * 64, "0" * 64)])
    assert missing_file.value.status == "MISSING_IMMUTABLE_INPUT"


def test_timeline_module_has_no_clickhouse_candle_fallback():
    source = Path(__file__).resolve().parents[1].joinpath(
        "live_micro_lld_v2_ch_test/phase11u_e/timeline.py"
    ).read_text(encoding="utf-8")
    assert "clickhouse_connect" not in source
    assert "candles_1m" not in source
