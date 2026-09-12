"""Streaming Full-OB segment import into ClickHouse bronze pilot tables (v1_2 UTC/ns)."""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import zstandard as zstd

from . import (
    BATCH_SIZE,
    DATABASE,
    EVENTS_TABLE,
    EXPECTED_FORMAT_VERSION,
    EXPECTED_START_CHECKPOINT_ORDINAL,
    LEDGER_TABLE,
    MAX_RSS_BYTES,
    MAX_WALL_CLOCK_S,
    SCHEMA_VERSION,
)
from .helpers import (
    ResourceLimits,
    check_resource_limits,
    current_rss_bytes,
    event_in_window,
    get_clickhouse_client,
    iso_to_ns_exact,
    load_manifest,
    make_import_id,
    make_record_id,
    verify_manifest_against_segment,
)
from .ns_sql import insert_events_via_ns_staging
from .schema import assert_compatible_schema, ensure_database_and_tables

STAGING_TABLE = "raw_full_ob_events_pilot_v1_2_ns_staging"


class PilotImportError(RuntimeError):
    """Carries a STOP_* verdict code in the message prefix."""


def _ns_to_iso_z(ns: int) -> str:
    """Human/ISO report string from event_time_ns (UTC, nanosecond fraction)."""
    sec, rem = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{rem:09d}Z"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass
class SampleRecord:
    record_id: str
    record_ordinal: int
    payload_sha256: str
    event_time_ns: int
    message_type: str
    original_payload: str


@dataclass
class ImportResult:
    verdict: str
    import_id: str
    status: str
    rows_seen: int = 0
    rows_in_window: int = 0
    rows_inserted: int = 0
    min_event_time: str | None = None
    max_event_time: str | None = None
    min_event_time_ns: int | None = None
    max_event_time_ns: int | None = None
    min_update_id: int | None = None
    max_update_id: int | None = None
    min_seq: int | None = None
    max_seq: int | None = None
    message_type_counts: dict[str, int] = field(default_factory=dict)
    peak_rss_bytes: int = 0
    elapsed_s: float = 0.0
    samples: list[SampleRecord] = field(default_factory=list)
    source_segment: str = ""
    source_segment_sha256: str = ""
    error_message: str = ""
    skipped: bool = False
    content_hash: str = ""
    start_checkpoint_ordinal: int | None = None
    start_checkpoint_u: int | None = None
    start_checkpoint_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "import_id": self.import_id,
            "status": self.status,
            "rows_seen": self.rows_seen,
            "rows_in_window": self.rows_in_window,
            "rows_inserted": self.rows_inserted,
            "min_event_time": self.min_event_time,
            "max_event_time": self.max_event_time,
            "min_event_time_ns": self.min_event_time_ns,
            "max_event_time_ns": self.max_event_time_ns,
            "min_update_id": self.min_update_id,
            "max_update_id": self.max_update_id,
            "min_seq": self.min_seq,
            "max_seq": self.max_seq,
            "message_type_counts": dict(self.message_type_counts),
            "peak_rss_bytes": self.peak_rss_bytes,
            "elapsed_s": self.elapsed_s,
            "samples": [
                {
                    "record_id": s.record_id,
                    "record_ordinal": s.record_ordinal,
                    "payload_sha256": s.payload_sha256,
                    "event_time_ns": s.event_time_ns,
                    "message_type": s.message_type,
                }
                for s in self.samples
            ],
            "source_segment": self.source_segment,
            "source_segment_sha256": self.source_segment_sha256,
            "error_message": self.error_message,
            "skipped": self.skipped,
            "schema_version": SCHEMA_VERSION,
            "content_hash": self.content_hash,
            "start_checkpoint_ordinal": self.start_checkpoint_ordinal,
            "start_checkpoint_u": self.start_checkpoint_u,
            "start_checkpoint_seq": self.start_checkpoint_seq,
        }


