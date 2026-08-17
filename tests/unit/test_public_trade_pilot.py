"""Unit tests for public-trade URL, parse, manifest, ingest guards, and resume."""

from __future__ import annotations

import gzip
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from signal_generator.bybit.public_trades.csv_parse import (
    PublicTradeParseError,
    parse_csv_trade_row,
    unix_seconds_str_to_utc,
)
from signal_generator.bybit.public_trades.downloader import (
    DISK_FREE_MIN_BYTES,
    PublicTradeDownloadError,
    download_day_file,
)
from signal_generator.bybit.public_trades.guards import (
    CanonicalTradeGuardError,
    assert_canonical_sql,
    assert_canonical_table,
    assert_source,
)
from signal_generator.bybit.public_trades.importer import (
    AbortAfterRows,
    PublicTradeImporter,
)
from signal_generator.bybit.public_trades.manifest import ManifestStore
from signal_generator.bybit.public_trades.audit import classify_minute
from signal_generator.bybit.public_trades.storage import (
    HARD_STOP_FREE_BYTES,
    project_storage,
)
from signal_generator.bybit.public_trades.urls import daily_filename, daily_url, utc_day_bounds
from signal_generator.db.setup import _split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_002 = (ROOT / "migrations" / "002_public_trades_canonical.sql").read_text(encoding="utf-8")

HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional,RPI\n"
)


def _ts(day: date, hour: int = 12, minute: int = 0, second: int = 0, micro: int = 123456) -> str:
    dt = datetime(day.year, day.month, day.day, hour, minute, second, micro, tzinfo=timezone.utc)
    whole = int(dt.timestamp())
    frac = f"{micro:06d}".rstrip("0") or "0"
    return f"{whole}.{frac}"


def _row(
    *,
    day: date = date(2026, 8, 15),
    symbol: str = "DOGEUSDT",
    side: str = "Buy",
    size: str = "10",
    price: str = "0.1",
    trade_id: str = "id-1",
    ts: str | None = None,
) -> str:
    stamp = ts if ts is not None else _ts(day)
    return (
        f"{stamp},{symbol},{side},{size},{price},PlusTick,{trade_id},"
        f"1000000000,{size},1.0,0\n"
    )


def _gz_bytes(body: str) -> bytes:
    return gzip.compress(body.encode("utf-8"))


class FakeTransport:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.gets = 0

    def head(self, url: str):  # pragma: no cover - unused in these tests
        raise AssertionError("head should not be required")

    def stream_get(self, url: str):
        self.gets += 1
        if self.status == 404:
            raise PublicTradeDownloadError("SOURCE_FILE_MISSING", "HTTP 404")
        yield self.payload


class FakeRepo:
    def __init__(self) -> None:
        self.inserted: list = []
        self.calls = 0

    def insert_trades(self, trades, *, ingest_timestamp, source="archive") -> int:
        self.calls += 1
        self.inserted.extend(trades)
        return len(trades)


def test_daily_url_and_filename():
    day = date(2026, 8, 15)
    assert daily_filename("dogeusdt", day) == "DOGEUSDT2026-08-15.csv.gz"
    assert daily_url("DOGEUSDT", day) == (
        "https://public.bybit.com/trading/DOGEUSDT/DOGEUSDT2026-08-15.csv.gz"
    )


def test_utc_day_bounds_are_half_open():
    start, end = utc_day_bounds(date(2026, 8, 15))
    assert start.isoformat() == "2026-08-15T00:00:00+00:00"
    assert end.isoformat() == "2026-08-16T00:00:00+00:00"


def test_unix_seconds_not_millis():
    ts = unix_seconds_str_to_utc("1755216000.5")
    assert ts.tzinfo is not None
    with pytest.raises(PublicTradeParseError, match="milliseconds"):
        unix_seconds_str_to_utc("1755216000000")


def test_parse_side_and_trade_id():
    row = {
        "timestamp": _ts(date(2026, 8, 15)),
        "symbol": "DOGEUSDT",
        "side": "Sell",
        "size": "2",
        "price": "0.2",
        "tickDirection": "MinusTick",
        "trdMatchID": "abc",
        "foreignNotional": "0.4",
    }
    parsed = parse_csv_trade_row(row, expected_symbol="DOGEUSDT")
    assert parsed.side == "Sell"
    assert parsed.trade_id == "abc"
    assert parsed.size == Decimal("2")
    assert parsed.notional == Decimal("0.4")


