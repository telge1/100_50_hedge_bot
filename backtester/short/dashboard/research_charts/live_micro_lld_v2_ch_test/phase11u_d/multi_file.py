"""Read several immutable 1m files as one contiguous input. Files stay unchanged."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from data.models import Candle

from ..immutable_candle_snapshot_v1 import (
    SnapshotError,
    load_snapshot,
    ohlcv_content_hash,
    revision_metadata_hash,
)
from ...trp_import import load_trp

COMPOSITE_VERSION = "composite_immutable_input_v1"


class MultiFileInputError(SnapshotError):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class FilePart:
    directory: Path
    snapshot_id: str
    price_hash: str
    revision_hash: str


@dataclass
class CompositeInput:
    composite_id: str
    symbol: str
    exchange: str
    interval: str
    range_start: datetime
    range_end_exclusive: datetime
    parts: tuple[dict, ...]
    rows: list
    clickhouse_candle_reads: int = 0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _public_records(records: list[dict]) -> list[dict]:
    hidden = {"directory"}
    return [{key: value for key, value in record.items() if key not in hidden} for record in records]


def composite_input_id(part_records: list[dict]) -> str:
    public = _public_records(part_records)
    payload = {
        "version": COMPOSITE_VERSION,
        "exchange": public[0]["exchange"],
        "symbol": public[0]["symbol"],
        "interval": public[0]["interval"],
        "parts": public,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest_record(part: FilePart) -> dict:
    manifest_path = part.directory / f"{part.snapshot_id}.manifest.json"
    if not manifest_path.is_file():
        raise MultiFileInputError("MISSING_IMMUTABLE_INPUT", f"manifest missing for {part.snapshot_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != part.snapshot_id:
        raise MultiFileInputError("INPUT_IDENTITY_MISMATCH", "manifest snapshot id mismatch")
    if manifest.get("ohlcv_content_hash") != part.price_hash or manifest.get("revision_metadata_hash") != part.revision_hash:
        raise MultiFileInputError("INPUT_IDENTITY_MISMATCH", "manifest hash mismatch")
    if manifest.get("snapshot_completeness_status") != "COMPLETE":
        raise MultiFileInputError("INPUT_IDENTITY_MISMATCH", "snapshot is not complete")
    return {
        "snapshot_id": part.snapshot_id,
        "ohlcv_content_hash": part.price_hash,
        "revision_metadata_hash": part.revision_hash,
        "range_start": manifest["range_start"],
        "range_end_exclusive": manifest["range_end_exclusive"],
        "exchange": manifest["exchange"],
        "symbol": manifest["symbol"],
        "interval": manifest["interval"],
        "row_count": int(manifest["row_count"]),
        "directory": str(part.directory),
    }


def _ordered_records(parts: list[FilePart]) -> list[dict]:
    if not parts:
        raise MultiFileInputError("INVALID_REQUEST", "no immutable files")
    records = [_manifest_record(part) for part in parts]
    records.sort(key=lambda item: (item["range_start"], item["snapshot_id"]))
    first = records[0]
    for record in records[1:]:
        if record["exchange"] != first["exchange"] or record["symbol"] != first["symbol"] or record["interval"] != first["interval"]:
            raise MultiFileInputError("MULTI_FILE_INPUT_CONFLICT", "files differ in exchange, symbol, or interval")
    for left, right in zip(records, records[1:]):
        if right["range_start"] < left["range_end_exclusive"]:
            raise MultiFileInputError("MULTI_FILE_INPUT_CONFLICT", "immutable files overlap")
        if right["range_start"] > left["range_end_exclusive"]:
            raise MultiFileInputError("INSUFFICIENT_CANDLE_COVERAGE", "gap between immutable files")
    return records


def load_composite(parts: list[FilePart]) -> CompositeInput:
    records = _ordered_records(parts)
    by_id = {part.snapshot_id: part for part in parts}
    rows = []
    seen = {}
    for record in records:
        part = by_id[record["snapshot_id"]]
        part_rows = load_snapshot(Path(record["directory"]), record["snapshot_id"])
        if ohlcv_content_hash(part_rows) != part.price_hash or revision_metadata_hash(part_rows) != part.revision_hash:
            raise MultiFileInputError("INPUT_IDENTITY_MISMATCH", "file bytes do not match the requested hashes")
        for row in part_rows:
            stamp = row.open_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            previous = seen.get(stamp)
            if previous is not None:
                same = (
                    ohlcv_content_hash([previous]) == ohlcv_content_hash([row])
                    and revision_metadata_hash([previous]) == revision_metadata_hash([row])
                )
                status = "MULTI_FILE_INPUT_CONFLICT" if not same else "MULTI_FILE_INPUT_CONFLICT"
                raise MultiFileInputError(status, f"minute {stamp} appears in more than one file")
            seen[stamp] = row
            rows.append(row)
    public = _public_records(records)
    return CompositeInput(
        composite_id=composite_input_id(public),
        symbol=public[0]["symbol"],
        exchange=public[0]["exchange"],
        interval=public[0]["interval"],
        range_start=_parse(public[0]["range_start"]),
        range_end_exclusive=_parse(public[-1]["range_end_exclusive"]),
        parts=tuple(public),
        rows=rows,
        clickhouse_candle_reads=0,
    )


def aggregate_rows(rows, timeframe: str) -> list[Candle]:
    one_m = [
        Candle(
            timestamp=row.open_time,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            symbol=row.symbol,
            timeframe="1m",
        )
        for row in rows
    ]
    return load_trp()["aggregate"](one_m, timeframe, strict_complete_buckets=True)


@dataclass
class CompositeCandleCache:
    max_entries: int = 2
    hits: int = 0
    loads: int = 0
    _store: dict = field(default_factory=dict)
    _order: list = field(default_factory=list)

    def get(self, parts: list[FilePart], timeframe: str) -> CompositeInput:
        records = _ordered_records(parts)
        key = (composite_input_id(records), timeframe)
        cached = self._store.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        probe = load_composite(parts)
        probe.rows = aggregate_rows(probe.rows, timeframe)
        if len(self._order) >= self.max_entries:
            self._store.pop(self._order.pop(0), None)
        self._store[key] = probe
        self._order.append(key)
        self.loads += 1
        return probe