def iter_ndjson_zst_records(path: Path) -> Iterator[tuple[int, dict[str, Any], bytes]]:
    """Yield (record_ordinal, envelope_dict, raw_line_bytes) streaming; ordinal from 1."""
    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as fh:
        with dctx.stream_reader(fh) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8")
            ordinal = 0
            for line in text:
                if not line.strip():
                    continue
                ordinal += 1
                raw = line.rstrip("\n").encode("utf-8")
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PilotImportError(
                        f"STOP_SOURCE_MANIFEST_INVALID: bad JSON at ordinal {ordinal}: {exc}"
                    ) from exc
                if not isinstance(obj, dict):
                    raise PilotImportError(
                        f"STOP_SOURCE_MANIFEST_INVALID: non-object envelope at ordinal {ordinal}"
                    )
                yield ordinal, obj, raw


def _ledger_status(client: Any, import_id: str) -> dict[str, Any] | None:
    q = (
        f"SELECT status, rows_inserted, error_message, started_at "
        f"FROM {DATABASE}.{LEDGER_TABLE} FINAL "
        f"WHERE import_id = {{id:String}} LIMIT 1"
    )
    result = client.query(q, parameters={"id": import_id})
    if not result.result_rows:
        return None
    row = result.result_rows[0]
    return {
        "status": str(row[0]),
        "rows_inserted": int(row[1]),
        "error_message": str(row[2] or ""),
        "started_at": row[3],
    }


def _write_ledger(
    client: Any,
    *,
    import_id: str,
    symbol: str,
    window_start_ns: int,
    window_end_ns: int,
    source_segment: str,
    source_sha: str,
    status: str,
    rows_seen: int,
    rows_in_window: int,
    rows_inserted: int,
    min_event_time_ns: int | None,
    max_event_time_ns: int | None,
    started_at_ms: int,
    completed_at_ms: int | None,
    error_message: str,
    ledger_version: int,
) -> None:
    cols = [
        "import_id",
        "schema_version",
        "symbol",
        "window_start",
        "window_start_ns",
        "window_end",
        "window_end_ns",
        "source_segment",
        "source_segment_sha256",
        "status",
        "rows_seen",
        "rows_in_window",
        "rows_inserted",
        "min_event_time",
        "min_event_time_ns",
        "max_event_time",
        "max_event_time_ns",
        "started_at",
        "started_at_ms",
        "completed_at",
        "completed_at_ms",
        "error_message",
        "ledger_version",
    ]
    # Placeholder 0 for nullable ms when absent — SQL path uses NULL for completed when ms is None
    # We encode nullable datetimes via sibling ns; for completed_at use ms columns with NULL.
    row = [
        import_id,
        SCHEMA_VERSION,
        symbol,
        0,  # window_start via ns
        int(window_start_ns),
        0,  # window_end via ns
        int(window_end_ns),
        source_segment,
        source_sha,
        status,
        rows_seen,
        rows_in_window,
        rows_inserted,
        0 if min_event_time_ns is not None else None,
        min_event_time_ns,
        0 if max_event_time_ns is not None else None,
        max_event_time_ns,
        0,  # started_at via ms
        int(started_at_ms),
        0 if completed_at_ms is not None else None,
        completed_at_ms,
        error_message,
        ledger_version,
    ]
    # Custom insert: nullable DateTime64 from ns/ms need NULL when sibling is None.
    from .ns_sql import from_unix_ms_utc_sql, from_unix_ns_utc_sql, sql_quote_string

    def cell(col: str, value: Any) -> str:
        if col == "window_start":
            return from_unix_ns_utc_sql(window_start_ns)
        if col == "window_end":
            return from_unix_ns_utc_sql(window_end_ns)
        if col == "min_event_time":
            return "NULL" if min_event_time_ns is None else from_unix_ns_utc_sql(min_event_time_ns)
        if col == "max_event_time":
            return "NULL" if max_event_time_ns is None else from_unix_ns_utc_sql(max_event_time_ns)
        if col == "started_at":
            return from_unix_ms_utc_sql(started_at_ms)
        if col == "completed_at":
            return "NULL" if completed_at_ms is None else from_unix_ms_utc_sql(completed_at_ms)
        if value is None:
            return "NULL"
        if isinstance(value, int):
            return str(int(value))
        return sql_quote_string(str(value))

    values = ", ".join(cell(c, v) for c, v in zip(cols, row))
    client.command(
        f"INSERT INTO {DATABASE}.{LEDGER_TABLE} ({', '.join(cols)}) VALUES ({values})"
    )


