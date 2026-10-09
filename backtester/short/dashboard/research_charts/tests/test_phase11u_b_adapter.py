"""Isolated Phase 11U-B adapter tests. Synthetic fixtures plus three stored oracles."""

from __future__ import annotations

import json
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
    AMBIGUOUS_PUBLICATION,
    INSUFFICIENT_CANDLE_COVERAGE,
    INPUT_IDENTITY_MISMATCH,
    INVALID_REQUEST,
    MISSING_CATALOG,
    MISSING_IMMUTABLE_INPUT,
    MISSING_PUBLICATION,
    READY,
    AdapterStop,
    CandleCache,
    CatalogRow,
    ClickHousePublicationSource,
    MemberRow,
    PublicationHit,
    candle_window_status,
    load_historical_remaining,
    project_active_pools,
)
from research_charts.trp_import import load_trp

ROOT = Path(__file__).resolve().parents[1]
ORACLE_DIR = ROOT / "live_micro_lld_v2_ch_test" / "phase11t_heatmap" / "payloads"
RUNS = ROOT / "live_micro_lld_v2_ch_test" / "research_runs"
SNAP = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
PRICE = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
REV = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
ORACLES = (
    "2026-09-24T12:00:00Z",
    "2026-09-28T12:00:00Z",
    "2026-09-30T12:00:00Z",
)


class MemorySource:
    def __init__(self, hits, members, catalog):
        self.hits = list(hits)
        self._members = list(members)
        self._catalog = list(catalog)
        self.member_calls = []

    def find(self, *, symbol, timeframe, pool_detail, decision_close):
        return list(self.hits)

    def members(self, snapshot_id):
        self.member_calls.append(snapshot_id)
        return list(self._members)

    def catalog(self, snapshot_id):
        return list(self._catalog)


def _dt(i: int) -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=i)


def _row(i: int, price: str = "1.00000000") -> CandleInputRow:
    start = _dt(i)
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
        ingested_at=datetime(2026, 6, 1, 2, tzinfo=timezone.utc),
    )


def _publish_minutes(tmp_path: Path, n: int = 20):
    rows = [_row(i) for i in range(n)]
    manifest = publish_snapshot(
        rows,
        tmp_path,
        range_start=rows[0].open_time,
        range_end_exclusive=rows[-1].open_time + timedelta(minutes=1),
        source_table="synthetic",
    )
    return manifest


def _pool(side: str, bottom: float, top: float, known: datetime, pool_id: str = "p1") -> CatalogRow:
    return CatalogRow(
        pool_id=pool_id,
        side=side,
        pool_type="normal",
        top=top,
        bottom=bottom,
        strength=5.0,
        strength_class="Strong",
        known_at=known,
    )


def _hit(snapshot_id: str = "pub-1", line: str = "phase10", fingerprint: str | None = None) -> PublicationHit:
    return PublicationHit(
        snapshot_id=snapshot_id,
        pipeline_line_id=line,
        active_state_fingerprint=fingerprint or active_state_fingerprint(["p1"], ["upper"]),
        config_hash="cfg",
        publish_complete=1,
    )


def _candles(n_bars: int, highs: list[float], lows: list[float]):
    Candle = load_trp()["Candle"]
    out = []
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(n_bars):
        mid = (highs[i] + lows[i]) / 2.0
        out.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * i),
                open=mid,
                high=highs[i],
                low=lows[i],
                close=mid,
                volume=1.0,
                symbol="DOGEUSDT",
                timeframe="5m",
            )
        )
    return out


def test_upper_partial_keeps_fixed_segments_and_color():
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("upper", 1.0, 2.0, known)
    candles = _candles(3, [1.0, 1.5, 9.0], [1.0, 1.0, 1.0])
    dc = datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc)
    out = project_active_pools([pool], candles, decision_close=dc, timeframe="5m")
    assert out["active"][0]["top"] == 2.0
    assert out["active"][0]["bottom"] == 1.0
    assert out["active"][0]["strength"] == 5.0
    assert out["active"][0]["color"] == "#00E85A"
    assert out["fully_traversed_active_ids"] == []
    assert out["remaining"]
    assert min(seg["bottom"] for seg in out["remaining"]) == pytest.approx(1.5)
    assert max(seg["top"] for seg in out["remaining"]) == pytest.approx(2.0)


