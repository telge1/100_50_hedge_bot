"""Synthetic viewer-provider checks. No ClickHouse candle reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from data.models import Candle
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import CandleInputRow, publish_snapshot
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import CatalogRow, PublicationHit, resolve_publication
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.multi_file import FilePart, MultiFileInputError, load_composite
from research_charts.live_micro_lld_v2_ch_test.phase11u_f.provider import (
    CHART_BARS,
    PayloadCache,
    ProviderError,
    assert_supported,
    build_chart_payload,
    decision_grid,
    load_cases,
    neighbor,
    parse_close,
    payload_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import STRENGTH_CLASS_COLORS


def _open(minute: int) -> datetime:
    return datetime(2026, 9, 24, 12, minute, tzinfo=timezone.utc) - timedelta(minutes=5)


def _candle(minute: int, high: float, low: float) -> Candle:
    return Candle(
        timestamp=_open(minute),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=1.0,
        symbol="DOGEUSDT",
        timeframe="5m",
    )


def _pool(pool_id: str, side: str, bottom: float, top: float, strength_class: str = "Medium") -> CatalogRow:
    return CatalogRow(
        pool_id=pool_id,
        side=side,
        pool_type="range",
        top=top,
        bottom=bottom,
        strength=3.0,
        strength_class=strength_class,
        known_at=_open(0),
    )


def _payload(pools, candles, minute: int = 10, focus: str | None = None):
    return build_chart_payload(
        decision_close=datetime(2026, 9, 24, 12, minute, tzinfo=timezone.utc),
        snapshot_id="snap",
        composite_id="composite",
        pipeline_line_ids=["phase9_line", "phase10_line"],
        pools=pools,
        candles=candles,
        focus_pool_id=focus,
    )


def test_decision_grid_has_2016_closes_and_rejects_invalid_input():
    grid = decision_grid()
    assert len(grid) == 2016
    assert grid[0] == datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert grid[-1] == datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
    assert parse_close("2026-09-30T12:05:00Z") == grid[grid.index(parse_close("2026-09-30T12:05:00Z"))]
    with pytest.raises(ProviderError) as naive:
        parse_close("2026-09-30T12:05:00")
    assert naive.value.status == "INVALID_REQUEST"
    with pytest.raises(ProviderError):
        parse_close("2026-09-30T12:03:00Z")
    with pytest.raises(ProviderError):
        assert_supported("BTCUSDT", "5m", 10)


def test_navigation_membership_and_cross_line():
    closes = [item.strftime("%Y-%m-%dT%H:%M:%SZ") for item in decision_grid()]
    assert neighbor(closes, "2026-09-24T00:00:00Z", 1) == "2026-09-24T00:05:00Z"
    assert neighbor(closes, "2026-09-30T23:55:00Z", -1) == "2026-09-30T23:50:00Z"
    with pytest.raises(ProviderError):
        neighbor(closes, "2026-09-23T23:55:00Z", 1)
    snapshot_id, _fp, lines = resolve_publication(
        [
            PublicationHit("snap", "phase9_line", "fp", "cfg", 1),
            PublicationHit("snap", "phase10_line", "fp", "cfg", 1),
        ]
    )
    assert snapshot_id == "snap"
    assert set(lines) == {"phase9_line", "phase10_line"}


def test_partial_full_colors_and_layers():
    candles = [_candle(0, 1.2, 0.8), _candle(5, 1.4, 0.55), _candle(10, 2.2, 0.2)]
    upper = _pool("u", "upper", 1.0, 2.0, "Strong")
    lower = _pool("l", "lower", 0.4, 0.9, "Weak")
    partial = _payload([upper, lower], candles[:2], 5)
    assert any(row["pool_id"] == "u" and row["bottom"] < row["top"] for row in partial["original"])
    upper_boxes = [row for row in partial["remaining"] if row["pool_id"] == "u"]
    lower_boxes = [row for row in partial["remaining"] if row["pool_id"] == "l"]
    assert upper_boxes and min(row["bottom"] for row in upper_boxes) == pytest.approx(1.4)
    assert lower_boxes and max(row["top"] for row in lower_boxes) == pytest.approx(0.55)
    assert {row["color"] for row in partial["remaining"]} <= set(STRENGTH_CLASS_COLORS.values())
    full = _payload([upper, lower], candles, 10)
    assert "u" in full["fully_traversed_active_ids"]
    assert "l" in full["fully_traversed_active_ids"]
    assert all(row["pool_id"] not in {"u", "l"} for row in full["remaining"])
    assert {row["pool_id"] for row in full["original"]} == {"u", "l"}
    assert all(row["bottom"] >= 1.0 - 1e-9 and row["top"] <= 2.0 + 1e-9 for row in upper_boxes)
    assert len(full["candles"]) <= CHART_BARS
    assert full["candles"][-1]["time"] + 300 == int(datetime(2026, 9, 24, 12, 10, tzinfo=timezone.utc).timestamp())
    assert payload_fingerprint(full) == payload_fingerprint(full)


def test_cache_isolation_and_no_candle_table_fallback(tmp_path: Path):
    cache = PayloadCache(max_entries=1)
    cache.put(("one", "2026-09-24T00:00:00Z", None), {"n": 1})
    assert cache.get(("one", "2026-09-24T00:00:00Z", None))["n"] == 1
    cache.put(("two", "2026-09-24T00:00:00Z", None), {"n": 2})
    assert cache.get(("one", "2026-09-24T00:00:00Z", None)) is None
    assert cache.hits == 1 and cache.misses == 1
    source = Path(__file__).resolve().parents[1].joinpath(
        "live_micro_lld_v2_ch_test/phase11u_f/provider.py"
    ).read_text(encoding="utf-8")
    assert "candles_1m" not in source
    assert "INSERT" not in source

    def row(i: int) -> CandleInputRow:
        start = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
        return CandleInputRow(
            exchange="bybit", symbol="DOGEUSDT", interval="1m", open_time=start,
            close_time=start + timedelta(minutes=1), open=Decimal("1"), high=Decimal("1.2"),
            low=Decimal("0.9"), close=Decimal("1"), volume=Decimal("1"), turnover=Decimal("1"),
            is_closed=1, source="test", source_event_time=None,
            ingested_at=datetime(2026, 6, 1, 2, tzinfo=timezone.utc),
        )

    manifest = publish_snapshot(
        rows=[row(i) for i in range(3)],
        directory=tmp_path / "a",
        range_start=row(0).open_time,
        range_end_exclusive=row(2).open_time + timedelta(minutes=1),
        source_table="synthetic",
    )
    with pytest.raises(MultiFileInputError) as missing:
        load_composite([FilePart(tmp_path / "missing", "absent", "0" * 64, "0" * 64)])
    assert missing.value.status == "MISSING_IMMUTABLE_INPUT"
    with pytest.raises(MultiFileInputError) as bad_hash:
        load_composite([FilePart(tmp_path / "a", manifest["snapshot_id"], "0" * 64, manifest["revision_metadata_hash"])])
    assert bad_hash.value.status == "INPUT_IDENTITY_MISMATCH"


def test_stored_cases_are_the_ten_real_observations():
    cases = load_cases()
    assert len(cases) == 10
    assert any(row["side"] == "upper" for row in cases)
    assert any(row["side"] == "lower" for row in cases)
    assert all(row["original_active"] is True and row["remaining_visibility"] == "NONE" for row in cases)
