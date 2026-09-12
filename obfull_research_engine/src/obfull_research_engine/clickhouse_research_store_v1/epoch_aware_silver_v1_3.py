"""Epoch-aware, canonical-order Silver replay for bounded v1.3 pilots.

The production source of truth is ClickHouse Bronze plus the persisted canonical
segment registry. Audit files under ``runs/`` are not read by this module.
"""

from __future__ import annotations

import hashlib
import json
import resource
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from .canonical_segment_order_v1_3 import (
    DATABASE_V13,
    EVENTS_TABLE_V13,
    SEGMENTS_TABLE_V13,
)
from .helpers import iso_to_ns_exact
from .silver_replay import (
    BronzeRecord,
    _BOOK_PACK,
    _dt_to_ns,
    _emit_side_changes,
    _floor_bucket,
    _metric_row,
    _ns_to_dt,
    book_map_sha256,
    bronze_row_from_ch,
)

EPOCHS_TABLE = "replay_epochs_epoch_pilot_v1_3"
LEVEL_CHANGES_TABLE = "ob_level_changes_epoch_pilot_v1_3"
STATES_TABLE = "ob_states_100ms_epoch_pilot_v1_3"
CHUNKS_TABLE = "ob_silver_chunks_epoch_pilot_v1_3"
SCHEMA_VERSION = "epoch_aware_silver_pilot_v1_3"
RSS_LIMIT_KB = 1_500 * 1024
BUCKET_NS = 100_000_000
INSERT_BATCH_SIZE = 2_000


class EpochSilverError(RuntimeError):
    """Hard-stop error carrying a STOP_* verdict."""


@dataclass
class BookHashProfile:
    calls: int = 0
    cache_hits: int = 0
    recomputations: int = 0
    dirty_marks: int = 0
    no_op_deltas: int = 0
    canonical_representation_s: float = 0.0
    level_sort_s: float = 0.0
    serialization_s: float = 0.0
    sha256_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["total_hash_pipeline_s"] = round(
            self.canonical_representation_s
            + self.level_sort_s
            + self.serialization_s
            + self.sha256_s,
            6,
        )
        for key in (
            "canonical_representation_s",
            "level_sort_s",
            "serialization_s",
            "sha256_s",
        ):
            result[key] = round(float(result[key]), 6)
        return result


def canonical_book_bytes_profiled(
    bids: dict[float, float],
    asks: dict[float, float],
    profile: BookHashProfile,
) -> bytes:
    """Build exactly the bytes consumed by ``book_map_sha256`` with timings."""
    started = time.perf_counter()
    bid_items = [(float(p), float(q)) for p, q in bids.items() if q and float(q) > 0]
    ask_items = [(float(p), float(q)) for p, q in asks.items() if q and float(q) > 0]
    profile.canonical_representation_s += time.perf_counter() - started

    started = time.perf_counter()
    bid_items.sort()
    ask_items.sort()
    profile.level_sort_s += time.perf_counter() - started

    started = time.perf_counter()
    raw = b"".join(
        [_BOOK_PACK.pack(0, p, q) for p, q in bid_items]
        + [_BOOK_PACK.pack(1, p, q) for p, q in ask_items]
    )
    profile.serialization_s += time.perf_counter() - started
    return raw


class BookHashCache:
    """Lossless dirty-state cache for the existing canonical SHA-256."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.dirty = True
        self.value: str | None = None
        self.profile = BookHashProfile()

    def mark_dirty(self) -> None:
        self.dirty = True
        self.profile.dirty_marks += 1

    def mark_no_op(self) -> None:
        self.profile.no_op_deltas += 1

    def get(self, state: FullBookState) -> str:
        self.profile.calls += 1
        if self.enabled and not self.dirty and self.value is not None:
            self.profile.cache_hits += 1
            return self.value
        raw = canonical_book_bytes_profiled(state.bids, state.asks, self.profile)
        started = time.perf_counter()
        value = hashlib.sha256(raw).hexdigest()
        self.profile.sha256_s += time.perf_counter() - started
        self.profile.recomputations += 1
        self.value = value
        self.dirty = False
        return value


EPOCH_SILVER_DDLS = (
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{EPOCHS_TABLE}
    (
        epoch_id FixedString(64),
        epoch_hash FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        symbol LowCardinality(String),
        anchor_type LowCardinality(String),
        anchor_provenance String,
        anchor_event_time_ns UInt64,
        anchor_event_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(anchor_event_time_ns, 'UTC'),
        anchor_receive_time_ns UInt64,
        anchor_receive_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(anchor_receive_time_ns, 'UTC'),
        anchor_u UInt64,
        anchor_seq UInt64,
        anchor_segment_chain_index UInt64,
        anchor_record_ordinal UInt64,
        safe_start_ns UInt64,
        safe_start DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(safe_start_ns, 'UTC'),
        safe_end_ns UInt64,
        safe_end DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(safe_end_ns, 'UTC'),
        terminating_reason LowCardinality(String),
        preceding_gap_id String,
        status LowCardinality(String),
        reconnect_resync_proven UInt8,
        version_ms UInt64
    )
    ENGINE = ReplacingMergeTree(version_ms)
    ORDER BY (chain_version, epoch_id)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{LEVEL_CHANGES_TABLE}
    (
        row_id FixedString(64),
        build_id FixedString(64),
        chunk_key FixedString(64),
        epoch_id FixedString(64),
        epoch_hash FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        symbol LowCardinality(String),
        canonical_segment_chain_index UInt64,
        record_ordinal UInt64,
        apply_order UInt64,
        event_time_ns UInt64,
        event_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(event_time_ns, 'UTC'),
        payload String,
        version_ms UInt64
    )
    ENGINE = ReplacingMergeTree(version_ms)
    ORDER BY (build_id, chunk_key, row_id)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{STATES_TABLE}
    (
        row_id FixedString(64),
        build_id FixedString(64),
        chunk_key FixedString(64),
        epoch_id FixedString(64),
        epoch_hash FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        symbol LowCardinality(String),
        bucket_start_ns UInt64,
        bucket_start DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(bucket_start_ns, 'UTC'),
        payload String,
        version_ms UInt64
    )
    ENGINE = ReplacingMergeTree(version_ms)
    ORDER BY (build_id, chunk_key, row_id)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{CHUNKS_TABLE}
    (
        chunk_key FixedString(64),
        build_id FixedString(64),
        epoch_id FixedString(64),
        epoch_hash FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        symbol LowCardinality(String),
        chunk_start_ns UInt64,
        chunk_end_ns UInt64,
        warmup_ns UInt64,
        status LowCardinality(String),
        stop_reason String,
        level_change_count UInt64,
        state_count UInt64,
        source_record_count UInt64,
        output_hash FixedString(64),
        version_ms UInt64
    )
    ENGINE = ReplacingMergeTree(version_ms)
    ORDER BY chunk_key
    """.strip(),
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _check_rss() -> None:
    if _rss_kb() > RSS_LIMIT_KB:
        raise EpochSilverError(
            f"STOP_SILVER_MEMORY_LIMIT: peak_rss_kb={_rss_kb()}"
        )