def test_lower_partial_and_future_candle_is_ignored():
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("lower", 1.0, 2.0, known, pool_id="low")
    candles = _candles(4, [2.0, 2.0, 2.0, 2.0], [2.0, 1.5, 0.1, 0.1])
    dc = datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc)
    out = project_active_pools([pool], candles, decision_close=dc, timeframe="5m")
    assert out["fully_traversed_active_ids"] == []
    assert max(seg["top"] for seg in out["remaining"]) == pytest.approx(1.5)
    assert min(seg["bottom"] for seg in out["remaining"]) == pytest.approx(1.0)


def test_full_traversal_stays_active_but_has_no_remaining_segments():
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("upper", 1.0, 2.0, known)
    candles = _candles(3, [1.0, 2.5, 1.0], [1.0, 1.0, 1.0])
    dc = datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc)
    out = project_active_pools([pool], candles, decision_close=dc, timeframe="5m")
    assert out["active"][0]["pool_id"] == "p1"
    assert out["fully_traversed_active_ids"] == ["p1"]
    assert out["remaining"] == []


def test_missing_publication_catalog_and_ambiguous(tmp_path):
    manifest = _publish_minutes(tmp_path)
    dc = datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc)
    common = dict(
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=dc,
        immutable_candle_snapshot_id=manifest["snapshot_id"],
        price_content_hash=manifest["ohlcv_content_hash"],
        revision_metadata_hash=manifest["revision_metadata_hash"],
        snapshot_root=tmp_path,
    )
    with pytest.raises(AdapterStop) as missing:
        load_historical_remaining(MemorySource([], [], []), **common)
    assert missing.value.status == MISSING_PUBLICATION
    member = MemberRow("p1", "upper")
    with pytest.raises(AdapterStop) as catalog:
        load_historical_remaining(
            MemorySource([_hit()], [member], []),
            **common,
        )
    assert catalog.value.status == MISSING_CATALOG
    with pytest.raises(AdapterStop) as ambiguous:
        load_historical_remaining(
            MemorySource([_hit("a"), _hit("b")], [member], []),
            **common,
        )
    assert ambiguous.value.status == AMBIGUOUS_PUBLICATION


def test_cross_line_same_snapshot_is_unique(tmp_path):
    manifest = _publish_minutes(tmp_path)
    known = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)
    pool = _pool("upper", 0.5, 1.5, known)
    member = MemberRow("p1", "upper")
    hits = [_hit(line="phase9"), _hit(line="phase10")]
    out = load_historical_remaining(
        MemorySource(hits, [member], [pool]),
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
    assert out["pipeline_line_ids"] == ["phase10", "phase9"]
    assert out["active_count"] == 1
    assert out["active"][0]["bottom"] == 0.5
    assert out["active"][0]["strength"] == 5.0


def test_empty_active_membership_is_ready(tmp_path):
    manifest = _publish_minutes(tmp_path)
    empty = active_state_fingerprint([], [])
    hit = PublicationHit("pub-empty", "phase10", empty, "cfg", 1)
    out = load_historical_remaining(
        MemorySource([hit], [], []),
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
    assert out["active_count"] == 0
    assert out["remaining_segment_count"] == 0


def test_wrong_identity_and_coverage(tmp_path):
    manifest = _publish_minutes(tmp_path)
    member = MemberRow("p1", "upper")
    pool = _pool("upper", 0.5, 1.5, datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc))
    source = MemorySource([_hit()], [member], [pool])
    with pytest.raises(AdapterStop) as missing_file:
        load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
            immutable_candle_snapshot_id="0" * 64,
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash=manifest["revision_metadata_hash"],
            snapshot_root=tmp_path,
        )
    assert missing_file.value.status == MISSING_IMMUTABLE_INPUT
    with pytest.raises(AdapterStop) as bad_price:
        load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
            immutable_candle_snapshot_id=manifest["snapshot_id"],
            price_content_hash="a" * 64,
            revision_metadata_hash=manifest["revision_metadata_hash"],
            snapshot_root=tmp_path,
        )
    assert bad_price.value.status == INPUT_IDENTITY_MISMATCH
    with pytest.raises(AdapterStop) as bad_rev:
        load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
            immutable_candle_snapshot_id=manifest["snapshot_id"],
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash="b" * 64,
            snapshot_root=tmp_path,
        )
    assert bad_rev.value.status == INPUT_IDENTITY_MISMATCH
    with pytest.raises(AdapterStop) as short:
        load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
            immutable_candle_snapshot_id=manifest["snapshot_id"],
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash=manifest["revision_metadata_hash"],
            snapshot_root=tmp_path,
        )
    assert short.value.status == INSUFFICIENT_CANDLE_COVERAGE


