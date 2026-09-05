"""ClickHouse importer: isolated DB only, idempotent inserts."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import DEFAULT_PILOT_DATABASE, FORBIDDEN_DATABASES, PROTECTED_RESEARCH_DATABASES
from .ch import ch_command, ch_query, get_ch_client
from .ids import payload_hash
from .readiness import SegmentCandidate, discover_and_validate, validate_candidate
from .reader import iter_segment_records, summarize_records
from .schema import render_schema
from .state_machine import LocalStateStore, SegmentImportState

# re-export for CLI
__all__ = ["get_ch_client", "run_import", "verify_segment_parity", "assert_safe_database"]


def assert_safe_database(database: str, *, allow_protected: bool = False) -> None:
    db = database.strip()
    if not db:
        raise ValueError("database required")
    if db in FORBIDDEN_DATABASES:
        raise RuntimeError(f"Refusing production/system database: {db}")
    if db in PROTECTED_RESEARCH_DATABASES and not allow_protected:
        raise RuntimeError(f"Refusing protected research database: {db}")
    if db == "orderbook_analysis":
        raise RuntimeError("Refusing orderbook_analysis")


def apply_schema(client, database: str) -> None:
    assert_safe_database(database)
    sql = render_schema(database)
    for stmt in sql.split(";"):
        s = stmt.strip()
        if not s:
            continue
        ch_command(client, s)


def _now_ch() -> datetime:
    return datetime.now(timezone.utc)


def upsert_state_ch(client, database: str, st: SegmentImportState) -> None:
    cols = [
        "segment_id",
        "source_path",
        "source_sha256",
        "file_size",
        "symbol",
        "topic",
        "fight_event_id",
        "segment_index",
        "continuation_index",
        "contract_version",
        "status",
        "first_ts",
        "last_ts",
        "first_u",
        "last_u",
        "first_seq",
        "last_seq",
        "record_count",
        "checkpoint_count",
        "continuity_epochs",
        "import_attempts",
        "last_error",
        "import_time",
        "verify_time",
        "db_rows_physical",
        "db_rows_logical",
        "replay_status",
        "updated_at",
    ]
    row = [
        st.segment_id,
        st.source_path,
        st.source_sha256,
        int(st.file_size),
        st.symbol,
        st.topic,
        st.fight_event_id,
        int(st.segment_index),
        int(st.continuation_index),
        st.contract_version,
        st.status,
        st.first_ts,
        st.last_ts,
        st.first_u,
        st.last_u,
        st.first_seq,
        st.last_seq,
        int(st.record_count),
        int(st.checkpoint_count),
        int(st.continuity_epochs),
        int(st.import_attempts),
        st.last_error or "",
        datetime.fromisoformat(st.import_time) if st.import_time else None,
        datetime.fromisoformat(st.verify_time) if st.verify_time else None,
        int(st.db_rows_physical),
        int(st.db_rows_logical),
        st.replay_status or "",
        _now_ch(),
    ]
    client.insert(f"{database}.full_ob_import_state", [row], column_names=cols)


def insert_records(client, database: str, rows: list[dict[str, Any]], *, batch_size: int = 50) -> int:
    if not rows:
        return 0
    cols = [
        "record_id",
        "record_kind",
        "fight_event_id",
        "segment_id",
        "segment_index",
        "continuation_index",
        "record_ordinal",
        "symbol",
        "topic",
        "continuity_epoch_id",
        "u",
        "seq",
        "exchange_ts_ms",
        "cts_ms",
        "receive_time_ns",
        "bids",
        "asks",
        "marker_type",
        "book_hash",
        "source_path",
        "source_sha256",
        "raw_payload_hash",
        "canonical_payload_hash",
        "raw_payload",
        "ingestion_ts",
    ]
    inserted = 0
    ts = _now_ch()
    batch: list[list[Any]] = []

    def flush() -> None:
        nonlocal batch, inserted
        if not batch:
            return
        client.insert(f"{database}.full_ob_records", batch, column_names=cols)
        inserted += len(batch)
        batch = []

    for r in rows:
        raw = r["raw_payload"]
        if len(raw) > 500_000:
            raw = raw[:500_000]
        values = [
            r["record_id"],
            r["record_kind"],
            r["fight_event_id"],
            r["segment_id"],
            int(r["segment_index"]),
            int(r["continuation_index"]),
            int(r["record_ordinal"]),
            r["symbol"],
            r["topic"],
            r.get("continuity_epoch_id"),
            r.get("u"),
            r.get("seq"),
            int(r["exchange_ts_ms"]) if isinstance(r.get("exchange_ts_ms"), (int, float)) else None,
            int(r["cts_ms"]) if isinstance(r.get("cts_ms"), (int, float)) else None,
            int(r["receive_time_ns"]) if isinstance(r.get("receive_time_ns"), (int, float)) else None,
            r.get("bids") or [],
            r.get("asks") or [],
            r.get("marker_type"),
            r.get("book_hash"),
            r["source_path"],
            r["source_sha256"],
            r["raw_payload_hash"],
            r["canonical_payload_hash"],
            raw,
            ts,
        ]
        level_n = len(r.get("bids") or []) + len(r.get("asks") or [])
        if level_n > 5000:
            flush()
            client.insert(f"{database}.full_ob_records", [values], column_names=cols)
            inserted += 1
            continue
        batch.append(values)
        if len(batch) >= batch_size:
            flush()
    flush()
    return inserted


def load_signals_from_event(event_dir: Path) -> tuple[list[dict], list[dict]]:
    """Best-effort: nested/parent signals from manifests under event dir."""
    signals: list[dict] = []
    contracts: list[dict] = []
    man_path = event_dir / "event_manifest.json"
    if not man_path.exists():
        return signals, contracts
    man = json.loads(man_path.read_text())
    fight = man.get("fight_event_id") or event_dir.name
    symbol = (man.get("symbol") or "").upper()
    # parent
    parent_id = fight
    signals.append(
        {
            "signal_id": parent_id,
            "parent_event_id": fight,
            "symbol": symbol,
            "profile_contract": str(man.get("profile_contract") or man.get("contract") or ""),
            "signal_role": "PARENT",
            "edge": str(man.get("edge") or ""),
            "trigger_type": str(man.get("trigger_type") or man.get("trigger") or ""),
            "arm_cycle": None,
            "continuity_epoch_id": 0,
            "overlap_cluster_id": man.get("overlap_cluster_id"),
            "vah": None,
            "val": None,
            "poc": None,
            "coverage": "FULL_OB",
            "research_eligible": 1 if man.get("research_eligible", True) else 0,
            "payload_json": json.dumps({"source": "event_manifest"}, sort_keys=True),
        }
    )
    # nested from segment manifests / nested files
    for nested_path in event_dir.rglob("nested_signal_*.json"):
        try:
            n = json.loads(nested_path.read_text())
        except Exception:
            continue
        sid = str(n.get("signal_id") or n.get("nested_signal_id") or nested_path.stem)
        signals.append(
            {
                "signal_id": sid,
                "parent_event_id": fight,
                "symbol": symbol,
                "profile_contract": str(n.get("profile_contract") or ""),
                "signal_role": "NESTED",
                "edge": str(n.get("edge") or ""),
                "trigger_type": str(n.get("trigger_type") or ""),
                "arm_cycle": n.get("arm_cycle"),
                "continuity_epoch_id": n.get("continuity_epoch_id"),
                "overlap_cluster_id": n.get("overlap_cluster_id"),
                "vah": str(n["vah"]) if n.get("vah") is not None else None,
                "val": str(n["val"]) if n.get("val") is not None else None,
                "poc": str(n["poc"]) if n.get("poc") is not None else None,
                "coverage": str(n.get("coverage") or "FULL_OB"),
                "research_eligible": 1 if n.get("research_eligible", True) else 0,
                "payload_json": json.dumps(n, sort_keys=True, default=str)[:200000],
            }
        )
        cid = f"contract::{sid}"
        contracts.append(
            {
                "contract_id": cid,
                "signal_id": sid,
                "parent_event_id": fight,
                "profile_contract": str(n.get("profile_contract") or ""),
                "pre_window_ms": int(n.get("pre_window_ms") or 0),
                "post_window_ms": int(n.get("post_window_ms") or 0),
                "continuity_epoch_id": n.get("continuity_epoch_id"),
                "gap_coverage": str(n.get("gap_coverage") or "UNKNOWN"),
                "eligibility": str(n.get("eligibility") or "UNKNOWN"),
                "overlap_cluster_id": n.get("overlap_cluster_id"),
                "payload_json": json.dumps(n, sort_keys=True, default=str)[:200000],
            }
        )
    return signals, contracts


def insert_signals(client, database: str, signals: list[dict], contracts: list[dict]) -> None:
    ts = _now_ch()
    if signals:
        cols = [
            "signal_id",
            "parent_event_id",
            "symbol",
            "profile_contract",
            "signal_role",
            "edge",
            "trigger_type",
            "arm_cycle",
            "continuity_epoch_id",
            "overlap_cluster_id",
            "vah",
            "val",
            "poc",
            "coverage",
            "research_eligible",
            "payload_json",
            "updated_at",
        ]
        client.insert(
            f"{database}.full_ob_signals",
            [
                [
                    s["signal_id"],
                    s["parent_event_id"],
                    s["symbol"],
                    s["profile_contract"],
                    s["signal_role"],
                    s["edge"],
                    s["trigger_type"],
                    s.get("arm_cycle"),
                    s.get("continuity_epoch_id"),
                    s.get("overlap_cluster_id"),
                    s.get("vah"),
                    s.get("val"),
                    s.get("poc"),
                    s["coverage"],
                    int(s["research_eligible"]),
                    s["payload_json"],
                    ts,
                ]
                for s in signals
            ],
            column_names=cols,
        )
    if contracts:
        cols = [
            "contract_id",
            "signal_id",
            "parent_event_id",
            "profile_contract",
            "pre_window_ms",
            "post_window_ms",
            "continuity_epoch_id",
            "gap_coverage",
            "eligibility",
            "overlap_cluster_id",
            "payload_json",
            "updated_at",
        ]
        client.insert(
            f"{database}.signal_analysis_contracts",
            [
                [
                    c["contract_id"],
                    c["signal_id"],
                    c["parent_event_id"],
                    c["profile_contract"],
                    int(c["pre_window_ms"]),
                    int(c["post_window_ms"]),
                    c.get("continuity_epoch_id"),
                    c["gap_coverage"],
                    c["eligibility"],
                    c.get("overlap_cluster_id"),
                    c["payload_json"],
                    ts,
                ]
                for c in contracts
            ],
            column_names=cols,
        )


def upsert_event(client, database: str, event_dir: Path, segment_count: int) -> None:
    man = json.loads((event_dir / "event_manifest.json").read_text())
    fight = man.get("fight_event_id") or event_dir.name
    cols = [
        "fight_event_id",
        "symbol",
        "trigger_type",
        "start_ts",
        "end_ts",
        "status",
        "contract_versions",
        "parent_signal_ids",
        "nested_signal_ids",
        "segment_count",
        "continuous_capture",
        "replayable_by_epochs",
        "research_eligible",
        "updated_at",
    ]
    client.insert(
        f"{database}.full_ob_events",
        [
            [
                fight,
                (man.get("symbol") or "").upper(),
                str(man.get("trigger_type") or man.get("trigger") or ""),
                man.get("start_ts") or man.get("event_start_ts"),
                man.get("end_ts") or man.get("event_end_ts"),
                str(man.get("status") or "CAPTURED"),
                [str(x) for x in (man.get("contract_versions") or [])],
                [fight],
                [],
                int(segment_count),
                1 if man.get("continuous_capture", True) else 0,
                1 if man.get("replayable_by_epochs", True) else 0,
                1 if man.get("research_eligible", True) else 0,
                _now_ch(),
            ]
        ],
        column_names=cols,
    )


def upsert_segment_row(client, database: str, cand: SegmentCandidate, st: SegmentImportState) -> None:
    cols = [
        "segment_id",
        "fight_event_id",
        "symbol",
        "continuation_index",
        "source_path",
        "source_sha256",
        "previous_segment_sha256",
        "file_size",
        "status",
        "record_count",
        "checkpoint_count",
        "first_ts",
        "last_ts",
        "first_u",
        "last_u",
        "last_error",
        "updated_at",
    ]
    client.insert(
        f"{database}.full_ob_segments",
        [
            [
                st.segment_id,
                st.fight_event_id,
                st.symbol,
                int(st.continuation_index),
                st.source_path,
                st.source_sha256,
                cand.previous_segment_sha256,
                int(st.file_size),
                st.status,
                int(st.record_count),
                int(st.checkpoint_count),
                st.first_ts,
                st.last_ts,
                st.first_u,
                st.last_u,
                st.last_error or "",
                _now_ch(),
            ]
        ],
        column_names=cols,
    )


def count_rows(client, database: str, segment_id: str) -> tuple[int, int]:
    phys = ch_query(
        client,
        f"SELECT count() FROM {database}.full_ob_records WHERE segment_id = {{s:String}}",
        {"s": segment_id},
    )[0][0]
    logi = ch_query(
        client,
        f"SELECT count() FROM {database}.v_full_ob_records_canonical WHERE segment_id = {{s:String}}",
        {"s": segment_id},
    )[0][0]
    return int(phys), int(logi)


def import_segment(
    client,
    database: str,
    cand: SegmentCandidate,
    store: LocalStateStore,
    *,
    dry_run: bool = False,
    batch_size: int = 200,
    resume: bool = True,
) -> SegmentImportState:
    assert_safe_database(database)
    cand = validate_candidate(cand)
    if cand.status != "VALIDATED":
        st = SegmentImportState(
            segment_id=cand.segment_id or payload_hash(str(cand.path)),
            source_path=str(cand.path),
            source_sha256=cand.actual_sha256 or cand.expected_sha256 or "",
            file_size=cand.file_size,
            symbol=cand.symbol,
            topic=cand.topic,
            fight_event_id=cand.fight_event_id,
            segment_index=cand.continuation_index,
            continuation_index=cand.continuation_index,
            status=cand.status,
            last_error=";".join(cand.reasons),
            first_ts=cand.segment_first_ts,
            last_ts=cand.segment_last_ts,
            first_u=cand.segment_first_u,
            last_u=cand.segment_last_u,
        )
        store.put(st)
        return st

    existing = store.get(cand.segment_id) if resume else None
    if existing and existing.status == "VERIFIED" and resume:
        return existing

    st = existing or SegmentImportState(
        segment_id=cand.segment_id,
        source_path=str(cand.path),
        source_sha256=cand.actual_sha256 or "",
        file_size=cand.file_size,
        symbol=cand.symbol,
        topic=cand.topic,
        fight_event_id=cand.fight_event_id,
        segment_index=cand.continuation_index,
        continuation_index=cand.continuation_index,
        first_ts=cand.segment_first_ts,
        last_ts=cand.segment_last_ts,
        first_u=cand.segment_first_u,
        last_u=cand.segment_last_u,
        status="VALIDATED",
    )
    st.import_attempts += 1
    rows = list(
        iter_segment_records(
            cand.path,
            source_sha256=cand.actual_sha256 or "",
            fight_event_id=cand.fight_event_id,
            symbol=cand.symbol,
            topic=cand.topic,
            segment_id=cand.segment_id,
            segment_index=cand.continuation_index,
            continuation_index=cand.continuation_index,
        )
    )
    summary = summarize_records(rows)
    st.record_count = summary["record_count"]
    st.checkpoint_count = summary["checkpoint_count"]
    st.continuity_epochs = summary["continuity_epochs"]
    if dry_run:
        st.bump("VALIDATED", last_error="dry_run")
        store.put(st)
        return st

    st.bump("IMPORTING")
    store.put(st)
    if not dry_run:
        upsert_state_ch(client, database, st)
    try:
        insert_records(client, database, rows, batch_size=batch_size)
        upsert_segment_row(client, database, cand, st)
        st.bump("IMPORTED", import_time=datetime.now(timezone.utc).isoformat())
        store.put(st)
        upsert_state_ch(client, database, st)
    except Exception as e:
        st.bump("FAILED_RETRYABLE", last_error=str(e)[:2000])
        store.put(st)
        try:
            upsert_state_ch(client, database, st)
        except Exception:
            pass
        raise
    return st


def verify_segment_parity(
    client,
    database: str,
    cand: SegmentCandidate,
    store: LocalStateStore,
) -> dict[str, Any]:
    from .parity import parity_check_segment

    st = store.get(cand.segment_id)
    if not st:
        raise RuntimeError("no state")
    st.bump("VERIFYING")
    store.put(st)
    result = parity_check_segment(client, database, cand)
    phys, logi = count_rows(client, database, cand.segment_id)
    st.db_rows_physical = phys
    st.db_rows_logical = logi
    if result.get("ok"):
        st.bump(
            "VERIFIED",
            verify_time=datetime.now(timezone.utc).isoformat(),
            replay_status="PASS",
            last_error="",
        )
    else:
        st.bump(
            "QUARANTINED",
            verify_time=datetime.now(timezone.utc).isoformat(),
            replay_status="FAIL",
            last_error=json.dumps(result.get("mismatches") or result)[:2000],
        )
        try:
            client.insert(
                f"{database}.full_ob_events",
                [
                    [
                        cand.fight_event_id,
                        cand.symbol,
                        "",
                        None,
                        None,
                        "QUARANTINED",
                        [],
                        [],
                        [],
                        0,
                        0,
                        0,
                        0,
                        _now_ch(),
                    ]
                ],
                column_names=[
                    "fight_event_id",
                    "symbol",
                    "trigger_type",
                    "start_ts",
                    "end_ts",
                    "status",
                    "contract_versions",
                    "parent_signal_ids",
                    "nested_signal_ids",
                    "segment_count",
                    "continuous_capture",
                    "replayable_by_epochs",
                    "research_eligible",
                    "updated_at",
                ],
            )
        except Exception:
            pass
    store.put(st)
    upsert_state_ch(client, database, st)
    result["state"] = st.to_dict()
    return result


def run_import(
    *,
    source_root: Path,
    database: str,
    symbols: set[str] | None,
    state_path: Path,
    dry_run: bool = False,
    once: bool = True,
    max_files: int | None = None,
    event_id: str | None = None,
    segment: int | None = None,
    verify_replay: bool = True,
    resume: bool = True,
    batch_size: int = 200,
    apply_ddl: bool = True,
) -> dict[str, Any]:
    assert_safe_database(database)
    store = LocalStateStore(state_path)
    client = None
    if not dry_run:
        client = get_ch_client()
        if apply_ddl:
            apply_schema(client, database)

    cands = discover_and_validate(source_root, symbols=symbols)
    if event_id:
        cands = [c for c in cands if c.fight_event_id == event_id]
    if segment is not None:
        cands = [c for c in cands if c.continuation_index == segment]
    # prefer finalized validated first
    eligible = [c for c in cands if c.status == "VALIDATED"]
    skipped = [c for c in cands if c.status != "VALIDATED"]
    if max_files is not None:
        eligible = eligible[: max_files]

    report: dict[str, Any] = {
        "database": database,
        "dry_run": dry_run,
        "discovered": len(cands),
        "eligible": len(eligible),
        "skipped": [c.to_dict() for c in skipped],
        "imports": [],
        "parity": [],
    }

    events_seen: set[str] = set()
    for cand in eligible:
        t0 = time.time()
        st = import_segment(
            client,
            database,
            cand,
            store,
            dry_run=dry_run,
            batch_size=batch_size,
            resume=resume,
        )
        item = {"segment": cand.to_dict(), "state": st.to_dict(), "seconds": time.time() - t0}
        report["imports"].append(item)
        if dry_run:
            continue
        if verify_replay and st.status in ("IMPORTED", "VERIFIED", "IMPORTING"):
            # after import_segment status is IMPORTED
            if st.status == "IMPORTED" or (resume and st.status != "VERIFIED"):
                # re-fetch if resumed VERIFIED skip already handled
                pass
            if st.status != "VERIFIED":
                # need re-import path: if already imported, verify
                if st.status == "IMPORTED":
                    par = verify_segment_parity(client, database, cand, store)
                    report["parity"].append(par)
        if cand.fight_event_id not in events_seen and not dry_run:
            events_seen.add(cand.fight_event_id)
            try:
                upsert_event(client, database, cand.event_dir, segment_count=len([c for c in eligible if c.fight_event_id == cand.fight_event_id]))
                sigs, cons = load_signals_from_event(cand.event_dir)
                insert_signals(client, database, sigs, cons)
            except Exception as e:
                report.setdefault("event_errors", []).append(str(e))
    report["once"] = once
    return report