def _record_key(record: BronzeRecord) -> tuple[int, int]:
    if record.canonical_segment_chain_index is None:
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: missing canonical segment rank"
        )
    return int(record.canonical_segment_chain_index), int(record.record_ordinal)


def _causal_ns(record: BronzeRecord) -> int:
    return int(record.event_time_ns or record.receive_time_ns)


def _payload(record: BronzeRecord) -> dict[str, Any]:
    return record.original_payload if isinstance(record.original_payload, dict) else {}


def _checkpoint_reason(record: BronzeRecord) -> str:
    return str(_payload(record).get("checkpoint_reason") or "")


def _gap_reason(record: BronzeRecord) -> str:
    details = _payload(record).get("details")
    return str(details.get("reason") or "sequence_gap") if isinstance(details, dict) else "sequence_gap"


def _anchor_parts(
    record: BronzeRecord,
) -> tuple[list[Any], list[Any], int | None, int | None, int]:
    payload = _payload(record)
    if record.message_type == "snapshot":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return (
            data.get("b") or data.get("bids") or [],
            data.get("a") or data.get("asks") or [],
            data.get("u"),
            data.get("seq"),
            int(payload.get("ts") or data.get("ts") or record.event_time_ns // 1_000_000),
        )
    return (
        payload.get("bids") or [],
        payload.get("asks") or [],
        payload.get("u"),
        payload.get("seq"),
        int(payload.get("event_time") or record.event_time_ns) // 1_000_000,
    )


def _anchor_book_hash(record: BronzeRecord) -> str | None:
    bids, asks, _, _, _ = _anchor_parts(record)
    if not bids or not asks:
        return None
    state = FullBookState(symbol=record.symbol)
    state.apply_snapshot(
        bids=bids,
        asks=asks,
        u=None,
        seq=None,
        ts_ms=record.event_time_ns // 1_000_000,
        receive_time_ns=record.receive_time_ns,
        mark_ready=True,
    )
    return book_map_sha256(state.bids, state.asks)


def _apply_anchor(record: BronzeRecord) -> FullBookState:
    bids, asks, u, seq, ts_ms = _anchor_parts(record)
    if not bids or not asks:
        raise EpochSilverError(
            "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: incomplete full-book anchor"
        )
    state = FullBookState(symbol=record.symbol)
    state.apply_snapshot(
        bids=bids,
        asks=asks,
        u=u,
        seq=seq,
        ts_ms=ts_ms,
        receive_time_ns=record.receive_time_ns,
        mark_ready=True,
    )
    return state


def _delta_changes_book(
    state: FullBookState, bids: list[Any], asks: list[Any]
) -> bool:
    for book, levels in ((state.bids, bids), (state.asks, asks)):
        for item in levels:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            price, quantity = float(item[0]), float(item[1])
            effective = quantity if quantity > 0 else 0.0
            if float(book.get(price, 0.0) or 0.0) != effective:
                return True
    return False


@dataclass
class GapDefinition:
    gap_id: str
    reason: str
    segment_chain_index: int
    record_ordinal: int
    gap_time_ns: int


@dataclass
class EpochDefinition:
    epoch_id: str
    epoch_hash: str
    chain_version: str
    canonical_chain_hash: str
    symbol: str
    anchor_type: str
    anchor_provenance: str
    anchor_event_time_ns: int
    anchor_receive_time_ns: int
    anchor_u: int
    anchor_seq: int
    anchor_segment_chain_index: int
    anchor_record_ordinal: int
    safe_start_ns: int
    safe_end_ns: int
    terminating_reason: str
    preceding_gap_id: str
    status: str
    reconnect_resync_proven: bool = False
    apply_end_segment_chain_index: int | None = None
    apply_end_record_ordinal: int | None = None

    def stable_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("epoch_hash", None)
        # Runtime-only exclusive source bound. The persisted epoch contract is
        # unchanged; this bound prevents event-time filtering during replay.
        payload.pop("apply_end_segment_chain_index", None)
        payload.pop("apply_end_record_ordinal", None)
        return payload

    def finalize_hash(self) -> None:
        self.epoch_hash = _hash(self.stable_payload())


@dataclass
class EpochDiscovery:
    epochs: list[EpochDefinition] = field(default_factory=list)
    gaps: list[GapDefinition] = field(default_factory=list)
    source_records: int = 0
    first_apply_key: tuple[int, int] | None = None
    last_apply_key: tuple[int, int] | None = None


def _make_gap(
    *,
    chain_version: str,
    chain_hash: str,
    record: BronzeRecord,
    reason: str,
) -> GapDefinition:
    rank, ordinal = _record_key(record)
    material = {
        "chain_version": chain_version,
        "canonical_chain_hash": chain_hash,
        "rank": rank,
        "ordinal": ordinal,
        "reason": reason,
    }
    return GapDefinition(
        gap_id=_hash(material),
        reason=reason,
        segment_chain_index=rank,
        record_ordinal=ordinal,
        gap_time_ns=_causal_ns(record),
    )


def _new_epoch(
    *,
    chain_version: str,
    chain_hash: str,
    record: BronzeRecord,
    anchor_type: str,
    provenance: str,
    preceding_gap_id: str,
) -> EpochDefinition:
    rank, ordinal = _record_key(record)
    _, _, u, seq, _ = _anchor_parts(record)
    identity = {
        "chain_version": chain_version,
        "canonical_chain_hash": chain_hash,
        "symbol": record.symbol,
        "anchor_type": anchor_type,
        "anchor_provenance": provenance,
        "rank": rank,
        "ordinal": ordinal,
        "record_id": record.record_id,
        "preceding_gap_id": preceding_gap_id,
    }
    start_ns = _causal_ns(record)
    return EpochDefinition(
        epoch_id=_hash(identity),
        epoch_hash="",
        chain_version=chain_version,
        canonical_chain_hash=chain_hash,
        symbol=record.symbol,
        anchor_type=anchor_type,
        anchor_provenance=provenance,
        anchor_event_time_ns=int(record.event_time_ns),
        anchor_receive_time_ns=int(record.receive_time_ns),
        anchor_u=int(u or 0),
        anchor_seq=int(seq or 0),
        anchor_segment_chain_index=rank,
        anchor_record_ordinal=ordinal,
        safe_start_ns=start_ns,
        safe_end_ns=start_ns,
        terminating_reason="OPEN",
        preceding_gap_id=preceding_gap_id,
        status="OPEN",
    )


def discover_epochs(
    records: Iterable[BronzeRecord],
    *,
    chain_version: str,
    canonical_chain_hash: str,
    scan_end_ns: int,
    clean_segment_start_ranks: set[int],
) -> EpochDiscovery:
    """Discover deterministic safe epochs in canonical physical apply order."""
    result = EpochDiscovery()
    active: EpochDefinition | None = None
    state: FullBookState | None = None
    tainted = False
    pending_gap_id = ""
    previous_key: tuple[int, int] | None = None
    last_snapshot: tuple[tuple[int, int], str] | None = None

    def close_active(
        end_ns: int, reason: str, terminal_key: tuple[int, int]
    ) -> None:
        nonlocal active, state
        if active is not None:
            active.safe_end_ns = max(active.safe_start_ns, int(end_ns))
            active.terminating_reason = reason
            active.status = "COMPLETE"
            active.apply_end_segment_chain_index = int(terminal_key[0])
            active.apply_end_record_ordinal = int(terminal_key[1])
            active.finalize_hash()
            result.epochs.append(active)
        active = None
        state = None

    def open_at(record: BronzeRecord, kind: str, provenance: str) -> None:
        nonlocal active, state, tainted, pending_gap_id
        if active is not None:
            close_active(
                _causal_ns(record),
                "INDEPENDENT_REANCHOR",
                _record_key(record),
            )
        state = _apply_anchor(record)
        active = _new_epoch(
            chain_version=chain_version,
            chain_hash=canonical_chain_hash,
            record=record,
            anchor_type=kind,
            provenance=provenance,
            preceding_gap_id=pending_gap_id,
        )
        tainted = False
        pending_gap_id = ""

    for record in records:
        result.source_records += 1
        key = _record_key(record)
        if previous_key is not None and key <= previous_key:
            raise EpochSilverError(
                "STOP_SILVER_REPLAY_ORDER_MISMATCH: records are not strictly canonical"
            )
        previous_key = key
        result.first_apply_key = result.first_apply_key or key
        result.last_apply_key = key
        kind = record.message_type

        if kind == "gap_marker":
            gap = _make_gap(
                chain_version=chain_version,
                chain_hash=canonical_chain_hash,
                record=record,
                reason=_gap_reason(record),
            )
            result.gaps.append(gap)
            close_active(gap.gap_time_ns, f"GAP:{gap.reason}", key)
            tainted = True
            pending_gap_id = gap.gap_id
            last_snapshot = None
            continue

        if kind == "snapshot":
            if _anchor_book_hash(record) is None:
                raise EpochSilverError(
                    "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: invalid exchange snapshot"
                )
            open_at(record, "exchange_snapshot", "exchange_websocket_original_payload")
            last_snapshot = (key, _anchor_book_hash(record) or "")
            continue

        if kind == "checkpoint":
            reason = _checkpoint_reason(record)
            checkpoint_hash = _anchor_book_hash(record)
            if reason == "reconnect_resync":
                proven = (
                    active is not None
                    and last_snapshot is not None
                    and last_snapshot[0][0] == key[0]
                    and last_snapshot[0][1] < key[1]
                    and key[1] - last_snapshot[0][1] == 1
                    and checkpoint_hash is not None
                    and checkpoint_hash == last_snapshot[1]
                )
                if not proven:
                    raise EpochSilverError(
                        "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: "
                        "reconnect_resync lacks adjacent matching exchange snapshot"
                    )
                active.reconnect_resync_proven = True
                active.anchor_provenance = (
                    "exchange_websocket_original_payload;"
                    "reconnect_resync_checkpoint_hash_match"
                )
                continue
            if reason == "segment_start":
                rank = key[0]
                if active is None:
                    if tainted or rank not in clean_segment_start_ranks:
                        continue
                    open_at(
                        record,
                        "segment_start_clean",
                        "persisted_canonical_chain_no_cross_segment_taint",
                    )
                # While an epoch is active this local checkpoint is not applied.
                # Collector queueing can place a future-valued segment_start
                # checkpoint before older deltas in the new segment. Physical
                # ordinal replay of those deltas is the continuity proof.
                continue
            # periodic_5m and shutdown are resume material only, never openers.
            continue

        if kind != "delta" or active is None or state is None or tainted:
            continue

        payload = _payload(record)
        if payload:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            bids = data.get("b") or []
            asks = data.get("a") or []
            u = data.get("u")
            seq = data.get("seq")
            ts_ms = int(
                payload.get("ts") or data.get("ts") or record.event_time_ns // 1_000_000
            )
            cts_ms = payload.get("cts") or data.get("cts")
        else:
            # Epoch planning streams lifecycle rows with payloads and deltas without
            # original_payload; continuity uses persisted update_id/seq columns only.
            bids, asks = [], []
            u = int(record.update_id) if record.update_id_present else None
            seq = int(record.seq) if record.seq_present else None
            ts_ms = int(record.event_time_ns // 1_000_000)
            cts_ms = None
        outcome = state.apply_delta(
            bids=bids,
            asks=asks,
            u=u,
            seq=seq,
            ts_ms=ts_ms,
            cts_ms=cts_ms,
            receive_time_ns=record.receive_time_ns,
            enforce_continuity=True,
        )
        if outcome in {DeltaOutcome.GAP, DeltaOutcome.U_RESET}:
            gap = _make_gap(
                chain_version=chain_version,
                chain_hash=canonical_chain_hash,
                record=record,
                reason=f"delta_{outcome.name.lower()}",
            )
            result.gaps.append(gap)
            close_active(_causal_ns(record), f"GAP:{gap.reason}", key)
            tainted = True
            pending_gap_id = gap.gap_id
            last_snapshot = None

    if active is not None:
        active.safe_end_ns = max(active.safe_start_ns, int(scan_end_ns))
        active.terminating_reason = "BOUNDED_INPUT_END"
        active.status = "BOUNDED_COMPLETE"
        if previous_key is not None:
            active.apply_end_segment_chain_index = previous_key[0]
            active.apply_end_record_ordinal = previous_key[1] + 1
        active.finalize_hash()
        result.epochs.append(active)
    return result


@dataclass
class BoundaryDecision:
    status: str
    epoch: EpochDefinition | None
    warmup_start_ns: int
    analysis_start_ns: int
    analysis_end_ns: int
    reason: str = ""


def validate_epoch_window(
    epochs: Iterable[EpochDefinition],
    *,
    analysis_start_ns: int,
    analysis_end_ns: int,
    warmup_ns: int,
) -> BoundaryDecision:
    warmup_start = int(analysis_start_ns) - int(warmup_ns)
    for epoch in epochs:
        if (
            epoch.safe_start_ns <= warmup_start
            and analysis_start_ns >= epoch.safe_start_ns
            and analysis_end_ns <= epoch.safe_end_ns
            and analysis_start_ns < analysis_end_ns
        ):
            return BoundaryDecision(
                status="OK",
                epoch=epoch,
                warmup_start_ns=warmup_start,
                analysis_start_ns=analysis_start_ns,
                analysis_end_ns=analysis_end_ns,
            )
    return BoundaryDecision(
        status="EPOCH_BOUNDARY",
        epoch=None,
        warmup_start_ns=warmup_start,
        analysis_start_ns=analysis_start_ns,
        analysis_end_ns=analysis_end_ns,
        reason="warm-up and analysis must be contained in one safe epoch",
    )


def _resume_checkpoint_candidate(
    record: BronzeRecord,
    *,
    candidate: BronzeRecord | None,
) -> BronzeRecord | None:
    if record.message_type == "snapshot":
        return record
    if record.message_type == "checkpoint" and _anchor_book_hash(record):
        reason = _checkpoint_reason(record)
        if reason in {
            "segment_start",
            "periodic_5m",
            "reconnect_resync",
            "shutdown",
        }:
            return record
    return candidate


def select_resume_checkpoint(
    records: Iterable[BronzeRecord],
    *,
    epoch: EpochDefinition,
    analysis_start_ns: int,
) -> BronzeRecord:
    """Use a local checkpoint only as resume material inside a proven epoch."""
    candidate: BronzeRecord | None = None
    for record in records:
        key = _record_key(record)
        if key < (epoch.anchor_segment_chain_index, epoch.anchor_record_ordinal):
            continue
        if _causal_ns(record) >= analysis_start_ns:
            # Once physical replay reaches analysis, no later checkpoint may
            # suppress already-observed analysis records, even if its own
            # event timestamp moves backwards.
            break
        candidate = _resume_checkpoint_candidate(record, candidate=candidate)
    if candidate is None:
        raise EpochSilverError(
            "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: no resume checkpoint in epoch"
        )
    return candidate


def split_resume_and_replay_stream(
    records: Iterable[BronzeRecord],
    *,
    epoch: EpochDefinition,
    analysis_start_ns: int,
) -> tuple[BronzeRecord, Iterator[BronzeRecord]]:
    """Single-pass resume selection with a tail iterator for replay."""
    candidate: BronzeRecord | None = None
    iterator = iter(records)
    head: BronzeRecord | None = None
    for record in iterator:
        key = _record_key(record)
        if key < (epoch.anchor_segment_chain_index, epoch.anchor_record_ordinal):
            continue
        if _causal_ns(record) >= analysis_start_ns:
            head = record
            break
        candidate = _resume_checkpoint_candidate(record, candidate=candidate)
    if candidate is None:
        raise EpochSilverError(
            "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: no resume checkpoint in epoch"
        )

    def tail() -> Iterator[BronzeRecord]:
        if head is not None:
            yield head
        yield from iterator

    return candidate, tail()


def epoch_apply_bounds(
    epoch: EpochDefinition,
) -> tuple[tuple[int, int], tuple[int, int]]:
    if (
        epoch.apply_end_segment_chain_index is None
        or epoch.apply_end_record_ordinal is None
    ):
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: epoch physical end bound missing"
        )
    start = (
        int(epoch.anchor_segment_chain_index),
        int(epoch.anchor_record_ordinal),
    )
    end = (
        int(epoch.apply_end_segment_chain_index),
        int(epoch.apply_end_record_ordinal),
    )
    if end <= start:
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: invalid epoch physical bounds"
        )
    return start, end