def test_cache_hit_and_identity_miss(tmp_path):
    manifest = _publish_minutes(tmp_path)
    member = MemberRow("p1", "upper")
    pool = _pool("upper", 0.5, 1.5, datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc))
    source = MemorySource([_hit()], [member], [pool])
    cache = CandleCache(max_entries=2)
    kwargs = dict(
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
        immutable_candle_snapshot_id=manifest["snapshot_id"],
        price_content_hash=manifest["ohlcv_content_hash"],
        revision_metadata_hash=manifest["revision_metadata_hash"],
        snapshot_root=tmp_path,
        cache=cache,
    )
    first = load_historical_remaining(source, **kwargs)
    second = load_historical_remaining(source, **kwargs)
    assert first["decision_close"] == second["decision_close"]
    assert cache.loads == 1
    assert cache.hits == 1
    other = publish_snapshot(
        [_row(i, price="2.00000000") for i in range(20)],
        tmp_path / "other",
        range_start=_dt(0),
        range_end_exclusive=_dt(20),
        source_table="synthetic-other",
    )
    load_historical_remaining(
        source,
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=10,
        decision_close=datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc),
        immutable_candle_snapshot_id=other["snapshot_id"],
        price_content_hash=other["ohlcv_content_hash"],
        revision_metadata_hash=other["revision_metadata_hash"],
        snapshot_root=tmp_path / "other",
        cache=cache,
    )
    assert cache.loads == 2
    assert cache.hits == 1


def test_invalid_request_and_coverage_helper():
    with pytest.raises(AdapterStop) as bad:
        load_historical_remaining(
            MemorySource([], [], []),
            symbol="DOGEUSDT",
            timeframe="2m",
            pool_detail=10,
            decision_close=datetime(2026, 6, 1, tzinfo=timezone.utc),
            immutable_candle_snapshot_id="x",
            price_content_hash="y",
            revision_metadata_hash="z",
            snapshot_root=Path("."),
        )
    assert bad.value.status == INVALID_REQUEST
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    status = candle_window_status(
        range_start=start,
        range_end_exclusive=start + timedelta(minutes=20),
        decision_close=start + timedelta(minutes=10),
        known_ats=[start + timedelta(minutes=5)],
        timeframe="5m",
    )
    assert status == READY


def test_phase11t_oracle_regression():
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    source = ClickHousePublicationSource(client, cfg.database)
    cache = CandleCache(max_entries=2)
    try:
        for iso in ORACLES:
            dc = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            got = load_historical_remaining(
                source,
                symbol="DOGEUSDT",
                timeframe="5m",
                pool_detail=10,
                decision_close=dc,
                immutable_candle_snapshot_id=SNAP,
                price_content_hash=PRICE,
                revision_metadata_hash=REV,
                snapshot_root=RUNS,
                cache=cache,
            )
            ref = json.loads((ORACLE_DIR / f"{dc.strftime('%Y%m%dT%H%M%SZ')}.json").read_text(encoding="utf-8"))
            assert got["coverage"] == READY
            assert got["immutable_candle_snapshot_id"] == ref["snapshot_id"]
            assert {row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in got["active"]} == {
                row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in ref["original"]
            }
            assert got["remaining_segment_count"] == len(ref["remaining"])
            assert "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b" in got["pipeline_line_ids"]
            if iso == "2026-09-24T12:00:00Z":
                assert "phase9_DOGEUSDT_5m_PD10_ROLLING4M_full_e4b5552e" in got["pipeline_line_ids"]
    finally:
        client.close()
    assert cache.loads == 1
    assert cache.hits == 2