def _envelope_to_row(
    *,
    envelope: dict[str, Any],
    record_ordinal: int,
    source_segment: str,
    source_sha: str,
    ingestion_ts_ms: int,
) -> dict[str, Any]:
    schema_v = str(envelope.get("schema_version") or "")
    msg_type = str(envelope.get("message_type") or "").lower()
    if not msg_type:
        raise PilotImportError("STOP_SOURCE_MANIFEST_INVALID: missing message_type")

    et_ns_raw = envelope.get("event_time_ns")
    rt_ns_raw = envelope.get("receive_time_ns")
    event_time_present = 1 if et_ns_raw is not None else 0
    receive_time_present = 1 if rt_ns_raw is not None else 0
    et_ns = int(et_ns_raw) if et_ns_raw is not None else 0
    rt_ns = int(rt_ns_raw) if rt_ns_raw is not None else 0

    u_raw = envelope.get("u")
    seq_raw = envelope.get("seq")
    update_id_present = 1 if u_raw is not None else 0
    seq_present = 1 if seq_raw is not None else 0
    update_id = int(u_raw) if u_raw is not None else 0
    seq = int(seq_raw) if seq_raw is not None else 0

    payload = envelope.get("original_payload")
    if payload is None:
        payload_text = "null"
    else:
        payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    payload_sha = str(envelope.get("payload_sha256") or "")
    if len(payload_sha) != 64:
        raise PilotImportError(
            f"STOP_SOURCE_PARITY_FAILED: missing payload_sha256 at ordinal {record_ordinal}"
        )

    record_id = make_record_id(source_segment_sha256=source_sha, record_ordinal=record_ordinal)
    symbol = str(envelope.get("symbol") or "").upper()

    return {
        "record_id": record_id,
        "symbol": symbol,
        "event_time": 0,  # filled via event_time_ns in SQL
        "event_time_ns": et_ns,
        "receive_time": 0,
        "receive_time_ns": rt_ns,
        "message_type": msg_type,
        "update_id": update_id,
        "seq": seq,
        "update_id_present": update_id_present,
        "seq_present": seq_present,
        "event_time_present": event_time_present,
        "receive_time_present": receive_time_present,
        "source_segment": source_segment,
        "source_segment_sha256": source_sha,
        "record_ordinal": record_ordinal,
        "payload_sha256": payload_sha,
        "original_payload": payload_text,
        "envelope_version": schema_v or "1",
        "ingestion_ts": 0,
        "ingestion_ts_ms": int(ingestion_ts_ms),
        "_event_time_ns_opt": None if event_time_present == 0 else et_ns,
        "_payload_obj": payload if isinstance(payload, dict) else {},
    }


EVENT_COLUMNS = [
    "record_id",
    "symbol",
    "event_time",
    "event_time_ns",
    "receive_time",
    "receive_time_ns",
    "message_type",
    "update_id",
    "seq",
    "update_id_present",
    "seq_present",
    "event_time_present",
    "receive_time_present",
    "source_segment",
    "source_segment_sha256",
    "record_ordinal",
    "payload_sha256",
    "original_payload",
    "envelope_version",
    "ingestion_ts",
    "ingestion_ts_ms",
]


def _existing_record_ids(
    client: Any, *, symbol: str, start_ns: int, end_ns: int
) -> set[str]:
    rows = client.query(
        f"""
        SELECT record_id
        FROM {DATABASE}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
        """,
        parameters={"symbol": symbol.upper(), "start_ns": start_ns, "end_ns": end_ns},
    ).result_rows
    out: set[str] = set()
    for (rid,) in rows:
        if isinstance(rid, (bytes, bytearray)):
            out.add(rid.decode("utf-8"))
        else:
            out.add(str(rid))
    return out


def _content_hash(client: Any, *, symbol: str, start_ns: int, end_ns: int) -> str:
    import hashlib

    rows = client.query(
        f"""
        SELECT record_id, record_ordinal, payload_sha256, event_time_ns
        FROM {DATABASE}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
        ORDER BY record_ordinal
        """,
        parameters={"symbol": symbol.upper(), "start_ns": start_ns, "end_ns": end_ns},
    ).result_rows
    h = hashlib.sha256()
    for rid, ordinal, psha, et in rows:
        rid_s = rid.decode("utf-8") if isinstance(rid, (bytes, bytearray)) else str(rid)
        psha_s = psha.decode("utf-8") if isinstance(psha, (bytes, bytearray)) else str(psha)
        h.update(f"{rid_s}|{int(ordinal)}|{psha_s}|{int(et)}\n".encode())
    return h.hexdigest()