def test_empty_trade_id_and_invalid_side_and_values():
    base = {
        "timestamp": _ts(date(2026, 8, 15)),
        "symbol": "DOGEUSDT",
        "side": "Buy",
        "size": "1",
        "price": "1",
        "tickDirection": "ZeroTick",
        "trdMatchID": "x",
    }
    with pytest.raises(PublicTradeParseError, match="empty trdMatchID"):
        parse_csv_trade_row({**base, "trdMatchID": "  "})
    with pytest.raises(PublicTradeParseError, match="invalid side"):
        parse_csv_trade_row({**base, "side": "LONG"})
    with pytest.raises(PublicTradeParseError, match="non-positive size"):
        parse_csv_trade_row({**base, "size": "0"})
    with pytest.raises(PublicTradeParseError, match="non-positive price"):
        parse_csv_trade_row({**base, "price": "-1"})


def test_schema_002_idempotent_and_no_drop():
    stmts = _split_sql_statements(SCHEMA_002)
    assert any("CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades_canonical" in s for s in stmts)
    assert any("ORDER BY (symbol, trade_id)" in s for s in stmts)
    assert any("ReplacingMergeTree(ingest_timestamp)" in s for s in stmts)
    for stmt in stmts:
        upper = stmt.upper()
        assert not upper.lstrip().startswith("DROP")
        assert "TRUNCATE" not in upper
        assert "INSERT INTO" not in upper


def test_guards_refuse_forbidden_tables_and_sql():
    with pytest.raises(CanonicalTradeGuardError):
        assert_canonical_table("signal_generator.candles_1m")
    with pytest.raises(CanonicalTradeGuardError):
        assert_canonical_table("orderbook_analysis.public_trades_archive")
    with pytest.raises(CanonicalTradeGuardError):
        assert_canonical_table("orderbook_analysis.orderbook_deltas")
    with pytest.raises(CanonicalTradeGuardError):
        assert_canonical_sql("ALTER TABLE public_trades_canonical DELETE WHERE 1")
    with pytest.raises(CanonicalTradeGuardError):
        assert_source("recorder")
    assert assert_source("archive") == "archive"


def test_manifest_status_transitions(tmp_path: Path):
    store = ManifestStore(tmp_path / "pilot_manifest.csv")
    row = store.ensure("DOGEUSDT", "2026-08-15", "http://example")
    store.set_status(row, "DOWNLOADED")
    store.set_status(row, "VERIFIED")
    store.set_status(row, "IMPORTED")
    with pytest.raises(ValueError, match="illegal status"):
        store.set_status(row, "PENDING")
    again = ManifestStore(tmp_path / "pilot_manifest.csv")
    assert again.get("DOGEUSDT", "2026-08-15").status == "IMPORTED"


def test_dry_run_does_not_write(tmp_path: Path):
    payload = _gz_bytes(HEADER + _row(trade_id="a") + _row(trade_id="a") + _row(trade_id="b"))
    transport = FakeTransport(payload)
    repo = FakeRepo()
    importer = PublicTradeImporter(
        cache_dir=tmp_path / "cache",
        manifest=ManifestStore(tmp_path / "manifest.csv"),
        repo=repo,
        transport=transport,
        disk_free_fn=lambda p: 10**18,
    )
    row = importer.process_file("DOGEUSDT", date(2026, 8, 15), dry_run=True)
    assert row.status == "VERIFIED"
    assert row.duplicate_rows == 1
    assert row.unique_trade_ids == 2
    assert repo.calls == 0
    assert transport.gets == 1


def test_repeat_import_is_logical_idempotent(tmp_path: Path):
    payload = _gz_bytes(HEADER + _row(trade_id="a") + _row(trade_id="b"))
    importer = PublicTradeImporter(
        cache_dir=tmp_path / "cache",
        manifest=ManifestStore(tmp_path / "manifest.csv"),
        repo=FakeRepo(),
        transport=FakeTransport(payload),
        batch_size=1,
        pause_ms=0,
        disk_free_fn=lambda p: 10**18,
    )
    first = importer.process_file("DOGEUSDT", date(2026, 8, 15))
    assert first.status == "IMPORTED"
    skipped = importer.process_file("DOGEUSDT", date(2026, 8, 15))
    assert skipped.status == "IMPORTED"
    assert importer.repo.calls == 2  # two batches of 1 on first import only
    importer.process_file("DOGEUSDT", date(2026, 8, 15), force_reimport=True)
    ids = [t.trade_id for t in importer.repo.inserted]
    assert ids.count("a") == 2
    assert ids.count("b") == 2