@dataclass
class ReplayResult:
    level_changes: list[dict[str, Any]]
    states: list[dict[str, Any]]
    source_records: int
    delta_records: int
    resume_key: tuple[int, int]
    end_apply_key: tuple[int, int] | None
    level_change_hash_apply_order: str
    timings_s: dict[str, float]
    hash_profile: dict[str, Any] = field(default_factory=dict)


def replay_epoch_window(
    records: Iterable[BronzeRecord],
    *,
    epoch: EpochDefinition,
    resume_record: BronzeRecord,
    analysis_start_ns: int,
    analysis_end_ns: int,
    build_id: str,
    use_book_hash_cache: bool = True,
) -> ReplayResult:
    """Replay one already-validated epoch; never crosses its boundaries."""
    if not (
        epoch.safe_start_ns <= _causal_ns(resume_record) < analysis_start_ns
        and analysis_end_ns <= epoch.safe_end_ns
    ):
        raise EpochSilverError(
            "STOP_EPOCH_BOUNDARY_VIOLATION: resume/analysis outside epoch"
        )
    started = time.monotonic()
    state = _apply_anchor(resume_record)
    resume_key = _record_key(resume_record)
    level_changes: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    source_records = delta_records = 0
    apply_order = message_order = 0
    end_key: tuple[int, int] | None = None
    last_bucket_end_ns = int(analysis_start_ns)
    hash_apply = hashlib.sha256()
    hash_cache = BookHashCache(enabled=use_book_hash_cache)
    level_change_s = bucket_s = delta_apply_s = json_conversion_s = 0.0

    def emit_due(before_ns: int) -> None:
        nonlocal last_bucket_end_ns, bucket_s
        t0 = time.monotonic()
        b = _floor_bucket(
            _ns_to_dt(max(analysis_start_ns, last_bucket_end_ns)), 100
        )
        while True:
            end = b + timedelta(milliseconds=100)
            start_ns = _dt_to_ns(b)
            end_ns = _dt_to_ns(end)
            if start_ns < analysis_start_ns:
                b = end
                continue
            if end_ns > before_ns or end_ns > analysis_end_ns:
                break
            if end_ns <= last_bucket_end_ns:
                b = end
                continue
            row = _metric_row(
                state=state,
                bucket_start=b,
                replay_epoch=1,
                symbol=epoch.symbol,
                silver_build_id=build_id,
                created_at_ms=0,
                book_hash_override=hash_cache.get(state),
            )
            row["epoch_id"] = epoch.epoch_id
            states.append(row)
            last_bucket_end_ns = end_ns
            b = end
        bucket_s += time.monotonic() - t0

    for record in records:
        key = _record_key(record)
        if key <= resume_key:
            continue
        if key < (epoch.anchor_segment_chain_index, epoch.anchor_record_ordinal):
            continue
        if record.message_type == "gap_marker":
            raise EpochSilverError(
                "STOP_EPOCH_BOUNDARY_VIOLATION: gap encountered inside selected epoch"
            )
        if record.message_type != "delta":
            continue
        source_records += 1
        event_ns = int(record.event_time_ns)
        emit_due(event_ns)
        json_started = time.perf_counter()
        payload = _payload(record)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        changes_book = _delta_changes_book(
            state, data.get("b") or [], data.get("a") or []
        )
        json_conversion_s += time.perf_counter() - json_started
        in_analysis = analysis_start_ns <= event_ns < analysis_end_ns
        bid_changes: list[dict[str, Any]] = []
        ask_changes: list[dict[str, Any]] = []
        apply_order_before = apply_order
        message_order += 1
        t_lc = time.monotonic()
        if in_analysis:
            delta_records += 1
            bid_changes, apply_order = _emit_side_changes(
                side="bid",
                book=state.bids,
                levels=data.get("b") or [],
                event_time_ns=event_ns,
                receive_time_ns=record.receive_time_ns,
                replay_epoch=1,
                apply_order_start=apply_order,
                message_order=message_order,
                update_id=int(data.get("u") or 0),
                seq=int(data.get("seq") or 0),
                source_record_id=record.record_id,
                source_record_ordinal=record.record_ordinal,
                symbol=epoch.symbol,
                silver_build_id=build_id,
                created_at_ms=0,
            )
            ask_changes, apply_order = _emit_side_changes(
                side="ask",
                book=state.asks,
                levels=data.get("a") or [],
                event_time_ns=event_ns,
                receive_time_ns=record.receive_time_ns,
                replay_epoch=1,
                apply_order_start=apply_order,
                message_order=message_order,
                update_id=int(data.get("u") or 0),
                seq=int(data.get("seq") or 0),
                source_record_id=record.record_id,
                source_record_ordinal=record.record_ordinal,
                symbol=epoch.symbol,
                silver_build_id=build_id,
                created_at_ms=0,
            )
        level_change_s += time.monotonic() - t_lc
        apply_started = time.perf_counter()
        outcome = state.apply_delta(
            bids=data.get("b") or [],
            asks=data.get("a") or [],
            u=data.get("u"),
            seq=data.get("seq"),
            ts_ms=int(payload.get("ts") or data.get("ts") or event_ns // 1_000_000),
            cts_ms=payload.get("cts") or data.get("cts"),
            receive_time_ns=record.receive_time_ns,
            enforce_continuity=True,
        )
        delta_apply_s += time.perf_counter() - apply_started
        if outcome in {DeltaOutcome.GAP, DeltaOutcome.U_RESET}:
            raise EpochSilverError(
                "STOP_EPOCH_BOUNDARY_VIOLATION: replay continuity diverged from epoch contract"
            )
        if outcome is not DeltaOutcome.APPLIED:
            apply_order = apply_order_before
            continue
        if outcome is DeltaOutcome.APPLIED and in_analysis:
            effective_changes = bid_changes + ask_changes
            for row in effective_changes:
                row["epoch_id"] = epoch.epoch_id
                row["canonical_segment_chain_index"] = key[0]
                level_changes.append(row)
                hash_apply.update(_canonical_bytes(row))
        if outcome is DeltaOutcome.APPLIED:
            if changes_book:
                hash_cache.mark_dirty()
            else:
                hash_cache.mark_no_op()
        end_key = key
    emit_due(analysis_end_ns)
    return ReplayResult(
        level_changes=level_changes,
        states=states,
        source_records=source_records,
        delta_records=delta_records,
        resume_key=resume_key,
        end_apply_key=end_key,
        level_change_hash_apply_order=hash_apply.hexdigest(),
        timings_s={
            "replay_total": round(time.monotonic() - started, 6),
            "level_change_generation": round(level_change_s, 6),
            "states_100ms": round(bucket_s, 6),
            "json_decimal_conversion": round(json_conversion_s, 6),
            "delta_apply": round(delta_apply_s, 6),
        },
        hash_profile=hash_cache.profile.to_dict(),
    )


def make_build_id(
    *,
    chain_version: str,
    canonical_chain_hash: str,
    epoch_hash: str,
    symbol: str,
    analysis_start_ns: int,
    analysis_end_ns: int,
) -> str:
    return _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "chain_version": chain_version,
            "canonical_chain_hash": canonical_chain_hash,
            "epoch_hash": epoch_hash,
            "symbol": symbol.upper(),
            "analysis_start_ns": int(analysis_start_ns),
            "analysis_end_ns": int(analysis_end_ns),
        }
    )


