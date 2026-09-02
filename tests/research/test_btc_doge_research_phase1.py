from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from research.btc_doge_research.batch_state import input_fingerprint
from research.btc_doge_research.clickhouse import (
    ensure_batch_available,
    ensure_source_file_available,
    validate_write_sql,
)
from research.btc_doge_research.config import BTC_WINDOW
from research.btc_doge_research.contracts import (
    FUNDING_STATUS,
    TARGET_DATABASE,
    parse_utc,
    sanitize_json,
    validate_symbol,
    validate_window,
)
from research.btc_doge_research.ddl import DDL
from research.btc_doge_research.liquidation_transform import (
    transform_liquidations,
)
from research.btc_doge_research.market_aggregation import build_market_seconds
from research.btc_doge_research.ob200_parser import OB200SegmentReader
from research.btc_doge_research.ob200_storage import build_orderbook_seconds
from research.btc_doge_research.source_file_registry import SourceFile
from research.btc_doge_research.trade_transform import transform_trades

UTC = timezone.utc


def _source(path: Path, *, fingerprint: str = "a" * 64) -> SourceFile:
    return SourceFile(
        path=path,
        relative_path=path.name,
        fingerprint=fingerprint,
        source_file_id="b" * 64,
        size=path.stat().st_size,
        manifest={
            "format_version": "ob200_v3_live_archive/v1",
            "parser_version": "ob200_v3",
            "event_count": 3,
            "replayable": False,
        },
        segment_start=parse_utc("2026-08-31T18:00:00Z"),
        segment_end=parse_utc("2026-08-31T19:00:00Z"),
    )


def _records() -> list[dict]:
    return [
        {
            "format_version": "ob200_v3_live_archive/v1",
            "type": "rotation_checkpoint",
            "ts": 1788199200000,
            "local_receive_ts": "2026-08-31T18:00:00.010000Z",
            "data": {
                "s": "BTCUSDT",
                "u": 10,
                "seq": 100,
                "b": [["100", "2"], ["99", "3"]],
                "a": [["101", "4"], ["102", "5"]],
            },
        },
        {
            "format_version": "ob200_v3_live_archive/v1",
            "type": "delta",
            "ts": 1788199200500,
            "local_receive_ts": "2026-08-31T18:00:00.510000Z",
            "data": {
                "s": "BTCUSDT",
                "u": 11,
                "seq": 500,
                "b": [["100", "0"], ["98", "7"]],
                "a": [["101", "6"]],
            },
        },
        {
            "format_version": "ob200_v3_live_archive/v1",
            "type": "delta",
            "ts": 1788199200500,
            "local_receive_ts": "2026-08-31T18:00:00.520000Z",
            "data": {
                "s": "BTCUSDT",
                "u": 12,
                "seq": 900,
                "b": [["100.5", "1"]],
                "a": [],
            },
        },
    ]


@pytest.fixture()
def ndjson_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "sample.ndjson"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in _records()),
        encoding="utf-8",
    )
    return _source(path)


def test_utc_and_bucket_contract() -> None:
    assert parse_utc("2026-08-31T18:00:00Z").tzinfo == UTC
    with pytest.raises(ValueError):
        validate_window(
            datetime(2026, 1, 1),
            datetime(2026, 1, 1) + timedelta(minutes=1),
        )
    with pytest.raises(ValueError):
        validate_window(
            parse_utc("2026-01-01T00:00:00Z"),
            parse_utc("2026-01-01T02:00:00Z"),
        )


def test_symbol_and_target_guards() -> None:
    assert validate_symbol("btcusdt") == "BTCUSDT"
    with pytest.raises(ValueError):
        validate_symbol("ETHUSDT")
    validate_write_sql(
        "CREATE TABLE IF NOT EXISTS btc_doge_research.safe (x UInt8) ENGINE=Log"
    )
    with pytest.raises(PermissionError):
        validate_write_sql("CREATE TABLE orderbook_analysis.unsafe (x UInt8) ENGINE=Log")
    with pytest.raises(PermissionError):
        validate_write_sql("DROP TABLE btc_doge_research.safe")