def test_resume_after_controlled_abort(tmp_path: Path):
    payload = _gz_bytes(
        HEADER
        + _row(symbol="LITUSDT", trade_id="a")
        + _row(symbol="LITUSDT", trade_id="b")
        + _row(symbol="LITUSDT", trade_id="c")
        + _row(symbol="LITUSDT", trade_id="d")
    )
    repo = FakeRepo()
    importer = PublicTradeImporter(
        cache_dir=tmp_path / "cache",
        manifest=ManifestStore(tmp_path / "manifest.csv"),
        repo=repo,
        transport=FakeTransport(payload),
        batch_size=2,
        pause_ms=0,
        disk_free_fn=lambda p: 10**18,
    )
    with pytest.raises(AbortAfterRows):
        importer.process_file("LITUSDT", date(2026, 8, 15), abort_after_rows=2)
    row = importer.manifest.get("LITUSDT", "2026-08-15")
    assert row.status == "FAILED"
    assert row.inserted_rows == 2
    importer.process_file("LITUSDT", date(2026, 8, 15))
    done = importer.manifest.get("LITUSDT", "2026-08-15")
    assert done.status == "IMPORTED"
    assert {t.trade_id for t in repo.inserted} == {"a", "b", "c", "d"}


def test_out_of_utc_day_counted(tmp_path: Path):
    other = _ts(date(2026, 8, 16), 0, 0, 1)
    payload = _gz_bytes(HEADER + _row(trade_id="in") + _row(trade_id="out", ts=other))
    importer = PublicTradeImporter(
        cache_dir=tmp_path / "cache",
        manifest=ManifestStore(tmp_path / "manifest.csv"),
        repo=FakeRepo(),
        transport=FakeTransport(payload),
        pause_ms=0,
        disk_free_fn=lambda p: 10**18,
    )
    row = importer.process_file("DOGEUSDT", date(2026, 8, 15), dry_run=True)
    assert row.out_of_day_rows == 1


def test_disk_hard_stop(tmp_path: Path):
    def boom(_path: Path) -> int:
        raise PublicTradeDownloadError("DISK_HARD_STOP", "below 80 GiB")

    importer = PublicTradeImporter(
        cache_dir=tmp_path / "cache",
        manifest=ManifestStore(tmp_path / "manifest.csv"),
        repo=FakeRepo(),
        transport=FakeTransport(_gz_bytes(HEADER + _row())),
        disk_free_fn=boom,
    )
    with pytest.raises(PublicTradeDownloadError, match="DISK_HARD_STOP"):
        importer.process_file("DOGEUSDT", date(2026, 8, 15), dry_run=True)
    assert DISK_FREE_MIN_BYTES == HARD_STOP_FREE_BYTES


def test_storage_projection_blocks_when_over_budget():
    out = project_storage(
        doge_rows=1_000_000,
        lit_rows=1000,
        combined_rows=1_001_000,
        combined_ch_bytes=200 * 1024**3,
        combined_physical_rows=1_001_000,
        combined_gz_bytes=50 * 1024**3,
        free_bytes_now=510 * 1024**3,
        first_import_ch_bytes=200 * 1024**3,
    )
    assert out["full_backfill_blocked_storage"] is True


def test_download_missing_file(tmp_path: Path):
    with pytest.raises(PublicTradeDownloadError, match="SOURCE_FILE_MISSING"):
        download_day_file(
            "DOGEUSDT",
            date(2026, 8, 15),
            tmp_path,
            transport=FakeTransport(b"", status=404),
            disk_free_fn=lambda p: 10**18,
        )


def test_classify_minute_zero_volume_and_missing():
    candle0 = {
        "open": 1, "high": 1, "low": 1, "close": 1,
        "volume": 0, "turnover": 0,
    }
    assert classify_minute(candle=candle0, trades=None) == "NO_TRADES_EXPECTED"
    candle_vol = {
        "open": 1, "high": 2, "low": 1, "close": 2,
        "volume": 10, "turnover": 10,
    }
    assert classify_minute(candle=candle_vol, trades=None) == "PUBLIC_TRADES_MISSING"
    trades = {
        "open": 1, "high": 2, "low": 1, "close": 2,
        "base_volume": 10, "quote_volume": 10, "n_trades": 3,
    }
    assert classify_minute(candle=candle_vol, trades=trades) == "MATCH"
    assert classify_minute(candle=None, trades=trades) == "CANDLE_MISSING"
    mismatch = dict(trades)
    mismatch["base_volume"] = 50
    assert classify_minute(candle=candle_vol, trades=mismatch) == "CANDLE_MISMATCH"