def make_chunk_key(
    *,
    build_id: str,
    epoch_id: str,
    epoch_hash: str,
    chunk_start_ns: int,
    chunk_end_ns: int,
    warmup_ns: int,
) -> str:
    return _hash(
        {
            "build_id": build_id,
            "epoch_id": epoch_id,
            "epoch_hash": epoch_hash,
            "chunk_start_ns": int(chunk_start_ns),
            "chunk_end_ns": int(chunk_end_ns),
            "warmup_ns": int(warmup_ns),
        }
    )


def ensure_epoch_silver_schema(client: Any) -> None:
    client.command("SET max_threads = 1")
    for ddl in EPOCH_SILVER_DDLS:
        client.command(ddl)


def resolve_single_chain(client: Any, *, symbol: str = "BTCUSDT") -> tuple[str, str]:
    rows = client.query(
        f"""
        SELECT chain_version, canonical_chain_hash, count()
        FROM {DATABASE_V13}.{SEGMENTS_TABLE_V13} FINAL
        WHERE symbol = {{symbol:String}} AND is_canonical = 1
        GROUP BY chain_version, canonical_chain_hash
        """,
        parameters={"symbol": symbol.upper()},
    ).result_rows
    if len(rows) != 1:
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: expected exactly one canonical chain"
        )
    return _text(rows[0][0]), _text(rows[0][1])


