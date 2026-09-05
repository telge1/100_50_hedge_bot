"""OB200 hour-boundary analysis and proven boundary-seed resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .contracts import sanitize_json, stable_hash, utc
from .ob200_parser import FullBookEvent, OB200SegmentReader, SUPPORTED_TYPES
from .source_file_registry import SourceFile, load_source_file

ZERO = Decimal("0")

COMPLETE_3600 = "COMPLETE_3600"
COMPLETE_WITH_PROVEN_BOUNDARY_SEED = "COMPLETE_WITH_PROVEN_BOUNDARY_SEED"
PARTIAL_TRUE_GAP = "PARTIAL_TRUE_GAP"
MISSING_INITIAL_STATE = "MISSING_INITIAL_STATE"
DUPLICATE_BOUNDARY = "DUPLICATE_BOUNDARY"
ZERO_DURATION_AUXILIARY = "ZERO_DURATION_AUXILIARY"
BOUNDARY_STATE_AUXILIARY = "BOUNDARY_STATE_AUXILIARY"


def iter_ndjson_chunked(path: Path) -> Iterator[dict[str, Any]]:
    import zstandard as zstd

    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as fh:
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl]
                    buf = buf[nl + 1 :]
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
            if buf.strip():
                obj = json.loads(buf)
                if isinstance(obj, dict):
                    yield obj


def _event_time(obj: dict[str, Any]) -> datetime | None:
    ts_ms = obj.get("ts")
    if not isinstance(ts_ms, int):
        return None
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def _receive_time(obj: dict[str, Any]) -> datetime | None:
    value = obj.get("local_receive_ts")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        scale = 1000.0 if value > 10_000_000_000 else 1.0
        return datetime.fromtimestamp(value / scale, tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def audit_raw_file(path: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or {}
    types: dict[str, int] = {}
    first_event = last_event = None
    first_recv = last_recv = None
    first_u = last_u = None
    first_seq = last_seq = None
    has_checkpoint = has_snapshot = has_terminal = False
    count = 0
    for obj in iter_ndjson_chunked(path):
        count += 1
        event_type = str(obj.get("type") or obj.get("archive_event") or "unknown")
        types[event_type] = types.get(event_type, 0) + 1
        if event_type == "rotation_checkpoint":
            has_checkpoint = True
        if event_type == "snapshot":
            has_snapshot = True
        if obj.get("archive_event") in {"CLEAN_CLOSE", "ROTATION_CHECKPOINT"}:
            has_terminal = True
        ts = _event_time(obj)
        if ts is not None:
            first_event = first_event or ts
            last_event = ts
        recv = _receive_time(obj)
        if recv is not None:
            first_recv = first_recv or recv
            last_recv = recv
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if data.get("u") is not None:
            u = int(data["u"])
            first_u = first_u if first_u is not None else u
            last_u = u
        if data.get("seq") is not None:
            seq = int(data["seq"])
            first_seq = first_seq if first_seq is not None else seq
            last_seq = seq
    return sanitize_json(
        {
            "record_count": count,
            "record_types": types,
            "first_event_ts": first_event.isoformat().replace("+00:00", "Z") if first_event else "",
            "last_event_ts": last_event.isoformat().replace("+00:00", "Z") if last_event else "",
            "first_receive_ts": first_recv.isoformat().replace("+00:00", "Z") if first_recv else "",
            "last_receive_ts": last_recv.isoformat().replace("+00:00", "Z") if last_recv else "",
            "first_u": first_u,
            "last_u": last_u,
            "first_sequence": first_seq,
            "last_sequence": last_seq,
            "has_rotation_checkpoint": has_checkpoint,
            "has_native_snapshot": has_snapshot,
            "has_terminal_marker": has_terminal,
            "manifest_event_count": manifest.get("event_count"),
            "manifest_checkpoint_count": manifest.get("checkpoint_count"),
            "manifest_delta_count": manifest.get("delta_count"),
            "writer_meaning": (
                "hour_rollover_boundary_stub"
                if count == 1 and types.get("delta") == 1
                else "hour_segment"
            ),
        }
    )


@dataclass
class Ob200FileIndex:
    full_hours: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    boundary_stubs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_discovery(cls, files: list[dict[str, Any]]) -> Ob200FileIndex:
        index = cls()
        for row in files:
            symbol = row["symbol"]
            start = row["segment_start"]
            key = (symbol, start)
            if row.get("zero_duration"):
                index.boundary_stubs[key] = row
            else:
                index.full_hours[key] = row
        return index

    def stub_for_hour(self, symbol: str, hour_start: datetime) -> dict[str, Any] | None:
        stamp = hour_start.isoformat().replace("+00:00", "Z")
        return self.boundary_stubs.get((symbol, stamp))

    def prev_hour_file(self, symbol: str, hour_start: datetime) -> dict[str, Any] | None:
        prev = hour_start - timedelta(hours=1)
        stamp = prev.isoformat().replace("+00:00", "Z")
        return self.full_hours.get((symbol, stamp))


def _first_rotation_checkpoint(path: Path) -> dict[str, Any] | None:
    for obj in iter_ndjson_chunked(path):
        if str(obj.get("type")) == "rotation_checkpoint":
            return obj
    return None


def _first_in_range_delta(path: Path, start: datetime, end: datetime) -> dict[str, Any] | None:
    for obj in iter_ndjson_chunked(path):
        if str(obj.get("type")) != "delta":
            continue
        ts = _event_time(obj)
        if ts is not None and start <= ts < end:
            return obj
    return None


def _apply_levels(book_b: dict[Decimal, Decimal], book_a: dict[Decimal, Decimal], data: dict[str, Any]) -> None:
    for side, book in (("b", book_b), ("a", book_a)):
        for level in data.get(side) or []:
            price, size = Decimal(str(level[0])), Decimal(str(level[1]))
            if size == ZERO:
                book.pop(price, None)
            else:
                book[price] = size


def _book_to_event(
    *,
    symbol: str,
    source: SourceFile,
    event_time: datetime,
    update_id: int,
    sequence: int,
    raw_event_type: str,
    bids: dict[Decimal, Decimal],
    asks: dict[Decimal, Decimal],
    source_record: int,
) -> FullBookEvent:
    sorted_bids = tuple(sorted(bids.items(), reverse=True))
    sorted_asks = tuple(sorted(asks.items()))
    key_payload = {
        "version": "ob200_boundary_seed_v1",
        "symbol": symbol,
        "event_ms": int(event_time.timestamp() * 1000),
        "update_id": update_id,
        "event_type": raw_event_type,
        "source_fingerprint": source.fingerprint,
        "source_record": source_record,
    }
    content_payload = {
        "bids": [(str(p), str(q)) for p, q in sorted_bids],
        "asks": [(str(p), str(q)) for p, q in sorted_asks],
    }
    return FullBookEvent(
        event_time=event_time,
        receive_time=None,
        exchange_sequence=sequence,
        update_id=update_id,
        raw_event_type=raw_event_type,
        bids=sorted_bids,
        asks=sorted_asks,
        source_record=source_record,
        event_key=stable_hash(key_payload),
        content_fingerprint=stable_hash(content_payload),
        quality_flags=("BOUNDARY_SEED",),
    )


def _replay_terminal_state(source: SourceFile, symbol: str, end: datetime) -> FullBookEvent | None:
    reader = OB200SegmentReader(source, symbol)
    last: FullBookEvent | None = None
    for event in reader.iter_full_books(end - timedelta(hours=1), end):
        last = event
    return last


@dataclass
class BoundarySeed:
    second: datetime
    event: FullBookEvent
    state_proven: bool
    boundary_seed_source: str
    carried_forward: bool
    auxiliary_path: str
    auxiliary_fingerprint: str


def resolve_boundary_seed(
    *,
    symbol: str,
    hour_start: datetime,
    primary: SourceFile,
    index: Ob200FileIndex,
    missing_second: datetime,
) -> BoundarySeed | None:
    if missing_second != hour_start:
        return None

    stub_row = index.stub_for_hour(symbol, hour_start)
    prev_row = index.prev_hour_file(symbol, hour_start)

    checkpoint = _first_rotation_checkpoint(primary.path)
    first_delta = _first_in_range_delta(primary.path, hour_start, hour_start + timedelta(hours=1))

    # A: explicit boundary delta in zero-duration stub, proven via u chain from prev hour
    if stub_row and prev_row:
        from .config import OB200_ROOT

        stub = load_source_file(OB200_ROOT / stub_row["relative_path"], OB200_ROOT)
        prev = load_source_file(OB200_ROOT / prev_row["relative_path"], OB200_ROOT)
        stub_audit = audit_raw_file(stub.path, stub.manifest)
        prev_last = _replay_terminal_state(prev, symbol, hour_start)
        if prev_last and stub_audit.get("first_u") == prev_last.update_id + 1:
            stub_obj = next(iter_ndjson_chunked(stub.path))
            data = stub_obj.get("data") or {}
            bids = {p: q for p, q in prev_last.bids}
            asks = {p: q for p, q in prev_last.asks}
            _apply_levels(bids, asks, data)
            event = _book_to_event(
                symbol=symbol,
                source=stub,
                event_time=missing_second,
                update_id=int(data.get("u") or 0),
                sequence=int(data.get("seq") or 0),
                raw_event_type="boundary_stub_delta",
                bids=bids,
                asks=asks,
                source_record=0,
            )
            return BoundarySeed(
                second=missing_second,
                event=event,
                state_proven=True,
                boundary_seed_source="ZERO_DURATION_BOUNDARY_DELTA",
                carried_forward=True,
                auxiliary_path=stub_row["relative_path"],
                auxiliary_fingerprint=stub_row.get("source_fingerprint", ""),
            )

    # C: rotation checkpoint in primary hour file with proven u+1 to first in-range delta
    if checkpoint and first_delta:
        cp_data = checkpoint.get("data") or {}
        fd_data = first_delta.get("data") or {}
        cp_u = int(cp_data.get("u") or 0)
        fd_u = int(fd_data.get("u") or 0)
        if cp_u and fd_u == cp_u + 1:
            bids = {}
            asks = {}
            _apply_levels(bids, asks, cp_data)
            event = _book_to_event(
                symbol=symbol,
                source=primary,
                event_time=missing_second,
                update_id=cp_u,
                sequence=int(cp_data.get("seq") or 0),
                raw_event_type="rotation_checkpoint",
                bids=bids,
                asks=asks,
                source_record=0,
            )
            return BoundarySeed(
                second=missing_second,
                event=event,
                state_proven=True,
                boundary_seed_source="ROTATION_CHECKPOINT_IN_PRIMARY",
                carried_forward=True,
                auxiliary_path=primary.relative_path,
                auxiliary_fingerprint=primary.fingerprint,
            )

    # B: terminal state from previous hour with proven u chain to first delta
    if prev_row and first_delta:
        from .config import OB200_ROOT

        prev = load_source_file(OB200_ROOT / prev_row["relative_path"], OB200_ROOT)
        prev_last = _replay_terminal_state(prev, symbol, hour_start)
        fd_u = int((first_delta.get("data") or {}).get("u") or 0)
        if prev_last and fd_u == prev_last.update_id + 1:
            event = _book_to_event(
                symbol=symbol,
                source=prev,
                event_time=missing_second,
                update_id=prev_last.update_id,
                sequence=prev_last.exchange_sequence,
                raw_event_type="prev_hour_terminal",
                bids={p: q for p, q in prev_last.bids},
                asks={p: q for p, q in prev_last.asks},
                source_record=prev_last.source_record,
            )
            return BoundarySeed(
                second=missing_second,
                event=event,
                state_proven=True,
                boundary_seed_source="PREV_HOUR_TERMINAL",
                carried_forward=True,
                auxiliary_path=prev_row["relative_path"],
                auxiliary_fingerprint=prev_row.get("source_fingerprint", ""),
            )

    return None


@dataclass
class Ob200SnapshotCollect:
    by_second: dict[datetime, tuple[FullBookEvent, int]]
    missing_seconds: list[datetime]
    expected_seconds: int
    classification: str
    boundary_seed: BoundarySeed | None = None
    source_gaps: list[str] = field(default_factory=list)


def collect_ob200_snapshots(
    *,
    source: SourceFile,
    symbol: str,
    start: datetime,
    end: datetime,
    index: Ob200FileIndex | None = None,
) -> Ob200SnapshotCollect:
    start, end = utc(start), utc(end)
    expected = int((end - start).total_seconds())
    reader = OB200SegmentReader(source, symbol)
    by_second: dict[datetime, tuple[FullBookEvent, int]] = {}
    counts: dict[datetime, int] = {}
    counts: dict[datetime, int] = {}
    for event in reader.iter_full_books(start, end):
        second = event.event_time.replace(microsecond=0)
        if second < start or second >= end:
            continue
        counts[second] = counts.get(second, 0) + 1
        by_second[second] = (event, counts[second])

    missing = [
        start + timedelta(seconds=offset)
        for offset in range(expected)
        if start + timedelta(seconds=offset) not in by_second
    ]

    seed: BoundarySeed | None = None
    if index and start in missing:
        seed = resolve_boundary_seed(
            symbol=symbol,
            hour_start=start,
            primary=source,
            index=index,
            missing_second=start,
        )
        if seed and seed.state_proven:
            if start in by_second:
                raise RuntimeError(DUPLICATE_BOUNDARY)
            by_second[start] = (seed.event, 1)
            missing.remove(start)

    if len(by_second) == expected:
        classification = COMPLETE_WITH_PROVEN_BOUNDARY_SEED if seed else COMPLETE_3600
    elif missing:
        if start in missing and not seed:
            classification = MISSING_INITIAL_STATE if len(by_second) == 0 else PARTIAL_TRUE_GAP
        else:
            classification = PARTIAL_TRUE_GAP
    else:
        classification = DUPLICATE_BOUNDARY

    return Ob200SnapshotCollect(
        by_second=by_second,
        missing_seconds=missing,
        expected_seconds=expected,
        classification=classification,
        boundary_seed=seed,
        source_gaps=[s.isoformat().replace("+00:00", "Z") for s in missing],
    )


def classify_segment_row(
    row: dict[str, Any],
    index: Ob200FileIndex,
    *,
    audit: bool = False,
) -> dict[str, Any]:
    from .config import OB200_ROOT

    start = datetime.fromisoformat(row["segment_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(row["segment_end"].replace("Z", "+00:00"))
    if not audit:
        stub = index.stub_for_hour(row["symbol"], start)
        meta = {
            "boundary_auxiliary_path": stub["relative_path"] if stub else "",
            "boundary_auxiliary_fingerprint": stub.get("source_fingerprint", "") if stub else "",
            "boundary_role": BOUNDARY_STATE_AUXILIARY if stub else "",
        }
        return meta

    source = load_source_file(OB200_ROOT / row["source_path"], OB200_ROOT)
    collected = collect_ob200_snapshots(
        source=source,
        symbol=row["symbol"],
        start=start,
        end=end,
        index=index,
    )
    return sanitize_json(
        {
            "classification": collected.classification,
            "observed_seconds": len(collected.by_second),
            "expected_seconds": collected.expected_seconds,
            "missing_seconds": collected.source_gaps,
            "boundary_seed_source": collected.boundary_seed.boundary_seed_source if collected.boundary_seed else "",
            "state_proven": bool(collected.boundary_seed and collected.boundary_seed.state_proven),
            "carried_forward": bool(collected.boundary_seed and collected.boundary_seed.carried_forward),
            "boundary_auxiliary_path": collected.boundary_seed.auxiliary_path if collected.boundary_seed else "",
        }
    )


def run_boundary_audit(
    files: list[dict[str, Any]] | None = None,
    *,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    max_hours: int | None = None,
) -> dict[str, Any]:
    from .source_discovery import build_source_discovery

    discovery = build_source_discovery() if files is None else {"ob200_files": files}
    all_files = [f for f in discovery["ob200_files"] if f["symbol"] in symbols]
    index = Ob200FileIndex.from_discovery(all_files)
    from .config import OB200_ROOT

    summary: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    full_hours = [
        row for row in all_files
        if not row.get("zero_duration")
        and int(
            (
                datetime.fromisoformat(row["segment_end"].replace("Z", "+00:00"))
                - datetime.fromisoformat(row["segment_start"].replace("Z", "+00:00"))
            ).total_seconds()
        )
        == 3600
    ]
    if max_hours is not None:
        full_hours = full_hours[:max_hours]

    for row in full_hours:
        start = datetime.fromisoformat(row["segment_start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(row["segment_end"].replace("Z", "+00:00"))
        try:
            source = load_source_file(OB200_ROOT / row["relative_path"], OB200_ROOT)
            collected = collect_ob200_snapshots(
                source=source,
                symbol=row["symbol"],
                start=start,
                end=end,
                index=index,
            )
            classification = collected.classification
        except Exception as exc:
            classification = PARTIAL_TRUE_GAP
            collected = None
            err = str(exc)[:200]
        else:
            err = ""
        summary[classification] = summary.get(classification, 0) + 1
        stub = index.stub_for_hour(row["symbol"], start)
        rows.append(
            sanitize_json(
                {
                    "symbol": row["symbol"],
                    "segment_start": row["segment_start"],
                    "segment_end": row["segment_end"],
                    "source_path": row["relative_path"],
                    "classification": classification,
                    "boundary_stub_path": stub["relative_path"] if stub else "",
                    "observed_seconds": len(collected.by_second) if collected else 0,
                    "missing_seconds": collected.source_gaps if collected else [],
                    "boundary_seed_source": (
                        collected.boundary_seed.boundary_seed_source if collected and collected.boundary_seed else ""
                    ),
                    "error": err,
                }
            )
        )

    stub_count = sum(1 for row in all_files if row.get("zero_duration"))
    summary[ZERO_DURATION_AUXILIARY] = stub_count
    return sanitize_json(
        {
            "audited_hours": len(full_hours),
            "boundary_stub_files": stub_count,
            "by_classification": summary,
            "rows": rows,
        }
    )
