"""Fail-closed Silver v1.3 analysis-readiness contract.

An analysis window is READY only when every covered chunk is COMPLETE in
``silver_build_chunks_v1_3 FINAL``, ledger LC/state counts match ClickHouse
outputs, ``output_hash`` is valid, no RUNNING/INTERRUPTED row is logically
current, the window lies inside one replay epoch, does not cross a gap or
blind hole, and stays within COMPLETE chunk bounds.

Parallel analysis during a live builder is SELECT-only, resource-capped, and
must never touch the builder lock. Builder priority is preserved by stopping
analysis under memory/swap pressure while leaving the builder untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .epoch_aware_silver_v1_3 import EpochDefinition, _text
from .helpers import get_clickhouse_client
from .silver_full_build_v1_3 import (
    CHUNKS_TABLE,
    DEFAULT_CHUNK_MARKET_MINUTES,
    DEFAULT_INPUT_DATABASE,
    DEFAULT_LOCK_PATH,
    DEFAULT_OUTPUT_DATABASE,
    DEFAULT_WARMUP_MINUTES,
    EPOCHS_TABLE,
    EXPECTED_BRONZE_RECORDS,
    GAPS_TABLE,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
    BuildConfig,
    SilverBuildError,
    _available_memory_bytes,
    _sha_text,
    plan_epoch_chunks,
    target_schema_exists,
    validate_input_database,
    validate_output_database,
)

ZERO_HASH = "0" * 64
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

ANALYSIS_MIN_AVAILABLE_RAM_BYTES = 6 * 1024**3
ANALYSIS_MAX_MEMORY_USAGE = 536_870_912  # 512 MiB
ANALYSIS_MAX_EXECUTION_TIME_S = 60
ANALYSIS_QUERY_SETTINGS: dict[str, Any] = {
    "max_threads": 1,
    "max_memory_usage": ANALYSIS_MAX_MEMORY_USAGE,
    "max_execution_time": ANALYSIS_MAX_EXECUTION_TIME_S,
    "readonly": 1,
}


class AnalysisReadinessError(RuntimeError):
    """Hard-stop verdict for incomplete or unsafe Silver analysis windows."""


@dataclass(frozen=True)
class ChunkLedgerRow:
    chunk_key: str
    epoch_id: str
    epoch_hash: str
    chunk_start_ns: int
    chunk_end_ns: int
    status: str
    level_change_count: int
    state_count: int
    output_hash: str
    version_ms: int


@dataclass
class WindowAssessment:
    start_ns: int
    end_ns: int
    epoch_id: str
    chunk_keys: list[str]
    level_change_count: int
    state_count: int
    output_hashes: list[str]
    status: str  # READY | NOT_READY
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkAssessment:
    chunk_key: str
    epoch_id: str
    start_ns: int
    end_ns: int
    level_change_count: int
    state_count: int
    output_hash: str
    ledger_status: str
    observed_level_changes: int | None
    observed_states: int | None
    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResourceGuard:
    """Fail-closed host/CH resource gate for parallel analysis."""

    baseline_swap_used_bytes: int
    min_available_ram_bytes: int = ANALYSIS_MIN_AVAILABLE_RAM_BYTES

    @classmethod
    def capture(cls) -> "AnalysisResourceGuard":
        return cls(baseline_swap_used_bytes=_swap_used_bytes())

    def check(self) -> None:
        available = _available_memory_bytes()
        if available < self.min_available_ram_bytes:
            raise AnalysisReadinessError(
                "STOP_SILVER_ANALYSIS_RESOURCE_RAM: "
                f"available={available} < {self.min_available_ram_bytes}"
            )
        swap_used = _swap_used_bytes()
        if swap_used > self.baseline_swap_used_bytes:
            raise AnalysisReadinessError(
                "STOP_SILVER_ANALYSIS_RESOURCE_SWAP_GROWING: "
                f"swap_used={swap_used} baseline={self.baseline_swap_used_bytes}"
            )


@dataclass
class ReadinessReport:
    database: str
    chain_version: str
    chunks: list[ChunkAssessment] = field(default_factory=list)
    analyzable_windows: list[WindowAssessment] = field(default_factory=list)
    analysis_watermark_ns: int | None = None
    complete_chunks: int = 0
    running_chunks: int = 0
    interrupted_chunks: int = 0
    failed_chunks: int = 0
    missing_chunks: int = 0
    expected_chunks: int = 0
    gap_count: int = 0
    epoch_count: int = 0
    resource_guard: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "chain_version": self.chain_version,
            "chunks": [row.to_dict() for row in self.chunks],
            "analyzable_windows": [row.to_dict() for row in self.analyzable_windows],
            "analysis_watermark_ns": self.analysis_watermark_ns,
            "complete_chunks": self.complete_chunks,
            "running_chunks": self.running_chunks,
            "interrupted_chunks": self.interrupted_chunks,
            "failed_chunks": self.failed_chunks,
            "missing_chunks": self.missing_chunks,
            "expected_chunks": self.expected_chunks,
            "gap_count": self.gap_count,
            "epoch_count": self.epoch_count,
            "resource_guard": self.resource_guard,
        }


def _swap_used_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            info: dict[str, int] = {}
            for line in handle:
                key, value = line.split(":", 1)
                info[key.strip()] = int(value.strip().split()[0]) * 1024
        total = int(info.get("SwapTotal", 0))
        free = int(info.get("SwapFree", 0))
        return max(total - free, 0)
    except OSError:
        return 0


def _is_valid_output_hash(value: str) -> bool:
    normalized = str(value).lower().strip()
    if normalized == ZERO_HASH:
        return False
    return bool(_HEX64.fullmatch(normalized))


def analysis_query_settings() -> dict[str, Any]:
    return dict(ANALYSIS_QUERY_SETTINGS)


def ensure_builder_lock_untouched(lock_path: Path = DEFAULT_LOCK_PATH) -> dict[str, Any]:
    """Read builder lock metadata without creating, truncating, or rewriting it."""
    if not lock_path.exists():
        return {"path": str(lock_path), "exists": False, "touched": False}
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisReadinessError(
            f"STOP_SILVER_ANALYSIS_BUILDER_LOCK_UNREADABLE: {exc}"
        ) from exc
    return {
        "path": str(lock_path),
        "exists": True,
        "touched": False,
        "status": payload.get("status"),
        "pid": payload.get("pid"),
    }


def _analysis_query(
    client: Any,
    sql: str,
    *,
    parameters: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> Any:
    merged = analysis_query_settings()
    if settings:
        merged.update(settings)
    try:
        return client.query(sql, parameters=parameters or {}, settings=merged)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "MEMORY_LIMIT" in message or "memory limit" in message.lower() or "Code: 241" in message:
            raise AnalysisReadinessError(
                f"STOP_SILVER_ANALYSIS_CH_MEMORY_LIMIT: {exc}"
            ) from exc
        raise


def load_chunk_ledger(client: Any, config: BuildConfig) -> list[ChunkLedgerRow]:
    rows = _analysis_query(
        client,
        f"""
        SELECT
          chunk_key,
          epoch_id,
          epoch_hash,
          chunk_start_ns,
          chunk_end_ns,
          status,
          level_change_count,
          state_count,
          output_hash,
          version_ms
        FROM {config.output_database}.{CHUNKS_TABLE} FINAL
        WHERE chain_version = {{chain_version:String}}
        ORDER BY chunk_start_ns, chunk_key
        """,
        parameters={"chain_version": config.chain_version},
    ).result_rows
    return [
        ChunkLedgerRow(
            chunk_key=_text(row[0]),
            epoch_id=_text(row[1]),
            epoch_hash=_text(row[2]),
            chunk_start_ns=int(row[3]),
            chunk_end_ns=int(row[4]),
            status=_text(row[5]),
            level_change_count=int(row[6]),
            state_count=int(row[7]),
            output_hash=_text(row[8]).lower(),
            version_ms=int(row[9]),
        )
        for row in rows
    ]


def load_gap_times(client: Any, config: BuildConfig) -> list[int]:
    rows = _analysis_query(
        client,
        f"""
        SELECT gap_time_ns
        FROM {config.output_database}.{GAPS_TABLE} FINAL
        WHERE chain_version = {{chain_version:String}}
        ORDER BY gap_time_ns
        """,
        parameters={"chain_version": config.chain_version},
    ).result_rows
    return [int(row[0]) for row in rows]


def load_persisted_epochs(client: Any, config: BuildConfig) -> list[EpochDefinition]:
    rows = _analysis_query(
        client,
        f"""
        SELECT
          epoch_id,
          epoch_hash,
          chain_version,
          canonical_chain_hash,
          symbol,
          anchor_type,
          anchor_provenance,
          anchor_event_time_ns,
          anchor_receive_time_ns,
          anchor_u,
          anchor_seq,
          anchor_segment_chain_index,
          anchor_record_ordinal,
          safe_start_ns,
          safe_end_ns,
          terminating_reason,
          preceding_gap_id,
          status,
          reconnect_resync_proven
        FROM {config.output_database}.{EPOCHS_TABLE} FINAL
        WHERE chain_version = {{chain_version:String}}
        ORDER BY safe_start_ns, epoch_id
        """,
        parameters={"chain_version": config.chain_version},
    ).result_rows
    epochs: list[EpochDefinition] = []
    for row in rows:
        epochs.append(
            EpochDefinition(
                epoch_id=_text(row[0]),
                epoch_hash=_text(row[1]),
                chain_version=_text(row[2]),
                canonical_chain_hash=_text(row[3]),
                symbol=_text(row[4]),
                anchor_type=_text(row[5]),
                anchor_provenance=_text(row[6]),
                anchor_event_time_ns=int(row[7]),
                anchor_receive_time_ns=int(row[8]),
                anchor_u=int(row[9]),
                anchor_seq=int(row[10]),
                anchor_segment_chain_index=int(row[11]),
                anchor_record_ordinal=int(row[12]),
                safe_start_ns=int(row[13]),
                safe_end_ns=int(row[14]),
                terminating_reason=_text(row[15]),
                preceding_gap_id=_text(row[16]),
                status=_text(row[17]),
                reconnect_resync_proven=bool(int(row[18])),
            )
        )
    return epochs


def expected_chunk_keys(
    epochs: Sequence[EpochDefinition],
    *,
    chunk_market_minutes: int,
    warmup_minutes: int,
) -> list[str]:
    chunks = plan_epoch_chunks(
        epochs,
        chunk_market_minutes=chunk_market_minutes,
        warmup_minutes=warmup_minutes,
    )
    return [chunk.chunk_key for chunk in chunks]


def _count_chunk_outputs(
    client: Any,
    config: BuildConfig,
    *,
    chunk_key: str,
) -> tuple[int, int]:
    lc = int(
        _analysis_query(
            client,
            f"""
            SELECT count()
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": chunk_key},
        ).result_rows[0][0]
    )
    st = int(
        _analysis_query(
            client,
            f"""
            SELECT count()
            FROM {config.output_database}.{METRICS_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": chunk_key},
        ).result_rows[0][0]
    )
    return lc, st


def classify_chunk(
    client: Any,
    config: BuildConfig,
    row: ChunkLedgerRow,
    *,
    verify_counts: bool = True,
) -> ChunkAssessment:
    if row.status in {"RUNNING", "INTERRUPTED", "FAILED", "QUEUED"}:
        return ChunkAssessment(
            chunk_key=row.chunk_key,
            epoch_id=row.epoch_id,
            start_ns=row.chunk_start_ns,
            end_ns=row.chunk_end_ns,
            level_change_count=row.level_change_count,
            state_count=row.state_count,
            output_hash=row.output_hash,
            ledger_status=row.status,
            observed_level_changes=None,
            observed_states=None,
            status="NOT_READY",
            reason=f"STOP_SILVER_ANALYSIS_CHUNK_{row.status}",
        )
    if row.status != "COMPLETE":
        return ChunkAssessment(
            chunk_key=row.chunk_key,
            epoch_id=row.epoch_id,
            start_ns=row.chunk_start_ns,
            end_ns=row.chunk_end_ns,
            level_change_count=row.level_change_count,
            state_count=row.state_count,
            output_hash=row.output_hash,
            ledger_status=row.status,
            observed_level_changes=None,
            observed_states=None,
            status="NOT_READY",
            reason=f"STOP_SILVER_ANALYSIS_CHUNK_STATUS_UNSUPPORTED:{row.status}",
        )
    if not _is_valid_output_hash(row.output_hash):
        return ChunkAssessment(
            chunk_key=row.chunk_key,
            epoch_id=row.epoch_id,
            start_ns=row.chunk_start_ns,
            end_ns=row.chunk_end_ns,
            level_change_count=row.level_change_count,
            state_count=row.state_count,
            output_hash=row.output_hash,
            ledger_status=row.status,
            observed_level_changes=None,
            observed_states=None,
            status="NOT_READY",
            reason="STOP_SILVER_ANALYSIS_OUTPUT_HASH_INVALID",
        )
    observed_lc = observed_st = None
    if verify_counts:
        observed_lc, observed_st = _count_chunk_outputs(
            client, config, chunk_key=row.chunk_key
        )
        if observed_lc != row.level_change_count or observed_st != row.state_count:
            return ChunkAssessment(
                chunk_key=row.chunk_key,
                epoch_id=row.epoch_id,
                start_ns=row.chunk_start_ns,
                end_ns=row.chunk_end_ns,
                level_change_count=row.level_change_count,
                state_count=row.state_count,
                output_hash=row.output_hash,
                ledger_status=row.status,
                observed_level_changes=observed_lc,
                observed_states=observed_st,
                status="NOT_READY",
                reason=(
                    "STOP_SILVER_ANALYSIS_COUNT_MISMATCH: "
                    f"lc={observed_lc}/{row.level_change_count} "
                    f"states={observed_st}/{row.state_count}"
                ),
            )
    return ChunkAssessment(
        chunk_key=row.chunk_key,
        epoch_id=row.epoch_id,
        start_ns=row.chunk_start_ns,
        end_ns=row.chunk_end_ns,
        level_change_count=row.level_change_count,
        state_count=row.state_count,
        output_hash=row.output_hash,
        ledger_status=row.status,
        observed_level_changes=observed_lc if verify_counts else row.level_change_count,
        observed_states=observed_st if verify_counts else row.state_count,
        status="READY",
        reason="",
    )


def _window_crosses_gap(start_ns: int, end_ns: int, gap_times: Sequence[int]) -> bool:
    # Half-open [start, end): a gap at exactly end_ns is outside the window.
    return any(start_ns < int(gap_ns) < end_ns for gap_ns in gap_times)


def _cover_window_with_ready_chunks(
    start_ns: int,
    end_ns: int,
    ready: Sequence[ChunkAssessment],
) -> tuple[list[ChunkAssessment] | None, str]:
    if end_ns <= start_ns:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_EMPTY"
    covering = [
        chunk
        for chunk in ready
        if chunk.end_ns > start_ns and chunk.start_ns < end_ns
    ]
    if not covering:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_OUTSIDE_COMPLETE_CHUNKS"
    covering = sorted(covering, key=lambda row: row.start_ns)
    epoch_ids = {chunk.epoch_id for chunk in covering}
    if len(epoch_ids) != 1:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_CROSSES_EPOCH"
    if covering[0].start_ns > start_ns:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_START_BEFORE_COMPLETE_BOUNDS"
    if covering[-1].end_ns < end_ns:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_END_AFTER_COMPLETE_BOUNDS"
    selected: list[ChunkAssessment] = []
    cursor = start_ns
    for chunk in covering:
        if chunk.end_ns <= cursor:
            continue
        if selected:
            if chunk.start_ns != selected[-1].end_ns:
                return None, "STOP_SILVER_ANALYSIS_WINDOW_BLIND_HOLE"
        elif chunk.start_ns > start_ns:
            # First overlapping chunk starts after the requested start.
            return None, "STOP_SILVER_ANALYSIS_WINDOW_START_BEFORE_COMPLETE_BOUNDS"
        selected.append(chunk)
        cursor = chunk.end_ns
        if cursor >= end_ns:
            break
    if not selected or cursor < end_ns:
        return None, "STOP_SILVER_ANALYSIS_WINDOW_INCOMPLETE_COVER"
    return selected, ""


def assess_analysis_window(
    *,
    start_ns: int,
    end_ns: int,
    ready_chunks: Sequence[ChunkAssessment],
    gap_times: Sequence[int],
) -> WindowAssessment:
    if _window_crosses_gap(start_ns, end_ns, gap_times):
        return WindowAssessment(
            start_ns=start_ns,
            end_ns=end_ns,
            epoch_id="",
            chunk_keys=[],
            level_change_count=0,
            state_count=0,
            output_hashes=[],
            status="NOT_READY",
            reason="STOP_SILVER_ANALYSIS_WINDOW_CROSSES_GAP",
        )
    covering, reason = _cover_window_with_ready_chunks(start_ns, end_ns, ready_chunks)
    if covering is None:
        return WindowAssessment(
            start_ns=start_ns,
            end_ns=end_ns,
            epoch_id="",
            chunk_keys=[],
            level_change_count=0,
            state_count=0,
            output_hashes=[],
            status="NOT_READY",
            reason=reason,
        )
    return WindowAssessment(
        start_ns=start_ns,
        end_ns=end_ns,
        epoch_id=covering[0].epoch_id,
        chunk_keys=[chunk.chunk_key for chunk in covering],
        level_change_count=sum(chunk.level_change_count for chunk in covering),
        state_count=sum(chunk.state_count for chunk in covering),
        output_hashes=[chunk.output_hash for chunk in covering],
        status="READY",
        reason="",
    )


def contiguous_ready_windows(
    ready_chunks: Sequence[ChunkAssessment],
) -> list[WindowAssessment]:
    """Merge adjacent READY chunks within the same epoch into analyzable windows."""
    ordered = sorted(ready_chunks, key=lambda row: (row.start_ns, row.chunk_key))
    windows: list[WindowAssessment] = []
    current: list[ChunkAssessment] = []
    for chunk in ordered:
        if not current:
            current = [chunk]
            continue
        prev = current[-1]
        if chunk.epoch_id == prev.epoch_id and chunk.start_ns == prev.end_ns:
            current.append(chunk)
            continue
        windows.append(_window_from_chunks(current))
        current = [chunk]
    if current:
        windows.append(_window_from_chunks(current))
    return windows


def _window_from_chunks(chunks: Sequence[ChunkAssessment]) -> WindowAssessment:
    return WindowAssessment(
        start_ns=chunks[0].start_ns,
        end_ns=chunks[-1].end_ns,
        epoch_id=chunks[0].epoch_id,
        chunk_keys=[chunk.chunk_key for chunk in chunks],
        level_change_count=sum(chunk.level_change_count for chunk in chunks),
        state_count=sum(chunk.state_count for chunk in chunks),
        output_hashes=[chunk.output_hash for chunk in chunks],
        status="READY",
        reason="",
    )


def analysis_watermark_ns(ready_chunks: Sequence[ChunkAssessment]) -> int | None:
    """Latest contiguous end from the earliest READY chunk without blind holes."""
    ordered = sorted(ready_chunks, key=lambda row: row.start_ns)
    if not ordered:
        return None
    watermark = ordered[0].end_ns
    epoch_id = ordered[0].epoch_id
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.epoch_id != epoch_id or nxt.start_ns != prev.end_ns:
            break
        watermark = nxt.end_ns
    return watermark


def build_readiness_report(
    client: Any,
    config: BuildConfig,
    *,
    verify_counts: bool = True,
    resource_guard: AnalysisResourceGuard | None = None,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> ReadinessReport:
    if not target_schema_exists(client, config.output_database):
        raise AnalysisReadinessError("STOP_SILVER_ANALYSIS_SCHEMA_MISSING")
    guard = resource_guard or AnalysisResourceGuard.capture()
    guard.check()
    lock_info = ensure_builder_lock_untouched(lock_path)

    ledger = load_chunk_ledger(client, config)
    gap_times = load_gap_times(client, config)
    epochs = load_persisted_epochs(client, config)
    expected_keys = expected_chunk_keys(
        epochs,
        chunk_market_minutes=config.chunk_market_minutes,
        warmup_minutes=config.warmup_minutes,
    )
    assessments = [
        classify_chunk(client, config, row, verify_counts=verify_counts)
        for row in ledger
    ]
    ready = [row for row in assessments if row.status == "READY"]
    present_keys = {row.chunk_key for row in ledger}
    missing = [key for key in expected_keys if key not in present_keys]

    report = ReadinessReport(
        database=config.output_database,
        chain_version=config.chain_version,
        chunks=assessments,
        analyzable_windows=contiguous_ready_windows(ready),
        analysis_watermark_ns=analysis_watermark_ns(ready),
        complete_chunks=sum(1 for row in assessments if row.ledger_status == "COMPLETE"),
        running_chunks=sum(1 for row in assessments if row.ledger_status == "RUNNING"),
        interrupted_chunks=sum(
            1 for row in assessments if row.ledger_status == "INTERRUPTED"
        ),
        failed_chunks=sum(1 for row in assessments if row.ledger_status == "FAILED"),
        missing_chunks=len(missing),
        expected_chunks=len(expected_keys),
        gap_count=len(gap_times),
        epoch_count=len(epochs),
        resource_guard={
            "available_ram_bytes": _available_memory_bytes(),
            "swap_used_bytes": _swap_used_bytes(),
            "baseline_swap_used_bytes": guard.baseline_swap_used_bytes,
            "min_available_ram_bytes": guard.min_available_ram_bytes,
            "builder_lock": lock_info,
            "query_settings": analysis_query_settings(),
        },
    )
    # Re-check after CH work so rising swap / falling RAM fail closed.
    guard.check()
    return report


def assert_analysis_window_ready(
    client: Any,
    config: BuildConfig,
    *,
    start_ns: int,
    end_ns: int,
    verify_counts: bool = True,
    resource_guard: AnalysisResourceGuard | None = None,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> WindowAssessment:
    """Mandatory fail-closed gate for every Silver analysis entry point."""
    report = build_readiness_report(
        client,
        config,
        verify_counts=verify_counts,
        resource_guard=resource_guard,
        lock_path=lock_path,
    )
    ready = [row for row in report.chunks if row.status == "READY"]
    gap_times = load_gap_times(client, config)
    assessment = assess_analysis_window(
        start_ns=int(start_ns),
        end_ns=int(end_ns),
        ready_chunks=ready,
        gap_times=gap_times,
    )
    if assessment.status != "READY":
        raise AnalysisReadinessError(
            assessment.reason or "STOP_SILVER_ANALYSIS_WINDOW_NOT_READY"
        )
    return assessment


def gated_select(
    client: Any,
    config: BuildConfig,
    sql: str,
    *,
    start_ns: int,
    end_ns: int,
    parameters: dict[str, Any] | None = None,
    resource_guard: AnalysisResourceGuard | None = None,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> Any:
    """SELECT helper that enforces readiness + analysis resource settings."""
    normalized = " ".join(str(sql).split()).lower()
    if not normalized.startswith("select"):
        raise AnalysisReadinessError("STOP_SILVER_ANALYSIS_SELECT_ONLY")
    forbidden = (" insert ", " alter ", " drop ", " truncate ", " optimize ", " delete ", " create ")
    padded = f" {normalized} "
    if any(token in padded for token in forbidden):
        raise AnalysisReadinessError("STOP_SILVER_ANALYSIS_SELECT_ONLY")
    if "level_changes" in normalized or "metrics_100ms" in normalized or "ob_" in normalized:
        # Require time or chunk/epoch predicates for Silver fact tables.
        if (
            "chunk_key" not in normalized
            and "epoch_id" not in normalized
            and "event_time_ns" not in normalized
            and "bucket_start_ns" not in normalized
        ):
            raise AnalysisReadinessError(
                "STOP_SILVER_ANALYSIS_UNBOUNDED_SCAN"
            )
    assert_analysis_window_ready(
        client,
        config,
        start_ns=start_ns,
        end_ns=end_ns,
        resource_guard=resource_guard,
        lock_path=lock_path,
    )
    return _analysis_query(client, sql, parameters=parameters)


def config_from_args(args: argparse.Namespace) -> BuildConfig:
    validate_input_database(str(args.input_database))
    validate_output_database(str(args.output_database))
    return BuildConfig(
        symbol=str(args.symbol),
        input_database=str(args.input_database),
        output_database=str(args.output_database),
        chain_version=str(args.chain_version),
        expected_chain_hash=_sha_text(
            str(args.expected_chain_hash), label="EXPECTED_CHAIN_HASH"
        ),
        expected_bronze_records=int(args.expected_bronze_records),
        resume=False,
        start_chain_index=0,
        end_chain_index=163,
        chunk_market_minutes=int(args.chunk_market_minutes),
        warmup_minutes=int(args.warmup_minutes),
        max_rss_mib=1536,
        min_free_disk_gib=1.0,
        min_available_memory_mib=1,
        progress_every_chunks=1,
        report_path=Path(args.report_path),
        lock_path=Path(args.lock_path),
        enforce_canonical_lock_path=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Silver v1.3 analysis-readiness reporter"
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--input-database", default=DEFAULT_INPUT_DATABASE)
    parser.add_argument("--output-database", default=DEFAULT_OUTPUT_DATABASE)
    parser.add_argument("--chain-version", required=True)
    parser.add_argument("--expected-chain-hash", required=True)
    parser.add_argument(
        "--expected-bronze-records", type=int, default=EXPECTED_BRONZE_RECORDS
    )
    parser.add_argument(
        "--chunk-market-minutes", type=int, default=DEFAULT_CHUNK_MARKET_MINUTES
    )
    parser.add_argument("--warmup-minutes", type=int, default=DEFAULT_WARMUP_MINUTES)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument(
        "--skip-count-verify",
        action="store_true",
        help="Classify from ledger only (tests/debug). Production use must verify counts.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--report-only",
        action="store_true",
        help="Emit analyzable windows, watermark, and chunk readiness JSON.",
    )
    mode.add_argument(
        "--assert-window",
        action="store_true",
        help="Fail-closed check for --start-ns/--end-ns.",
    )
    parser.add_argument("--start-ns", type=int)
    parser.add_argument("--end-ns", type=int)
    return parser


def execute(args: argparse.Namespace, *, client: Any | None = None) -> dict[str, Any]:
    own_client = client is None
    if own_client:
        client = get_clickhouse_client()
    try:
        config = config_from_args(args)
        guard = AnalysisResourceGuard.capture()
        if args.assert_window:
            if args.start_ns is None or args.end_ns is None:
                raise AnalysisReadinessError(
                    "STOP_SILVER_ANALYSIS_WINDOW_BOUNDS_REQUIRED"
                )
            assessment = assert_analysis_window_ready(
                client,
                config,
                start_ns=int(args.start_ns),
                end_ns=int(args.end_ns),
                verify_counts=not args.skip_count_verify,
                resource_guard=guard,
                lock_path=Path(args.lock_path),
            )
            payload = {
                "verdict": "SILVER_V1_3_ANALYSIS_WINDOW_READY",
                "window": assessment.to_dict(),
            }
        else:
            report = build_readiness_report(
                client,
                config,
                verify_counts=not args.skip_count_verify,
                resource_guard=guard,
                lock_path=Path(args.lock_path),
            )
            payload = {
                "verdict": "SILVER_V1_3_ANALYSIS_READINESS_REPORT",
                "report": report.to_dict(),
            }
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        return payload
    finally:
        if own_client and client is not None:
            client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        execute(args)
        return 0
    except (AnalysisReadinessError, SilverBuildError) as exc:
        payload = {"verdict": "STOP", "error": str(exc)}
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