def load_segment_metadata(
    client: Any,
    *,
    symbol: str,
    chain_version: str,
    canonical_chain_hash: str,
) -> dict[int, dict[str, Any]]:
    rows = client.query(
        f"""
        SELECT
          canonical_segment_chain_index, source_segment_sha256, source_path,
          segment_start_ns, segment_end_ns, first_archive_time_ns,
          last_archive_time_ns, first_event_time_ns, last_event_time_ns,
          first_receive_time_ns, last_receive_time_ns, archive_instance_id,
          record_count, resolution_status, is_canonical,
          predecessor_segment_sha256, anchor_type, anchor_provenance,
          continuity_status, canonical_chain_hash
        FROM {DATABASE_V13}.{SEGMENTS_TABLE_V13} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND is_canonical = 1
        ORDER BY canonical_segment_chain_index
        """,
        parameters={"symbol": symbol.upper(), "chain_version": chain_version},
    ).result_rows
    metadata: dict[int, dict[str, Any]] = {}
    for row in rows:
        rank = int(row[0])
        if rank in metadata or _text(row[19]) != canonical_chain_hash:
            raise EpochSilverError(
                "STOP_SILVER_REPLAY_ORDER_MISMATCH: duplicate rank or changed chain hash"
            )
        metadata[rank] = {
            "canonical_segment_chain_index": rank,
            "source_segment_sha256": _text(row[1]),
            "source_path": _text(row[2]),
            "segment_start_ns": int(row[3]),
            "segment_end_ns": int(row[4]),
            "first_archive_time_ns": int(row[5]),
            "last_archive_time_ns": int(row[6]),
            "first_event_time_ns": int(row[7]),
            "last_event_time_ns": int(row[8]),
            "first_receive_time_ns": int(row[9]),
            "last_receive_time_ns": int(row[10]),
            "archive_instance_id": _text(row[11]),
            "record_count": int(row[12]),
            "resolution_status": _text(row[13]),
            "is_canonical": int(row[14]),
            "predecessor_segment_sha256": _text(row[15]),
            "anchor_type": _text(row[16]),
            "anchor_provenance": _text(row[17]),
            "continuity_status": _text(row[18]),
        }
    if not metadata or sorted(metadata) != list(range(min(metadata), max(metadata) + 1)):
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: non-contiguous loaded segment ranks"
        )
    return metadata


