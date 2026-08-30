"""Segment integrity + causal replay audit (streaming, in-place book)."""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import orjson

from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow, sample_from_mutable_book
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    is_replayable_line,
    line_to_replay_payload,
)

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]

ZERO = Decimal("0")

META_NOTE = (
    "manifest_replayable=False expected: writer gap-checks Bybit data.seq (+1) but "
    "continuity is data.u; seq normally jumps. completion_status=open on finalized "
    "files is a metadata bug (close sets closed only when replayable=True)."
)


@dataclass
class SegmentAudit:
    symbol: str
    path: str
    manifest_path: str
    start_utc: str
    end_utc: str
    duration_sec: float
    is_boundary_stub: bool
    manifest_present: bool
    sha256_ok: bool
    decompress_ok: bool
    schema_ok: bool
    event_count_read: int
    event_count_manifest: int | None
    checkpoint_count_read: int
    checkpoint_count_manifest: int | None
    delta_count_read: int
    delta_count_manifest: int | None
    native_snapshot_count_manifest: int | None
    first_event_ts: int | None
    last_event_ts: int | None
    start_checkpoint_bids: int | None
    start_checkpoint_asks: int | None
    end_bids: int | None
    end_asks: int | None
    end_best_bid: str | None
    end_best_ask: str | None
    u_gaps: int
    u_dups: int
    u_monotonic_ok: bool
    seq_jumps: int
    seq_jump_is_loss: str
    ts_backsteps: int
    max_inter_event_ms: float | None
    p95_inter_event_ms: float | None
    queue_overflow_manifest: int | None
    writer_errors_manifest: int | None
    crossed_book_events: int
    empty_book_events: int
    invalid_price_qty: int
    min_depth: int | None
    max_depth: int | None
    median_depth: float | None
    reconstruction_ok: bool
    end_book_valid: bool
    manifest_replayable: bool | None
    manifest_completion_status: str | None
    replay_verdict: str
    notes: str = ""
    sequence_gap_count_manifest: int | None = None