def test_trade_side_and_trade_id_dedup() -> None:
    now = datetime.now(UTC)
    source = [("id1", now, now, Decimal("1"), Decimal("2"), Decimal("2"), "Buy", "live")]
    rows = transform_trades(source, symbol="BTCUSDT", batch_id="b", ingested_at=now)
    assert rows[0][7] == "Buy"
    assert rows[0][16] == "BTCUSDT|id1"
    with pytest.raises(ValueError):
        transform_trades(source * 2, symbol="BTCUSDT", batch_id="b", ingested_at=now)


def test_liquidation_frozen_mapping_and_null_execution() -> None:
    now = datetime.now(UTC)
    source = [
        ("event", now, now, "Sell", "LIQUIDATED_SHORT", Decimal("2"), Decimal("10"), now)
    ]
    row = transform_liquidations(
        source, symbol="BTCUSDT", batch_id="b", ingested_at=now
    )[0]
    assert row[4:6] == ("LIQUIDATED_SHORT", "FORCED_BUY")
    assert row[8] == Decimal("20")
    assert row[9] is None and row[10] is None


def test_ob200_uncompressed_parser_sort_count_timestamp_and_provenance(
    ndjson_source: SourceFile,
) -> None:
    reader = OB200SegmentReader(ndjson_source, "BTCUSDT")
    events = list(
        reader.iter_full_books(
            parse_utc("2026-08-31T18:00:00Z"),
            parse_utc("2026-08-31T18:01:00Z"),
        )
    )
    assert len(events) == 3
    assert events[-1].bids[0][0] == Decimal("100.5")
    assert events[-1].asks[0][0] == Decimal("101")
    assert len(events[-1].bids) == len(events[-1].bids)
    assert events[1].event_time == events[2].event_time
    assert events[1].event_key != events[2].event_key
    assert "SHORT_BOOK" in events[-1].quality_flags
    assert reader.audit.full_file_consumed
    assert reader.audit.effective_replayable
    assert reader.audit.identical_timestamp_groups == 1


def test_ob200_compressed_segment(ndjson_source: SourceFile, tmp_path: Path) -> None:
    zstd = pytest.importorskip("zstandard")
    compressed = tmp_path / "sample.zst"
    compressed.write_bytes(
        zstd.ZstdCompressor().compress(ndjson_source.path.read_bytes())
    )
    reader = OB200SegmentReader(_source(compressed), "BTCUSDT")
    assert len(
        list(
            reader.iter_full_books(
                parse_utc("2026-08-31T18:00:00Z"),
                parse_utc("2026-08-31T18:01:00Z"),
            )
        )
    ) == 3


def test_ob200_u_gap_fails_closed(ndjson_source: SourceFile) -> None:
    records = _records()
    records[-1]["data"]["u"] = 13
    ndjson_source.path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    reader = OB200SegmentReader(ndjson_source, "BTCUSDT")
    with pytest.raises(ValueError, match="continuity gap"):
        list(
            reader.iter_full_books(
                parse_utc("2026-08-31T18:00:00Z"),
                parse_utc("2026-08-31T18:01:00Z"),
            )
        )


def test_genuine_and_carried_forward(ndjson_source: SourceFile) -> None:
    reader = OB200SegmentReader(ndjson_source, "BTCUSDT")
    events = list(
        reader.iter_full_books(
            parse_utc("2026-08-31T18:00:00Z"),
            parse_utc("2026-08-31T18:00:03Z"),
        )
    )
    rows = build_orderbook_seconds(
        "BTCUSDT",
        parse_utc("2026-08-31T18:00:00Z"),
        parse_utc("2026-08-31T18:00:03Z"),
        [events[-1]],
        "b",
        datetime.now(UTC),
    )
    assert rows[0][14:16] == (1, 0)
    assert rows[1][14:16] == (0, 1)
    assert rows[2][14:16] == (0, 1)