def _mapping_preflight(
    client: Any,
    *,
    symbol: str,
    chain_version: str,
    canonical_chain_hash: str,
) -> None:
    rows = client.query(
        f"""
        SELECT
          count(), uniqExact(source_segment_sha256),
          uniqExact(canonical_segment_chain_index),
          uniqExact(canonical_chain_hash), any(canonical_chain_hash)
        FROM {DATABASE_V13}.{SEGMENTS_TABLE_V13} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND is_canonical = 1
        """,
        parameters={"symbol": symbol.upper(), "chain_version": chain_version},
    ).result_rows
    values = rows[0] if rows else (0, 0, 0, 0, "")
    if int(values[0]) == 0 or int(values[0]) != int(values[1]) or int(values[0]) != int(values[2]):
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: invalid canonical segment mapping"
        )
    if int(values[3]) != 1 or _text(values[4]) != canonical_chain_hash:
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: canonical chain hash changed"
        )


def iter_bronze_window(
    client: Any,
    *,
    symbol: str,
    chain_version: str,
    canonical_chain_hash: str,
    start_ns: int,
    end_ns: int,
    profile: dict[str, Any] | None = None,
    start_apply_key: tuple[int, int] | None = None,
    end_apply_key: tuple[int, int] | None = None,
) -> Iterator[BronzeRecord]:
    """Stream canonical Bronze blocks; SQL order is the apply order.

    Replay callers must pass physical apply-key bounds. Event-time bounds are
    reserved for bounded discovery because event time is not apply order.
    """
    _mapping_preflight(
        client,
        symbol=symbol,
        chain_version=chain_version,
        canonical_chain_hash=canonical_chain_hash,
    )
    if (start_apply_key is None) != (end_apply_key is None):
        raise EpochSilverError(
            "STOP_SILVER_REPLAY_ORDER_MISMATCH: incomplete physical apply bounds"
        )
    if start_apply_key is not None and end_apply_key is not None:
        source_predicate = """
      AND (s.canonical_segment_chain_index, e.record_ordinal) >=
          ({start_rank:UInt64}, {start_ordinal:UInt64})
      AND (s.canonical_segment_chain_index, e.record_ordinal) <
          ({end_rank:UInt64}, {end_ordinal:UInt64})
        """
    else:
        source_predicate = """
      AND e.event_time_ns >= {start_ns:UInt64}
      AND e.event_time_ns < {end_ns:UInt64}
        """
    sql = f"""
    SELECT
      e.record_id, e.symbol, e.message_type, e.event_time_ns, e.receive_time_ns,
      e.update_id, e.seq, e.update_id_present, e.seq_present,
      e.source_segment_sha256, e.record_ordinal, e.payload_sha256,
      e.original_payload, s.canonical_segment_chain_index
    FROM {DATABASE_V13}.{EVENTS_TABLE_V13} AS e FINAL
    INNER JOIN
    (
      SELECT source_segment_sha256, canonical_segment_chain_index
      FROM {DATABASE_V13}.{SEGMENTS_TABLE_V13} FINAL
      WHERE symbol = {{symbol:String}}
        AND chain_version = {{chain_version:String}}
        AND is_canonical = 1
    ) AS s
      ON e.source_segment_sha256 = s.source_segment_sha256
     AND e.canonical_segment_chain_index = s.canonical_segment_chain_index
    WHERE e.symbol = {{symbol:String}}
      AND e.chain_version = {{chain_version:String}}
      {source_predicate}
    ORDER BY s.canonical_segment_chain_index, e.record_ordinal
    """
    parameters = {
        "symbol": symbol.upper(),
        "chain_version": chain_version,
        "start_ns": int(start_ns),
        "end_ns": int(end_ns),
    }
    if start_apply_key is not None and end_apply_key is not None:
        parameters.update(
            {
                "start_rank": int(start_apply_key[0]),
                "start_ordinal": int(start_apply_key[1]),
                "end_rank": int(end_apply_key[0]),
                "end_ordinal": int(end_apply_key[1]),
            }
        )
    stream_open_started = time.perf_counter()
    conversion_s = 0.0
    block_fetch_s = 0.0
    row_count = 0
    with client.query_row_block_stream(sql, parameters=parameters) as stream:
        stream_open_s = time.perf_counter() - stream_open_started
        previous: tuple[int, int] | None = None
        iterator = iter(stream)
        while True:
            fetch_started = time.perf_counter()
            try:
                block = next(iterator)
            except StopIteration:
                block_fetch_s += time.perf_counter() - fetch_started
                break
            block_fetch_s += time.perf_counter() - fetch_started
            for row in block:
                conversion_started = time.perf_counter()
                record = bronze_row_from_ch(tuple(row))
                conversion_s += time.perf_counter() - conversion_started
                row_count += 1
                key = _record_key(record)
                if previous is not None and key <= previous:
                    raise EpochSilverError(
                        "STOP_SILVER_REPLAY_ORDER_MISMATCH: streamed order is not strict"
                    )
                previous = key
                yield record
    if profile is not None:
        profile["bronze_load_and_server_sort_s"] = round(
            stream_open_s + block_fetch_s, 6
        )
        profile["json_decimal_conversion_s"] = round(conversion_s, 6)
        profile["rows"] = row_count


