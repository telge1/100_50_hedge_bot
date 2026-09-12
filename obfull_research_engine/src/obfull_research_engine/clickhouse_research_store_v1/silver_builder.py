"""Orchestrate silver build from bronze pilot rows into ClickHouse (v1_2 UTC/ns)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zstandard as zstd

from . import (
    DATABASE,
    EVENTS_TABLE,
    EXPECTED_START_CHECKPOINT_ORDINAL,
    LEDGER_TABLE,
    MAX_RSS_BYTES,
    MAX_WALL_CLOCK_S,
    PILOT_SYMBOL,
    PILOT_WINDOW_END,
    PILOT_WINDOW_START,
    PREFIX_WINDOW_END,
    PREFIX_WINDOW_START,
)
from .datetime_integrity import (
    DateTimeStorageMismatch,
    assert_event_receive_datetime_match_ns,
)
from .helpers import (
    ResourceLimits,
    check_resource_limits,
    current_rss_bytes,
    get_clickhouse_client,
    iso_to_ns_exact,
)
from .ns_sql import (
    from_unix_ms_utc_sql,
    from_unix_ns_utc_sql,
    insert_checkpoints_via_ns_staging,
    insert_rows_with_ns_datetimes,
    sql_quote_string,
)
from .silver_constants import (
    BUILDS_TABLE,
    CHECKPOINTS_TABLE,
    DEFAULT_REFERENCE_STATES,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
    SILVER_SCHEMA_VERSION,
)
from .silver_replay import (
    SilverReplayError,
    bronze_row_from_ch,
    contract_hash,
    dedupe_bronze_by_record_id,
    find_first_full_anchor,
    is_full_book_anchor,
    make_silver_build_id,
    replay_bronze_to_silver,
    sort_bronze_source_order,
)
from .silver_schema import assert_silver_schema_compatible, ensure_silver_tables


class SilverBuildError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


@dataclass
class SilverBuildResult:
    verdict: str
    silver_build_id: str
    status: str
    bronze_import_id: str = ""
    skipped: bool = False
    source_record_count: int = 0
    unique_record_count: int = 0
    delta_message_count: int = 0
    skipped_pre_anchor_deltas: int = 0
    level_change_count: int = 0
    checkpoint_count: int = 0
    metric_bucket_count: int = 0
    replay_epoch_count: int = 0
    gap_count: int = 0
    reset_count: int = 0
    anchor_record_id: str = ""
    anchor_time: str | None = None
    replay_coverage_start: str | None = None
    replay_coverage_end: str | None = None
    start_book_hash: str = ""
    end_book_hash: str = ""
    checkpoint_bid_levels: int = 0
    checkpoint_ask_levels: int = 0
    parity: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    query_s: float = 0.0
    elapsed_s: float = 0.0
    peak_rss_bytes: int = 0
    error_message: str = ""
    rows_inserted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "silver_build_id": self.silver_build_id,
            "status": self.status,
            "bronze_import_id": self.bronze_import_id,
            "skipped": self.skipped,
            "source_record_count": self.source_record_count,
            "unique_record_count": self.unique_record_count,
            "delta_message_count": self.delta_message_count,
            "skipped_pre_anchor_deltas": self.skipped_pre_anchor_deltas,
            "level_change_count": self.level_change_count,
            "checkpoint_count": self.checkpoint_count,
            "metric_bucket_count": self.metric_bucket_count,
            "replay_epoch_count": self.replay_epoch_count,
            "gap_count": self.gap_count,
            "reset_count": self.reset_count,
            "anchor_record_id": self.anchor_record_id,
            "anchor_time": self.anchor_time,
            "replay_coverage_start": self.replay_coverage_start,
            "replay_coverage_end": self.replay_coverage_end,
            "start_book_hash": self.start_book_hash,
            "end_book_hash": self.end_book_hash,
            "checkpoint_bid_levels": self.checkpoint_bid_levels,
            "checkpoint_ask_levels": self.checkpoint_ask_levels,
            "parity": self.parity,
            "preflight": self.preflight,
            "query_s": self.query_s,
            "elapsed_s": self.elapsed_s,
            "peak_rss_bytes": self.peak_rss_bytes,
            "error_message": self.error_message,
            "rows_inserted": self.rows_inserted,
            "schema_version": SILVER_SCHEMA_VERSION,
        }


def _ledger_complete_bronze_import_id(client: Any) -> str:
    rows = client.query(
        f"""
        SELECT import_id FROM {DATABASE}.{LEDGER_TABLE} FINAL
        WHERE status = 'COMPLETE'
        ORDER BY completed_at DESC
        LIMIT 1
        """
    ).result_rows
    if not rows:
        raise SilverBuildError("STOP_BRONZE_INPUT_INVALID: no COMPLETE bronze import ledger row")
    return _as_text(rows[0][0])


def _build_status(client: Any, silver_build_id: str) -> dict[str, Any] | None:
    q = (
        f"SELECT status, error_message FROM {DATABASE}.{BUILDS_TABLE} FINAL "
        f"WHERE silver_build_id = {{id:String}} LIMIT 1"
    )
    rows = client.query(q, parameters={"id": silver_build_id}).result_rows
    if not rows:
        return None
    return {"status": _as_text(rows[0][0]), "error_message": _as_text(rows[0][1] or "")}


def _write_build_sql(client: Any, *, row: dict[str, Any]) -> None:
    """Insert build ledger row using fromUnixTimestamp64Nano/Milli only."""
    cols = list(row.keys())
    cells: list[str] = []
    for c in cols:
        v = row[c]
        if c in ("requested_start",) and "requested_start_ns" in row:
            cells.append(from_unix_ns_utc_sql(int(row["requested_start_ns"])))
        elif c in ("requested_end",) and "requested_end_ns" in row:
            cells.append(from_unix_ns_utc_sql(int(row["requested_end_ns"])))
        elif c == "replay_coverage_start":
            ns = row.get("replay_coverage_start_ns")
            cells.append("NULL" if ns is None else from_unix_ns_utc_sql(int(ns)))
        elif c == "replay_coverage_end":
            ns = row.get("replay_coverage_end_ns")
            cells.append("NULL" if ns is None else from_unix_ns_utc_sql(int(ns)))
        elif c == "anchor_time":
            ns = row.get("anchor_time_ns")
            cells.append("NULL" if ns is None else from_unix_ns_utc_sql(int(ns)))
        elif c == "started_at":
            cells.append(from_unix_ms_utc_sql(int(row["started_at_ms"])))
        elif c == "completed_at":
            ms = row.get("completed_at_ms")
            cells.append("NULL" if ms is None else from_unix_ms_utc_sql(int(ms)))
        elif v is None:
            cells.append("NULL")
        elif isinstance(v, int):
            cells.append(str(int(v)))
        else:
            cells.append(sql_quote_string(str(v)))
    client.command(
        f"INSERT INTO {DATABASE}.{BUILDS_TABLE} ({', '.join(cols)}) VALUES ({', '.join(cells)})"
    )


def load_bronze_window(
    client: Any,
    *,
    symbol: str,
    window_start: str,
    window_end: str,
    chain_version: str | None = None,
    canonical_chain_hash: str | None = None,
) -> tuple[list[Any], float]:
    start_ns = iso_to_ns_exact(window_start)
    end_ns = iso_to_ns_exact(window_end)
    if chain_version is None:
        # Legacy v1.2 is safe only for a single segment. Never SHA-sort it.
        sql = f"""
        SELECT
          record_id, symbol, message_type, event_time_ns, receive_time_ns,
          update_id, seq, update_id_present, seq_present,
          source_segment_sha256, record_ordinal, payload_sha256, original_payload
        FROM {DATABASE}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
        ORDER BY record_ordinal
        """
        parameters = {
            "symbol": symbol.upper(),
            "start_ns": start_ns,
            "end_ns": end_ns,
        }
    else:
        if not canonical_chain_hash:
            raise SilverBuildError(
                "STOP_V1_3_CHAIN_NOT_STABLE: expected canonical chain hash is required"
            )
        v13_database = "research_full_ob_continuous_pilot_v1_3"
        events = "raw_full_ob_events_pilot_v1_3"
        segments = "canonical_segments_pilot_v1_3"
        mapping = client.query(
            f"""
            SELECT
              count(),
              uniqExact(source_segment_sha256),
              uniqExact(canonical_segment_chain_index),
              uniqExact(canonical_chain_hash),
              any(canonical_chain_hash)
            FROM {v13_database}.{segments} FINAL
            WHERE symbol = {{symbol:String}}
              AND chain_version = {{chain_version:String}}
              AND is_canonical = 1
            """,
            parameters={"symbol": symbol.upper(), "chain_version": chain_version},
        ).result_rows
        count, unique_shas, unique_ranks, unique_hashes, actual_hash = (
            mapping[0] if mapping else (0, 0, 0, 0, "")
        )
        actual_hash = _as_text(actual_hash)
        if (
            int(count) == 0
            or int(count) != int(unique_shas)
            or int(count) != int(unique_ranks)
        ):
            raise SilverBuildError(
                "STOP_V1_3_BRONZE_SEGMENT_MAPPING_INVALID: missing or duplicate canonical rank"
            )
        if int(unique_hashes) != 1 or actual_hash != canonical_chain_hash:
            raise SilverBuildError(
                "STOP_V1_3_CHAIN_NOT_STABLE: canonical chain hash changed"
            )
        sql = f"""
        SELECT
          e.record_id, e.symbol, e.message_type, e.event_time_ns, e.receive_time_ns,
          e.update_id, e.seq, e.update_id_present, e.seq_present,
          e.source_segment_sha256, e.record_ordinal, e.payload_sha256, e.original_payload,
          s.canonical_segment_chain_index
        FROM {v13_database}.{events} AS e FINAL
        INNER JOIN
        (
          SELECT
            source_segment_sha256,
            canonical_segment_chain_index
          FROM {v13_database}.{segments} FINAL
          WHERE symbol = {{symbol:String}}
            AND chain_version = {{chain_version:String}}
            AND is_canonical = 1
        ) AS s
          ON e.source_segment_sha256 = s.source_segment_sha256
         AND e.canonical_segment_chain_index = s.canonical_segment_chain_index
        WHERE e.symbol = {{symbol:String}}
          AND e.chain_version = {{chain_version:String}}
          AND e.event_time_ns >= {{start_ns:UInt64}}
          AND e.event_time_ns < {{end_ns:UInt64}}
        ORDER BY s.canonical_segment_chain_index, e.record_ordinal
        """
        parameters = {
            "symbol": symbol.upper(),
            "chain_version": chain_version,
            "start_ns": start_ns,
            "end_ns": end_ns,
        }

    t0 = time.monotonic()
    rows = client.query(sql, parameters=parameters).result_rows
    if chain_version is None:
        segment_shas = {_as_text(row[9]) for row in rows}
        if len(segment_shas) > 1:
            raise SilverBuildError(
                "STOP_SILVER_CANONICAL_SEGMENT_ORDER_SCHEMA_MISSING: "
                "legacy v1.2 window contains multiple segments"
            )
    return rows, time.monotonic() - t0


def run_silver_preflight(
    client: Any,
    *,
    symbol: str,
    bronze_start: str,
    bronze_end: str,
    analysis_start: str,
    analysis_end: str,
    records: list[Any],
) -> dict[str, Any]:
    """Hard preflight before silver writes. Raises SilverBuildError on failure."""
    bronze_start_ns = iso_to_ns_exact(bronze_start)
    bronze_end_ns = iso_to_ns_exact(bronze_end)
    analysis_start_ns = iso_to_ns_exact(analysis_start)
    analysis_end_ns = iso_to_ns_exact(analysis_end)

    try:
        dt_proof = assert_event_receive_datetime_match_ns(
            client,
            database=DATABASE,
            table=EVENTS_TABLE,
            symbol=symbol,
            start_ns=bronze_start_ns,
            end_ns=bronze_end_ns,
            sample_limit=5,
        )
    except DateTimeStorageMismatch as exc:
        raise SilverBuildError(str(exc)) from exc

    # Full-window exact equality count (stop on any mismatch).
    client.command("SET session_timezone = 'UTC'")
    bad = client.query(
        f"""
        SELECT count()
        FROM {DATABASE}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
          AND (
            toUnixTimestamp64Nano(event_time) != event_time_ns
            OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
          )
        """,
        parameters={
            "symbol": symbol.upper(),
            "start_ns": bronze_start_ns,
            "end_ns": bronze_end_ns,
        },
    ).result_rows[0][0]
    if int(bad) != 0:
        raise SilverBuildError(
            f"STOP_DATETIME_STORAGE_MISMATCH: {bad} rows with DateTime64!=*_ns in bronze prefix"
        )

    anchor = find_first_full_anchor(records)
    if anchor is None or not is_full_book_anchor(anchor.original_payload, anchor.message_type):
        raise SilverBuildError("STOP_SILVER_ANCHOR_MISSING: no complete start checkpoint")
    if int(anchor.record_ordinal) != int(EXPECTED_START_CHECKPOINT_ORDINAL):
        raise SilverBuildError(
            "STOP_SILVER_ANCHOR_MISSING: start checkpoint ordinal "
            f"{anchor.record_ordinal} != {EXPECTED_START_CHECKPOINT_ORDINAL}"
        )
    if int(anchor.event_time_ns) >= analysis_start_ns:
        raise SilverBuildError(
            "STOP_SILVER_ANCHOR_MISSING: start checkpoint not strictly before analysis start"
        )

    # Continuity: no gap in ordinals from anchor through first analysis event.
    ords = [int(r.record_ordinal) for r in records]
    if ords != sorted(ords) or len(ords) != len(set(ords)):
        raise SilverBuildError("STOP_BRONZE_INPUT_INVALID: ordinals not unique/sorted")
    # Ensure every ordinal between min and max present (no holes).
    if ords and (ords[-1] - ords[0] + 1) != len(ords):
        raise SilverBuildError("STOP_BRONZE_INPUT_INVALID: ordinal gap in bronze prefix")

    analysis_recs = [r for r in records if analysis_start_ns <= r.event_time_ns < analysis_end_ns]
    if not analysis_recs:
        raise SilverBuildError("STOP_BRONZE_INPUT_INVALID: no bronze rows in analysis window")

    return {
        "ok": True,
        "datetime_samples": dt_proof.get("samples"),
        "anchor_ordinal": anchor.record_ordinal,
        "anchor_event_time_ns": anchor.event_time_ns,
        "bronze_rows": len(records),
        "analysis_rows": len(analysis_recs),
        "utc_session": "UTC",
    }


def compare_reference_metrics(
    *,
    metrics: list[dict[str, Any]],
    reference_path: str | Path,
    coverage_start_ns: int,
    coverage_end_ns: int,
) -> dict[str, Any]:
    path = Path(reference_path)
    if not path.is_file():
        return {"status": "REFERENCE_NOT_AVAILABLE", "reason": f"missing {path}"}

    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as fh:
        raw = dctx.decompress(fh.read())
    ref_by_bucket: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("record_type") == "header":
            continue
        bs = str(obj.get("bucket_start") or "")
        if not bs:
            continue
        # Filter reference to analysis coverage using bucket_start parse when possible.
        ref_by_bucket[bs.replace("Z", "")] = obj

    compared = 0
    mismatches: list[dict[str, Any]] = []

    def normalize_bucket_key(value: str) -> str:
        s = value.replace(" ", "T").replace("Z", "")
        if "." in s:
            head, frac = s.split(".", 1)
            frac = frac.rstrip("0")
            s = head if not frac else f"{head}.{frac}"
        return s

    def ms_to_key(ms: int) -> str:
        sec, rem = divmod(int(ms), 1000)
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{rem:03d}".rstrip("0").rstrip(".")

    ref_norm = {normalize_bucket_key(k): v for k, v in ref_by_bucket.items()}

    for m in metrics:
        bs_ms = int(m.get("bucket_start_ms") or 0)
        # Keep metrics inside analysis window only (already filtered in replay).
        if bs_ms * 1_000_000 < coverage_start_ns or bs_ms * 1_000_000 >= coverage_end_ns:
            # allow bucket starting in window; end may equal coverage_end
            if not (coverage_start_ns <= bs_ms * 1_000_000 < coverage_end_ns):
                continue
        key = normalize_bucket_key(ms_to_key(bs_ms))
        ref = ref_norm.get(key)
        if ref is None and "." in key:
            head, frac = key.split(".", 1)
            ref = ref_norm.get(f"{head}.{frac[:1]}") or ref_norm.get(head)
        if ref is None:
            mismatches.append({"bucket_start_ms": bs_ms, "field": "_missing_ref", "silver": key})
            continue
        compared += 1
        for field_name in ("best_bid", "best_ask", "mid", "book_hash"):
            left, right = m.get(field_name), ref.get(field_name)
            if field_name == "book_hash":
                # reference may use book_sha256
                right = ref.get("book_hash") or ref.get("book_sha256")
                if left is None or right is None or right == "":
                    continue
                if str(left) != str(right):
                    mismatches.append(
                        {"bucket_start_ms": bs_ms, "field": field_name, "silver": left, "reference": right}
                    )
                continue
            if left is None or right is None or right == "":
                continue
            if abs(float(left) - float(right)) > 1e-6:
                mismatches.append(
                    {
                        "bucket_start_ms": bs_ms,
                        "field": field_name,
                        "silver": left,
                        "reference": right,
                    }
                )
    if compared == 0:
        return {
            "status": "REFERENCE_NOT_AVAILABLE",
            "reason": "no overlapping metric buckets with reference",
            "reference_path": str(path),
        }
    if mismatches:
        return {
            "status": "PARITY_FAILED",
            "compared_buckets": compared,
            "mismatches": mismatches[:20],
            "reference_path": str(path),
        }
    return {
        "status": "PARITY_EXACT",
        "compared_buckets": compared,
        "reference_path": str(path),
    }


def run_silver_build(
    *,
    symbol: str = PILOT_SYMBOL,
    window_start: str = PILOT_WINDOW_START,
    window_end: str = PILOT_WINDOW_END,
    bronze_start: str = PREFIX_WINDOW_START,
    bronze_end: str = PREFIX_WINDOW_END,
    max_wall_clock_s: float = MAX_WALL_CLOCK_S,
    max_rss_bytes: int = MAX_RSS_BYTES,
    reference_states_path: str | Path | None = DEFAULT_REFERENCE_STATES,
    client: Any | None = None,
) -> SilverBuildResult:
    t0 = time.monotonic()
    peak = current_rss_bytes()
    limits = ResourceLimits(
        deadline_monotonic=t0 + float(max_wall_clock_s), max_rss_bytes=int(max_rss_bytes)
    )
    own = client is None
    if client is None:
        client = get_clickhouse_client()

    try:
        ensure_silver_tables(client)
        assert_silver_schema_compatible(client)
        client.command("SET session_timezone = 'UTC'")
        check_resource_limits(limits)

        bronze_import_id = _ledger_complete_bronze_import_id(client)

        raw_rows, query_s = load_bronze_window(
            client, symbol=symbol, window_start=bronze_start, window_end=bronze_end
        )
        peak = max(peak, current_rss_bytes())
        if not raw_rows:
            raise SilverBuildError("STOP_BRONZE_INPUT_INVALID: zero bronze rows in prefix")

        records = [bronze_row_from_ch(r) for r in raw_rows]
        records = sort_bronze_source_order(dedupe_bronze_by_record_id(records))

        preflight = run_silver_preflight(
            client,
            symbol=symbol,
            bronze_start=bronze_start,
            bronze_end=bronze_end,
            analysis_start=window_start,
            analysis_end=window_end,
            records=records,
        )
        anchor = find_first_full_anchor(records)
        assert anchor is not None

        chash = contract_hash()
        silver_build_id = make_silver_build_id(
            schema_version=SILVER_SCHEMA_VERSION,
            bronze_import_id=bronze_import_id,
            symbol=symbol,
            window_start=window_start,
            window_end=window_end,
            replay_contract_hash=chash,
            anchor_record_id=anchor.record_id,
        )

        existing = _build_status(client, silver_build_id)
        if existing is not None and existing["status"] == "COMPLETE":
            return SilverBuildResult(
                verdict="CLICKHOUSE_UTC_NS_PREFIX_SILVER_PARITY_PROVEN",
                silver_build_id=silver_build_id,
                status="SKIPPED_ALREADY_COMPLETE",
                bronze_import_id=bronze_import_id,
                skipped=True,
                preflight=preflight,
                elapsed_s=time.monotonic() - t0,
                peak_rss_bytes=current_rss_bytes(),
                rows_inserted=0,
            )

        started_at_ms = _now_ms()
        analysis_start_ns = iso_to_ns_exact(window_start)
        analysis_end_ns = iso_to_ns_exact(window_end)

        _write_build_sql(
            client,
            row={
                "silver_build_id": silver_build_id,
                "schema_version": SILVER_SCHEMA_VERSION,
                "bronze_import_id": bronze_import_id,
                "symbol": symbol.upper(),
                "requested_start": 0,
                "requested_start_ns": analysis_start_ns,
                "requested_end": 0,
                "requested_end_ns": analysis_end_ns,
                "replay_coverage_start": None,
                "replay_coverage_start_ns": None,
                "replay_coverage_end": None,
                "replay_coverage_end_ns": None,
                "anchor_record_id": "",
                "anchor_time": None,
                "anchor_time_ns": None,
                "source_record_count": 0,
                "unique_record_count": 0,
                "delta_message_count": 0,
                "level_change_count": 0,
                "metric_bucket_count": 0,
                "replay_epoch_count": 0,
                "gap_count": 0,
                "reset_count": 0,
                "status": "RUNNING",
                "started_at": 0,
                "started_at_ms": started_at_ms,
                "completed_at": None,
                "completed_at_ms": None,
                "error_message": "",
                "contract_hash": chash,
                "build_version": 1,
            },
        )

        created_at_ms = _now_ms()
        replayed = replay_bronze_to_silver(
            rows=records,
            symbol=symbol,
            window_start_ns=analysis_start_ns,
            window_end_ns=analysis_end_ns,
            silver_build_id=silver_build_id,
            created_at_ms=created_at_ms,
        )
        peak = max(peak, current_rss_bytes())

        if replayed.delta_message_count <= 0 or len(replayed.level_changes) <= 0:
            raise SilverBuildError("STOP_FULL_REPLAY_NOT_EXERCISED: no analysis deltas/level_changes")

        rows_inserted = 0
        if replayed.checkpoints:
            insert_checkpoints_via_ns_staging(
                client,
                database=DATABASE,
                checkpoints_table=CHECKPOINTS_TABLE,
                staging_table="ob_checkpoints_pilot_v1_2_ns_staging",
                rows=replayed.checkpoints,
            )
            rows_inserted += len(replayed.checkpoints)

        if replayed.level_changes:
            # batch level changes
            cols = list(replayed.level_changes[0].keys())
            batch_size = 200
            for i in range(0, len(replayed.level_changes), batch_size):
                chunk = replayed.level_changes[i : i + batch_size]
                insert_rows_with_ns_datetimes(
                    client,
                    database=DATABASE,
                    table=LEVEL_CHANGES_TABLE,
                    column_names=cols,
                    rows=[[c[k] for k in cols] for c in chunk],
                    datetime_ns_columns={"event_time": "event_time_ns"},
                    datetime_ms_columns={"created_at": "created_at_ms"},
                )
            rows_inserted += len(replayed.level_changes)

        if replayed.metrics:
            cols = list(replayed.metrics[0].keys())
            batch_size = 200
            for i in range(0, len(replayed.metrics), batch_size):
                chunk = replayed.metrics[i : i + batch_size]
                insert_rows_with_ns_datetimes(
                    client,
                    database=DATABASE,
                    table=METRICS_TABLE,
                    column_names=cols,
                    rows=[[c[k] for k in cols] for c in chunk],
                    datetime_ns_columns={},
                    datetime_ms_columns={
                        "bucket_start": "bucket_start_ms",
                        "created_at": "created_at_ms",
                    },
                )
            rows_inserted += len(replayed.metrics)

        parity = {"status": "SKIPPED"}
        if reference_states_path:
            parity = compare_reference_metrics(
                metrics=replayed.metrics,
                reference_path=reference_states_path,
                coverage_start_ns=analysis_start_ns,
                coverage_end_ns=analysis_end_ns,
            )
            if parity.get("status") == "PARITY_FAILED":
                raise SilverBuildError(
                    "STOP_FULL_MINUTE_PARITY_FAILED: "
                    + json.dumps(parity.get("mismatches", [])[:1])
                )
            if parity.get("status") not in ("PARITY_EXACT",):
                raise SilverBuildError(
                    f"STOP_FULL_MINUTE_PARITY_FAILED: parity status={parity.get('status')}"
                )

        ck0 = replayed.checkpoints[0] if replayed.checkpoints else {}
        _write_build_sql(
            client,
            row={
                "silver_build_id": silver_build_id,
                "schema_version": SILVER_SCHEMA_VERSION,
                "bronze_import_id": bronze_import_id,
                "symbol": symbol.upper(),
                "requested_start": 0,
                "requested_start_ns": analysis_start_ns,
                "requested_end": 0,
                "requested_end_ns": analysis_end_ns,
                "replay_coverage_start": 0,
                "replay_coverage_start_ns": replayed.replay_coverage_start_ns,
                "replay_coverage_end": 0,
                "replay_coverage_end_ns": replayed.replay_coverage_end_ns,
                "anchor_record_id": replayed.anchor_record_id,
                "anchor_time": 0,
                "anchor_time_ns": replayed.anchor_time_ns,
                "source_record_count": replayed.source_record_count,
                "unique_record_count": replayed.unique_record_count,
                "delta_message_count": replayed.delta_message_count,
                "level_change_count": len(replayed.level_changes),
                "metric_bucket_count": len(replayed.metrics),
                "replay_epoch_count": replayed.replay_epoch_count,
                "gap_count": replayed.gap_count,
                "reset_count": replayed.reset_count,
                "status": "COMPLETE",
                "started_at": 0,
                "started_at_ms": started_at_ms,
                "completed_at": 0,
                "completed_at_ms": _now_ms(),
                "error_message": "",
                "contract_hash": chash,
                "build_version": 2,
            },
        )

        return SilverBuildResult(
            verdict="CLICKHOUSE_UTC_NS_PREFIX_SILVER_PARITY_PROVEN",
            silver_build_id=silver_build_id,
            status="COMPLETE",
            bronze_import_id=bronze_import_id,
            source_record_count=replayed.source_record_count,
            unique_record_count=replayed.unique_record_count,
            delta_message_count=replayed.delta_message_count,
            skipped_pre_anchor_deltas=replayed.skipped_pre_anchor_deltas,
            level_change_count=len(replayed.level_changes),
            checkpoint_count=len(replayed.checkpoints),
            metric_bucket_count=len(replayed.metrics),
            replay_epoch_count=replayed.replay_epoch_count,
            gap_count=replayed.gap_count,
            reset_count=replayed.reset_count,
            anchor_record_id=replayed.anchor_record_id,
            anchor_time=str(replayed.anchor_time_ns),
            replay_coverage_start=str(replayed.replay_coverage_start_ns),
            replay_coverage_end=str(replayed.replay_coverage_end_ns),
            start_book_hash=replayed.start_book_hash,
            end_book_hash=replayed.end_book_hash,
            checkpoint_bid_levels=int(ck0.get("bid_level_count") or 0),
            checkpoint_ask_levels=int(ck0.get("ask_level_count") or 0),
            parity=parity,
            preflight=preflight,
            query_s=query_s,
            elapsed_s=time.monotonic() - t0,
            peak_rss_bytes=peak,
            rows_inserted=rows_inserted,
        )
    except (SilverBuildError, SilverReplayError, DateTimeStorageMismatch) as exc:
        raise SilverBuildError(str(exc)) from exc
    finally:
        if own and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