def iter_decompressed_lines(path: Path) -> Iterator[tuple[bytes, dict[str, Any]]]:
    """Stream zstd/ndjson without loading the full uncompressed payload."""
    if path.suffix == ".zst" or path.name.endswith(".zst"):
        if zstd is None:
            raise RuntimeError("zstandard not installed")
        with path.open("rb") as fh:
            with zstd.ZstdDecompressor().stream_reader(fh) as reader:
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
                        obj = orjson.loads(line)
                        if not isinstance(obj, dict):
                            raise ValueError("non-object line")
                        yield line + b"\n", obj
                if buf.strip():
                    obj = orjson.loads(buf)
                    if not isinstance(obj, dict):
                        raise ValueError("non-object trailing line")
                    yield buf if buf.endswith(b"\n") else buf + b"\n", obj
        return

    with path.open("rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            raw = line if line.endswith(b"\n") else line + b"\n"
            obj = orjson.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("non-object line")
            yield raw, obj


def load_manifest_light(path: Path) -> dict[str, Any]:
    """Load manifest scalar fields; count sequence_gaps without retaining them."""
    text = path.read_text(encoding="utf-8")
    gap_count = 0
    gm = re.search(r'"sequence_gaps"\s*:\s*\[', text)
    if gm is not None:
        depth = 1
        i = gm.end()
        while i < len(text) and depth:
            ch = text[i]
            if ch == "[":
                depth += 1
                if depth == 2:
                    gap_count += 1
            elif ch == "]":
                depth -= 1
            i += 1
        end = i
        while end < len(text) and text[end] in " \n\r\t":
            end += 1
        if end < len(text) and text[end] == ",":
            end += 1
        text = text[: gm.start()] + text[end:]

    obj = orjson.loads(text.encode())
    obj["sequence_gap_count"] = gap_count
    obj["sequence_gaps"] = []
    return obj


def process_segment(
    ref: SegmentRef,
    *,
    collect_samples: bool = False,
    sample_ms: int = 1000,
    warmup_ms: int = 60_000,
) -> tuple[SegmentAudit, list[SampleRow]]:
    """Single-pass integrity audit + optional 1s causal samples."""
    manifest_path = ref.manifest_path
    manifest_present = manifest_path.is_file()
    manifest: dict[str, Any] | None = None
    if manifest_present:
        manifest = load_manifest_light(manifest_path)

    sha_ok = False
    decompress_ok = False
    schema_ok = True
    hasher = hashlib.sha256()
    events = 0
    checkpoints = 0
    deltas = 0
    first_ts = None
    last_ts = None
    inter: list[float] = []
    prev_ts = None
    ts_back = 0
    u_gaps = 0
    u_dups = 0
    seq_jumps = 0
    prev_seq = None
    crosses = 0
    empties = 0
    bad_pq = 0
    depths: list[int] = []
    start_bids = start_asks = None
    book = MutableBook()
    reconstruction_ok = True
    notes: list[str] = []
    samples: list[SampleRow] = []
    last_emit_bucket: int | None = None
    sample_first_ts: int | None = None

    try:
        for line_bytes, obj in iter_decompressed_lines(ref.path):
            hasher.update(line_bytes)
            events += 1
            decompress_ok = True
            msg_type = obj.get("type")
            archive_event = obj.get("archive_event")
            if msg_type is None and archive_event is None:
                schema_ok = False
            ts = obj.get("ts")
            if isinstance(ts, int):
                if first_ts is None:
                    first_ts = ts
                if prev_ts is not None:
                    if ts < prev_ts:
                        ts_back += 1
                    if len(inter) < 5000:
                        inter.append(float(ts - prev_ts))
                prev_ts = ts
                last_ts = ts

            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            if mtype == "snapshot":
                if obj.get("type") == "rotation_checkpoint":
                    checkpoints += 1
                    start_bids = len(data.get("b") or [])
                    start_asks = len(data.get("a") or [])
                book.apply_snapshot(data)
                prev_seq = book.last_seq
            elif mtype == "delta":
                deltas += 1
                if not book.is_valid and checkpoints == 0:
                    reconstruction_ok = False
                    notes.append("delta_before_checkpoint")
                    continue
                warns = book.apply_delta(data)
                if any(w.startswith("seq_dup") for w in warns):
                    u_dups += 1
                if any(w.startswith("seq_gap") for w in warns):
                    u_gaps += 1
                    reconstruction_ok = False
                    notes.append("u_gap")
                seq = data.get("seq")
                if prev_seq is not None and seq is not None and int(seq) > int(prev_seq) + 1:
                    seq_jumps += 1
                if seq is not None:
                    prev_seq = int(seq)
            else:
                schema_ok = False
                continue

            if events % 200 == 1:
                for side in ("b", "a"):
                    for item in data.get(side) or []:
                        try:
                            p = Decimal(item[0])
                            q = Decimal(item[1])
                            if p <= ZERO or q < ZERO:
                                bad_pq += 1
                        except Exception:
                            bad_pq += 1
                if book.is_valid:
                    depths.append(len(book.bids) + len(book.asks))
                    if not book.bids or not book.asks:
                        empties += 1
                    elif book.is_crossed():
                        crosses += 1

            if collect_samples and book.is_valid and isinstance(ts, int):
                if sample_first_ts is None:
                    sample_first_ts = ts
                bucket = (ts // sample_ms) * sample_ms
                if last_emit_bucket is None or bucket > last_emit_bucket:
                    last_emit_bucket = bucket
                    warmup = sample_first_ts is not None and (ts - sample_first_ts) < warmup_ms
                    row = sample_from_mutable_book(
                        ref.symbol,
                        bucket,
                        book,
                        source_file=str(ref.path),
                        warmup=warmup,
                    )
                    if row is not None:
                        samples.append(row)
    except Exception as exc:
        decompress_ok = False
        schema_ok = False
        reconstruction_ok = False
        notes.append(f"read_error:{type(exc).__name__}:{exc}")

    digest = hasher.hexdigest()
    if manifest is not None:
        sha_ok = digest == str(manifest.get("sha256") or "")
    else:
        notes.append("missing_manifest")

    end_bids = end_asks = None
    end_bb = end_ba = None
    end_valid = False
    if book.is_valid:
        n_b, n_a, end_bb, end_ba, _, _ = book.end_fingerprint()
        end_bids, end_asks = n_b, n_a
        end_valid = bool(book.bids and book.asks and not book.is_crossed())

    if not decompress_ok or not schema_ok or events == 0:
        verdict = "CORRUPT"
    elif reconstruction_ok and end_valid and u_gaps == 0 and crosses == 0:
        verdict = "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT"
    elif u_gaps == 0 and decompress_ok and events > 0:
        verdict = "PARTIAL_BUT_DISCOVERY_USABLE"
    else:
        verdict = "NOT_REPLAYABLE"

    notes.append(META_NOTE)

    audit = SegmentAudit(
        symbol=ref.symbol,
        path=str(ref.path),
        manifest_path=str(manifest_path),
        start_utc=ref.start_utc.isoformat().replace("+00:00", "Z"),
        end_utc=ref.end_utc.isoformat().replace("+00:00", "Z"),
        duration_sec=ref.duration_sec,
        is_boundary_stub=ref.is_boundary_stub,
        manifest_present=manifest_present,
        sha256_ok=sha_ok,
        decompress_ok=decompress_ok,
        schema_ok=schema_ok,
        event_count_read=events,
        event_count_manifest=None if manifest is None else int(manifest.get("event_count") or 0),
        checkpoint_count_read=checkpoints,
        checkpoint_count_manifest=None if manifest is None else int(manifest.get("checkpoint_count") or 0),
        delta_count_read=deltas,
        delta_count_manifest=None if manifest is None else int(manifest.get("delta_count") or 0),
        native_snapshot_count_manifest=None
        if manifest is None
        else int(manifest.get("native_snapshot_count") or 0),
        first_event_ts=first_ts,
        last_event_ts=last_ts,
        start_checkpoint_bids=start_bids,
        start_checkpoint_asks=start_asks,
        end_bids=end_bids,
        end_asks=end_asks,
        end_best_bid=end_bb,
        end_best_ask=end_ba,
        u_gaps=u_gaps,
        u_dups=u_dups,
        u_monotonic_ok=u_gaps == 0,
        seq_jumps=seq_jumps,
        seq_jump_is_loss="no",
        ts_backsteps=ts_back,
        max_inter_event_ms=max(inter) if inter else None,
        p95_inter_event_ms=(
            statistics.quantiles(inter, n=20)[18]
            if len(inter) >= 20
            else (statistics.median(inter) if inter else None)
        ),
        queue_overflow_manifest=None if manifest is None else int(manifest.get("queue_overflow") or 0),
        writer_errors_manifest=None if manifest is None else int(manifest.get("writer_errors") or 0),
        crossed_book_events=crosses,
        empty_book_events=empties,
        invalid_price_qty=bad_pq,
        min_depth=min(depths) if depths else None,
        max_depth=max(depths) if depths else None,
        median_depth=statistics.median(depths) if depths else None,
        reconstruction_ok=reconstruction_ok and u_gaps == 0,
        end_book_valid=end_valid,
        manifest_replayable=None if manifest is None else bool(manifest.get("replayable")),
        manifest_completion_status=None if manifest is None else str(manifest.get("completion_status")),
        replay_verdict=verdict,
        notes="; ".join(notes),
        sequence_gap_count_manifest=None if manifest is None else int(manifest.get("sequence_gap_count") or 0),
    )
    return audit, samples


def audit_segment(ref: SegmentRef) -> SegmentAudit:
    audit, _ = process_segment(ref, collect_samples=False)
    return audit


def audit_to_row(a: SegmentAudit) -> dict[str, Any]:
    return asdict(a)