def clean_segment_start_ranks(metadata: dict[int, dict[str, Any]]) -> set[int]:
    return {
        rank
        for rank, row in metadata.items()
        if row["anchor_type"] == "segment_start"
        and row["anchor_provenance"] == "in_memory_snapshot_continuation"
        and row["is_canonical"] == 1
    }


def persist_epochs(
    client: Any,
    epochs: Iterable[EpochDefinition],
    *,
    table: str = EPOCHS_TABLE,
) -> None:
    epochs = list(epochs)
    if not epochs:
        return
    ids = [epoch.epoch_id for epoch in epochs]
    existing = client.query(
        f"""
        SELECT epoch_id, epoch_hash
        FROM {DATABASE_V13}.{table} FINAL
        WHERE epoch_id IN {{epoch_ids:Array(String)}}
        """,
        parameters={"epoch_ids": ids},
    ).result_rows
    existing_hashes = {_text(row[0]): _text(row[1]) for row in existing}
    for epoch in epochs:
        prior = existing_hashes.get(epoch.epoch_id)
        if prior is not None and prior != epoch.epoch_hash:
            raise EpochSilverError(
                "STOP_SILVER_RESUME_NOT_IDEMPOTENT: persisted epoch hash changed"
            )
    epochs = [epoch for epoch in epochs if epoch.epoch_id not in existing_hashes]
    if not epochs:
        return
    now = int(time.time() * 1000)
    columns = [
        "epoch_id", "epoch_hash", "chain_version", "canonical_chain_hash",
        "symbol", "anchor_type", "anchor_provenance", "anchor_event_time_ns",
        "anchor_receive_time_ns", "anchor_u", "anchor_seq",
        "anchor_segment_chain_index", "anchor_record_ordinal", "safe_start_ns",
        "safe_end_ns", "terminating_reason", "preceding_gap_id", "status",
        "reconnect_resync_proven", "version_ms",
    ]
    rows = [
        [
            epoch.epoch_id, epoch.epoch_hash, epoch.chain_version,
            epoch.canonical_chain_hash, epoch.symbol, epoch.anchor_type,
            epoch.anchor_provenance, epoch.anchor_event_time_ns,
            epoch.anchor_receive_time_ns, epoch.anchor_u, epoch.anchor_seq,
            epoch.anchor_segment_chain_index, epoch.anchor_record_ordinal,
            epoch.safe_start_ns, epoch.safe_end_ns, epoch.terminating_reason,
            epoch.preceding_gap_id, epoch.status,
            int(epoch.reconnect_resync_proven), now,
        ]
        for epoch in epochs
    ]
    client.insert(f"{DATABASE_V13}.{table}", rows, column_names=columns)


def _chunk_status(client: Any, chunk_key: str) -> tuple[str, str] | None:
    rows = client.query(
        f"""
        SELECT status, epoch_hash
        FROM {DATABASE_V13}.{CHUNKS_TABLE} FINAL
        WHERE chunk_key = {{chunk_key:String}}
        LIMIT 1
        """,
        parameters={"chunk_key": chunk_key},
    ).result_rows
    return (_text(rows[0][0]), _text(rows[0][1])) if rows else None