def test_oi_stale_and_funding_not_available(ndjson_source: SourceFile) -> None:
    reader = OB200SegmentReader(ndjson_source, "BTCUSDT")
    event = list(
        reader.iter_full_books(
            parse_utc("2026-08-31T18:00:00Z"),
            parse_utc("2026-08-31T18:00:01Z"),
        )
    )[-1]
    ob = build_orderbook_seconds(
        "BTCUSDT",
        parse_utc("2026-08-31T18:00:00Z"),
        parse_utc("2026-08-31T18:00:01Z"),
        [event],
        "b",
        datetime.now(UTC),
    )
    market = build_market_seconds(
        symbol="BTCUSDT",
        start=parse_utc("2026-08-31T18:00:00Z"),
        end=parse_utc("2026-08-31T18:00:01Z"),
        trades=[
            (
                "trade",
                datetime(2026, 8, 31, 18, 0, 0, 500000),
                datetime(2026, 8, 31, 18, 0, 0, 600000),
                Decimal("100"),
                Decimal("2"),
                Decimal("200"),
                "Buy",
                "live",
            )
        ],
        liquidations=[],
        oi_rows=[],
        orderbook_rows=ob,
        batch_id="b",
        ingested_at=datetime.now(UTC),
    )
    assert market[0][10] is None
    assert market[0][13] == "MISSING"
    assert market[0][4] == Decimal("2")
    assert market[0][8] == 1
    assert market[0][22] == FUNDING_STATUS


def test_batch_and_source_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "research.btc_doge_research.clickhouse.rows",
        lambda *args, **kwargs: [("x" * 64, "COMPLETE")],
    )
    with pytest.raises(RuntimeError, match="CONFLICT"):
        ensure_batch_available(object(), "batch", "y" * 64)
    monkeypatch.setattr(
        "research.btc_doge_research.clickhouse.rows",
        lambda *args, **kwargs: [("x" * 64,)],
    )
    with pytest.raises(RuntimeError, match="CONFLICT"):
        ensure_source_file_available(object(), "file", "y" * 64)


def test_batch_fingerprint_deterministic() -> None:
    kwargs = dict(
        pilot_id="p",
        symbol="BTCUSDT",
        start=BTC_WINDOW.start,
        end=BTC_WINDOW.end,
        source_fingerprints=["b", "a"],
    )
    assert input_fingerprint(**kwargs) == input_fingerprint(**kwargs)
    assert input_fingerprint(**kwargs) == input_fingerprint(
        **{**kwargs, "source_fingerprints": ["a", "b"]}
    )


def test_no_hindsight_tables_and_nan_sanitizing() -> None:
    lowered = DDL.lower()
    assert "strategy_signals" not in lowered
    assert "hindsight_labels" not in lowered
    assert "live_alerts" not in lowered
    assert sanitize_json({"x": float("nan"), "y": float("inf")}) == {
        "x": None,
        "y": None,
    }


@pytest.mark.skipif(
    os.getenv("RUN_RESEARCH_DB_INTEGRATION") != "1",
    reason="set RUN_RESEARCH_DB_INTEGRATION=1",
)
def test_clickhouse_phase1_idempotent_schema_and_loaded_facts() -> None:
    from research.btc_doge_research.clickhouse import connect
    from research.btc_doge_research.pilot_runner import create_schema, run_pilot
    from research.btc_doge_research.validation import table_identity

    client = connect()
    create_schema(client)
    create_schema(client)
    before = table_identity(client, "research_public_trades", symbol="BTCUSDT")
    rerun = run_pilot(client, BTC_WINDOW)
    after = table_identity(client, "research_public_trades", symbol="BTCUSDT")
    assert rerun["status"] == "IDEMPOTENT_SKIP"
    assert before == after
    assert after["duplicate_keys"] == 0
    ob = table_identity(
        client, "research_orderbook_ob200_snapshots", symbol="BTCUSDT"
    )
    assert ob["physical_rows"] == ob["logical_keys"] > 0
