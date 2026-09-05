"""Event package writer: JSONL.zst + atomic finalize + manifest."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2_live.full_book_state import RPI_INCLUDED_IN_FULL_OB
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.record_envelope import (
    level_update_count,
)

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_event_id(symbol: str, ts: datetime) -> str:
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{symbol.upper()}_{stamp}_{uuid.uuid4().hex[:10]}"


def event_dir(root: Path, symbol: str, ts: datetime, event_id: str) -> Path:
    ts = ts.astimezone(timezone.utc)
    return root / symbol.upper() / f"{ts.year:04d}-{ts.month:02d}-{ts.day:02d}" / event_id


@dataclass
class FileHasher:
    path: Path
    sha = hashlib.sha256()
    count: int = 0
    bytes_in: int = 0

    def update(self, data: bytes) -> None:
        self.sha.update(data)
        self.count += 1
        self.bytes_in += len(data)

    def hex(self) -> str:
        return self.sha.hexdigest()


@dataclass
class ActiveEventWriter:
    event_id: str
    symbol: str
    directory: Path
    started_at: datetime
    status: str = "open"
    trigger_reason: str = ""
    trigger_meta: dict[str, Any] = field(default_factory=dict)
    profile_context: dict[str, Any] = field(default_factory=dict)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    sequence: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    continuation_index: int = 0
    fight_event_id: str = ""
    extra_manifest: dict[str, Any] = field(default_factory=dict)
    previous_segment_sha256: str | None = None
    segment_first_ts: datetime | None = None
    segment_last_ts: datetime | None = None
    first_u: int | None = None
    last_u: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    first_cts: int | None = None
    last_cts: int | None = None
    delta_count: int = 0
    trade_count: int = 0
    gap_count: int = 0
    reconnect_count: int = 0
    buffer_overflow: int = 0
    continuity_warning_count: int = 0
    last_continuity_warning: str | None = None
    queue_drops: int = 0
    persisted_u_gap_count: int = 0
    persisted_missing_u_estimate: int = 0
    persisted_gap_intervals: list[list[int]] = field(default_factory=list)
    level_update_count: int = 0
    snapshot_bid_levels: int = 0
    snapshot_ask_levels: int = 0
    snapshot_ts: int | None = None
    prebuffer_start_ns: int | None = None
    flush_count: int = 0
    last_orjson_ns: int = 0
    last_zstd_ns: int = 0
    last_flush_ns: int = 0
    _delta_fh: Any = None
    _delta_comp: Any = None
    _trade_fh: Any = None
    _trade_comp: Any = None
    _delta_hash: FileHasher | None = None
    _trade_hash: FileHasher | None = None
    _snap_path: Path | None = None
    _snap_hash: str = ""
    _opened: bool = False
    _bytes_since_flush: int = 0

    def open(self) -> None:
        if zstd is None:
            raise RuntimeError("zstandard required")
        self.directory.mkdir(parents=True, exist_ok=True)
        delta_tmp = self.directory / "full_ob_raw_deltas.jsonl.zst.tmp"
        trade_tmp = self.directory / "public_trades_raw.jsonl.zst.tmp"
        self._delta_fh = open(delta_tmp, "wb")
        self._trade_fh = open(trade_tmp, "wb")
        # Streaming compressor: no per-record frame flush.
        self._delta_comp = zstd.ZstdCompressor(level=3).stream_writer(
            self._delta_fh, closefd=False, write_return_read=True
        )
        self._trade_comp = zstd.ZstdCompressor(level=3).stream_writer(
            self._trade_fh, closefd=False, write_return_read=True
        )
        self._delta_hash = FileHasher(delta_tmp)
        self._trade_hash = FileHasher(trade_tmp)
        self._opened = True
        self._bytes_since_flush = 0
        if not self.fight_event_id:
            self.fight_event_id = self.event_id

    def mark_incomplete(self, reason: str) -> None:
        self.status = reason
        self.coverage["incomplete_reason"] = reason

    @property
    def open_tmp_bytes(self) -> int:
        total = 0
        for name in (
            "full_ob_raw_deltas.jsonl.zst.tmp",
            "public_trades_raw.jsonl.zst.tmp",
            "rest_full_snapshot.json.zst.tmp",
        ):
            p = self.directory / name
            if p.exists():
                total += p.stat().st_size
        return total

    def write_rest_snapshot(self, snapshot: dict[str, Any]) -> None:
        path = self.directory / "rest_full_snapshot.json.zst.tmp"
        raw = orjson.dumps(snapshot)
        cctx = zstd.ZstdCompressor(level=3)
        compressed = cctx.compress(raw)
        path.write_bytes(compressed)
        self._snap_path = path
        self._snap_hash = hashlib.sha256(compressed).hexdigest()
        self.snapshot_bid_levels = len(snapshot.get("b") or [])
        self.snapshot_ask_levels = len(snapshot.get("a") or [])
        self.snapshot_ts = snapshot.get("ts") or snapshot.get("cts")
        u = snapshot.get("u")
        seq = snapshot.get("seq")
        if u is not None:
            self.first_u = int(u)
            self.last_u = int(u)
        if seq is not None:
            self.first_seq = int(seq)
            self.last_seq = int(seq)
        cts = snapshot.get("cts")
        if cts is not None:
            self.first_cts = int(cts)
            self.last_cts = int(cts)

    def write_resync_checkpoint(self, snapshot: dict[str, Any], *, epoch_id: int) -> None:
        """Persist an immutable mid-event REST/resync seed beside the delta stream."""
        name = f"resync_checkpoint_epoch_{int(epoch_id)}.json.zst"
        path = self.directory / f"{name}.tmp"
        raw = orjson.dumps(snapshot)
        compressed = zstd.ZstdCompressor(level=3).compress(raw)
        path.write_bytes(compressed)
        final = self.directory / name
        path.replace(final)
        # Continuity baseline will be reset when the RESYNC_CHECKPOINT record is appended.

    def append_delta(self, record: dict[str, Any]) -> None:
        self.append_delta_batch([record])

    def append_delta_batch(self, records: list[dict[str, Any]]) -> tuple[int, int]:
        """Serialize and stream-compress many JSONL records. No fsync."""
        if not self._opened:
            raise RuntimeError("writer not open")
        if not records:
            return 0, 0
        t0 = time.perf_counter_ns()
        parts: list[bytes] = []
        levels = 0
        written_n = 0
        for record in records:
            # Strip internal sizing hints before persistence.
            if "_approx_bytes" in record:
                rec = {k: v for k, v in record.items() if k != "_approx_bytes"}
            else:
                rec = dict(record)
            # Normalize timezone-aware datetime ts so markers never trip orjson/int(ts).
            ts_val = rec.get("ts")
            if isinstance(ts_val, datetime):
                rec["ts"] = _iso(ts_val)
            try:
                line = orjson.dumps(rec) + b"\n"
            except Exception as exc:
                # Fail-closed for this event, but do not discard the rest of the batch.
                self.continuity_warning_count += 1
                self.last_continuity_warning = f"orjson:{type(exc).__name__}:{exc}"
                self.mark_incomplete("INVALID_RECORD_TS")
                continue
            parts.append(line)
            lvl = int(rec.get("level_update_count") or level_update_count(rec))
            levels += lvl
            self._note_continuity(rec)
            written_n += 1
        if not parts:
            return 0, 0
        blob = b"".join(parts)
        t1 = time.perf_counter_ns()
        self._delta_comp.write(blob)
        t2 = time.perf_counter_ns()
        assert self._delta_hash is not None
        self._delta_hash.update(blob)
        self.delta_count += written_n
        self.level_update_count += levels
        self._bytes_since_flush += len(blob)
        self.last_orjson_ns = t1 - t0
        self.last_zstd_ns = t2 - t1
        return len(blob), levels

    def _note_continuity(self, record: dict[str, Any]) -> None:
        from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
            CHECKPOINT_KINDS,
            RECORD_BOOK_DELTA,
            RECORD_RESYNC_BOUNDARY,
            is_book_delta_record,
        )

        kind = record.get("record_kind")
        # Markers / boundaries never participate in u+1 continuity.
        if (
            record.get("channel") == "marker"
            or record.get("marker_type")
            or kind == RECORD_RESYNC_BOUNDARY
            or kind == "EVENT_MARKER"
            or kind == "EVENT_END"
        ):
            return

        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        u = data.get("u")
        seq = data.get("seq")
        cts = record.get("cts") or data.get("cts")

        # Checkpoint seeds reset the within-epoch baseline (no gap vs prior epoch).
        if kind in CHECKPOINT_KINDS:
            if u is not None:
                ui = int(u)
                if self.first_u is None:
                    self.first_u = ui
                self.last_u = ui
                self._last_epoch_id = record.get("continuity_epoch_id")
            if seq is not None:
                si = int(seq)
                if self.first_seq is None:
                    self.first_seq = si
                self.last_seq = si
            return

        if not is_book_delta_record(record) and kind not in (None, RECORD_BOOK_DELTA):
            return

        if u is not None:
            ui = int(u)
            epoch = record.get("continuity_epoch_id")
            # Only count forward gaps inside the same continuity epoch.
            if (
                self.last_u is not None
                and epoch is not None
                and getattr(self, "_last_epoch_id", None) is not None
                and epoch == self._last_epoch_id
                and ui > self.last_u + 1
            ):
                missing = ui - self.last_u - 1
                self.persisted_u_gap_count += 1
                self.persisted_missing_u_estimate += missing
                if len(self.persisted_gap_intervals) < 64:
                    self.persisted_gap_intervals.append([self.last_u + 1, ui - 1, missing])
            elif (
                self.last_u is not None
                and epoch is None
                and ui > self.last_u + 1
            ):
                # Legacy records without epoch id: keep prior behavior.
                missing = ui - self.last_u - 1
                self.persisted_u_gap_count += 1
                self.persisted_missing_u_estimate += missing
                if len(self.persisted_gap_intervals) < 64:
                    self.persisted_gap_intervals.append([self.last_u + 1, ui - 1, missing])
            if self.first_u is None:
                self.first_u = ui
            self.last_u = ui
            if epoch is not None:
                self._last_epoch_id = epoch
        if seq is not None:
            si = int(seq)
            if self.first_seq is None:
                self.first_seq = si
            self.last_seq = si
        if cts is not None:
            try:
                ci = int(cts)
            except (TypeError, ValueError):
                ci = None
            if ci is not None:
                if self.first_cts is None:
                    self.first_cts = ci
                self.last_cts = ci
        ts_raw = record.get("ts")
        if isinstance(ts_raw, datetime):
            tdt = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
            if self.segment_first_ts is None:
                self.segment_first_ts = tdt.astimezone(timezone.utc)
            self.segment_last_ts = tdt.astimezone(timezone.utc)
            return
        ts_ms = ts_raw if isinstance(ts_raw, (int, float)) else None
        if ts_ms is None and isinstance(data.get("ts"), (int, float)):
            ts_ms = data.get("ts")
        if ts_ms is None and isinstance(cts, (int, float)):
            ts_ms = cts
        if ts_ms is None and ts_raw is not None:
            self.continuity_warning_count += 1
            self.last_continuity_warning = f"invalid_ts_type:{type(ts_raw).__name__}:{ts_raw!r}"[:200]
            self.mark_incomplete("INVALID_RECORD_TS")
            return
        if ts_ms is not None:
            try:
                tdt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError) as exc:
                self.continuity_warning_count += 1
                self.last_continuity_warning = f"invalid_ts_value:{exc}"[:200]
                self.mark_incomplete("INVALID_RECORD_TS")
                return
            if self.segment_first_ts is None:
                self.segment_first_ts = tdt
            self.segment_last_ts = tdt

    def flush_pending(self) -> None:
        """Flush compressor/OS buffers without fsync (interval / rollover / shutdown)."""
        if not self._opened:
            return
        t0 = time.perf_counter_ns()
        if self._delta_comp is not None:
            self._delta_comp.flush(zstd.FLUSH_BLOCK)
        if self._trade_comp is not None:
            self._trade_comp.flush(zstd.FLUSH_BLOCK)
        if self._delta_fh is not None and not self._delta_fh.closed:
            self._delta_fh.flush()
        if self._trade_fh is not None and not self._trade_fh.closed:
            self._trade_fh.flush()
        self.last_flush_ns = time.perf_counter_ns() - t0
        self.flush_count += 1
        self._bytes_since_flush = 0

    def append_trade(self, record: dict[str, Any]) -> None:
        if not self._opened:
            raise RuntimeError("writer not open")
        line = orjson.dumps(record) + b"\n"
        self._trade_comp.write(line)
        self._trade_hash.update(line)
        self.trade_count += 1

    def finalize(self, *, ended_at: datetime, status: str, report_md: str) -> dict[str, Any]:
        if not self._opened:
            raise RuntimeError("writer not open")
        # Final frame flush + durable fsync only at finalize / segment close.
        self._delta_comp.flush(zstd.FLUSH_FRAME)
        self._trade_comp.flush(zstd.FLUSH_FRAME)
        self._delta_comp.close()
        self._trade_comp.close()
        self._delta_comp = None
        self._trade_comp = None
        for fh in (self._delta_fh, self._trade_fh):
            if fh is not None and not fh.closed:
                fh.flush()
                os.fsync(fh.fileno())
                fh.close()
        self._delta_fh = None
        self._trade_fh = None
        self.flush_count += 1

        delta_final = self.directory / "full_ob_raw_deltas.jsonl.zst"
        trade_final = self.directory / "public_trades_raw.jsonl.zst"
        os.replace(self.directory / "full_ob_raw_deltas.jsonl.zst.tmp", delta_final)
        os.replace(self.directory / "public_trades_raw.jsonl.zst.tmp", trade_final)
        snap_final = None
        if self._snap_path and self._snap_path.exists():
            snap_final = self.directory / "rest_full_snapshot.json.zst"
            os.replace(self._snap_path, snap_final)

        def _file_sha(path: Path) -> str:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        sha = {
            "full_ob_raw_deltas.jsonl.zst": _file_sha(delta_final),
            "public_trades_raw.jsonl.zst": _file_sha(trade_final),
            "rest_full_snapshot.json.zst": _file_sha(snap_final) if snap_final else "",
        }
        manifest = {
            "contract_version": CONTRACT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "symbol": self.symbol,
            "trigger_reason": self.trigger_reason,
            "trigger_time": self.trigger_meta.get("trigger_time"),
            "relevant_edge": self.trigger_meta.get("edge"),
            "profile_cutoff": self.profile_context.get("cutoff"),
            "event_start": _iso(self.started_at),
            "event_end": _iso(ended_at),
            "prebuffer_start_ns": self.prebuffer_start_ns,
            "snapshot_timestamp": self.snapshot_ts,
            "first_cts": self.first_cts,
            "last_cts": self.last_cts,
            "first_u": self.first_u,
            "last_u": self.last_u,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "delta_count": self.delta_count,
            "level_update_count": self.level_update_count,
            "trade_count": self.trade_count,
            "snapshot_bid_levels": self.snapshot_bid_levels,
            "snapshot_ask_levels": self.snapshot_ask_levels,
            "gap_count": self.gap_count,
            "reconnect_count": self.reconnect_count,
            "buffer_overflow": self.buffer_overflow,
            "queue_drops": self.queue_drops,
            "persisted_u_gap_count": self.persisted_u_gap_count,
            "persisted_missing_u_estimate": self.persisted_missing_u_estimate,
            "persisted_gap_intervals": list(self.persisted_gap_intervals),
            "source_feed_u_gap_count": self.gap_count,
            "flush_count": self.flush_count,
            "completion_status": status,
            "rpi_included": RPI_INCLUDED_IN_FULL_OB,
            "data_sources": {
                "full_ob_ws": "orderbook.full.{symbol}",
                "full_ob_rest": "/v5/market/full_orderbook",
                "public_trades": "canonical_or_ws",
            },
            "sha256": sha,
            "config": self.config_snapshot,
            "storage_root": str(self.directory.parent.parent.parent),
            "event_directory": str(self.directory),
            "fight_event_id": self.fight_event_id or self.event_id,
            "continuation_index": self.continuation_index,
            "previous_segment_sha256": self.previous_segment_sha256,
            "segment_sha256": sha.get("full_ob_raw_deltas.jsonl.zst"),
            "segment_first_u": self.first_u,
            "segment_last_u": self.last_u,
        }
        manifest.update(self.extra_manifest)
        (self.directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / "profile_context.json").write_text(
            json.dumps(self.profile_context, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        (self.directory / "lifecycle.json").write_text(
            json.dumps(self.lifecycle, indent=2) + "\n", encoding="utf-8"
        )
        (self.directory / "coverage_audit.json").write_text(
            json.dumps(self.coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / "sequence_integrity.json").write_text(
            json.dumps(self.sequence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / "health_summary.json").write_text(
            json.dumps(self.health, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary = {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "status": status,
            "delta_count": self.delta_count,
            "level_update_count": self.level_update_count,
            "trade_count": self.trade_count,
            "trigger_reason": self.trigger_reason,
        }
        (self.directory / "event_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / "REPORT.md").write_text(report_md, encoding="utf-8")
        self.status = status
        self._opened = False
        return manifest