def _write_chunk_status(
    client: Any,
    *,
    chunk_key: str,
    build_id: str,
    epoch: EpochDefinition,
    chunk_start_ns: int,
    chunk_end_ns: int,
    warmup_ns: int,
    status: str,
    stop_reason: str = "",
    level_change_count: int = 0,
    state_count: int = 0,
    source_record_count: int = 0,
    output_hash: str = "0" * 64,
) -> None:
    client.insert(
        f"{DATABASE_V13}.{CHUNKS_TABLE}",
        [[
            chunk_key, build_id, epoch.epoch_id, epoch.epoch_hash,
            epoch.chain_version, epoch.canonical_chain_hash, epoch.symbol,
            chunk_start_ns, chunk_end_ns, warmup_ns, status, stop_reason,
            level_change_count, state_count, source_record_count, output_hash,
            int(time.time() * 1000),
        ]],
        column_names=[
            "chunk_key", "build_id", "epoch_id", "epoch_hash",
            "chain_version", "canonical_chain_hash", "symbol",
            "chunk_start_ns", "chunk_end_ns", "warmup_ns", "status",
            "stop_reason", "level_change_count", "state_count",
            "source_record_count", "output_hash", "version_ms",
        ],
    )


def persist_replay_chunk(
    client: Any,
    *,
    epoch: EpochDefinition,
    replay: ReplayResult,
    analysis_start_ns: int,
    analysis_end_ns: int,
    warmup_ns: int,
    simulate_abort_after_running: bool = False,
) -> dict[str, Any]:
    build_id = make_build_id(
        chain_version=epoch.chain_version,
        canonical_chain_hash=epoch.canonical_chain_hash,
        epoch_hash=epoch.epoch_hash,
        symbol=epoch.symbol,
        analysis_start_ns=analysis_start_ns,
        analysis_end_ns=analysis_end_ns,
    )
    chunk_key = make_chunk_key(
        build_id=build_id,
        epoch_id=epoch.epoch_id,
        epoch_hash=epoch.epoch_hash,
        chunk_start_ns=analysis_start_ns,
        chunk_end_ns=analysis_end_ns,
        warmup_ns=warmup_ns,
    )
    existing = _chunk_status(client, chunk_key)
    if existing is not None:
        if existing[1] != epoch.epoch_hash:
            raise EpochSilverError(
                "STOP_SILVER_RESUME_NOT_IDEMPOTENT: epoch hash changed"
            )
        if existing[0] == "COMPLETE":
            return {
                "status": "SKIPPED_ALREADY_COMPLETE",
                "build_id": build_id,
                "chunk_key": chunk_key,
                "rows_inserted": 0,
            }
    _write_chunk_status(
        client,
        chunk_key=chunk_key,
        build_id=build_id,
        epoch=epoch,
        chunk_start_ns=analysis_start_ns,
        chunk_end_ns=analysis_end_ns,
        warmup_ns=warmup_ns,
        status="RUNNING",
    )
    if simulate_abort_after_running:
        return {
            "status": "INTERRUPTED",
            "build_id": build_id,
            "chunk_key": chunk_key,
            "rows_inserted": 0,
        }

    version_ms = int(time.time() * 1000)
    insert_started = time.monotonic()
    inserted = 0
    lc_columns = [
        "row_id", "build_id", "chunk_key", "epoch_id", "epoch_hash",
        "chain_version", "canonical_chain_hash", "symbol",
        "canonical_segment_chain_index", "record_ordinal", "apply_order",
        "event_time_ns", "payload", "version_ms",
    ]
    for offset in range(0, len(replay.level_changes), INSERT_BATCH_SIZE):
        batch = replay.level_changes[offset : offset + INSERT_BATCH_SIZE]
        rows = []
        for row in batch:
            row_id = _hash(
                {
                    "chunk_key": chunk_key,
                    "segment_rank": row["canonical_segment_chain_index"],
                    "record_ordinal": row["source_record_ordinal"],
                    "apply_order": row["apply_order"],
                }
            )
            rows.append([
                row_id, build_id, chunk_key, epoch.epoch_id, epoch.epoch_hash,
                epoch.chain_version, epoch.canonical_chain_hash, epoch.symbol,
                row["canonical_segment_chain_index"],
                row["source_record_ordinal"], row["apply_order"],
                row["event_time_ns"], json.dumps(row, separators=(",", ":")),
                version_ms,
            ])
        client.insert(
            f"{DATABASE_V13}.{LEVEL_CHANGES_TABLE}",
            rows,
            column_names=lc_columns,
        )
        inserted += len(rows)
        _check_rss()

    state_columns = [
        "row_id", "build_id", "chunk_key", "epoch_id", "epoch_hash",
        "chain_version", "canonical_chain_hash", "symbol",
        "bucket_start_ns", "payload", "version_ms",
    ]
    for offset in range(0, len(replay.states), INSERT_BATCH_SIZE):
        batch = replay.states[offset : offset + INSERT_BATCH_SIZE]
        rows = []
        for row in batch:
            bucket_ns = int(row["bucket_start_ms"]) * 1_000_000
            rows.append([
                _hash({"chunk_key": chunk_key, "bucket_start_ns": bucket_ns}),
                build_id, chunk_key, epoch.epoch_id, epoch.epoch_hash,
                epoch.chain_version, epoch.canonical_chain_hash, epoch.symbol,
                bucket_ns, json.dumps(row, separators=(",", ":")), version_ms,
            ])
        client.insert(
            f"{DATABASE_V13}.{STATES_TABLE}", rows, column_names=state_columns
        )
        inserted += len(rows)
        _check_rss()

    output_hash = _hash(
        {
            "level_change_apply_hash": replay.level_change_hash_apply_order,
            "level_change_count": len(replay.level_changes),
            "state_count": len(replay.states),
            "last_apply_key": replay.end_apply_key,
        }
    )
    _write_chunk_status(
        client,
        chunk_key=chunk_key,
        build_id=build_id,
        epoch=epoch,
        chunk_start_ns=analysis_start_ns,
        chunk_end_ns=analysis_end_ns,
        warmup_ns=warmup_ns,
        status="COMPLETE",
        level_change_count=len(replay.level_changes),
        state_count=len(replay.states),
        source_record_count=replay.source_records,
        output_hash=output_hash,
    )
    return {
        "status": "COMPLETE",
        "build_id": build_id,
        "chunk_key": chunk_key,
        "rows_inserted": inserted,
        "insert_s": round(time.monotonic() - insert_started, 6),
        "output_hash": output_hash,
    }


def ns_to_iso(ns: int) -> str:
    seconds, remainder = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{remainder:09d}Z"


def iso(value: str) -> int:
    return iso_to_ns_exact(value)
