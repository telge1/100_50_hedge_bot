"""Independent replay and integrity verification for one hourly segment."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Iterator

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from .checkpoint import book_sha256
from .envelope import payload_sha256

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]


def read_uncompressed(path: Path) -> bytes:
    if zstd is None:
        raise RuntimeError("zstandard not installed")
    with zstd.ZstdDecompressor().stream_reader(io.BytesIO(path.read_bytes())) as reader:
        return reader.read()


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    for line_no, line in enumerate(read_uncompressed(path).splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"line {line_no}: envelope is not an object")
        yield obj


def manifest_path_for(segment: Path) -> Path:
    return Path(str(segment) + ".manifest.json")


def replay_segment(
    segment: Path,
    *,
    verify_hashes: bool = True,
    verify_sequence: bool = True,
    verify_final_book: bool = True,
) -> dict[str, Any]:
    segment = Path(segment)
    errors: list[str] = []
    manifest_path = manifest_path_for(segment)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    raw = read_uncompressed(segment)
    if verify_hashes and manifest:
        if hashlib.sha256(segment.read_bytes()).hexdigest() != manifest.get("segment_sha256"):
            errors.append("segment_sha256_mismatch")
        if hashlib.sha256(raw).hexdigest() != manifest.get("payload_sha256"):
            errors.append("payload_sha256_mismatch")
    state: FullBookState | None = None
    anchored = False
    records = 0
    applied_deltas = 0
    ignored_pre_anchor = 0
    checkpoints = 0
    previous_u: int | None = None
    for record in iter_records(segment):
        records += 1
        payload = record.get("original_payload")
        if not isinstance(payload, dict):
            errors.append(f"record_{records}_missing_payload")
            continue
        if verify_hashes and payload_sha256(payload) != record.get("payload_sha256"):
            errors.append(f"record_{records}_payload_sha256_mismatch")
        symbol = str(record.get("symbol") or manifest.get("symbol") or "").upper()
        if state is None:
            state = FullBookState(symbol=symbol)
        kind = str(record.get("message_type") or "")
        if kind == "checkpoint":
            checkpoints += 1
            bids, asks = payload.get("bids") or [], payload.get("asks") or []
            if verify_final_book and book_sha256(bids, asks) != payload.get("book_sha256"):
                errors.append(f"record_{records}_checkpoint_book_sha256_mismatch")
            state.apply_snapshot(
                bids=bids,
                asks=asks,
                u=payload.get("u"),
                seq=payload.get("seq"),
                ts_ms=None if payload.get("event_time") is None else int(payload["event_time"]) // 1_000_000,
                receive_time_ns=payload.get("receive_time"),
                mark_ready=True,
            )
            anchored = True
            previous_u = state.update_id
            continue
        if kind == "snapshot":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            state.apply_snapshot(
                bids=data.get("b") or [],
                asks=data.get("a") or [],
                u=data.get("u"),
                seq=data.get("seq"),
                ts_ms=payload.get("ts") or data.get("ts"),
                cts_ms=payload.get("cts") or data.get("cts"),
                receive_time_ns=record.get("receive_time_ns"),
                mark_ready=True,
            )
            anchored = True
            previous_u = state.update_id
            continue
        if kind != "delta":
            continue
        if not anchored:
            ignored_pre_anchor += 1
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        current_u = data.get("u")
        if verify_sequence and previous_u is not None and current_u is not None:
            if int(current_u) not in {previous_u, previous_u + 1}:
                errors.append(f"record_{records}_u_gap:{previous_u}->{current_u}")
        outcome = state.apply_delta(
            bids=data.get("b") or [],
            asks=data.get("a") or [],
            u=current_u,
            seq=data.get("seq"),
            ts_ms=payload.get("ts") or data.get("ts"),
            cts_ms=payload.get("cts") or data.get("cts"),
            receive_time_ns=record.get("receive_time_ns"),
            enforce_continuity=verify_sequence,
        )
        if outcome is DeltaOutcome.APPLIED:
            applied_deltas += 1
            previous_u = state.update_id
        elif outcome not in {DeltaOutcome.IGNORED_DUP_U, DeltaOutcome.IGNORED_STALE_U}:
            errors.append(f"record_{records}_apply_{outcome.value}")
    final_bids: list[list[float]] = []
    final_asks: list[list[float]] = []
    if state is not None:
        final_bids, final_asks = state.copy_consistent_snapshot().full_levels()
    if verify_final_book and (state is None or not state.book_ready):
        errors.append("no_replayable_anchor")
    return {
        "ok": not errors,
        "segment": str(segment),
        "completion_status": manifest.get("completion_status"),
        "records": records,
        "checkpoint_count": checkpoints,
        "applied_delta_count": applied_deltas,
        "ignored_pre_anchor_count": ignored_pre_anchor,
        "anchored": anchored,
        "first_u": manifest.get("first_u"),
        "last_u": None if state is None else state.update_id,
        "final_bid_level_count": len(final_bids),
        "final_ask_level_count": len(final_asks),
        "final_book_sha256": None if state is None else book_sha256(final_bids, final_asks),
        "errors": errors,
    }
