"""Immutable 1m candle input snapshot v1 (local files only, no ClickHouse writes).

Hash semantics
--------------
ohlcv_content_hash
    SHA-256 over the price-relevant fields, sorted by
    (exchange, symbol, interval, open_time). Includes open, high, low, close,
    volume, close_time, turnover, is_closed. Excludes ingested_at, source, and
    source_event_time. A re-ingest of identical prices does not change it.
    An OHLCV change does.

revision_metadata_hash
    SHA-256 over the same rows plus ingested_at, source, and source_event_time.
    A re-ingest with a newer ingested_at changes this hash even when prices do not.

file_checksum
    SHA-256 of the published parquet bytes. Detects modification of that file.
    It is not the logical candle identity.

snapshot_id
    SHA-256 of format version, range, symbol, exchange, interval, both logical
    hashes. Deterministic. Not a random id.

engine_input_hash (existing research identity, not this module)
    Hashes config, window, candle count, and a fingerprint of only the first and
    last close. It does not prove the full OHLCV series.

Capture limitation
------------------
One SELECT ... FINAL is the read used to build a snapshot. ClickHouse
ReplacingMergeTree(FINAL) is not a transactional snapshot across statements.
Two matching reads are not proof of isolation. After os.replace of the parquet
and a complete manifest, workers read only the file.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

FORMAT_VERSION = "immutable_candle_input_v1"
SOURCE_QUERY_SEMANTICS = (
    "single SELECT ... FINAL on signal_generator.candles_1m "
    "WHERE exchange, symbol, interval='1m', is_closed=1, "
    "open_time >= range_start AND open_time < range_end_exclusive "
    "ORDER BY open_time"
)

# How a future published pool state should point at its candle input.
# Phase-10 rows were not written with these fields and must not be backdated.
REFERENCE_CONTRACT = {
    "candle_input_snapshot_id": "deterministic id of the frozen 1m file",
    "candle_content_hash": "ohlcv_content_hash of that file",
    "revision_metadata_hash": "revision_metadata_hash of that file",
    "engine_input_hash": "existing prefix hash; does not replace candle_content_hash",
    "config_hash": "PD preset hash",
    "history_policy": "ROLLING_4_CALENDAR_MONTHS or the policy actually used",
    "decision_close": "as-of close of the engine prefix",
    "published_snapshot_id": "active-pool snapshot id, separate from the candle file",
    "projection_algorithm_version": "remaining_pool_projection_v1",
    "phase10_retroactive_binding": False,
}


class SnapshotError(Exception):
    """Fail-closed snapshot error."""


class SnapshotConflictError(SnapshotError):
    """Same snapshot id already exists with different logical content."""


@dataclass(frozen=True)
class CandleInputRow:
    exchange: str
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    is_closed: int
    source: str
    source_event_time: Optional[datetime]
    ingested_at: datetime

    def key(self) -> tuple[str, str, str, str]:
        return (self.exchange, self.symbol, self.interval, _ts(self.open_time))


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise SnapshotError("naive timestamp")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _dec(value: Decimal | str | int | float) -> str:
    return f"{Decimal(value):.8f}"


def _sha(lines: Sequence[str]) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def sorted_rows(rows: Sequence[CandleInputRow]) -> list[CandleInputRow]:
    return sorted(rows, key=lambda r: (r.exchange, r.symbol, r.interval, r.open_time))


def ohlcv_lines(rows: Sequence[CandleInputRow]) -> list[str]:
    out = []
    for r in sorted_rows(rows):
        out.append(
            "|".join(
                (
                    r.exchange,
                    r.symbol,
                    r.interval,
                    _ts(r.open_time),
                    _ts(r.close_time),
                    _dec(r.open),
                    _dec(r.high),
                    _dec(r.low),
                    _dec(r.close),
                    _dec(r.volume),
                    _dec(r.turnover),
                    str(int(r.is_closed)),
                )
            )
        )
    return out


def revision_lines(rows: Sequence[CandleInputRow]) -> list[str]:
    out = []
    for r, base in zip(sorted_rows(rows), ohlcv_lines(rows)):
        src_ev = _ts(r.source_event_time) if r.source_event_time is not None else r"\N"
        out.append("|".join((base, _ts(r.ingested_at), src_ev, r.source)))
    return out


def ohlcv_content_hash(rows: Sequence[CandleInputRow]) -> str:
    return _sha(ohlcv_lines(rows))


def revision_metadata_hash(rows: Sequence[CandleInputRow]) -> str:
    return _sha(revision_lines(rows))


def deterministic_snapshot_id(
    *,
    symbol: str,
    exchange: str,
    interval: str,
    range_start: datetime,
    range_end_exclusive: datetime,
    ohlcv_hash: str,
    revision_hash: str,
) -> str:
    raw = "|".join(
        (
            FORMAT_VERSION,
            exchange,
            symbol,
            interval,
            _ts(range_start),
            _ts(range_end_exclusive),
            ohlcv_hash,
            revision_hash,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def assert_unique_complete(
    rows: Sequence[CandleInputRow],
    *,
    range_start: datetime,
    range_end_exclusive: datetime,
    symbol: str,
    exchange: str,
) -> None:
    ordered = sorted_rows(rows)
    if not ordered:
        raise SnapshotError("empty snapshot")
    keys = [r.key() for r in ordered]
    if len(keys) != len(set(keys)):
        raise SnapshotError("duplicate candle key")
    for r in ordered:
        if r.symbol != symbol or r.exchange != exchange or r.interval != "1m":
            raise SnapshotError(f"unexpected key {r.key()}")
        if int(r.is_closed) != 1:
            raise SnapshotError(f"open candle {r.key()}")
        if r.open_time < range_start or r.open_time >= range_end_exclusive:
            raise SnapshotError(f"row outside range {r.key()}")
    expect = range_start
    for r in ordered:
        if r.open_time != expect:
            raise SnapshotError(f"missing minute at {_ts(expect)}")
        expect = expect + timedelta(minutes=1)
    if expect != range_end_exclusive:
        raise SnapshotError(f"coverage ended at {_ts(expect)}")


def file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_parquet(path: Path, rows: Sequence[CandleInputRow]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    ordered = sorted_rows(rows)
    table = pa.table(
        {
            "exchange": [r.exchange for r in ordered],
            "symbol": [r.symbol for r in ordered],
            "interval": [r.interval for r in ordered],
            "open_time": [_ts(r.open_time) for r in ordered],
            "close_time": [_ts(r.close_time) for r in ordered],
            "open": [_dec(r.open) for r in ordered],
            "high": [_dec(r.high) for r in ordered],
            "low": [_dec(r.low) for r in ordered],
            "close": [_dec(r.close) for r in ordered],
            "volume": [_dec(r.volume) for r in ordered],
            "turnover": [_dec(r.turnover) for r in ordered],
            "is_closed": [int(r.is_closed) for r in ordered],
            "source": [r.source for r in ordered],
            "source_event_time": [
                _ts(r.source_event_time) if r.source_event_time else "" for r in ordered
            ],
            "ingested_at": [_ts(r.ingested_at) for r in ordered],
        }
    )
    pq.write_table(table, path, compression="zstd")


def _parse_ts(value: str) -> datetime:
    if not value:
        raise SnapshotError("empty timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _read_parquet(path: Path) -> list[CandleInputRow]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    rows = []
    for i in range(table.num_rows):
        src_ev = cols["source_event_time"][i]
        rows.append(
            CandleInputRow(
                exchange=cols["exchange"][i],
                symbol=cols["symbol"][i],
                interval=cols["interval"][i],
                open_time=_parse_ts(cols["open_time"][i]),
                close_time=_parse_ts(cols["close_time"][i]),
                open=Decimal(cols["open"][i]),
                high=Decimal(cols["high"][i]),
                low=Decimal(cols["low"][i]),
                close=Decimal(cols["close"][i]),
                volume=Decimal(cols["volume"][i]),
                turnover=Decimal(cols["turnover"][i]),
                is_closed=int(cols["is_closed"][i]),
                source=cols["source"][i],
                source_event_time=_parse_ts(src_ev) if src_ev else None,
                ingested_at=_parse_ts(cols["ingested_at"][i]),
            )
        )
    return rows


def publish_snapshot(
    rows: Sequence[CandleInputRow],
    directory: Path,
    *,
    range_start: datetime,
    range_end_exclusive: datetime,
    source_table: str,
) -> dict[str, Any]:
    """Write temp files, validate, then publish by rename. Does not overwrite a different payload."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    symbol = rows[0].symbol
    exchange = rows[0].exchange
    assert_unique_complete(
        rows,
        range_start=range_start,
        range_end_exclusive=range_end_exclusive,
        symbol=symbol,
        exchange=exchange,
    )
    ohlcv_hash = ohlcv_content_hash(rows)
    rev_hash = revision_metadata_hash(rows)
    sid = deterministic_snapshot_id(
        symbol=symbol,
        exchange=exchange,
        interval="1m",
        range_start=range_start,
        range_end_exclusive=range_end_exclusive,
        ohlcv_hash=ohlcv_hash,
        revision_hash=rev_hash,
    )
    final_data = directory / f"{sid}.parquet"
    final_manifest = directory / f"{sid}.manifest.json"
    if final_manifest.exists():
        existing = json.loads(final_manifest.read_text(encoding="utf-8"))
        if existing.get("ohlcv_content_hash") == ohlcv_hash and existing.get("revision_metadata_hash") == rev_hash:
            loaded = load_snapshot(directory, sid)
            if ohlcv_content_hash(loaded) != ohlcv_hash:
                raise SnapshotConflictError(sid)
            return existing
        raise SnapshotConflictError(sid)

    tmp_data = directory / f".{sid}.data.tmp"
    tmp_manifest = directory / f".{sid}.manifest.tmp"
    _write_parquet(tmp_data, rows)
    with tmp_data.open("rb") as f:
        os.fsync(f.fileno())
    loaded = _read_parquet(tmp_data)
    if ohlcv_content_hash(loaded) != ohlcv_hash or revision_metadata_hash(loaded) != rev_hash:
        tmp_data.unlink(missing_ok=True)
        raise SnapshotError("read-back hash mismatch")
    checksum = file_checksum(tmp_data)
    manifest = {
        "snapshot_format_version": FORMAT_VERSION,
        "snapshot_id": sid,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "symbol": symbol,
        "exchange": exchange,
        "interval": "1m",
        "range_start": _ts(range_start),
        "range_end_exclusive": _ts(range_end_exclusive),
        "row_count": len(rows),
        "first_open_time": _ts(sorted_rows(rows)[0].open_time),
        "last_open_time": _ts(sorted_rows(rows)[-1].open_time),
        "ohlcv_content_hash": ohlcv_hash,
        "revision_metadata_hash": rev_hash,
        "candle_content_hash": ohlcv_hash,
        "file_checksum": checksum,
        "source_table": source_table,
        "source_query_semantics": SOURCE_QUERY_SEMANTICS,
        "snapshot_completeness_status": "COMPLETE",
        "data_file": final_data.name,
    }
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    with tmp_manifest.open("rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp_data, final_data)
    _fsync_dir(directory)
    os.replace(tmp_manifest, final_manifest)
    _fsync_dir(directory)
    return manifest


def load_snapshot(directory: Path, snapshot_id: str) -> list[CandleInputRow]:
    directory = Path(directory)
    manifest_path = directory / f"{snapshot_id}.manifest.json"
    if not manifest_path.exists():
        raise SnapshotError(f"manifest missing for {snapshot_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_completeness_status") != "COMPLETE":
        raise SnapshotError("snapshot not complete")
    if manifest.get("snapshot_id") != snapshot_id:
        raise SnapshotError("manifest id mismatch")
    data = directory / f"{snapshot_id}.parquet"
    if file_checksum(data) != manifest["file_checksum"]:
        raise SnapshotError("file checksum mismatch")
    rows = _read_parquet(data)
    if ohlcv_content_hash(rows) != manifest["ohlcv_content_hash"]:
        raise SnapshotError("ohlcv hash mismatch")
    if revision_metadata_hash(rows) != manifest["revision_metadata_hash"]:
        raise SnapshotError("revision hash mismatch")
    if len(rows) != int(manifest["row_count"]):
        raise SnapshotError("row count mismatch")
    return rows


def require_snapshot(directory: Path, snapshot_id: str, *, ohlcv_hash: str) -> list[CandleInputRow]:
    rows = load_snapshot(directory, snapshot_id)
    if ohlcv_content_hash(rows) != ohlcv_hash:
        raise SnapshotError("worker rejected foreign content hash")
    return rows


def reject_mixed(a: Sequence[CandleInputRow], b: Sequence[CandleInputRow]) -> None:
    if ohlcv_content_hash(a) != ohlcv_content_hash(b) or revision_metadata_hash(a) != revision_metadata_hash(b):
        raise SnapshotError("mixed candle revisions")
