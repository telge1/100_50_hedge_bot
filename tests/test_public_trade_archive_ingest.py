"""Tests for historical public-trade archive ingest (no live ClickHouse writes)."""

from __future__ import annotations

import gzip
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.public_trade_source.archive_ingest import (
    ARCHIVE_FQN,
    ARCHIVE_TABLE,
    ArchiveIngestError,
    ArchiveIngestWriter,
    assert_archive_sql,
    assert_archive_table,
    discover_trade_files,
    ingest_files,
    parse_rpi_flag,
    trade_to_archive_row,
)
from orderbook_analyse.public_trade_source.csv_parse import parse_csv_trade_row

HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional,RPI"
)


def _gz(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, list, list, dict | None]] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert(self, table: str, data: list, column_names: list, settings=None) -> None:
        self.inserts.append((table, list(data), list(column_names), settings))


def test_guard_blocks_live_and_scanner_tables() -> None:
    with pytest.raises(ArchiveIngestError):
        assert_archive_table("public_trades")
    with pytest.raises(ArchiveIngestError):
        assert_archive_table("candles_1m")
    with pytest.raises(ArchiveIngestError):
        assert_archive_table("signal_generator.candles_1m")
    with pytest.raises(ArchiveIngestError):
        assert_archive_table("orderbook_deltas")
    assert assert_archive_table(ARCHIVE_FQN) == ARCHIVE_FQN
    assert assert_archive_table(ARCHIVE_TABLE) == ARCHIVE_FQN


def test_guard_blocks_dangerous_sql() -> None:
    with pytest.raises(ArchiveIngestError):
        assert_archive_sql("INSERT INTO public_trades VALUES")
    with pytest.raises(ArchiveIngestError):
        assert_archive_sql("INSERT INTO signal_generator.candles_1m VALUES")
    with pytest.raises(ArchiveIngestError):
        assert_archive_sql("DROP TABLE public_trades_archive")
    assert_archive_sql("CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades_archive (x Int8)")


def test_writer_rejects_wrong_database() -> None:
    with pytest.raises(ArchiveIngestError):
        ArchiveIngestWriter(FakeClient(), database="signal_generator")


def test_rpi_and_row_mapping() -> None:
    row = {
        "timestamp": "1767225600.0",
        "symbol": "APTUSDT",
        "side": "Buy",
        "size": "2",
        "price": "1.5",
        "tickDirection": "PlusTick",
        "trdMatchID": "abc",
        "foreignNotional": "3.0",
        "RPI": "1",
    }
    trade = parse_csv_trade_row(row, expected_symbol="APTUSDT", source_file="f")
    assert parse_rpi_flag(row) == 1
    received = datetime(2026, 8, 17, tzinfo=timezone.utc)
    mapped = trade_to_archive_row(
        trade, received_ts=received, ingest_source="test", is_rpi_trade=1
    )
    assert mapped[3] == "abc"
    assert mapped[4] == "Buy"
    assert mapped[10] == 1
    assert mapped[11] == "test"


def test_discover_nested_and_flat(tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    nested = tmp_path / "nested" / "APTUSDT" / "2026-07-24"
    _gz(flat / "APTUSDT2026-07-24.csv.gz", [HEADER])
    _gz(nested / "APTUSDT2026-07-25.csv.gz", [HEADER])
    _gz(tmp_path / "ignore.txt", ["nope"])
    files = discover_trade_files([flat, tmp_path / "nested"], symbol="APTUSDT")
    names = [p.name for p in files]
    assert names == ["APTUSDT2026-07-24.csv.gz", "APTUSDT2026-07-25.csv.gz"]


def test_dry_run_and_throttled_insert(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    rows = [HEADER]
    for i in range(5):
        rows.append(
            f"178485120{i}.0,APTUSDT,Buy,1,0.5,PlusTick,id{i},0,1,0.5,0"
        )
    _gz(path, rows)
    dry = ingest_files(writer=None, files=[path], batch_size=2, pause_ms=0, dry_run=True)
    assert dry.rows_inserted == 5
    assert dry.files[0].status == "DRY_RUN"

    client = FakeClient()
    writer = ArchiveIngestWriter(client, database="orderbook_analysis")
    writer.ensure_table()
    assert client.commands
    assert "public_trades_archive" in client.commands[0]
    assert "public_trades\n" not in client.commands[0]

    ck = tmp_path / "ck.json"
    run = ingest_files(
        writer=writer,
        files=[path],
        batch_size=2,
        pause_ms=0,
        dry_run=False,
        checkpoint_path=ck,
    )
    assert run.rows_inserted == 5
    assert all(ins[0] == ARCHIVE_FQN for ins in client.inserts)
    assert all(ins[3]["priority"] == 16 for ins in client.inserts)
    assert all(ins[3]["max_insert_threads"] == 1 for ins in client.inserts)

    again = ingest_files(
        writer=writer,
        files=[path],
        batch_size=2,
        pause_ms=0,
        checkpoint_path=ck,
    )
    assert again.files[0].status == "SKIPPED_CHECKPOINT"
    assert again.rows_inserted == 0