def run_pilot_import(
    *,
    segment_path: str | Path,
    symbol: str,
    window_start: str,
    window_end: str,
    max_wall_clock_s: float = MAX_WALL_CLOCK_S,
    max_rss_bytes: int = MAX_RSS_BYTES,
    batch_size: int = BATCH_SIZE,
    client: Any | None = None,
    require_start_checkpoint_ordinal: int | None = EXPECTED_START_CHECKPOINT_ORDINAL,
) -> ImportResult:
    t0 = time.monotonic()
    peak_rss = current_rss_bytes()
    limits = ResourceLimits(
        deadline_monotonic=t0 + float(max_wall_clock_s),
        max_rss_bytes=int(max_rss_bytes),
    )
    segment = Path(segment_path)
    if not segment.is_file():
        raise PilotImportError(f"STOP_SOURCE_MANIFEST_INVALID: missing segment {segment}")

    manifest = load_manifest(segment)
    fmt = str(manifest.get("format_version") or "")
    if fmt != EXPECTED_FORMAT_VERSION:
        raise PilotImportError(
            f"STOP_SOURCE_MANIFEST_INVALID: format_version={fmt!r} "
            f"expected={EXPECTED_FORMAT_VERSION!r}"
        )
    if str(manifest.get("symbol") or "").upper() != symbol.upper():
        raise PilotImportError("STOP_SOURCE_MANIFEST_INVALID: symbol mismatch")

    source_sha = verify_manifest_against_segment(segment, manifest)
    import_id = make_import_id(
        schema_version=SCHEMA_VERSION,
        source_segment_sha256=source_sha,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
    )

    own_client = client is None
    if client is None:
        try:
            client = get_clickhouse_client()
        except Exception as exc:  # noqa: BLE001
            raise PilotImportError(f"STOP_CLICKHOUSE_ERROR: connect failed: {exc}") from exc

    try:
        ensure_database_and_tables(client)
        assert_compatible_schema(client)
        client.command("SET session_timezone = 'UTC'")

        window_start_ns = iso_to_ns_exact(window_start)
        window_end_ns = iso_to_ns_exact(window_end)

        existing = _ledger_status(client, import_id)
        if existing is not None:
            st = existing["status"]
            if st == "COMPLETE":
                ch = _content_hash(
                    client, symbol=symbol, start_ns=window_start_ns, end_ns=window_end_ns
                )
                return ImportResult(
                    verdict="FULL_OB_CLICKHOUSE_60S_PILOT_EXACT",
                    import_id=import_id,
                    status="SKIPPED_ALREADY_COMPLETE",
                    rows_seen=0,
                    rows_in_window=0,
                    rows_inserted=0,
                    peak_rss_bytes=current_rss_bytes(),
                    elapsed_s=time.monotonic() - t0,
                    source_segment=str(segment),
                    source_segment_sha256=source_sha,
                    skipped=True,
                    content_hash=ch,
                )
            if st in ("IMPORTING", "FAILED"):
                raise PilotImportError(
                    f"STOP_IMPORT_INCOMPLETE: ledger status={st} for import_id={import_id}; "
                    "refusing overwrite"
                )

        started_at_ms = _now_ms()
        _write_ledger(
            client,
            import_id=import_id,
            symbol=symbol.upper(),
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            source_segment=str(segment),
            source_sha=source_sha,
            status="IMPORTING",
            rows_seen=0,
            rows_in_window=0,
            rows_inserted=0,
            min_event_time_ns=None,
            max_event_time_ns=None,
            started_at_ms=started_at_ms,
            completed_at_ms=None,
            error_message="",
            ledger_version=1,
        )

        rows_seen = 0
        rows_in_window = 0
        rows_inserted = 0
        min_et_ns: int | None = None
        max_et_ns: int | None = None
        min_u: int | None = None
        max_u: int | None = None
        min_seq: int | None = None
        max_seq: int | None = None
        msg_counts: dict[str, int] = {}
        batch: list[list[Any]] = []
        samples: list[SampleRecord] = []
        last_et_ns: int | None = None
        monotonic_ok = True
        ingestion_ts_ms = _now_ms()
        source_path = str(segment)
        existing_ids = _existing_record_ids(
            client, symbol=symbol, start_ns=window_start_ns, end_ns=window_end_ns
        )
        start_ckpt_ord: int | None = None
        start_ckpt_u: int | None = None
        start_ckpt_seq: int | None = None
        first_in_window = True

        def flush() -> None:
            nonlocal batch, rows_inserted, peak_rss
            if not batch:
                return
            check_resource_limits(limits)
            try:
                insert_events_via_ns_staging(
                    client,
                    database=DATABASE,
                    events_table=EVENTS_TABLE,
                    staging_table=STAGING_TABLE,
                    column_names=EVENT_COLUMNS,
                    rows=batch,
                )
            except Exception as exc:  # noqa: BLE001
                raise PilotImportError(f"STOP_CLICKHOUSE_ERROR: insert failed: {exc}") from exc
            rows_inserted += len(batch)
            batch = []
            peak_rss = max(peak_rss, current_rss_bytes())

        try:
            for ordinal, envelope, _raw in iter_ndjson_zst_records(segment):
                check_resource_limits(limits)
                peak_rss = max(peak_rss, current_rss_bytes())
                rows_seen += 1

                row = _envelope_to_row(
                    envelope=envelope,
                    record_ordinal=ordinal,
                    source_segment=source_path,
                    source_sha=source_sha,
                    ingestion_ts_ms=ingestion_ts_ms,
                )
                et_opt = row["_event_time_ns_opt"]

                if et_opt is not None:
                    if last_et_ns is not None and et_opt < last_et_ns:
                        monotonic_ok = False
                    last_et_ns = et_opt
                    if monotonic_ok and et_opt >= window_end_ns and rows_in_window > 0:
                        break

                if not event_in_window(
                    et_opt, window_start_ns=window_start_ns, window_end_ns=window_end_ns
                ):
                    continue

                rows_in_window += 1
                mt = row["message_type"]
                msg_counts[mt] = msg_counts.get(mt, 0) + 1
                assert et_opt is not None
                if min_et_ns is None or et_opt < min_et_ns:
                    min_et_ns = et_opt
                if max_et_ns is None or et_opt > max_et_ns:
                    max_et_ns = et_opt
                if row["update_id_present"]:
                    u = int(row["update_id"])
                    min_u = u if min_u is None else min(min_u, u)
                    max_u = u if max_u is None else max(max_u, u)
                if row["seq_present"]:
                    s = int(row["seq"])
                    min_seq = s if min_seq is None else min(min_seq, s)
                    max_seq = s if max_seq is None else max(max_seq, s)

                if mt == "checkpoint" and start_ckpt_ord is None:
                    payload = row["_payload_obj"]
                    bids = payload.get("bids") or []
                    asks = payload.get("asks") or []
                    if isinstance(bids, list) and isinstance(asks, list) and bids and asks:
                        start_ckpt_ord = int(row["record_ordinal"])
                        start_ckpt_u = int(payload.get("u") or row["update_id"] or 0)
                        start_ckpt_seq = int(payload.get("seq") or row["seq"] or 0)
                        if (
                            require_start_checkpoint_ordinal is not None
                            and start_ckpt_ord != int(require_start_checkpoint_ordinal)
                        ):
                            raise PilotImportError(
                                "STOP_EARLIER_CHECKPOINT_NOT_FOUND: first full checkpoint "
                                f"ordinal={start_ckpt_ord} expected={require_start_checkpoint_ordinal}"
                            )

                if first_in_window:
                    first_in_window = False

                if len(samples) < 5:
                    samples.append(
                        SampleRecord(
                            record_id=row["record_id"],
                            record_ordinal=int(row["record_ordinal"]),
                            payload_sha256=row["payload_sha256"],
                            event_time_ns=et_opt,
                            message_type=mt,
                            original_payload=row["original_payload"],
                        )
                    )

                if row["record_id"] in existing_ids:
                    continue

                batch.append([row[c] for c in EVENT_COLUMNS])
                existing_ids.add(row["record_id"])
                if len(batch) >= batch_size:
                    flush()

            flush()

            if start_ckpt_ord is None:
                raise PilotImportError(
                    "STOP_EARLIER_CHECKPOINT_NOT_FOUND: no full checkpoint in import window"
                )

            from .validation import validate_pilot_window

            val = validate_pilot_window(
                client=client,
                symbol=symbol.upper(),
                window_start=window_start,
                window_end=window_end,
                expected_rows=rows_in_window,
                samples=samples,
                source_segment_sha256=source_sha,
            )
            if not val["ok"]:
                raise PilotImportError(f"STOP_SOURCE_PARITY_FAILED: {val.get('reason')}")

            # Exact DateTime64 ↔ ns proof on imported window (hard fail).
            from .datetime_integrity import assert_event_receive_datetime_match_ns

            assert_event_receive_datetime_match_ns(
                client,
                database=DATABASE,
                table=EVENTS_TABLE,
                symbol=symbol,
                start_ns=window_start_ns,
                end_ns=window_end_ns,
                sample_limit=5,
            )

            content_hash = _content_hash(
                client, symbol=symbol, start_ns=window_start_ns, end_ns=window_end_ns
            )

            _write_ledger(
                client,
                import_id=import_id,
                symbol=symbol.upper(),
                window_start_ns=window_start_ns,
                window_end_ns=window_end_ns,
                source_segment=source_path,
                source_sha=source_sha,
                status="COMPLETE",
                rows_seen=rows_seen,
                rows_in_window=rows_in_window,
                rows_inserted=rows_inserted,
                min_event_time_ns=min_et_ns,
                max_event_time_ns=max_et_ns,
                started_at_ms=started_at_ms,
                completed_at_ms=_now_ms(),
                error_message="",
                ledger_version=2,
            )

            return ImportResult(
                verdict="FULL_OB_CLICKHOUSE_60S_PILOT_EXACT",
                import_id=import_id,
                status="COMPLETE",
                rows_seen=rows_seen,
                rows_in_window=rows_in_window,
                rows_inserted=rows_inserted,
                min_event_time=None if min_et_ns is None else _ns_to_iso_z(min_et_ns),
                max_event_time=None if max_et_ns is None else _ns_to_iso_z(max_et_ns),
                min_event_time_ns=min_et_ns,
                max_event_time_ns=max_et_ns,
                min_update_id=min_u,
                max_update_id=max_u,
                min_seq=min_seq,
                max_seq=max_seq,
                message_type_counts=msg_counts,
                peak_rss_bytes=peak_rss,
                elapsed_s=time.monotonic() - t0,
                samples=samples,
                source_segment=source_path,
                source_segment_sha256=source_sha,
                content_hash=content_hash,
                start_checkpoint_ordinal=start_ckpt_ord,
                start_checkpoint_u=start_ckpt_u,
                start_checkpoint_seq=start_ckpt_seq,
            )
        except PilotImportError as exc:
            _write_ledger(
                client,
                import_id=import_id,
                symbol=symbol.upper(),
                window_start_ns=window_start_ns,
                window_end_ns=window_end_ns,
                source_segment=source_path,
                source_sha=source_sha,
                status="FAILED",
                rows_seen=rows_seen,
                rows_in_window=rows_in_window,
                rows_inserted=rows_inserted,
                min_event_time_ns=min_et_ns,
                max_event_time_ns=max_et_ns,
                started_at_ms=started_at_ms,
                completed_at_ms=_now_ms(),
                error_message=str(exc)[:2000],
                ledger_version=2,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"STOP_CLICKHOUSE_ERROR: {exc}"
            _write_ledger(
                client,
                import_id=import_id,
                symbol=symbol.upper(),
                window_start_ns=window_start_ns,
                window_end_ns=window_end_ns,
                source_segment=source_path,
                source_sha=source_sha,
                status="FAILED",
                rows_seen=rows_seen,
                rows_in_window=rows_in_window,
                rows_inserted=rows_inserted,
                min_event_time_ns=min_et_ns,
                max_event_time_ns=max_et_ns,
                started_at_ms=started_at_ms,
                completed_at_ms=_now_ms(),
                error_message=msg[:2000],
                ledger_version=2,
            )
            raise PilotImportError(msg) from exc
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
